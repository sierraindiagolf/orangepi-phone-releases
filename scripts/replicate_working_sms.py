#!/usr/bin/env python3
"""Reproduce the SMS configuration that demonstrably worked.

Forensics from the previous deployment's own AT log (phone.db at_logs) show a
successful send at 2026-08-14 11:38:32, and at that moment the modem had:

    AT+CMGF=1
    AT+CNMI=2,1,0,0,0
    AT+CPMS="SM","SM","SM"          <- SIM storage, not "ME"
    AT+CSCA                          never set — modem/SIM native value
    AT+CGSMS                         never set — modem default
    AT+CSMP                          never set — modem default
    AT+CSMS                          never set — modem default

Sends began failing minutes later, right after that session switched the MBN
profile to Commercial-DT-VOLTE and cycled CFUN.

This resets the modem to clear every override, records the native values, and
tries a send with only the two settings that were present when it worked.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")
SKIP_RESET = "--no-reset" in sys.argv


def q(m, cmd, timeout=10):
    return (m.send_raw(cmd, timeout=timeout)[1] or "").replace("\n", " | ")


def wait_lte(m, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = q(m, "AT+QNWINFO")
        if "LTE" in nw:
            return nw
        time.sleep(5)
    return None


def open_modem(port="/dev/ttyUSB2", tries=40):
    """Wait for the AT port to come back after a reset."""
    for _ in range(tries):
        try:
            m = modem.Modem(port=port)
            m.open()
            m.send_raw("ATE0")
            return m
        except Exception:
            time.sleep(5)
    raise SystemExit("AT port never returned")


def report_native(m, label):
    print("   --- %s ---" % label)
    for cmd in ["AT+CSCA?", "AT+CGSMS?", "AT+CSMP?", "AT+CPMS?", "AT+CSMS?",
                "AT+CMGF?", 'AT+QMBNCFG="select"', 'AT+QMBNCFG="autosel"',
                'AT+QCFG="ims"', "AT+CGDCONT?"]:
        print("     %-22s %s" % (cmd, q(m, cmd, timeout=15)[:150]))


def attempt(m, label):
    body = "native-cfg test %s" % time.strftime("%H%M%S")
    try:
        res = m.send_sms(TARGET, body)
        print("   >>> %-28s SENT ref=%s" % (label, res.get("reference")))
        return True
    except Exception as exc:
        print("   >>> %-28s FAILED %s" % (label, exc))
        print("       CEER: %s" % q(m, "AT+CEER"))
        return False


def main():
    print("target:", TARGET)

    m = open_modem()
    if not SKIP_RESET:
        print()
        print("=== resetting the modem to clear every override ===")
        m.send_raw("AT+CFUN=1,1", timeout=20)
        m.close()
        time.sleep(20)
        m = open_modem()
        print("   AT port back")
        nw = wait_lte(m)
        print("   network: %s" % nw)

    m.send_raw("AT+CMEE=2")
    print()
    print("=== NATIVE values after reset (nothing overridden yet) ===")
    report_native(m, "as the modem powers up")

    # Exactly the state that worked at 11:38:32.
    print()
    print("=== applying ONLY the settings present when sending worked ===")
    for cmd in ["AT+CMGF=1", "AT+CNMI=2,1,0,0,0",
                'AT+CPMS="SM","SM","SM"']:
        print("   %-24s %s" % (cmd, q(m, cmd, timeout=15)))

    print()
    print("=== attempt 1: native SMSC / native CGSMS / SIM storage ===")
    if attempt(m, "native everything"):
        print()
        print("*** SENDING WORKS WITH THE NATIVE CONFIGURATION ***")
        report_native(m, "state that worked")
        m.close()
        return

    # Isolate which of my overrides is the culprit, one at a time.
    print()
    print("=== attempt 2: + the WG1608 SMSC (was this the breaking change?) ===")
    q(m, 'AT+CSCA="+40770000050",145')
    attempt(m, "native + WG1608 SMSC")

    print()
    print("=== attempt 3: back to native SMSC, ME storage ===")
    m.send_raw("AT+CFUN=0", timeout=20)
    time.sleep(3)
    m.send_raw("AT+CFUN=1", timeout=20)
    wait_lte(m)
    q(m, "AT+CMGF=1")
    q(m, 'AT+CPMS="ME","ME","ME"')
    attempt(m, "native SMSC + ME storage")

    print()
    print("=== final state ===")
    report_native(m, "end")
    m.close()
    print()
    print("DONE")


if __name__ == "__main__":
    main()
