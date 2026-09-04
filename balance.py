#!/usr/bin/env python3
"""Prepaid option tracking: check the balance, remind before the option lapses.

DIGI publishes the prepaid state only through an interactive USSD menu, and it
answers by SMS rather than in the USSD reply itself. `*100#` -> `1` -> `1` ends
with *"S-a trimis statusul serviciilor prepaid prin SMS"* and then five messages
arrive from shortcode 68737173 over the next few seconds. So a check is: walk
the menu, wait for the messages, parse them, and compare the option date against
today.

Three things about that data shape drive the design here.

**The dates carry no year.** The option message says `pana pe 11.09` and nothing
more, so the year has to be inferred. Naively stamping the current year breaks
every December: an option expiring `05.01` would be read as ten months in the
past and the reminder would fire permanently. `_resolve_year` picks whichever of
last/this/next year lands nearest today instead.

**The option auto-renews.** `cu auto-reactivare` means DIGI re-charges the option
from the remaining credit on the expiry date, so the thing that actually breaks
service is not the date arriving — it is the date arriving with too little credit
to renew. That is why the reminder carries the credit figure and not just the
date: the date alone would be a reminder about something that usually resolves
itself. The option price is not published anywhere in these messages, so the
message reports both numbers and lets a human make the call.

**USSD is the one operation on this board with a documented history of taking the
modem down** (docs/power-and-recovery.md: it forces the CS domain, which drops to
GSM, whose transmit bursts are the highest-current state the module enters). That
was a power-delivery fault, since fixed by splitting power from data, and USSD
has run clean since. But it is still the riskiest thing here, so a check is
skipped rather than queued whenever the modem is busy or unwell, and the session
is always cancelled on the way out — a half-open USSD session is how the next
one fails.

Runs as a daemon thread inside the Flask process, for the same reason
`mqtt_bridge` does: `app.py` owns the serial port through a single reader thread,
so an external cron job could not drive the modem without fighting it for the
port. The schedule lives here instead of in systemd, and `next_run` in the status
dict is what to look at when asking whether it is due.
"""

import datetime
import os
import re
import threading
import time

import db

# systemd-timesyncd drops this once the clock is trustworthy. It matters more
# here than almost anywhere else in this project: every figure this module
# produces is a date subtraction, and the board has no RTC battery, so after a
# power cut it comes up believing it is whenever it last shut down. A check run
# then would not fail — it would confidently report the wrong number of days
# left, which is worse.
CLOCK_SYNC_FLAG = "/run/systemd/timesync/synchronized"

# The shortcode the prepaid status arrives from. 700 answers with the credit
# alone and is kept because it is a useful cross-check on the parsed figure.
SENDERS = ("68737173", "700")

# The menu walk. Each step is (what to send, what the reply must contain for the
# step to count as having worked). Matching on the reply text is what stops a
# changed menu from being answered with a blind "1" — if DIGI reorders the menu,
# this fails loudly on the step that no longer matches instead of silently
# selecting whatever option 1 has become.
USSD_STEPS = (
    ("*100#", "credit"),        # main menu: "1 Credit si Consum ..."
    ("1", "verifica credit"),   # submenu:   "1 Verifica Credit"
    ("1", "sms"),               # ends with: "S-a trimis ... prin SMS."
)

# How long to wait for the five SMS replies. Observed arrival spread is about 5
# seconds; the ceiling is generous because a busy network can stretch it and the
# cost of waiting is nothing.
SMS_WAIT_SECONDS = 120
SMS_POLL_SECONDS = 3

# How late a scheduled check may still run. Without this, a process that starts
# at 23:00 finds the 09:15 slot long past and unrun, and fires USSD immediately —
# so every deploy and every reboot would trigger the riskiest operation on the
# board at whatever hour it happened. Past the window the day is simply skipped:
# this data changes once a month, and a missed day costs nothing next to an
# unexpected transmit burst.
GRACE_SECONDS = 3 * 3600

DEFAULTS = {
    "balance_enabled": True,
    "balance_check_at": "09:15",     # local time, HH:MM
    "balance_alert_days": 3,
    "balance_alert_number": None,    # no reminder is sent until this is set
}

# --- settings ---------------------------------------------------------------


def config():
    """Current configuration, defaults filled in."""
    out = {}
    for key, fallback in DEFAULTS.items():
        out[key] = db.get_setting(key, fallback)
    return out


def set_config(**kw):
    """Update configuration. Unknown keys are refused rather than stored."""
    bad = [k for k in kw if k not in DEFAULTS]
    if bad:
        raise ValueError("unknown setting(s): %s" % ", ".join(sorted(bad)))
    for key, value in kw.items():
        db.set_setting(key, value)
    return config()


