#!/usr/bin/env python3
"""Read-only checks against the live modem: config, status, parsing.

Sends nothing chargeable — no calls, no SMS, no USSD.
"""
import json
import sys

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402


def main():
    m = modem.Modem()
    m.open()

    print("== configure (DIGI profile) ==")
    for step in m.configure():
        flag = "ok  " if step["ok"] else "FAIL"
        print("  [%s] %-46s %s" % (flag, step["command"], step.get("error", "")))

    print()
    print("== status ==")
    print(json.dumps(m.status(), indent=2, ensure_ascii=False))

    print()
    print("== band config (should show the EMEA mask) ==")
    print("  ", m.band_config())

    print()
    print("== stored messages (AT+CMGL) ==")
    msgs = m.read_messages("ALL")
    print("  count:", len(msgs))
    for msg in msgs[:5]:
        print("   idx=%-3s %-6s %-15s %s | %s" % (
            msg.get("index"), msg.get("direction"), msg.get("number"),
            msg.get("ts_iso"), (msg.get("body") or "")[:60]))

    print()
    print("== SIM phonebook (AT+CPBR) ==")
    pb = m.sim_phonebook()
    print("  entries:", len(pb))
    for e in pb[:5]:
        print("   %-3s %-15s %s" % (e["index"], e["number"], e["name"]))

    print()
    print("== active calls (AT+CLCC) ==")
    print("  ", m.call_status())

    print()
    print("== number normalisation ==")
    for n in ["0722123456", "+40722123456", "0040722123456", "1234",
              "0212345678", "07 22 12 34 56"]:
        print("   %-18s -> %s" % (n, modem.normalise_number(n)))

    print()
    print("== charset detection ==")
    for t in ["Hello from Orange Pi", "Salut, ce faci? Numarul tau"]:
        print("   gsm7=%-5s %s" % (modem.is_gsm7(t), t))

    m.close()
    print()
    print("SELFTEST DONE")


if __name__ == "__main__":
    main()
