"""Authentication and authorisation for the phone API.

Two ways in:

* **A session cookie** for the browser UI. Signed by Flask with a secret persisted
  in the ``settings`` table, so it survives the restarts this service does on every
  deploy and modem reset. A session is treated as full access.
* **A bearer token** for machines — HomeAssistant ringing the gate, scripts. Tokens
  carry a *set* of scopes so a token that needs to both ring and read status does
  not have to be widened to ``full``.

Enforcement is a single fail-closed hook rather than a decorator on each of the
~50 routes: a forgotten decorator is a silent hole, whereas an unclassified route
simply requires ``full`` until someone classifies it.

Opaque tokens rather than JWT: this is one process with SQLite on local disk, so
stateless validation buys nothing, while revocation — which matters a great deal
for a token that can open a gate — would need a database denylist anyway.
"""

import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import db

# --- tunables ---------------------------------------------------------------

DEFAULT_USER = os.environ.get("PHONE_USER", "admin")
SESSION_HOURS = int(os.environ.get("PHONE_SESSION_HOURS", "720"))  # 30 days

# pbkdf2's default of 600k iterations costs well over a second on this SoC. 200k
# is still far above the point where an offline attack on a decent passphrase is
# practical, and keeps login responsive on an Allwinner H6.
PBKDF2 = "pbkdf2:sha256:200000"

LOGIN_MAX_FAILURES = 10          # per window, then locked out
LOGIN_WINDOW_MIN = 15
RESET_MAX_PER_HOUR = 3
RESET_CODE_MINUTES = 15
RESET_MAX_ATTEMPTS = 5

TOKEN_PREFIX_BYTES = 4           # 8 hex characters
TOKEN_SECRET_BYTES = 32

SCOPES = ("read", "dial", "sms", "power", "full")
SCOPE_HELP = {
    "read": "Read everything: status, messages, call history and recorded audio.",
    "dial": "Place, answer and end calls, and send DTMF. Cannot read recordings.",
    "sms":  "Read and send SMS.",
    "power": "Reboot the board. Cannot shut it down, and cannot read or send anything.",
    "full": "Everything, including raw AT commands, USSD and carrier settings.",
}


# --- endpoint -> permitted scopes -------------------------------------------
#
# First match wins, so the restrictive entries come first. Anything not matched
# falls through to FULL_ONLY: a route added later is protected until it is
# classified here. Sessions are unaffected by that, so the UI never breaks — only
# tokens fail closed, which is the direction we want.

_R = lambda p: re.compile(p)  # noqa: E731

READ = frozenset({"read", "full"})
DIAL = frozenset({"dial", "full"})
SMS = frozenset({"sms", "full"})
POWER = frozenset({"power", "full"})
FULL = frozenset({"full"})
ANY = frozenset({"read", "dial", "sms", "full"})

RULES = [
    # Total modem control. Raw AT can do anything the firmware can, and USSD has
    # taken this modem down hard twice (docs/power-and-recovery.md).
    (_R(r"^/api/at(/|$)"),               None,                     FULL),
    (_R(r"^/api/ussd(/|$)"),             None,                     FULL),

    # Powering the board down or rebooting it. `power` exists as its own scope so
    # the Home Assistant reboot button does not need a `full` token: that token
    # lives in a config file on another host, and `full` would also hand whoever
    # read it raw AT and USSD — the two things that have taken this modem down
    # hard. Which of the two actions a token may request is a separate question,
    # settled by TOKEN_POWER_ACTIONS below.
    (_R(r"^/api/system/power$"),         None,                     POWER),

    # Changing the NTP server or forcing a resync reaches a root helper, so it is
    # `full` regardless of how harmless it looks. Reading the clock is a read.
    (_R(r"^/api/time/(ntp|sync)$"),      None,                     FULL),
    (_R(r"^/api/time$"),                 {"GET"},                  READ),

    # Triggering a balance check runs USSD, so it is gated as tightly as USSD
    # itself. Reading the last result is only reading, and is what a dashboard
    # wants, so the GET is separated out below rather than swept into FULL.
    (_R(r"^/api/balance/check$"),        None,                     FULL),
    (_R(r"^/api/balance$"),              {"POST"},                 FULL),
    (_R(r"^/api/balance$"),              {"GET"},                  READ),

    # Software and firmware release check.
    (_R(r"^/api/firmware/check$"),       {"GET"},                  READ),

    # Configuration changes.
    (_R(r"^/api/modem/configure$"),      None,                     FULL),
    (_R(r"^/api/modem/mode$"),           {"POST"},                 FULL),
    (_R(r"^/api/carrier$"),              {"POST"},                 FULL),
    (_R(r"^/api/voicemail$"),            {"POST"},                 FULL),

    # Writing greetings, deleting recordings, driving the audio path.
    (_R(r"^/api/audio/files/"),          {"PUT", "DELETE"},        FULL),
    (_R(r"^/api/audio/(play|record)$"),  None,                     FULL),
    (_R(r"^/api/recordings/"),           {"DELETE"},               FULL),
    (_R(r"^/api/phonebook"),             {"POST", "PUT", "DELETE"}, FULL),

    # Domain scopes.
    (_R(r"^/api/sms(/|$)"),              None,                     SMS),
    (_R(r"^/api/call(/|$)"),             None,                     DIAL),

    # Status is the one thing every scope needs — a dial-only token must be able
    # to see whether the modem is registered before it rings anything.
    (_R(r"^/api/status$"),               {"GET"},                  ANY),

    # Everything else that only reads.
    (_R(r"^/api/"),                      {"GET"},                  READ),
]

