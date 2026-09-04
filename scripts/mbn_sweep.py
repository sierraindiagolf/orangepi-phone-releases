#!/usr/bin/env python3
"""Find an MBN carrier profile that allows VoLTE and SMS at the same time.

With the generic ROW_Generic_3GPP profile, registering IMS makes the EC25 submit
SMS over IMS, and DIGI refuses it (+CEER: 0,29, facility rejected) no matter what
AT+CGSMS says. So today the board runs with IMS off: SMS works, but a call has to
CSFB to GSM and rings in 6.5-12 s instead of ~2.5 s.

Most European operators run VoLTE for voice while carrying SMS over NAS/SGs, and
that domain policy is exactly what an MBN encodes. Three European VoLTE profiles
are installed and have never been tried. This selects each in turn with IMS forced
on and tests whether SMS sends.

Pass condition: an SMS is accepted while AT+QCFG="ims" reports 1,1.

Every profile change needs a full AT+CFUN=1,1 reset (a soft cycle leaves the IMS
PDN down and IMS never registers), which re-enumerates USB — so the port is
reopened between steps.

Usage: mbn_sweep.py [number] [--profiles A,B] [--rollback-only]
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = "0771324635"
CANDIDATES = [
    "VF_Germany_VoLTE",             # large, conventional VoLTE + SMS-over-NAS
    "Bouygues_France_VoLTE",
    "Swis_switzerland_VoLTE_VoWiFi",
]
KNOWN_GOOD = "ROW_Generic_3GPP"


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def open_modem(tries=40):
    """Reopen after a reset; the AT port disappears while the module restarts."""
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


def reset_and_reopen(m, settle=25):
    try:
        m.send_raw("AT+CFUN=1,1", timeout=15)
    except Exception:
        pass
    try:
        m.close()
    except Exception:
        pass
    time.sleep(settle)
    m = open_modem()
    # The reset drops the modem to PDU mode and clears the SMSC, so re-apply the
    # operator profile before anything tries to send.
    m.configure_sms_params()
    return m


def wait_attached(m, timeout=150):
    """Wait for an LTE attach with a real signal reading."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = q(m, "AT+QNWINFO", 6)
        csq = q(m, "AT+CSQ", 6)
        if "LTE" in nw and "+CSQ: 99" not in csq:
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


def apply_profile(m, profile, ims_on=True):
    """Select an MBN profile and bring the modem back up on it."""
    print("   autosel off : %s" % q(m, 'AT+QMBNCFG="autosel",0', 15))
    print("   select      : %s" % q(m, 'AT+QMBNCFG="select","%s"' % profile, 20))
    print("   ims         : %s" % q(m, 'AT+QCFG="ims",%d' % (1 if ims_on else 2), 15))
    m = reset_and_reopen(m)
    print("   network     : %s" % wait_attached(m))
    print("   selected    : %s" % q(m, 'AT+QMBNCFG="select"', 10))
    return m


def try_sms(m, label):
    body = "mbn %s %s" % (label[:14], time.strftime("%H%M%S"))
    try:
        res = m.send_sms(TARGET, body, auto_mode=False)
        print("   >>> SMS SENT ref=%s" % res.get("reference"))
        return True
    except Exception as exc:
        print("   >>> SMS FAILED %s" % str(exc)[-70:])
        print("       CEER: %s" % q(m, "AT+CEER", 8))
        return False


def rollback(m):
    print()
    print("=" * 66)
    print("ROLLBACK to the known-good configuration")
    print("=" * 66)
    print("   autosel on  : %s" % q(m, 'AT+QMBNCFG="autosel",1', 15))
    print("   select      : %s" % q(m, 'AT+QMBNCFG="select","%s"' % KNOWN_GOOD, 20))
    print("   ims off     : %s" % q(m, 'AT+QCFG="ims",2', 15))
    m = reset_and_reopen(m)
    print("   network     : %s" % wait_attached(m))
    print("   selected    : %s" % q(m, 'AT+QMBNCFG="select"', 10))
    print("   ims         : %s" % q(m, 'AT+QCFG="ims"', 10))
    ok = try_sms(m, "rollback")
    print("   rollback SMS works: %s" % ok)
    return m, ok


def main():
    global TARGET
    TARGET = modem.normalise_number(
        next((a for a in sys.argv[1:] if not a.startswith("--")), TARGET))
    profiles = (opt("--profiles").split(",") if opt("--profiles")
                else list(CANDIDATES))

    m = open_modem()
    print("target :", TARGET)
    print("start  :", m.current_mode())
    print("profile:", q(m, 'AT+QMBNCFG="select"', 10))

    # Prove the escape route before touching anything, so a failed sweep cannot
    # strand the board in a non-working configuration.
    if "--rollback-only" in sys.argv:
        m, _ = rollback(m)
        m.close()
        return 0

    print()
    print("=" * 66)
    print("STEP 0 — verify rollback works BEFORE changing anything")
    print("=" * 66)
    m, base_ok = rollback(m)
    if not base_ok:
        print()
        print("Baseline SMS does not work — aborting rather than sweeping from a")
        print("broken state. Investigate before continuing.")
        m.close()
        return 1

    winner = None
    for profile in profiles:
        print()
        print("=" * 66)
        print("PROFILE: %s  (IMS forced on)" % profile)
        print("=" * 66)
        try:
            m = apply_profile(m, profile, ims_on=True)
        except SystemExit:
            raise
        except Exception as exc:
            print("   could not apply: %s" % exc)
            continue

        registered = wait_ims(m)
        print("   ims         : %s  (registered=%s)"
              % (q(m, 'AT+QCFG="ims"', 10), registered))
        if not registered:
            print("   IMS did not register on this profile — skipping SMS test")
            continue

        if try_sms(m, profile):
            winner = profile
            print()
            print("   *** %s GIVES VoLTE **AND** SMS ***" % profile)
            print("   verifying the call path stays on LTE...")
            try:
                m.dial(TARGET)
                for i in range(5):
                    time.sleep(3)
                    print("     t+%-2ds %-44s CPAS=%s"
                          % ((i + 1) * 3, q(m, "AT+QNWINFO", 6),
                             (m.phone_activity() or {}).get("text")))
                print("     hangup after %ss" % m.hangup())
            except Exception as exc:
                print("     call failed: %s" % exc)
            break

    print()
    print("=" * 66)
    if winner:
        print("RESULT: %s — VoLTE and SMS work together. Leaving it selected." % winner)
        print("        ims: %s" % q(m, 'AT+QCFG="ims"', 10))
        print("        mbn: %s" % q(m, 'AT+QMBNCFG="select"', 10))
    else:
        print("RESULT: no profile allowed SMS with IMS registered.")
        print("        Rolling back to the known-good configuration.")
        m, _ = rollback(m)
    print("=" * 66)
    m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
