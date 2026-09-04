#!/usr/bin/env python3
"""Does SMS work on 2G/3G when it fails on LTE?

On LTE a mobile-originated SMS must travel over IMS (SMS over IP) or over
CSFB/SGs. On GSM/WCDMA it goes over the CS domain natively. If the SIM is fine
but the LTE SMS path is not available, forcing a 2G/3G attach should make SMS
and USSD start working.

Restores AT+QCFG="nwscanmode",0 (automatic) on the way out, including on error.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")

MODES = [
    (1, "GSM only"),
    (2, "WCDMA only"),
]


def line(m, cmd, timeout=8):
    return (m.send_raw(cmd, timeout=timeout)[1] or "").replace("\n", " | ")


def show_registration(m, indent="     "):
    for cmd in ["AT+COPS?", "AT+QNWINFO", "AT+CREG?", "AT+CGREG?", "AT+CEREG?",
                "AT+CSQ"]:
        print("%s%-12s %s" % (indent, cmd, line(m, cmd)))


def wait_registered(m, timeout=120):
    """Wait until CREG/CGREG/CEREG reports registered (1 or 5)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for cmd in ["AT+CREG?", "AT+CGREG?", "AT+CEREG?"]:
            resp = line(m, cmd)
            for stat in (",1", ",5"):
                if stat in resp.replace(" ", ""):
                    return True, cmd, resp
        time.sleep(5)
    return False, None, None


def try_sms(m, label):
    body = "RAT test %s %s" % (label, time.strftime("%H%M%S"))
    try:
        res = m.send_sms(TARGET, body)
        print("     SMS  -> SENT ref=%s" % res.get("reference"))
        return True
    except Exception as exc:
        print("     SMS  -> FAILED %s" % exc)
        print("             CEER: %s" % line(m, "AT+CEER"))
        return False


def try_ussd(m):
    try:
        res = m.send_ussd("*118#", timeout=45)
        print("     USSD -> [%s] %s" % (res.get("status_text"),
                                        (res.get("text") or "")[:120]))
        return res.get("status") == 0
    except Exception as exc:
        print("     USSD -> FAILED %s" % exc)
        return False
    finally:
        try:
            m.cancel_ussd()
        except Exception:
            pass


def main():
    m = modem.Modem()
    m.open()
    for cmd in ["ATE0", "AT+CMEE=2", "AT+CMGF=1", 'AT+CSCS="GSM"', "AT+CSMS=1",
                'AT+CSCA="+40770000050",145']:
        m.send_raw(cmd)

    print("target:", TARGET)
    print()
    print("=" * 68)
    print("CURRENT NETWORK SELECTION CONFIG")
    print("=" * 68)
    for cmd in ['AT+QCFG="nwscanmode"', 'AT+QCFG="nwscanseq"',
                'AT+QCFG="servicedomain"', 'AT+QCFG="roamservice"',
                'AT+QCFG="band"', "AT+CEMODE?", "AT+COPS=?"]:
        print("   %-26s %s" % (cmd, line(m, cmd, timeout=60)))
    print()
    print("   registration now:")
    show_registration(m, "   ")

    print()
    print("=" * 68)
    print("IS THE VOICE CALL USING CSFB? (which RAT serves the call)")
    print("=" * 68)
    try:
        m.dial(TARGET)
        for i in range(4):
            time.sleep(3)
            print("   t+%-2ds QNWINFO %s | CPAS %s" % (
                (i + 1) * 3, line(m, "AT+QNWINFO"), m.phone_activity()))
        m.hangup()
        print("   hung up")
    except Exception as exc:
        print("   call failed:", exc)
        try:
            m.hangup()
        except Exception:
            pass
    time.sleep(3)

    try:
        for mode, label in MODES:
            print()
            print("=" * 68)
            print("FORCING %s (nwscanmode=%d)" % (label.upper(), mode))
            print("=" * 68)
            ok, _ = m.send_raw('AT+QCFG="nwscanmode",%d,1' % mode, timeout=15)
            if not ok:
                print("   mode not accepted, skipping")
                continue
            print("   waiting for registration...")
            registered, which, resp = wait_registered(m, timeout=120)
            if not registered:
                print("   NOT REGISTERED within 120s — %s may be unavailable "
                      "here" % label)
                show_registration(m)
                continue
            print("   registered via %s: %s" % (which, resp))
            show_registration(m)
            time.sleep(5)
            sms_ok = try_sms(m, label)
            ussd_ok = try_ussd(m)
            print()
            print("   >>> %s: SMS=%s USSD=%s" % (
                label, "OK" if sms_ok else "FAIL", "OK" if ussd_ok else "FAIL"))
            if sms_ok:
                print()
                print("   *** SMS WORKS ON %s ***" % label.upper())
    finally:
        print()
        print("=" * 68)
        print("RESTORING AUTOMATIC MODE")
        print("=" * 68)
        m.send_raw('AT+QCFG="nwscanmode",0,1', timeout=15)
        registered, which, resp = wait_registered(m, timeout=120)
        print("   back to auto, registered=%s (%s %s)" % (registered, which, resp))
        show_registration(m, "   ")
        m.close()

    print()
    print("RAT TEST DONE")


if __name__ == "__main__":
    main()
