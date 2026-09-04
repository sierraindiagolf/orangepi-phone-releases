#!/usr/bin/env python3
"""Can SMS and voice both work if we avoid RAT transitions entirely?

Established so far:
  ims=1 (VoLTE)  -> voice works on LTE, SMS refused (submitted over IMS)
  ims=2 (IMS off)-> SMS works over SGs on LTE, but a call triggers CSFB
                    LTE->GSM and the modem resets its firmware mid-transition

DIGI does have GSM 900 here (AT+QNWINFO reported "GSM","22605","GSM 900",24
after a fallback). On 2G both SMS and voice are native CS services, so camping
there removes the transition that crashes the modem.

Trade-off: no LTE data while camped on GSM. The board has Ethernet, so that may
be acceptable. Restores automatic scan mode on exit unless --keep is passed.
"""
import sys
import time

import serial

TARGET = sys.argv[1] if len(sys.argv) > 1 else "+40771324635"
KEEP = "--keep" in sys.argv
PORT = "/dev/ec25-at"


def wait_port(tries=48):
    for _ in range(tries):
        for p in (PORT, "/dev/ttyUSB2", "/dev/ttyUSB3"):
            try:
                s = serial.Serial(p, 115200, timeout=0.5, write_timeout=5)
                s.write(b"AT\r\n")
                s.flush()
                time.sleep(0.6)
                if "OK" in s.read(s.in_waiting or 1).decode(errors="replace"):
                    return s
                s.close()
            except Exception:
                pass
        time.sleep(5)
    return None


def cmd(s, text, wait=1.2):
    try:
        s.reset_input_buffer()
        s.write((text + "\r\n").encode())
        s.flush()
        time.sleep(wait)
        return s.read(s.in_waiting or 1).decode(errors="replace").strip() \
            .replace("\n", " | ")
    except Exception as exc:
        return "PORT ERROR: %s" % exc


def alive(s):
    return "OK" in cmd(s, "AT", 0.8)


def send_sms(s, number, text):
    cmd(s, "AT+CMGF=1")
    cmd(s, 'AT+CSCS="GSM"')
    try:
        s.reset_input_buffer()
        s.write(('AT+CMGS="%s"\r' % number).encode())
        s.flush()
        buf = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            buf += s.read(s.in_waiting or 1).decode(errors="replace")
            if ">" in buf:
                break
            if "ERROR" in buf:
                return False, buf.strip().replace("\n", " | ")
        else:
            s.write(b"\x1b")
            return False, "no prompt"
        s.write(text.encode("ascii", "replace") + b"\x1a")
        s.flush()
        out = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            out += s.read(s.in_waiting or 1).decode(errors="replace")
            if "+CMGS:" in out:
                return True, out.strip().replace("\n", " | ")
            if "ERROR" in out:
                return False, out.strip().replace("\n", " | ")
        return False, "timeout"
    except Exception as exc:
        return False, "port error: %s" % exc


def wait_service(s, want, timeout=180):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = cmd(s, "AT+QNWINFO")
        if want in last:
            return last
        time.sleep(6)
    return last


def main():
    print("target:", TARGET)
    s = wait_port()
    if not s:
        print("modem not responsive")
        return
    cmd(s, "ATE0")
    cmd(s, "AT+CMEE=2")

    print()
    print("=== forcing GSM only, IMS off ===")
    print("   ims=2      : %s" % cmd(s, 'AT+QCFG="ims",2', 2))
    print("   nwscanmode : %s" % cmd(s, 'AT+QCFG="nwscanmode",1,1', 2))
    print("   waiting for a GSM attach...")
    nw = wait_service(s, "GSM")
    print("   network    : %s" % nw)
    if not alive(s):
        print("   modem dropped while switching; reopening")
        s = wait_port()
        if not s:
            print("   lost the modem")
            return
        cmd(s, "ATE0")
        nw = wait_service(s, "GSM")
        print("   network    : %s" % nw)

    for c in ["AT+CREG?", "AT+CSQ", "AT+COPS?", "AT+CGSMS?", "AT+CSCA?"]:
        print("   %-12s %s" % (c, cmd(s, c)))

    print()
    print("=== SMS on 2G ===")
    ok, info = send_sms(s, TARGET, "gsm-only sms %s" % time.strftime("%H%M%S"))
    print("   %s  %s" % ("SENT" if ok else "FAILED", info[:130]))
    sms_ok = ok
    print("   alive: %s" % alive(s))

    print()
    print("=== voice on 2G (no RAT transition needed) ===")
    voice_ok = False
    print("   dial: %s" % cmd(s, "ATD%s;" % TARGET, 3))
    for i in range(5):
        time.sleep(3)
        print("   t+%-2ds CPAS %-16s NW %s" % (
            (i + 1) * 3, cmd(s, "AT+CPAS", 0.8), cmd(s, "AT+QNWINFO", 0.8)))
    print("   hangup: %s" % cmd(s, "AT+CHUP", 2))
    voice_ok = alive(s)
    print("   alive after call: %s" % voice_ok)

    print()
    print("=== verdict ===")
    print("   SMS on GSM  : %s" % ("OK" if sms_ok else "FAILED"))
    print("   voice on GSM: %s" % ("modem survived" if voice_ok else "modem dropped"))

    if not KEEP:
        print()
        print("=== restoring automatic scan mode ===")
        s2 = s if alive(s) else wait_port()
        if s2:
            cmd(s2, "ATE0")
            print("   %s" % cmd(s2, 'AT+QCFG="nwscanmode",0,1', 2))
            print("   network: %s" % wait_service(s2, "LTE"))
    print()
    print("DONE")


if __name__ == "__main__":
    main()
