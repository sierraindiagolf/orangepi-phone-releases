#!/usr/bin/env python3
"""Restore the PDP/APN configuration that was live when SMS last worked.

From the previous deployment's own at_logs, this was the state at 10:31 today,
and a send succeeded at 11:38:

    AT+QCFG="usbnet",0
    AT+QCFG="band",0,800d5,0
    AT+QCFG="ims",1
    AT+QMBNCFG="autosel",1
    AT+CGDCONT=1,"IP","net"        <- default bearer APN "net", plain IP
    AT+CGDCONT=2,"IPV4V6","ims"
    AT+CMGF=1
    AT+CNMI=2,1,0,0,0

Its SMS parameters (CPMS/CGSMS/CSCA/CSMP) were the same ones this stack already
applies, so the PDP context is the remaining difference. Also tries a correctly
formed PDU submit — the previous PDU path set VPF=relative (first octet 0x11)
without ever emitting the TP-VP octet, so it could only ever error.
"""
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

TARGET = modem.normalise_number(sys.argv[1] if len(sys.argv) > 1 else "0771324635")

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1b"
    "ÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def q(m, cmd, timeout=15):
    return (m.send_raw(cmd, timeout=timeout)[1] or "").replace("\n", " | ")


def swap_bcd(number):
    n = number.lstrip("+")
    padded = n + "F" if len(n) % 2 else n
    return "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2)), len(n)


def pack_gsm7(text):
    """7-bit pack a GSM-7 string. Returns (hex, septet_count)."""
    septets = []
    for ch in text:
        idx = GSM7_BASIC.find(ch)
        if idx < 0:
            return None, 0
        septets.append(idx)
    bits = ""
    for s in reversed(septets):
        bits = format(s, "07b") + bits
    # Pack least-significant-first into octets.
    out = bytearray()
    acc = 0
    nbits = 0
    for s in septets:
        acc |= s << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    return out.hex().upper(), len(septets)


def build_pdu(number, text):
    """SMS-SUBMIT with VPF=0 (no validity period) and the modem's own SMSC.

    Layout: 00 | 01 | 00 | AL | 91 | addr | 00(PID) | DCS | UDL | UD
    First octet 0x01 means MTI=SUBMIT with VPF=00, so no TP-VP octet follows —
    which is exactly what the previous implementation got wrong.
    """
    addr, alen = swap_bcd(number)
    ud, udl = pack_gsm7(text)
    if ud is None:
        raw = text.encode("utf-16-be")
        ud, udl, dcs = raw.hex().upper(), len(raw), "08"
    else:
        dcs = "00"
    pdu = "00" + "01" + "00" + ("%02X" % alen) + "91" + addr + "00" + dcs \
          + ("%02X" % udl) + ud
    tpdu_len = (len(pdu) - 2) // 2
    return pdu, tpdu_len


def send_pdu(m, number, text):
    pdu, tpdu_len = build_pdu(number, text)
    print("     PDU: %s (tpdu_len=%d)" % (pdu, tpdu_len))
    with m._cmd_lock:
        m.send_raw("AT+CMGF=0", timeout=10)
        try:
            m.send("AT+CMGS=%d" % tpdu_len, timeout=15, collect_prompt=True)
            m._in_command.set()
            try:
                m._write_payload(pdu.encode("ascii") + b"\x1a")
                lines = m._collect("PDU payload", 60, False)
            finally:
                m._in_command.clear()
            return True, lines
        except Exception as exc:
            return False, str(exc)
        finally:
            m.send_raw("AT+CMGF=1", timeout=10)


def attempt_text(m, label):
    try:
        res = m.send_sms(TARGET, "pdp test %s" % time.strftime("%H%M%S"))
        print("   >>> %-26s TEXT SENT ref=%s" % (label, res.get("reference")))
        return True
    except Exception as exc:
        print("   >>> %-26s TEXT FAILED %s" % (label, exc))
        print("       CEER: %s" % q(m, "AT+CEER"))
        return False


def wait_lte(m, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = q(m, "AT+QNWINFO")
        if "LTE" in nw:
            return nw
        time.sleep(5)
    return None


def main():
    m = modem.Modem()
    m.open()
    for c in ["ATE0", "AT+CMEE=2"]:
        m.send_raw(c)
    print("target:", TARGET)

    print()
    print("=== restoring the 10:31 configuration ===")
    for cmd in ['AT+QCFG="usbnet",0',
                'AT+QCFG="band",0,800d5,0',
                'AT+QCFG="ims",1',
                'AT+QMBNCFG="autosel",1',
                'AT+CGDCONT=1,"IP","net"',
                'AT+CGDCONT=2,"IPV4V6","ims"',
                "AT+CMGF=1",
                "AT+CNMI=2,1,0,0,0"]:
        print("   %-32s %s" % (cmd, q(m, cmd)))

    print()
    print("=== radio cycle so the new PDP context is used on attach ===")
    q(m, "AT+CFUN=0", timeout=25)
    time.sleep(5)
    q(m, "AT+CFUN=1", timeout=25)
    print("   network: %s" % wait_lte(m))
    print("   CGDCONT: %s" % q(m, "AT+CGDCONT?")[:200])
    print("   CGPADDR: %s" % q(m, "AT+CGPADDR"))
    print("   ims    : %s" % q(m, 'AT+QCFG="ims"'))
    print("   band   : %s" % q(m, 'AT+QCFG="band"'))

    # SMS parameters exactly as the working deployment set them before each send.
    for cmd in ['AT+CPMS="ME","ME","ME"', "AT+CMGF=1", "AT+CSMP=17,167,0,0",
                'AT+CSCA="+40770000050",145', "AT+CGSMS=1"]:
        q(m, cmd)
    time.sleep(3)

    print()
    print("=== attempt: text mode with the restored PDP context ===")
    if attempt_text(m, "APN net + CGSMS=1"):
        print()
        print("*** SENDING WORKS AGAIN ***")
        m.close()
        return

    print()
    print("=== attempt: correctly formed PDU submit ===")
    ok, info = send_pdu(m, TARGET, "pdu test %s" % time.strftime("%H%M%S"))
    print("   >>> PDU %s: %s" % ("SENT" if ok else "FAILED", info))
    if not ok:
        print("       CEER: %s" % q(m, "AT+CEER"))

    print()
    print("=== attempt: text mode with CGSMS=0 on this PDP context ===")
    q(m, "AT+CGSMS=0")
    time.sleep(2)
    attempt_text(m, "APN net + CGSMS=0")

    print()
    print("=== state ===")
    for cmd in ["AT+CGDCONT?", "AT+CGPADDR", "AT+CSCA?", "AT+CGSMS?",
                "AT+CPMS?", "AT+QNWINFO", 'AT+QCFG="ims"']:
        print("   %-14s %s" % (cmd, q(m, cmd)[:170]))
    m.close()
    print()
    print("DONE")


if __name__ == "__main__":
    main()