# --- parsing ----------------------------------------------------------------
#
# Every pattern below was written against real captured messages, kept as
# fixtures in scripts/balance_parse_test.py. Romanian text without diacritics,
# which is how it arrives.

# "Aveti Digi National 2.5 pana pe 11.09 cu auto-reactivare"
RE_OPTION = re.compile(
    r"Aveti\s+(?P<name>.+?)\s+pana\s+pe\s+(?P<day>\d{1,2})\.(?P<month>\d{1,2})"
    r"(?P<auto>\s+cu\s+auto-reactivare)?",
    re.IGNORECASE)

# "Creditul tau este de 3,98  euro." / "Creditul dvs este de 3.98 euro. Multumim!"
# Comma and dot both appear as the decimal separator, sometimes with a double
# space before the currency.
RE_CREDIT = re.compile(
    r"Creditul\s+(?:tau|dvs|dumneavoastra)\s+este\s+de\s+"
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*euro",
    re.IGNORECASE)

# "Mai aveti 99.53 GB date pana pe 11.09."
RE_DATA = re.compile(
    r"Mai\s+aveti\s+(?P<amount>[\d.,]+)\s*(?P<unit>GB|MB)\s+date\s+pana\s+pe\s+"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})",
    re.IGNORECASE)

# "Mai aveti nelimitat SMS in retea pana pe 11.09."
RE_SMS = re.compile(
    r"Mai\s+aveti\s+(?P<amount>nelimitat|\d+)\s+SMS\s+in\s+retea\s+pana\s+pe\s+"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})",
    re.IGNORECASE)

# "Mai aveti nelimitat minute in retea pana pe 11.09, 298 minute in alte retele
#  pana pe 11.09, 100 minute internationale in SEE si Moldova incluse pana pe
#  11.09."
RE_MINUTES_HOME = re.compile(
    r"(?P<amount>nelimitat|\d+)\s+minute\s+in\s+retea", re.IGNORECASE)
RE_MINUTES_OTHER = re.compile(
    r"(?P<amount>nelimitat|\d+)\s+minute\s+in\s+alte\s+retele", re.IGNORECASE)
RE_MINUTES_INTL = re.compile(
    r"(?P<amount>nelimitat|\d+)\s+minute\s+internationale", re.IGNORECASE)


def _amount(text):
    """'nelimitat' -> None (meaning no limit); '99.53' / '3,98' -> float."""
    if text is None:
        return None
    t = text.strip().lower()
    if t.startswith("nelimitat"):
        return None
    return float(t.replace(",", "."))


def _resolve_year(day, month, today=None):
    """A day/month with no year -> the nearest such date to today.

    DIGI sends `11.09` and leaves the year out. Assuming the current year is
    wrong for a month either side of New Year: on 28 December an option running
    to `05.01` would be read as eleven months expired. Picking the candidate
    closest to today is right in every case where the date is within about six
    months, which a monthly prepaid option always is.
    """
    today = today or datetime.date.today()
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = datetime.date(year, month, day)
        except ValueError:
            continue        # 29 February in a non-leap year
        gap = abs((cand - today).days)
        if best is None or gap < best[0]:
            best = (gap, cand)
    if best is None:
        raise ValueError("no valid date for %02d.%02d" % (day, month))
    return best[1]


def parse_messages(bodies, today=None):
    """Parse a batch of prepaid SMS bodies into one snapshot.

    Order-independent and tolerant of extra messages, because the batch is
    whatever arrived in the window — including, on a bad day, only some of it.
    Anything not found stays None rather than raising: a missing quota line is
    not a reason to lose the option date, which is the only field the reminder
    actually needs.
    """
    today = today or datetime.date.today()
    snap = {
        "option": None,
        "option_expires": None,
        "auto_reactivate": None,
        "credit_eur": None,
        "data_gb": None,
        "sms_home": None,
        "minutes_home": None,
        "minutes_other": None,
        "minutes_intl": None,
        "messages_seen": len(bodies),
    }

    for body in bodies:
        if not body:
            continue

        m = RE_OPTION.search(body)
        if m and snap["option"] is None:
            snap["option"] = m.group("name").strip()
            snap["option_expires"] = _resolve_year(
                int(m.group("day")), int(m.group("month")), today).isoformat()
            snap["auto_reactivate"] = bool(m.group("auto"))

        m = RE_CREDIT.search(body)
        if m and snap["credit_eur"] is None:
            snap["credit_eur"] = _amount(m.group("amount"))

        m = RE_DATA.search(body)
        if m and snap["data_gb"] is None:
            amount = _amount(m.group("amount"))
            if amount is not None and m.group("unit").upper() == "MB":
                amount = round(amount / 1024, 2)
            snap["data_gb"] = amount

        m = RE_SMS.search(body)
        if m and snap["sms_home"] is None:
            snap["sms_home"] = _amount(m.group("amount"))

        # The minutes all arrive in one message, so these are read together and
        # only from a body that mentions minutes at all.
        if "minute" in body.lower():
            for key, pattern in (("minutes_home", RE_MINUTES_HOME),
                                 ("minutes_other", RE_MINUTES_OTHER),
                                 ("minutes_intl", RE_MINUTES_INTL)):
                mm = pattern.search(body)
                if mm and snap[key] is None:
                    snap[key] = _amount(mm.group("amount"))

    if snap["option_expires"]:
        expires = datetime.date.fromisoformat(snap["option_expires"])
        snap["days_left"] = (expires - today).days
    else:
        snap["days_left"] = None
    return snap


