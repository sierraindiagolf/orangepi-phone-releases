#!/usr/bin/env python3
"""Does AT+CGSMS work with IMS on, if the preference is set BEFORE attaching?

Earlier CGSMS testing set the value and sent immediately, without re-attaching.
That is very likely invalid: the SMS-domain preference is signalled to the network
during attach/TAU, so changing it on an already-attached modem may never reach the
network at all. Every "CGSMS makes no difference" conclusion rests on that flawed
method.

This sets the preference first, then forces a full re-attach so the value is
carried in the attach, and only then sends. If SMS succeeds with IMS registered,
the voice/SMS mode switching becomes unnecessary and the board can sit in VoLTE
permanently.

Also sweeps AT+CSMS (message service 0 vs 1), which was never varied with IMS on.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(
    next((a for a in sys.argv[1:] if not a.startswith("--")), "0771324635"))


def open_modem(tries=40):
    for _ in range(tries):
        try:
            m = modem.Modem()
            m.open()
            m.send_raw("ATE0")
            m.send_raw("AT+CMEE=2")
            return m
        except Exception:
            time.sleep(5)
    raise SystemExit("AT port never came back")


def q(m, cmd, timeout=10):
    return (m.send_raw(cmd, timeout=timeout)[1] or "").replace("\n", " | ")


def full_reset(m):
    try:
        m.send_raw("AT+CFUN=1,1", timeout=15)
    except Exception:
        pass
    try:
        m.close()
    except Exception:
        pass
    time.sleep(25)
    return open_modem()


def wait_lte(m, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = q(m, "AT+QNWINFO", 6)
        if "LTE" in nw and "+CSQ: 99" not in q(m, "AT+CSQ", 6):
            return nw
        time.sleep(6)
    return None


def wait_ims(m, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if m.current_mode().get("ims_registered") == 1:
            return True
        time.sleep(6)
    return False


def attempt(m, label):
    try:
        res = m.send_sms(TARGET, "reattach %s %s" % (label, time.strftime("%H%M%S")),
                         auto_mode=False)
        print("   >>> SENT ref=%s" % res.get("reference"))
        return True
    except Exception as exc:
        print("   >>> FAILED %s" % str(exc)[-75:])
        print("       CEER: %s" % q(m, "AT+CEER", 8))
        return False


def trial(m, cgsms, csms, label):
    print()
    print("=" * 66)
    print("%s  (CGSMS=%s, CSMS=%s, set BEFORE re-attach)" % (label, cgsms, csms))
    print("=" * 66)
    # Set the preference while detached-ish, then let the attach carry it.
    print("   ims on   : %s" % q(m, 'AT+QCFG="ims",1', 15))
    print("   cgsms    : %s" % q(m, "AT+CGSMS=%d" % cgsms, 10))
    print("   csms     : %s" % q(m, "AT+CSMS=%d" % csms, 10))
    m = full_reset(m)
    print("   network  : %s" % wait_lte(m))
    # Re-assert after the reset, since a reset clears them, then attach again so
    # the values are definitely present in the registration.
    print("   cgsms    : %s" % q(m, "AT+CGSMS=%d" % cgsms, 10))
    print("   csms     : %s" % q(m, "AT+CSMS=%d" % csms, 10))
    print("   detach   : %s" % q(m, "AT+CFUN=0", 20))
    time.sleep(5)
    print("   attach   : %s" % q(m, "AT+CFUN=1", 20))
    print("   network  : %s" % wait_lte(m))
    registered = wait_ims(m)
    print("   ims      : %s (registered=%s)" % (q(m, 'AT+QCFG="ims"', 8), registered))
    print("   readback : cgsms=%s csms=%s"
          % (q(m, "AT+CGSMS?", 8), q(m, "AT+CSMS?", 8)))
    if not registered:
        print("   IMS not registered — this trial cannot answer the question")
        return m, False
    m.configure_sms_params()
    return m, attempt(m, label)


def main():
    m = open_modem()
    print("target:", TARGET)
    print("start :", m.current_mode())

    combos = [
        (1, 1, "CS domain, phase 2+"),
        (3, 1, "CS preferred, phase 2+"),
        (1, 0, "CS domain, phase 1"),
    ]
    won = None
    for cgsms, csms, label in combos:
        m, ok = trial(m, cgsms, csms, label)
        if ok:
            won = (cgsms, csms, label)
            break

    print()
    print("=" * 66)
    if won:
        print("*** SMS WORKS WITH IMS REGISTERED: CGSMS=%d CSMS=%d (%s) ***" % won)
        print("    Mode switching is unnecessary — the board can stay on VoLTE.")
        print("    ims: %s" % q(m, 'AT+QCFG="ims"', 8))
    else:
        print("No CGSMS/CSMS combination works with IMS registered, even when the")
        print("preference is set before the attach. Restoring the working setup.")
        q(m, 'AT+QCFG="ims",2', 15)
        m = full_reset(m)
        print("   network: %s" % wait_lte(m))
        m.configure_sms_params()
        print("   sanity SMS with IMS off:")
        attempt(m, "restore")
    print("=" * 66)
    m.close()


if __name__ == "__main__":
    main()
