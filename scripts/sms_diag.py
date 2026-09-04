#!/usr/bin/env python3
"""Diagnose SMS submission on DIGI Romania / LTE.

On an LTE-only registration an SMS can only leave the device over IMS (SMS over
IP) or over CSFB/SGs. This walks the state, then tries the CGSMS domain matrix,
capturing AT+CEER after every failure so the network's own reason is recorded.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")


def show(m, cmds):
    for cmd in cmds:
        ok, txt = m.send_raw(cmd, timeout=8)
        flat = (txt or "").replace("\n", " | ")
        print("   %-28s %s %s" % (cmd, "  " if ok else "!!", flat[:120]))


def attempt(m, label):
    body = "OrangePi test %s" % time.strftime("%H%M%S")
    try:
        res = m.send_sms(TARGET, body)
        print("   >>> %-22s SENT ref=%s" % (label, res.get("reference")))
        return True
    except Exception as exc:
        print("   >>> %-22s FAILED %s" % (label, exc))
        ok, ceer = m.send_raw("AT+CEER", timeout=8)
        print("       AT+CEER -> %s" % (ceer or "").replace("\n", " | "))
        return False


def main():
    m = modem.Modem()
    m.open()
    m.send_raw("ATE0")
    m.send_raw("AT+CMEE=2")
    print("target:", TARGET)

    print()
    print("== registration state ==")
    show(m, ["AT+CPIN?", "AT+COPS?", "AT+CREG?", "AT+CGREG?", "AT+CEREG?",
             "AT+CSQ", "AT+QNWINFO"])

    print()
    print("== SMS configuration ==")
    show(m, ["AT+CMGF?", "AT+CSCA?", "AT+CSMP?", "AT+CPMS?", "AT+CGSMS?",
             "AT+CSCS?"])

    print()
    print("== IMS / VoLTE state ==")
    show(m, ['AT+QCFG="ims"', "AT+CIREG?", "AT+CIREP?", 'AT+QCFG="volte"',
             "AT+QIMSCFG?"])

    print()
    print("== installed carrier profiles (MBN) ==")
    show(m, ['AT+QMBNCFG="list"'])

    print()
    print("== CGSMS domain matrix ==")
    # 0=PS(IMS) 1=CS 2=PS preferred 3=CS preferred
    for domain, name in [(1, "CS"), (3, "CS preferred"),
                         (2, "PS preferred"), (0, "PS / IMS")]:
        ok, _ = m.send_raw("AT+CGSMS=%d" % domain, timeout=8)
        if not ok:
            print("   >>> CGSMS=%d not accepted" % domain)
            continue
        time.sleep(2)
        if attempt(m, "CGSMS=%d (%s)" % (domain, name)):
            print()
            print("SUCCESS with CGSMS=%d (%s)" % (domain, name))
            m.close()
            return

    print()
    print("== retry with IMS enabled ==")
    ok, txt = m.send_raw('AT+QCFG="ims",1', timeout=10)
    print("   enable ims ->", ok, (txt or "").replace("\n", " | ")[:80])
    if ok:
        print("   waiting 30s for IMS registration...")
        for i in range(6):
            time.sleep(5)
            _, cireg = m.send_raw("AT+CIREG?", timeout=8)
            print("     CIREG: %s" % (cireg or "").replace("\n", " | ")[:60])
        m.send_raw("AT+CGSMS=0", timeout=8)
        time.sleep(2)
        attempt(m, "IMS on + CGSMS=0")

    print()
    print("SMS DIAG DONE")
    m.close()


if __name__ == "__main__":
    main()