# Public: no session, no token. Kept deliberately tiny.
PUBLIC = [
    _R(r"^/login$"),
    _R(r"^/api/auth/(login|reset/request|reset/confirm|state)$"),
    _R(r"^/(app\.css|app\.js|login\.js|favicon\.ico)$"),
]

# Managing tokens requires a session, never a token: otherwise a leaked `dial`
# token could mint itself a `full` one, and containment is the whole point.
SESSION_ONLY = [
    _R(r"^/api/auth/(tokens|password|recovery)"),
]

# Power actions a token may request. A reboot comes back on its own; a shutdown
# does not, and this board has no wake-on-LAN to undo one remotely — the NIC
# advertises `Supports Wake-on: g`, but the device tree wires no wake interrupt
# (`IRQ eth_wake_irq not found`) and describes no PHY regulator, so nothing reads
# that register once the board is off. The only wake source is the PMIC power
# key, which means a physical trip. So `shutdown` stays session-only even though
# the route no longer is.
#
# An allowlist rather than a denylist: an action added to POWER_COMMANDS later is
# refused to tokens until it is named here, which is the direction we want.
TOKEN_POWER_ACTIONS = frozenset({"reboot"})

# The only thing reachable on a board that has no password yet: enough to set one.
PUBLIC_SETUP = _R(r"^/(login|api/auth/(login|state))$")


# --- secrets ----------------------------------------------------------------

def secret_key():
    """Signing key for the session cookie, generated once and persisted.

    It must not be regenerated per process: this service restarts on every deploy
    and on some modem faults, and a fresh key would sign everyone out each time.
    """
    key = db.get_setting("secret_key")
    if not key:
        key = secrets.token_hex(32)
        db.set_setting("secret_key", key)
    return key


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


# --- passwords --------------------------------------------------------------

def set_password(password, username=None):
    username = username or DEFAULT_USER
    db.upsert_user(username, generate_password_hash(password, method=PBKDF2))
    return username


def verify_password(username, password):
    user = db.get_user(username)
    if not user:
        return False
    return check_password_hash(user["password_hash"], password)


def has_user():
    return db.user_count() > 0


# --- tokens -----------------------------------------------------------------

def _hash_token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_token(name, scopes, expires_days=None):
    """Returns (row_id, value). The value is never recoverable afterwards."""
    scopes = [s for s in scopes if s in SCOPES] or ["read"]
    if "full" in scopes:
        scopes = ["full"]
    prefix = secrets.token_hex(TOKEN_PREFIX_BYTES)
    value = f"iris_{prefix}_{secrets.token_urlsafe(TOKEN_SECRET_BYTES)}"
    expires_at = None
    if expires_days:
        expires_at = _iso(_now() + timedelta(days=int(expires_days)))
    token_id = db.add_token(name, scopes, _hash_token(value), prefix, expires_at)
    db.add_auth_event("token_created", f"{name} {','.join(scopes)}")
    return token_id, value


def verify_token(value):
    """Row for a live token, or None. Constant-time on the secret."""
    if not value or not value.startswith("iris_"):
        return None
    parts = value.split("_", 2)
    if len(parts) != 3:
        return None
    row = db.token_by_prefix(parts[1])
    if not row:
        return None
    # The prefix lookup is the index; this is the check that actually matters.
    if not secrets.compare_digest(row["token_hash"], _hash_token(value)):
        return None
    if row["revoked_at"]:
        return None
    if row["expires_at"] and row["expires_at"] < _iso(_now()):
        return None
    row["scopes"] = json.loads(row["scopes"])
    return row


# --- rate limiting ----------------------------------------------------------

def _since(minutes):
    return _iso(_now() - timedelta(minutes=minutes))


def login_locked():
    n = db.count_auth_events("login_fail", _since(LOGIN_WINDOW_MIN))
    return n >= LOGIN_MAX_FAILURES


def note_login_failure(remote):
    db.add_auth_event("login_fail", None, remote)


