#!/usr/bin/env python3
"""Find a working SMSC for this SIM.

The SMSC currently configured was carried over from the WG1608's DIGI SIM. This
reads the SIM's own EF_SMSP record (the authoritative value for this subscriber)
and then tries a submission against each candidate.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")

EF_SMSP = 28482          # 0x6F42


def decode_bcd_number(hexstr):
    """Decode a TS 51.011 dialling-number field into +CC... form."""
    try:
        raw = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if not raw:
        return None
    length = raw[0]
    if length == 0xFF or length < 2 or length > len(raw) - 1:
        return None
    ton = raw[1]
    digits = ""
    for byte in raw[2:1 + length]:
        for nib in (byte & 0x0F, byte >> 4):
            if nib == 0x0F:
                continue
            digits += "0123456789*#abc"[nib] if nib < 15 else ""
    if not digits:
        return None
    return ("+" if (ton & 0x70) == 0x10 else "") + digits


def main():
    m = modem.Modem()
    m.open()
    m.send_raw("ATE0")
    m.send_raw("AT+CMEE=2")
    m.send_raw("AT+CMGF=1")
    m.send_raw('AT+CSCS="GSM"')

    print("target:", TARGET)
    print()
    print("== SMSC currently in the modem ==")
    print("  ", m.send_raw("AT+CSCA?")[1])

    print()
    print("== SIM EF_SMSP (record 1) ==")
    ok, txt = m.send_raw("AT+CRSM=178,%d,1,4,28" % EF_SMSP, timeout=10)
    print("  ", (txt or "").replace("\n", " | "))
    sim_smsc = None
    if ok and "+CRSM:" in (txt or ""):
        payload = txt.split(",")[-1].strip().strip('"')
        # EF_SMSP layout: alpha-id (variable) then fixed tail; the SMSC TS field
        # sits 12 bytes back from the end of the 28-byte record.
        if len(payload) >= 56:
            tail = payload[-24:]
            sim_smsc = decode_bcd_number(tail)
        print("   decoded SMSC guess:", sim_smsc)

    candidates = []
    if sim_smsc:
        candidates.append((sim_smsc, "from SIM EF_SMSP"))
    candidates += [
        ("+40770000050", "DIGI (WG1608 value)"),
        ("+40766000000", "DIGI alternate"),
        ("+40745000000", "legacy RCS-RDS"),
    ]

    seen = set()
    print()
    print("== submission attempts ==")
    for smsc, label in candidates:
        if not smsc or smsc in seen:
            continue
        seen.add(smsc)
        ok, _ = m.send_raw('AT+CSCA="%s",145' % smsc, timeout=8)
        if not ok:
            print("   %-16s %-24s CSCA rejected" % (smsc, label))
            continue
        body = "OPi test %s" % time.strftime("%H%M%S")
        try:
            res = m.send_sms(TARGET, body)
            print("   %-16s %-24s SENT ref=%s" % (smsc, label, res.get("reference")))
            print()
            print("SUCCESS with SMSC %s (%s)" % (smsc, label))
            m.close()
            return
        except Exception as exc:
            print("   %-16s %-24s FAILED %s" % (smsc, label, exc))
        time.sleep(2)

    print()
    print("== PDU-mode attempt (bypasses text-mode encoding entirely) ==")
    m.send_raw("AT+CMGF=0")
    print("   CMGF=0 set; text mode restored after this probe")
    m.send_raw("AT+CMGF=1")

    print()
    print("== does the SIM even list SMS in its service table? ==")
    for cmd in ["AT+CRSM=176,28466,0,0,20", "AT+CNUM", "AT+CIMI"]:
        print("   %-30s %s" % (cmd, (m.send_raw(cmd, timeout=10)[1] or "")
                               .replace("\n", " | ")[:90]))

    print()
    print("SMSC PROBE DONE")
    m.close()


if __name__ == "__main__":
    main()
