#!/usr/bin/env python3
"""Live end-to-end checks against the DIGI SIM. Sends real traffic.

Usage: livetest.py [number] [--sms] [--ussd] [--call]
Default target: +40771324635
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = "0771324635"

events = []


def on_event(kind, data):
    events.append((time.strftime("%H:%M:%S"), kind, data))
    print("   [URC %s] %s %s" % (time.strftime("%H:%M:%S"), kind, data))


def main():
    args = sys.argv[1:]
    target = next((a for a in args if not a.startswith("--")), TARGET)
    do_sms = "--sms" in args
    do_ussd = "--ussd" in args
    do_call = "--call" in args
    if not (do_sms or do_ussd or do_call):
        do_sms = do_ussd = True

    m = modem.Modem(on_event=on_event)
    m.open()
    m.configure()
    num = modem.normalise_number(target)
    print("target: %s" % num)

    if do_sms:
        print()
        print("== SMS send ==")
        body = "OrangePi EC25 test %s" % time.strftime("%H:%M:%S")
        try:
            res = m.send_sms(num, body)
            print("   SENT ->", res)
        except Exception as exc:
            print("   FAILED ->", type(exc).__name__, exc)

        print("   waiting 25s for delivery / incoming echo...")
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1)

        print("   stored messages now:")
        for msg in m.read_messages("ALL"):
            print("     idx=%-3s %-4s %-15s %s | %s" % (
                msg.get("index"), msg.get("direction"), msg.get("number"),
                msg.get("ts_iso"), (msg.get("body") or "")[:60]))

    if do_ussd:
        print()
        print("== USSD ==")
        for code in ["*118#", "*100#"]:
            try:
                res = m.send_ussd(code, timeout=40)
                print("   %-8s -> [%s] %s" % (
                    code, res.get("status_text"), (res.get("text") or "")[:200]))
            except Exception as exc:
                print("   %-8s -> FAILED %s" % (code, exc))
            try:
                m.cancel_ussd()
            except Exception:
                pass
            time.sleep(2)

    if do_call:
        print()
        print("== voice call ==")
        try:
            m.dial(num)
            print("   dialing... (12s)")
            for _ in range(6):
                time.sleep(2)
                print("     CLCC:", m.call_status(),
                      "CPAS:", m.phone_activity())
            dur = m.hangup()
            print("   hung up, duration=%ss" % dur)
        except Exception as exc:
            print("   FAILED ->", type(exc).__name__, exc)

    print()
    print("== URC log ==")
    for ts, kind, data in events:
        print("   %s %-16s %s" % (ts, kind, data))

    m.close()
    print()
    print("LIVETEST DONE")


if __name__ == "__main__":
    main()
