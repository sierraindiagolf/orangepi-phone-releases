"""SQLite persistence for the Orange Pi phone stack.

All data lives on the USB SSD. On this board the SSD *is* the root filesystem
(sda3 mounted at /), so ~/phone/data is already SSD-backed — no separate mount
is involved.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("PHONE_DB", os.path.expanduser("~/phone/data/phone.db"))

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False


def now_iso():
    """UTC timestamp, second resolution, no microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect():
    """One connection per thread; Flask handles requests on several threads."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    number      TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('OUTGOING','INCOMING')),
    status      TEXT    NOT NULL,
    duration    INTEGER NOT NULL DEFAULT 0,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts     ON calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_number ON calls(number);

CREATE TABLE IF NOT EXISTS sms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    number      TEXT    NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('OUT','IN')),
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'OK',
    is_read     INTEGER NOT NULL DEFAULT 0,
    sim_index   INTEGER,
    error       TEXT,
    UNIQUE (ts, number, direction, body)
);
CREATE INDEX IF NOT EXISTS idx_sms_number ON sms(number, ts DESC);
CREATE INDEX IF NOT EXISTS idx_sms_ts     ON sms(ts DESC);

CREATE TABLE IF NOT EXISTS phonebook (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    number      TEXT    NOT NULL UNIQUE,
    notes       TEXT,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pb_name ON phonebook(name);

CREATE TABLE IF NOT EXISTS at_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    command     TEXT    NOT NULL,
    response    TEXT,
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_at_ts ON at_history(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- A single admin account. Multi-user was considered and rejected: there is one
-- operator, and every extra account is another password that can be weak.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- API tokens for machines (HomeAssistant opening the gate, scripts).
--
-- The token value is never stored. What is stored is its sha256 plus a clear-text
-- prefix, and the prefix is the lookup key: verification is one indexed row fetch
-- and one constant-time compare, not a scan over every token on every request.
-- The prefix also makes a token identifiable in a log or a config file without
-- revealing it.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    scopes       TEXT    NOT NULL,          -- JSON array, e.g. ["dial","read"]
    token_hash   TEXT    NOT NULL UNIQUE,
    prefix       TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT,
    last_used_at TEXT,
    last_used_ip TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix ON api_tokens(prefix);

-- Rate limiting and a minimal audit trail. Failed logins and reset requests are
-- counted out of here rather than out of memory, so a service restart cannot be
-- used to clear a lockout.
CREATE TABLE IF NOT EXISTS auth_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT    NOT NULL,
    kind   TEXT    NOT NULL,
    detail TEXT,
    remote TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_kind_ts ON auth_events(kind, ts DESC);

CREATE TABLE IF NOT EXISTS ussd_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    code        TEXT    NOT NULL,
    response    TEXT,
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ussd_ts ON ussd_history(ts DESC);
"""


def init():
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = connect()
        conn.executescript(SCHEMA)
        _initialised = True


# --- calls ------------------------------------------------------------------

def add_call(number, direction, status, duration=0, notes=None):
    cur = connect().execute(
        "INSERT INTO calls (ts, number, direction, status, duration, notes) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), number, direction, status, duration, notes),
    )
    return cur.lastrowid


def update_call(call_id, **fields):
    allowed = {"status", "duration", "notes"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k}=?" for k in sets)
    connect().execute(
        f"UPDATE calls SET {clause} WHERE id=?", (*sets.values(), call_id)
    )


def list_calls(limit=100, offset=0, search=None, direction=None):
    sql = "SELECT * FROM calls WHERE 1=1"
    args = []
    if search:
        sql += " AND (number LIKE ? OR notes LIKE ?)"
        args += [f"%{search}%", f"%{search}%"]
    if direction:
        sql += " AND direction=?"
        args.append(direction)
    total = connect().execute(
        sql.replace("SELECT *", "SELECT COUNT(*) c", 1), args
    ).fetchone()["c"]
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    rows = [dict(r) for r in connect().execute(sql, args).fetchall()]
    return rows, total


def clear_calls():
    cur = connect().execute("DELETE FROM calls")
    return cur.rowcount


# --- sms --------------------------------------------------------------------

