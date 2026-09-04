#!/usr/bin/env python3
"""With IMS disabled (needed for SMS), does voice still work?

SMS now works with AT+QCFG="ims",2 + AT+CGSMS=1, because SMS over SGs is
signalled over LTE and needs no other radio access technology. A voice call is
different: with IMS off the modem must do a real CSFB to 2G/3G, and DIGI offers
neither here, so the fallback may fail or wedge the firmware.

Starts from a full reset so the measurement is not taken on an already-unhealthy
modem, waits for a genuine signal reading, then tries voice and SMS in turn.
"""
import sys
import time

import serial

TARGET = sys.argv[1] if len(sys.argv) > 1 else "+40771324635"


def opened(tries=60):
    for _ in range(tries):
        for p in ("/dev/ttyUSB2", "/dev/ttyUSB3"):
            try:
                s = serial.Serial(p, 115200, timeout=0.4, write_timeout=5)
                s.write(b"AT\r\n")
                s.flush()
                time.sleep(0.6)
                if "OK" in s.read(s.in_waiting or 1).decode(errors="replace"):
                    return s
                s.close()
            except Exception:
                pass
        time.sleep(5)
    raise SystemExit("modem never became responsive")


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


def healthy(s, timeout=210):
    """Wait for an LTE attach with a real (non-99) signal reading."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = cmd(s, "AT+QNWINFO")
        csq = cmd(s, "AT+CSQ")
        if "LTE" in nw and "+CSQ: 99" not in csq:
            return nw, csq
        time.sleep(6)
    return None, None


def main():
    print("target:", TARGET)
    s = opened()
    cmd(s, "ATE0")
    cmd(s, "AT+CMEE=2")

    print()
    print("=== full reset for a clean baseline ===")
    cmd(s, "AT+CFUN=1,1", wait=2)
    try:
        s.close()
    except Exception:
        pass
    time.sleep(25)
    s = opened()
    cmd(s, "ATE0")
    cmd(s, "AT+CMEE=2")
    nw, csq = healthy(s)
    print("   network : %s" % nw)
    print("   signal  : %s" % csq)
    print("   ims     : %s" % cmd(s, 'AT+QCFG="ims"'))
    print("   cgsms   : %s" % cmd(s, "AT+CGSMS?"))
    print("   creg    : %s" % cmd(s, "AT+CREG?"))
    if not nw:
        print("   modem never reached a healthy attach — aborting")
        return

    print()
    print("=== voice call attempt (IMS off => CSFB required) ===")
    print("   dial: %s" % cmd(s, "ATD%s;" % TARGET, wait=3))
    for i in range(5):
        time.sleep(3)
        print("   t+%-2ds CPAS %-14s CLCC %-24s NW %s" % (
            (i + 1) * 3,
            cmd(s, "AT+CPAS", 0.8), cmd(s, "AT+CLCC", 0.8)[:24],
            cmd(s, "AT+QNWINFO", 0.8)))
    print("   hangup: %s" % cmd(s, "AT+CHUP", wait=2))
    print("   CEER  : %s" % cmd(s, "AT+CEER"))

    print()
    print("=== is the modem still alive after the call attempt? ===")
    print("   AT      : %s" % cmd(s, "AT"))
    print("   network : %s" % cmd(s, "AT+QNWINFO"))
    print("   signal  : %s" % cmd(s, "AT+CSQ"))

    print()
    print("=== SMS still working? ===")
    cmd(s, "AT+CMGF=1")
    cmd(s, 'AT+CSCS="GSM"')
    try:
        s.reset_input_buffer()
        s.write(('AT+CMGS="%s"\r' % TARGET).encode())
        s.flush()
        buf = ""
        deadline = time.time() + 8
        got = False
        while time.time() < deadline:
            buf += s.read(s.in_waiting or 1).decode(errors="replace")
            if ">" in buf:
                got = True
                break
        if not got:
            s.write(b"\x1b")
            print("   no prompt: %s" % buf.strip().replace("\n", " | "))
        else:
            s.write(("voice+sms check %s" % time.strftime("%H%M%S"))
                    .encode() + b"\x1a")
            s.flush()
            out = ""
            deadline = time.time() + 60
            while time.time() < deadline:
                out += s.read(s.in_waiting or 1).decode(errors="replace")
                if "+CMGS:" in out or "ERROR" in out:
                    break
            print("   result: %s" % out.strip().replace("\n", " | ")[:140])
    except Exception as exc:
        print("   SMS attempt errored: %s" % exc)

    try:
        s.close()
    except Exception:
        pass
    print()
    print("DONE")


if __name__ == "__main__":
    main()