def note_login_success(remote):
    db.clear_auth_events("login_fail")
    db.add_auth_event("login_ok", None, remote)


# --- password reset by SMS --------------------------------------------------
#
# The board is a phone, so reset needs no SMTP host and no stored mail password.
# The always-works path when the modem is wedged is manage.py on the board.

def reset_rate_exceeded():
    return db.count_auth_events("reset_request", _since(60)) >= RESET_MAX_PER_HOUR


def create_reset_code(remote=None):
    code = f"{secrets.randbelow(1000000):06d}"
    db.set_setting("reset_code", {
        "hash": _hash_token(code),
        "expires": _iso(_now() + timedelta(minutes=RESET_CODE_MINUTES)),
        "attempts": 0,
    })
    db.add_auth_event("reset_request", None, remote)
    return code


def consume_reset_code(code):
    """(ok, reason). The code is single use and attempt-limited."""
    state = db.get_setting("reset_code")
    if not state:
        return False, "no reset in progress"
    if state.get("expires", "") < _iso(_now()):
        db.set_setting("reset_code", None)
        return False, "code expired"
    if state.get("attempts", 0) >= RESET_MAX_ATTEMPTS:
        db.set_setting("reset_code", None)
        return False, "too many attempts"
    if not secrets.compare_digest(state.get("hash", ""), _hash_token(code or "")):
        state["attempts"] = state.get("attempts", 0) + 1
        db.set_setting("reset_code", state)
        return False, "incorrect code"
    db.set_setting("reset_code", None)
    db.add_auth_event("reset_ok")
    return True, ""


def recovery_number():
    return db.get_setting("recovery_number") or ""


# --- the gate ---------------------------------------------------------------

def _matches(rules, path):
    return any(rx.search(path) for rx in rules)


def required_scopes(path, method):
    for rx, methods, scopes in RULES:
        if rx.search(path) and (methods is None or method in methods):
            return scopes
    return FULL


def _wants_json(path):
    return path.startswith("/api/")


def install(app):
    """Wire the gate into a Flask app."""
    app.secret_key = secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Set only when actually behind TLS. ProxyFix must be installed for the
        # request to know that, otherwise the browser drops the cookie and login
        # appears to succeed while never sticking.
        SESSION_COOKIE_SECURE=os.environ.get("PHONE_INSECURE_COOKIE") != "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )

    @app.before_request
    def _gate():
        g.identity = None
        g.scopes = frozenset()
        path = request.path

        if request.method == "OPTIONS" or _matches(PUBLIC, path):
            return None
        # Static assets are served from the app root; the SPA shell itself is
        # harmless without data, and gating it would only produce a broken page
        # instead of a redirect.
        if not path.startswith("/api/") and "." in path.rsplit("/", 1)[-1]:
            return None

        # Before the first password is set the board is unconfigured. Only the
        # paths needed to *create* that password are open — not the whole API.
        #
        # Granting full access during setup was the original approach and it was
        # wrong: with no password set, anyone on the network could dial the SIM,
        # read recordings, or mint themselves a full-scope token. An unconfigured
        # board is the least protected moment there is, so it gets the narrowest
        # surface, not the widest.
        if not has_user():
            if PUBLIC_SETUP.search(path):
                g.identity, g.scopes = "setup", FULL
                return None
            return jsonify(
                success=False,
                error="this board has no password yet — set one at /login",
            ), 403

        # A cookie naming a user that no longer exists must not authenticate.
        # Without this, deleting or renaming the account leaves every already
        # issued session valid, which is the opposite of what deleting it means.
        if session.get("user"):
            if db.get_user(session["user"]):
                g.identity, g.scopes = session["user"], FULL
            else:
                session.clear()
        else:
            header = request.headers.get("Authorization", "")
            value = header[7:].strip() if header.lower().startswith("bearer ") \
                else request.headers.get("X-API-Token", "")
            row = verify_token(value) if value else None
            if row:
                g.identity = f"token:{row['name']}"
                g.scopes = frozenset(row["scopes"])
                g.token_id = row["id"]
                db.touch_token(row["id"], request.remote_addr)

        if not g.identity:
            if _wants_json(path):
                return jsonify(success=False, error="authentication required"), 401
            return app.response_class(
                status=302, headers={"Location": "/login"})

        if _matches(SESSION_ONLY, path) and g.identity.startswith("token:"):
            return jsonify(
                success=False,
                error="token management requires an interactive session",
            ), 403

        need = required_scopes(path, request.method)
        if not (g.scopes & need):
            db.add_auth_event("scope_denied", f"{g.identity} {request.method} {path}",
                              request.remote_addr)
            return jsonify(
                success=False,
                error=f"token scope insufficient: needs one of {sorted(need)}",
            ), 403
        return None
