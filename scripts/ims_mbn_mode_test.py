#!/usr/bin/env python3
"""Test AT+QCFG="ims",0 — let the MBN profile decide IMS behaviour.

Known so far:
  ims=1 (force enable)  calls work over VoLTE, SMS refused (submitted over IMS)
  ims=2 (force disable) SMS works over LTE NAS/SGs, calls trigger CSFB and the
                        firmware resets itself

Value 0 means "follow the MBN profile" (ROW_Generic_3GPP here) rather than
forcing either way. The WG1608 work used AT+QCFG="ims",0,0 for its "LTE NAS SMS"
fix. A carrier profile can enable IMS for voice while leaving SMS on NAS, which
would give both features under one configuration — this checks that.

Tests SMS first, then voice, verifying the modem is alive between each.
"""
import sys
import time

import serial

TARGET = sys.argv[1] if len(sys.argv) > 1 else "+40771324635"
IMS_ARG = sys.argv[2] if len(sys.argv) > 2 else "0"


def wait_port(tries=48):
    for _ in range(tries):
        for p in ("/dev/ec25-at", "/dev/ttyUSB2", "/dev/ttyUSB3", "/dev/ttyUSB4"):
            try:
                s = serial.Serial(p, 115200, timeout=0.5, write_timeout=5)
                s.write(b"AT\r\n")
                s.flush()
                time.sleep(0.6)
                if "OK" in s.read(s.in_waiting or 1).decode(errors="replace"):
                    return s, p
                s.close()
            except Exception:
                pass
        time.sleep(5)
    return None, None


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


def wait_lte(s, timeout=150):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        last = cmd(s, "AT+QNWINFO")
        csq = cmd(s, "AT+CSQ", 0.8)
        if "LTE" in last and "+CSQ: 99" not in csq:
            return last
        time.sleep(5)
    return last


def sms_params(s):
    for c in ["AT+CMGF=1", 'AT+CSCS="GSM"', 'AT+CSCA="+40770000050",145',
              "AT+CSMP=17,167,0,0", 'AT+CPMS="ME","ME","ME"', "AT+CGSMS=1"]:
        cmd(s, c, 0.8)


def send_sms(s, number, text):
    sms_params(s)
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


def main():
    print("target:", TARGET, "| ims value under test:", IMS_ARG)
    s, port = wait_port()
    if not s:
        print("modem not responsive")
        return
    print("port:", port)
    cmd(s, "ATE0")
    cmd(s, "AT+CMEE=2")

    print()
    print("=== applying ims=%s and cycling the radio ===" % IMS_ARG)
    print("   set   : %s" % cmd(s, 'AT+QCFG="ims",%s' % IMS_ARG, 2))
    cmd(s, "AT+CFUN=0", 3)
    time.sleep(4)
    cmd(s, "AT+CFUN=1", 3)
    if not alive(s):
        s, port = wait_port()
        if not s:
            print("   modem lost during the radio cycle")
            return
        cmd(s, "ATE0")
    print("   network: %s" % wait_lte(s))
    for c in ['AT+QCFG="ims"', "AT+CGSMS?", "AT+CREG?", "AT+CEREG?",
              'AT+QMBNCFG="select"', "AT+CSQ"]:
        print("   %-22s %s" % (c, cmd(s, c)))

    print()
    print("=== TEST 1: SMS ===")
    ok, info = send_sms(s, TARGET, "ims%s sms %s" % (IMS_ARG, time.strftime("%H%M%S")))
    print("   %s  %s" % ("SENT" if ok else "FAILED", info[:130]))
    if not ok:
        print("   CEER: %s" % cmd(s, "AT+CEER"))
    sms_ok = ok
    healthy = alive(s)
    print("   modem alive: %s" % healthy)
    if not healthy:
        s, port = wait_port()
        if s:
            cmd(s, "ATE0")
            print("   recovered, network: %s" % wait_lte(s))

    print()
    print("=== TEST 2: voice ===")
    voice_ok = False
    if s:
        print("   dial: %s" % cmd(s, "ATD%s;" % TARGET, 3))
        for i in range(5):
            time.sleep(3)
            print("   t+%-2ds CPAS %-14s NW %s" % (
                (i + 1) * 3, cmd(s, "AT+CPAS", 0.8), cmd(s, "AT+QNWINFO", 0.8)))
        print("   hangup: %s" % cmd(s, "AT+CHUP", 2))
        voice_ok = alive(s)
        print("   modem alive after call: %s" % voice_ok)

    print()
    print("=== VERDICT for ims=%s ===" % IMS_ARG)
    print("   SMS   : %s" % ("OK" if sms_ok else "FAILED"))
    print("   voice : %s" % ("modem survived" if voice_ok else "modem reset"))
    if sms_ok and voice_ok:
        print()
        print("   *** BOTH WORK UNDER ims=%s — no mode switching needed ***" % IMS_ARG)
    print()
    print("DONE")


if __name__ == "__main__":
    main()
