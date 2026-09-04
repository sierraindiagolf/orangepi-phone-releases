#!/usr/bin/env python3
"""Find a send method that works, using raw serial I/O.

The SIM sends SMS fine in an LTE router, so the subscription is good and the
fault is on this side. This deliberately bypasses modem.py so the abstraction
cannot be the confound, and walks the ways our submission could differ from a
router's:

  1. text mode terminated with CRLF   (what modem.py does today — the trailing
     LF is consumed as the first character of the message body)
  2. text mode terminated with CR only
  3. PDU mode, SMSC octet 00 (modem's own CSCA)
  4. PDU mode, SMSC written explicitly into the PDU
  5. PDU mode with a relative validity period (first octet 0x11 + TP-VP)

Routers almost always use PDU mode, which is the prime suspect.
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB2"
TARGET = (sys.argv[1] if len(sys.argv) > 1 else "0771324635")

GSM7 = ("@£$¥èéùìòÇ\nØø\rÅå"
        "Δ_ΦΓΛΩΠΨΣΘΞ\x1b"
        "ÆæßÉ"
        " !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
        "¿abcdefghijklmnopqrstuvwxyzäöñüà")


def norm(n):
    n = n.strip().replace(" ", "")
    if n.startswith("+"):
        return n
    if n.startswith("0") and len(n) == 10:
        return "+40" + n[1:]
    return n


def swap_bcd(number):
    d = number.lstrip("+")
    padded = d + "F" if len(d) % 2 else d
    return "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2)), len(d)


def pack_gsm7(text):
    septets = []
    for ch in text:
        i = GSM7.find(ch)
        if i < 0:
            return None, 0
        septets.append(i)
    out, acc, nbits = bytearray(), 0, 0
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


def build_pdu(number, text, smsc=None, with_vp=False):
    if smsc:
        s_digits, _ = swap_bcd(smsc)
        smsc_field = "%02X91%s" % (1 + len(s_digits) // 2, s_digits)
    else:
        smsc_field = "00"
    addr, alen = swap_bcd(number)
    ud, udl = pack_gsm7(text)
    dcs = "00"
    if ud is None:
        raw = text.encode("utf-16-be")
        ud, udl, dcs = raw.hex().upper(), len(raw), "08"
    first = "11" if with_vp else "01"
    vp = "AA" if with_vp else ""          # AA = 4 days relative
    tpdu = first + "00" + ("%02X" % alen) + "91" + addr + "00" + dcs + vp \
        + ("%02X" % udl) + ud
    return smsc_field + tpdu, len(tpdu) // 2


class Raw:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.3, write_timeout=5)
        self.s.reset_input_buffer()

    def cmd(self, text, wait=1.2, term="\r\n"):
        self.s.reset_input_buffer()
        self.s.write((text + term).encode())
        self.s.flush()
        time.sleep(wait)
        return self.s.read(self.s.in_waiting or 1).decode(errors="replace")

    def submit(self, header, payload, term="\r\n", prompt_wait=6, resp_wait=45):
        """Send a CMGS header, wait for '>', write the payload, read the result."""
        self.s.reset_input_buffer()
        self.s.write((header + term).encode())
        self.s.flush()
        buf = ""
        deadline = time.time() + prompt_wait
        while time.time() < deadline:
            chunk = self.s.read(self.s.in_waiting or 1).decode(errors="replace")
            buf += chunk
            if ">" in buf:
                break
            if "ERROR" in buf:
                return False, "no prompt: " + buf.strip().replace("\n", " | ")
        else:
            self.s.write(b"\x1b")
            return False, "no prompt (timeout): " + buf.strip().replace("\n", " | ")

        self.s.write(payload.encode("ascii", errors="replace") + b"\x1a")
        self.s.flush()
        out = ""
        deadline = time.time() + resp_wait
        while time.time() < deadline:
            out += self.s.read(self.s.in_waiting or 1).decode(errors="replace")
            if "+CMGS:" in out or "OK" in out.replace("\r", "").split("\n")[-1:][0]:
                if "+CMGS:" in out:
                    return True, out.strip().replace("\n", " | ")
            if "ERROR" in out:
                return False, out.strip().replace("\n", " | ")
        return False, "timeout: " + out.strip().replace("\n", " | ")

    def close(self):
        self.s.close()


def ceer(r):
    return r.cmd("AT+CEER", 1.0).strip().replace("\n", " | ")


def main():
    target = norm(TARGET)
    r = Raw(PORT)
    for c in ["ATE0", "AT+CMEE=2", "AT+CSMS=1"]:
        r.cmd(c)
    print("target:", target)
    print("port  :", PORT)
    print("CSCA  :", r.cmd("AT+CSCA?").strip().replace("\n", " | "))
    print("CGSMS :", r.cmd("AT+CGSMS?").strip().replace("\n", " | "))

    stamp = time.strftime("%H%M%S")
    results = []

    # 1 + 2: text mode, both line terminations.
    for label, term in [("text mode, CRLF header", "\r\n"),
                        ("text mode, CR only", "\r")]:
        r.cmd("AT+CMGF=1")
        r.cmd('AT+CSCS="GSM"')
        ok, info = r.submit('AT+CMGS="%s"' % target, "raw %s %s" % (label[:4], stamp),
                            term=term)
        print()
        print("--- %s ---" % label)
        print("   %s  %s" % ("SENT" if ok else "FAILED", info[:160]))
        if not ok:
            print("   CEER: %s" % ceer(r))
        results.append((label, ok))
        if ok:
            break
        time.sleep(2)

    # 3-5: PDU mode variants.
    if not any(ok for _, ok in results):
        for label, smsc, vp in [
                ("PDU, SMSC=00 (use CSCA)", None, False),
                ("PDU, explicit SMSC", "+40770000050", False),
                ("PDU, with validity period", None, True)]:
            r.cmd("AT+CMGF=0")
            pdu, tlen = build_pdu(target, "raw pdu %s" % stamp, smsc=smsc,
                                  with_vp=vp)
            print()
            print("--- %s ---" % label)
            print("   PDU  : %s" % pdu)
            print("   len  : %d" % tlen)
            ok, info = r.submit("AT+CMGS=%d" % tlen, pdu, term="\r")
            print("   %s  %s" % ("SENT" if ok else "FAILED", info[:160]))
            if not ok:
                print("   CEER: %s" % ceer(r))
            results.append((label, ok))
            if ok:
                break
            time.sleep(2)

    r.cmd("AT+CMGF=1")
    print()
    print("=== summary ===")
    for label, ok in results:
        print("   %-30s %s" % (label, "SENT" if ok else "failed"))
    r.close()


if __name__ == "__main__":
    main()
