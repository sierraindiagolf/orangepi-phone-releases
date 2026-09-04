#!/usr/bin/env python3
"""Force SMS onto the CS/NAS domain at NV level while keeping VoLTE registered.

AT+CGSMS is ignored once IMS registers. Underneath it sits a Qualcomm NV item:

    /nv/item_files/modem/mmode/sms_domain_pref   0=CS pref 1=PS pref 2=CS only 3=PS only

which reads 01 (PS preferred) on this module — the modem is told at NV level to
submit SMS over IMS, which DIGI refuses. AT+QNVFW can write it directly.

If setting it to CS makes SMS work while AT+QCFG="ims" still reports 1,1, then
voice and SMS coexist and the ~120 s mode switching becomes unnecessary.

The original value is saved and restored if nothing works. AT+QCFG="ims",2 plus a
reset always returns the board to the known-good state regardless.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(
    next((a for a in sys.argv[1:] if not a.startswith("--")), "0771324635"))

NV_DOMAIN = "/nv/item_files/modem/mmode/sms_domain_pref"
NV_SMS_ONLY = "/nv/item_files/modem/mmode/sms_only"

VALUES = [
    ("00", "CS preferred"),
    ("02", "CS only"),
]


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


def read_nv(m, item):
    r = q(m, 'AT+QNVFR="%s"' % item, 10)
    for tok in r.replace("|", " ").split():
        if tok.startswith("+QNVFR:"):
            continue
        if all(ch in "0123456789abcdefABCDEF" for ch in tok) and tok not in ("OK",):
            return tok
    return r


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
        res = m.send_sms(TARGET, "nv %s %s" % (label, time.strftime("%H%M%S")),
                         auto_mode=False)
        print("   >>> SMS SENT ref=%s" % res.get("reference"))
        return True
    except Exception as exc:
        print("   >>> SMS FAILED %s" % str(exc)[-70:])
        print("       CEER: %s" % q(m, "AT+CEER", 8))
        return False


def main():
    m = open_modem()
    original = read_nv(m, NV_DOMAIN)
    print("target                :", TARGET)
    print("sms_domain_pref (orig):", original)
    print("sms_only              :", read_nv(m, NV_SMS_ONLY))
    print("ims                   :", q(m, 'AT+QCFG="ims"', 8))

    winner = None
    for value, label in VALUES:
        print()
        print("=" * 66)
        print("sms_domain_pref = %s (%s), IMS forced ON" % (value, label))
        print("=" * 66)
        print("   write : %s" % q(m, 'AT+QNVFW="%s",%s' % (NV_DOMAIN, value), 15))
        print("   ims on: %s" % q(m, 'AT+QCFG="ims",1', 15))
        m = full_reset(m)
        print("   readback: sms_domain_pref = %s" % read_nv(m, NV_DOMAIN))
        print("   network : %s" % wait_lte(m))
        registered = wait_ims(m)
        print("   ims     : %s (registered=%s)"
              % (q(m, 'AT+QCFG="ims"', 8), registered))
        if not registered:
            print("   IMS not registered — cannot answer the question on this value")
            continue
        m.configure_sms_params()
        if attempt(m, label):
            winner = (value, label)
            print()
            print("   *** SMS WORKS WITH IMS REGISTERED ***")
            print("   verifying the call path stays on LTE:")
            try:
                m.dial(TARGET)
                for i in range(4):
                    time.sleep(3)
                    print("     t+%-2ds %-44s CPAS=%s"
                          % ((i + 1) * 3, q(m, "AT+QNWINFO", 6),
                             (m.phone_activity() or {}).get("text")))
                print("     hung up after %ss" % m.hangup())
            except Exception as exc:
                print("     call failed: %s" % exc)
            break

    print()
    print("=" * 66)
    if winner:
        print("RESULT: sms_domain_pref=%s (%s) gives VoLTE **and** SMS." % winner)
        print("        Leaving it in place. ims: %s" % q(m, 'AT+QCFG="ims"', 8))
    else:
        print("RESULT: no NV value allowed SMS with IMS registered.")
        print("        Restoring sms_domain_pref=%s and IMS off." % original)
        q(m, 'AT+QNVFW="%s",%s' % (NV_DOMAIN, original), 15)
        q(m, 'AT+QCFG="ims",2', 15)
        m = full_reset(m)
        print("   network: %s" % wait_lte(m))
        m.configure_sms_params()
        attempt(m, "restore")
    print("=" * 66)
    m.close()


if __name__ == "__main__":
    main()