def add_sms(number, direction, body, status="OK", ts=None, sim_index=None,
            is_read=None, error=None):
    """Insert an SMS. Returns the row id, or None if it was a duplicate.

    The UNIQUE constraint makes re-syncing the SIM/ME store idempotent: a
    message already imported will not be inserted twice.
    """
    if is_read is None:
        is_read = 1 if direction == "OUT" else 0
    try:
        cur = connect().execute(
            "INSERT INTO sms (ts, number, direction, body, status, is_read, "
            "sim_index, error) VALUES (?,?,?,?,?,?,?,?)",
            (ts or now_iso(), number, direction, body, status,
             is_read, sim_index, error),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def list_conversations():
    """One row per correspondent, newest message first."""
    rows = connect().execute(
        """
        SELECT number,
               COUNT(*)                                        AS total,
               SUM(CASE WHEN is_read=0 AND direction='IN'
                        THEN 1 ELSE 0 END)                     AS unread,
               MAX(ts)                                         AS last_ts
        FROM sms
        GROUP BY number
        ORDER BY last_ts DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        last = connect().execute(
            "SELECT body, direction FROM sms WHERE number=? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (r["number"],)
        ).fetchone()
        item = dict(r)
        item["last_body"] = last["body"] if last else ""
        item["last_direction"] = last["direction"] if last else ""
        contact = connect().execute(
            "SELECT name FROM phonebook WHERE number=?", (r["number"],)
        ).fetchone()
        item["name"] = contact["name"] if contact else None
        out.append(item)
    return out


def get_conversation(number, limit=500):
    rows = connect().execute(
        "SELECT * FROM sms WHERE number=? ORDER BY ts ASC, id ASC LIMIT ?",
        (number, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_conversation_read(number):
    cur = connect().execute(
        "UPDATE sms SET is_read=1 WHERE number=? AND direction='IN'", (number,)
    )
    return cur.rowcount


def delete_conversation(number):
    cur = connect().execute("DELETE FROM sms WHERE number=?", (number,))
    return cur.rowcount


def unread_count():
    return connect().execute(
        "SELECT COUNT(*) c FROM sms WHERE direction='IN' AND is_read=0"
    ).fetchone()["c"]


# --- phonebook --------------------------------------------------------------

def add_contact(name, number, notes=None):
    ts = now_iso()
    cur = connect().execute(
        "INSERT INTO phonebook (name, number, notes, created_at, updated_at) "
        "VALUES (?,?,?,?,?)", (name, number, notes, ts, ts),
    )
    return cur.lastrowid


def update_contact(contact_id, name=None, number=None, notes=None):
    sets, args = [], []
    for col, val in (("name", name), ("number", number), ("notes", notes)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if not sets:
        return 0
    sets.append("updated_at=?")
    args.append(now_iso())
    args.append(contact_id)
    cur = connect().execute(
        f"UPDATE phonebook SET {', '.join(sets)} WHERE id=?", args
    )
    return cur.rowcount


def delete_contact(contact_id):
    cur = connect().execute("DELETE FROM phonebook WHERE id=?", (contact_id,))
    return cur.rowcount


def list_contacts(search=None):
    sql = "SELECT * FROM phonebook"
    args = []
    if search:
        sql += " WHERE name LIKE ? OR number LIKE ?"
        args = [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY name COLLATE NOCASE ASC"
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def find_contact_by_number(number):
    r = connect().execute(
        "SELECT * FROM phonebook WHERE number=?", (number,)
    ).fetchone()
    return dict(r) if r else None


def contact_names():
    """number -> name map, for decorating call/SMS lists."""
    return {
        r["number"]: r["name"]
        for r in connect().execute("SELECT number, name FROM phonebook")
    }


# --- history logs -----------------------------------------------------------

def add_at_history(command, response, ok=True):
    connect().execute(
        "INSERT INTO at_history (ts, command, response, ok) VALUES (?,?,?,?)",
        (now_iso(), command, response, 1 if ok else 0),
    )


def list_at_history(limit=100):
    return [dict(r) for r in connect().execute(
        "SELECT * FROM at_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()]


def add_ussd_history(code, response, ok=True):
    connect().execute(
        "INSERT INTO ussd_history (ts, code, response, ok) VALUES (?,?,?,?)",
        (now_iso(), code, response, 1 if ok else 0),
    )


def list_ussd_history(limit=50):
    return [dict(r) for r in connect().execute(
        "SELECT * FROM ussd_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()]


# --- settings ---------------------------------------------------------------
#
# Anything configurable at runtime must live here rather than in a module-level
# dict: the service restarts on every deploy and on every modem reset, and an
# in-memory setting silently reverts to its default. Voicemail was switched on
# through the UI and was found disabled again later for exactly that reason.

def get_setting(key, default=None):
    row = connect().execute(
        "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def set_setting(key, value):
    connect().execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (key, json.dumps(value), now_iso()),
    )
    return value


# --- users ------------------------------------------------------------------

def get_user(username=None):
    """The admin user, or None before one has been created."""
    if username is None:
        r = connect().execute(
            "SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    else:
        r = connect().execute(
            "SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def upsert_user(username, password_hash):
    ts = now_iso()
    connect().execute(
        "INSERT INTO users (username, password_hash, created_at, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
        "password_hash=excluded.password_hash, updated_at=excluded.updated_at",
        (username, password_hash, ts, ts),
    )


def user_count():
    return connect().execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


# --- api tokens -------------------------------------------------------------

def add_token(name, scopes, token_hash, prefix, expires_at=None):
    cur = connect().execute(
        "INSERT INTO api_tokens (name, scopes, token_hash, prefix, created_at, "
        "expires_at) VALUES (?,?,?,?,?,?)",
        (name, json.dumps(scopes), token_hash, prefix, now_iso(), expires_at),
    )
    return cur.lastrowid


def token_by_prefix(prefix):
    r = connect().execute(
        "SELECT * FROM api_tokens WHERE prefix=?", (prefix,)).fetchone()
    return dict(r) if r else None


def touch_token(token_id, remote=None):
    connect().execute(
        "UPDATE api_tokens SET last_used_at=?, last_used_ip=? WHERE id=?",
        (now_iso(), remote, token_id),
    )


def list_tokens():
    rows = [dict(r) for r in connect().execute(
        "SELECT * FROM api_tokens ORDER BY revoked_at IS NOT NULL, id DESC"
    ).fetchall()]
    for r in rows:
        r["scopes"] = json.loads(r["scopes"])
        r.pop("token_hash", None)   # never leaves the database
    return rows


def get_token(token_id):
    r = connect().execute(
        "SELECT * FROM api_tokens WHERE id=?", (token_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["scopes"] = json.loads(d["scopes"])
    return d


def revoke_token(token_id):
    cur = connect().execute(
        "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (now_iso(), token_id),
    )
    return cur.rowcount


def delete_token(token_id):
    cur = connect().execute("DELETE FROM api_tokens WHERE id=?", (token_id,))
    return cur.rowcount


# --- auth events ------------------------------------------------------------

def add_auth_event(kind, detail=None, remote=None):
    connect().execute(
        "INSERT INTO auth_events (ts, kind, detail, remote) VALUES (?,?,?,?)",
        (now_iso(), kind, detail, remote),
    )


def count_auth_events(kind, since_iso):
    return connect().execute(
        "SELECT COUNT(*) n FROM auth_events WHERE kind=? AND ts>=?",
        (kind, since_iso),
    ).fetchone()["n"]


def clear_auth_events(kind):
    connect().execute("DELETE FROM auth_events WHERE kind=?", (kind,))


def prune_auth_events(before_iso):
    """Keep the table from growing without bound; it is a rate limiter, not a log."""
    connect().execute("DELETE FROM auth_events WHERE ts < ?", (before_iso,))


def stats():
    c = connect()
    return {
        "calls": c.execute("SELECT COUNT(*) n FROM calls").fetchone()["n"],
        "sms": c.execute("SELECT COUNT(*) n FROM sms").fetchone()["n"],
        "contacts": c.execute("SELECT COUNT(*) n FROM phonebook").fetchone()["n"],
        "unread": unread_count(),
        "db_path": DB_PATH,
        "db_size_bytes": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
    }
