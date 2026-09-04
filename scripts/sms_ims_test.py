#!/usr/bin/env python3
"""Retest SMS over the PS/IMS domain from a known-good LTE state.

Background: voice on this SIM runs over VoLTE (AT+QNWINFO stays "FDD LTE" for
the whole call, no CSFB drop), so IMS works. AT+CGSMS=1 (CS preferred) — the
value inherited from the WG1608 — pushes SMS at a CS domain DIGI does not
provide here, which makes the modem fall back to GSM, find no service, and
strand itself in nwscanmode=1.

The earlier CGSMS matrix ran 1 -> 3 -> 2 -> 0, so CGSMS=0 was measured on an
already-stranded modem. This retests each domain from a verified LTE
registration, restoring LTE between attempts.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")


def q(m, cmd, timeout=8):
    return (m.send_raw(cmd, timeout=timeout)[1] or "").replace("\n", " | ")


def on_lte(m):
    return "LTE" in q(m, "AT+QNWINFO")


def ensure_lte(m, label=""):
    """Undo any GSM stranding and wait for an LTE attach."""
    if on_lte(m):
        return True
    print("       %s not on LTE, recovering..." % label)
    q(m, 'AT+QCFG="nwscanmode",0,1', timeout=15)
    for _ in range(24):
        time.sleep(5)
        if on_lte(m):
            print("       recovered: %s" % q(m, "AT+QNWINFO"))
            return True
    print("       STILL NOT ON LTE: %s" % q(m, "AT+QNWINFO"))
    return False


def main():
    m = modem.Modem()
    m.open()
    for cmd in ["ATE0", "AT+CMEE=2", "AT+CMGF=1", 'AT+CSCS="GSM"', "AT+CSMS=1",
                'AT+CSCA="+40770000050",145', "AT+CSMP=17,167,0,0",
                'AT+CPMS="ME","ME","ME"']:
        m.send_raw(cmd)

    print("target:", TARGET)
    print()
    print("=== baseline ===")
    for cmd in ['AT+QCFG="nwscanmode"', 'AT+QCFG="ims"', "AT+CGSMS?",
                "AT+COPS?", "AT+QNWINFO", "AT+CEREG?", "AT+CSQ"]:
        print("   %-24s %s" % (cmd, q(m, cmd)))

    if not ensure_lte(m, "baseline"):
        print("cannot start from an LTE attach, aborting")
        m.close()
        return

    # PS/IMS first, so a CS attempt cannot poison the later results.
    for domain, name in [(0, "PS / IMS"), (2, "PS preferred"),
                         (3, "CS preferred"), (1, "CS only")]:
        print()
        print("=== AT+CGSMS=%d (%s) ===" % (domain, name))
        if not ensure_lte(m, "pre-attempt"):
            print("   skipping, no LTE")
            continue
        ok, txt = m.send_raw("AT+CGSMS=%d" % domain, timeout=10)
        if not ok:
            print("   domain rejected: %s" % txt)
            continue
        print("   CGSMS set, readback: %s" % q(m, "AT+CGSMS?"))
        time.sleep(3)
        print("   before send: %s" % q(m, "AT+QNWINFO"))

        body = "IMS test d%d %s" % (domain, time.strftime("%H%M%S"))
        sent = False
        try:
            res = m.send_sms(TARGET, body)
            sent = True
            print("   >>> SENT ref=%s" % res.get("reference"))
        except Exception as exc:
            print("   >>> FAILED %s" % exc)
            print("       CEER: %s" % q(m, "AT+CEER"))

        # Did the attempt knock the modem off LTE?
        time.sleep(2)
        after_nw = q(m, "AT+QNWINFO")
        after_mode = q(m, 'AT+QCFG="nwscanmode"')
        print("   after send : %s" % after_nw)
        print("   scanmode   : %s" % after_mode)
        if "LTE" not in after_nw:
            print("   !! the send attempt dropped the modem off LTE")

        if sent:
            print()
            print("   *** SMS ACCEPTED WITH CGSMS=%d (%s) ***" % (domain, name))
            ensure_lte(m, "post-success")
            print("   final: %s" % q(m, "AT+QNWINFO"))
            m.close()
            return

    print()
    print("=== restoring a sane state ===")
    m.send_raw("AT+CGSMS=0", timeout=10)
    ensure_lte(m, "final")
    print("   nwscanmode: %s" % q(m, 'AT+QCFG="nwscanmode"'))
    print("   network   : %s" % q(m, "AT+QNWINFO"))
    print("   cgsms     : %s" % q(m, "AT+CGSMS?"))
    m.close()
    print()
    print("DONE")


if __name__ == "__main__":
    main()