# --- the reminder -----------------------------------------------------------


def _plural(n, one, many):
    return one if n == 1 else many


def reminder_text(snap):
    """The SMS body. Short, because it is read on a lock screen."""
    days = snap.get("days_left")
    when = snap.get("option_expires")
    option = snap.get("option") or "prepaid option"
    credit = snap.get("credit_eur")

    if days is None or when is None:
        return None

    if days < 0:
        head = "%s expired on %s" % (option, when)
    elif days == 0:
        head = "%s expires today (%s)" % (option, when)
    else:
        head = "%s expires in %d %s (%s)" % (
            option, days, _plural(days, "day", "days"), when)

    parts = [head]
    if credit is not None:
        parts.append("credit %.2f EUR" % credit)
    if snap.get("auto_reactivate"):
        # The distinction worth spelling out: the option renews itself, so the
        # thing to check is whether there is enough credit for it to do so.
        parts.append("auto-renews, so top up if credit is short")
    else:
        parts.append("no auto-renew")
    return ". ".join(parts) + "."


def _should_alert(snap, cfg):
    """(send it?, why not) — separated out so the reason can be reported."""
    if not cfg.get("balance_alert_number"):
        return False, "no alert number configured"
    days = snap.get("days_left")
    if days is None:
        return False, "no option date parsed"
    threshold = int(cfg.get("balance_alert_days") or 0)
    if days > threshold:
        return False, "%d days left, threshold is %d" % (days, threshold)
    # One reminder per expiry cycle. Keyed on the date itself, so a renewal that
    # moves the date arms the next reminder automatically, and a check running
    # daily inside the window does not send the same thing four times.
    if db.get_setting("balance_alerted_for") == snap.get("option_expires"):
        return False, "already alerted for %s" % snap.get("option_expires")
    return True, None


# --- running a check --------------------------------------------------------


def _new_sms_since(row_id):
    """Prepaid messages inserted after row_id, oldest first."""
    marks = ",".join("?" for _ in SENDERS)
    rows = db.connect().execute(
        "SELECT id, number, body FROM sms WHERE id > ? AND direction = 'IN' "
        "AND number IN (%s) ORDER BY id" % marks,
        (row_id,) + tuple(SENDERS)).fetchall()
    return [dict(r) for r in rows]


def _max_sms_id():
    row = db.connect().execute("SELECT COALESCE(MAX(id), 0) AS m FROM sms").fetchone()
    return row["m"] if row else 0


def run_check(modem, send_alert=True, now=None):
    """Walk the USSD menu, collect the replies, parse, and maybe remind.

    Returns the result dict that also gets stored as the last snapshot, so the
    caller can report it without a second read.
    """
    started = (now or datetime.datetime.now()).isoformat(timespec="seconds")
    result = {"ran_at": started, "ok": False, "error": None,
              "alert_sent": False, "alert_skipped": None, "snapshot": None}

    if not os.path.exists(CLOCK_SYNC_FLAG):
        result["error"] = "skipped: clock not synchronised"
        _remember(result)
        return result

    # A check is never worth interrupting a call for. This board is the gate
    # opener, and USSD drops the modem to GSM to run.
    try:
        if modem.status().get("active_call"):
            result["error"] = "skipped: a call is in progress"
            _remember(result)
            return result
    except Exception as exc:
        result["error"] = "skipped: modem status unavailable (%s)" % exc
        _remember(result)
        return result

    since = _max_sms_id()

    try:
        for code, expect in USSD_STEPS:
            reply = modem.send_ussd(code)
            text = (reply.get("text") or "")
            db.add_ussd_history(code, text, True)
            if expect not in text.lower():
                raise RuntimeError(
                    "step %r: expected %r in reply, got %r"
                    % (code, expect, text[:120]))
    except Exception as exc:
        result["error"] = "ussd failed: %s" % exc
        db.add_ussd_history("balance-check", str(exc), False)
        _cancel(modem)
        _remember(result)
        return result

    # The session ends itself after the last step, but cancelling is cheap and a
    # session left half-open is how the *next* check fails.
    _cancel(modem)

    # The replies arrive as SMS, pulled into the database by app.py's +CMTI
    # handler. Waiting on row ids rather than timestamps is deliberate: the
    # stored timestamps come from the network and are not consistently offset,
    # so "newer than when I started" is not reliably expressible in them.
    deadline = time.time() + SMS_WAIT_SECONDS
    bodies, snap = [], None
    while time.time() < deadline:
        rows = _new_sms_since(since)
        bodies = [r["body"] for r in rows]
        snap = parse_messages(bodies)
        if snap.get("option_expires"):
            break
        time.sleep(SMS_POLL_SECONDS)

    if not snap or not snap.get("option_expires"):
        result["error"] = ("no option message in %d messages after %ds"
                           % (len(bodies), SMS_WAIT_SECONDS))
        result["snapshot"] = snap
        _remember(result)
        return result

    result["ok"] = True
    result["snapshot"] = snap

    should, why = _should_alert(snap, config())
    if not should:
        result["alert_skipped"] = why
    elif send_alert:
        text = reminder_text(snap)
        number = config()["balance_alert_number"]
        try:
            modem.send_sms(number, text)
            db.add_sms(number, "OUT", text, status="SENT")
            db.set_setting("balance_alerted_for", snap["option_expires"])
            result["alert_sent"] = True
        except Exception as exc:
            result["error"] = "reminder SMS failed: %s" % exc
            db.add_sms(number, "OUT", text, status="FAILED", error=str(exc))

    _remember(result)
    return result


