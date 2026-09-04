#!/usr/bin/env python3
"""Parser tests for balance.py, against real captured DIGI messages.

Sends nothing and touches no hardware — pure text in, snapshot out. Run this
after any change to the patterns, because the only way to find out the other way
is to wait a month for the option to lapse.

The fixtures are verbatim from the database (sms ids 44-48, captured
2026-08-26), double spaces and all.
"""
import datetime
import sys

sys.path.insert(0, "/home/shoim2/phone")
sys.path.insert(0, ".")
sys.path.insert(0, "..")

import balance  # noqa: E402

# The real batch: five messages from 68737173.
REAL = [
    "Aveti Digi National 2.5 pana pe 11.09 cu auto-reactivare",
    "Mai aveti 99.53 GB date pana pe 11.09.",
    "Mai aveti nelimitat SMS in retea pana pe 11.09.",
    "Mai aveti nelimitat minute in retea pana pe 11.09, 298 minute in alte "
    "retele pana pe 11.09, 100 minute internationale in SEE si Moldova "
    "incluse pana pe 11.09.",
    "Creditul tau este de 3,98  euro.",
]

# The 700 shortcode phrases the credit differently, with a dot separator.
REAL_700 = ["Creditul dvs este de 3.98 euro. Multumim!"]

fails = []


def check(label, got, want):
    if got != want:
        fails.append("%s: got %r, want %r" % (label, got, want))
        print("  FAIL %-28s got %r want %r" % (label, got, want))
    else:
        print("  ok   %-28s %r" % (label, got))


print("== the real batch, as if read on 2026-08-26")
snap = balance.parse_messages(REAL, today=datetime.date(2026, 8, 26))
check("option", snap["option"], "Digi National 2.5")
check("option_expires", snap["option_expires"], "2026-09-11")
check("auto_reactivate", snap["auto_reactivate"], True)
check("credit_eur", snap["credit_eur"], 3.98)
check("data_gb", snap["data_gb"], 99.53)
check("sms_home", snap["sms_home"], None)          # nelimitat
check("minutes_home", snap["minutes_home"], None)  # nelimitat
check("minutes_other", snap["minutes_other"], 298.0)
check("minutes_intl", snap["minutes_intl"], 100.0)
check("days_left", snap["days_left"], 16)

print()
print("== credit from the 700 shortcode (dot separator)")
snap700 = balance.parse_messages(REAL_700, today=datetime.date(2026, 8, 26))
check("credit_eur", snap700["credit_eur"], 3.98)
check("option_expires", snap700["option_expires"], None)
check("days_left", snap700["days_left"], None)

print()
print("== the year DIGI never sends")
# The whole reason _resolve_year exists: on 28 December, an option running to
# 05.01 is next year. Stamping the current year reads it as 357 days expired and
# the reminder fires forever.
check("Dec 28 -> 05.01",
      balance._resolve_year(5, 1, datetime.date(2026, 12, 28)).isoformat(),
      "2027-01-05")
check("Jan 3 -> 28.12",
      balance._resolve_year(28, 12, datetime.date(2027, 1, 3)).isoformat(),
      "2026-12-28")
check("Aug 26 -> 11.09",
      balance._resolve_year(11, 9, datetime.date(2026, 8, 26)).isoformat(),
      "2026-09-11")
check("29 Feb, non-leap year",
      balance._resolve_year(29, 2, datetime.date(2027, 2, 20)).isoformat(),
      "2028-02-29")

print()
print("== partial batches keep the option date")
# A quota message missing is not a reason to lose the date the reminder needs.
only_option = balance.parse_messages([REAL[0]], today=datetime.date(2026, 8, 26))
check("option kept", only_option["option_expires"], "2026-09-11")
check("credit absent", only_option["credit_eur"], None)
check("no crash on empty", balance.parse_messages([])["option_expires"], None)
check("None body tolerated",
      balance.parse_messages([None, REAL[0]],
                             today=datetime.date(2026, 8, 26))["option"],
      "Digi National 2.5")

print()
print("== an option without auto-reactivare")
no_auto = balance.parse_messages(
    ["Aveti Digi National 5 pana pe 03.10"], today=datetime.date(2026, 9, 30))
check("name", no_auto["option"], "Digi National 5")
check("auto_reactivate", no_auto["auto_reactivate"], False)
check("days_left", no_auto["days_left"], 3)

print()
print("== reminder wording")
for label, days, auto in (("3 days, auto-renew", 3, True),
                          ("1 day, singular", 1, True),
                          ("today", 0, True),
                          ("expired", -2, True),
                          ("no auto-renew", 3, False)):
    s = dict(snap)
    s["auto_reactivate"] = auto
    s["days_left"] = days
    s["option_expires"] = (
        datetime.date(2026, 8, 26) + datetime.timedelta(days=days)).isoformat()
    print("  %-20s %s" % (label, balance.reminder_text(s)))

# Wording must never say "1 days".
one = dict(snap)
one["days_left"] = 1
check("singular day", " 1 day " in balance.reminder_text(one), True)

print()
print("== alert gating")


class FakeSettings:
    """Stands in for the settings table so gating can be tested without a db."""

    def __init__(self, alerted_for=None):
        self.alerted_for = alerted_for

    def __call__(self, key, default=None):
        return self.alerted_for if key == "balance_alerted_for" else default


real_get = balance.db.get_setting
try:
    balance.db.get_setting = FakeSettings()
    cfg = {"balance_alert_number": "+40771324635", "balance_alert_days": 3}
    check("fires at 3 of 3",
          balance._should_alert({"days_left": 3, "option_expires": "2026-09-11"}, cfg)[0],
          True)
    check("silent at 4 of 3",
          balance._should_alert({"days_left": 4, "option_expires": "2026-09-11"}, cfg)[0],
          False)
    check("fires when overdue",
          balance._should_alert({"days_left": -1, "option_expires": "2026-09-11"}, cfg)[0],
          True)
    check("silent with no number",
          balance._should_alert({"days_left": 1, "option_expires": "2026-09-11"},
                                {"balance_alert_number": None,
                                 "balance_alert_days": 3})[0],
          False)
    check("silent with no date parsed",
          balance._should_alert({"days_left": None, "option_expires": None}, cfg)[0],
          False)

    balance.db.get_setting = FakeSettings(alerted_for="2026-09-11")
    check("no repeat for same expiry",
          balance._should_alert({"days_left": 2, "option_expires": "2026-09-11"}, cfg)[0],
          False)
    check("fires again after renewal",
          balance._should_alert({"days_left": 2, "option_expires": "2026-10-11"}, cfg)[0],
          True)
finally:
    balance.db.get_setting = real_get

print()
if fails:
    print("FAILED (%d):" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("all checks passed")