def _cancel(modem):
    try:
        modem.cancel_ussd()
    except Exception:
        pass


def _remember(result):
    db.set_setting("balance_last_result", result)
    if result.get("snapshot") and result["snapshot"].get("option_expires"):
        db.set_setting("balance_last_snapshot", result["snapshot"])


# --- status -----------------------------------------------------------------


def status():
    """Everything the UI or an API caller needs, without touching the modem.

    `days_left` is recomputed from the stored expiry rather than read back from
    the snapshot: the snapshot was written whenever the check last ran, and a
    day-count from three days ago is exactly the kind of stale number this
    project keeps deciding not to display.
    """
    snap = db.get_setting("balance_last_snapshot")
    out = {
        "config": config(),
        "last_result": db.get_setting("balance_last_result"),
        "alerted_for": db.get_setting("balance_alerted_for"),
        "snapshot": snap,
        "next_run": _next_run().isoformat(timespec="seconds"),
    }
    if snap and snap.get("option_expires"):
        expires = datetime.date.fromisoformat(snap["option_expires"])
        out["days_left"] = (expires - datetime.date.today()).days
    else:
        out["days_left"] = None
    return out


# --- schedule ---------------------------------------------------------------


def _check_at():
    """Configured run time as (hour, minute), falling back on nonsense."""
    raw = str(config().get("balance_check_at") or DEFAULTS["balance_check_at"])
    try:
        hh, mm = raw.split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        hh, mm = DEFAULTS["balance_check_at"].split(":")
        return int(hh), int(mm)


def _next_run(now=None):
    """When a check will next actually happen, allowing for the grace window."""
    now = now or datetime.datetime.now()
    hh, mm = _check_at()
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    ran_today = db.get_setting("balance_last_run_date") == now.date().isoformat()
    if not ran_today and slot <= now < slot + datetime.timedelta(
            seconds=GRACE_SECONDS):
        return now                      # due right now
    if not ran_today and now < slot:
        return slot                     # still to come today
    return slot + datetime.timedelta(days=1)


def _loop(modem):
    while True:
        try:
            # Checked before anything is stamped: with a stale clock, today's
            # date is wrong, and recording a run against it would suppress the
            # real day's check once the clock jumps forward.
            if not os.path.exists(CLOCK_SYNC_FLAG):
                time.sleep(60)
                continue

            cfg = config()
            now = datetime.datetime.now()
            hh, mm = _check_at()
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now.date().isoformat()
            already = db.get_setting("balance_last_run_date") == today

            in_window = due <= now < due + datetime.timedelta(
                seconds=GRACE_SECONDS)

            if cfg.get("balance_enabled") and not already and in_window:
                # Stamped before the run, not after. A check that wedges or
                # crashes must not be retried in a tight loop for the rest of
                # the day — this is the one operation here that has taken the
                # modem down, so the failure mode to avoid is repetition.
                db.set_setting("balance_last_run_date", today)
                run_check(modem)
        except Exception:
            # Never let the scheduler die; the next tick is a minute away.
            pass
        time.sleep(60)


def start(modem):
    """Start the daily scheduler as a daemon thread."""
    threading.Thread(target=_loop, args=(modem,), daemon=True,
                     name="balance").start()
