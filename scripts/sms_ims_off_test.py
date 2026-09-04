#!/usr/bin/env python3
"""Test SMS with IMS disabled, so submission goes over CS/SGs instead of IMS.

Everything else is now ruled out: the SIM sends fine in an LTE router, the SMSC
+40770000050 is confirmed correct, and text mode, PDU mode, both line endings and
an explicit in-PDU SMSC all fail identically.

What remains is that this modem has IMS forced on (AT+QCFG="ims",1, set by the
previous deployment) with AT+CGSMS=0, so it submits SMS over IMS. If DIGI does
not offer SMS-over-IP to this subscriber, that fails no matter how the message is
encoded. A router with IMS off submits over CS/SGs — and the CS domain is
attached here (AT+CREG: 0,1).

For EC25 firmware, AT+QCFG="ims" takes 0 = follow the MBN profile, 1 = force
enable, 2 = force disable.
"""
import sys
import time

import serial

PORT = "/dev/ttyUSB2"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "+40771324635"
if not TARGET.startswith("+"):
    TARGET = "+40" + TARGET[1:] if TARGET.startswith("0") else TARGET


class Raw:
    def __init__(self, port=PORT):
        self.open(port)

    def open(self, port=PORT, tries=40):
        for _ in range(tries):
            try:
                self.s = serial.Serial(port, 115200, timeout=0.3, write_timeout=5)
                self.s.reset_input_buffer()
                self.cmd("ATE0")
                self.cmd("AT+CMEE=2")
                return
            except Exception:
                time.sleep(5)
        raise SystemExit("AT port never came back")

    def cmd(self, text, wait=1.2):
        self.s.reset_input_buffer()
        self.s.write((text + "\r\n").encode())
        self.s.flush()
        time.sleep(wait)
        return self.s.read(self.s.in_waiting or 1).decode(errors="replace") \
            .strip().replace("\n", " | ")

    def send_sms(self, number, text):
        self.cmd("AT+CMGF=1")
        self.cmd('AT+CSCS="GSM"')
        self.s.reset_input_buffer()
        self.s.write(('AT+CMGS="%s"\r' % number).encode())
        self.s.flush()
        buf = ""
        deadline = time.time() + 8
        while time.time() < deadline:
            buf += self.s.read(self.s.in_waiting or 1).decode(errors="replace")
            if ">" in buf:
                break
            if "ERROR" in buf:
                return False, buf.strip().replace("\n", " | ")
        else:
            self.s.write(b"\x1b")
            return False, "no prompt"
        self.s.write(text.encode("ascii", "replace") + b"\x1a")
        self.s.flush()
        out = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            out += self.s.read(self.s.in_waiting or 1).decode(errors="replace")
            if "+CMGS:" in out:
                return True, out.strip().replace("\n", " | ")
            if "ERROR" in out:
                return False, out.strip().replace("\n", " | ")
        return False, "timeout: " + out.strip().replace("\n", " | ")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def radio_cycle(r, full=False):
    if full:
        r.cmd("AT+CFUN=1,1", wait=2)
        r.close()
        time.sleep(22)
        r.open()
    else:
        r.cmd("AT+CFUN=0", wait=3)
        time.sleep(4)
        r.cmd("AT+CFUN=1", wait=3)


def wait_attached(r, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        nw = r.cmd("AT+QNWINFO")
        if "LTE" in nw or "GSM" in nw or "WCDMA" in nw:
            return nw
        time.sleep(5)
    return None


def state(r):
    for c in ['AT+QCFG="ims"', "AT+CGSMS?", "AT+CREG?", "AT+CEREG?",
              "AT+QNWINFO", "AT+CSCA?"]:
        print("     %-16s %s" % (c, r.cmd(c)))


def trial(r, ims_value, cgsms, label, full_reset=True):
    print()
    print("=" * 66)
    print("%s  (ims=%s, CGSMS=%s)" % (label, ims_value, cgsms))
    print("=" * 66)
    print("   set ims  : %s" % r.cmd('AT+QCFG="ims",%d' % ims_value, wait=2))
    radio_cycle(r, full=full_reset)
    nw = wait_attached(r)
    print("   attached : %s" % nw)
    print("   set cgsms: %s" % r.cmd("AT+CGSMS=%d" % cgsms, wait=1.5))
    r.cmd('AT+CSCA="+40770000050",145')
    r.cmd("AT+CSMP=17,167,0,0")
    r.cmd('AT+CPMS="ME","ME","ME"')
    print("   state:")
    state(r)
    time.sleep(4)
    ok, info = r.send_sms(TARGET, "ims%s cgsms%s %s"
                          % (ims_value, cgsms, time.strftime("%H%M%S")))
    print("   >>> %s  %s" % ("SENT" if ok else "FAILED", info[:150]))
    if not ok:
        print("       CEER: %s" % r.cmd("AT+CEER"))
    return ok


def main():
    print("target:", TARGET)
    r = Raw()
    print("baseline state:")
    state(r)

    # IMS disabled -> SMS must use the CS/SGs path.
    if trial(r, 2, 1, "IMS DISABLED, SMS over CS"):
        print("\n*** WORKS WITH IMS DISABLED + CGSMS=1 ***")
        r.close()
        return
    if trial(r, 2, 0, "IMS DISABLED, SMS over PS", full_reset=False):
        print("\n*** WORKS WITH IMS DISABLED + CGSMS=0 ***")
        r.close()
        return
    # Let the MBN profile decide instead of forcing.
    if trial(r, 0, 1, "IMS per MBN profile, SMS over CS"):
        print("\n*** WORKS WITH IMS=MBN + CGSMS=1 ***")
        r.close()
        return

    print()
    print("=== restoring IMS enabled (VoLTE for voice) ===")
    r.cmd('AT+QCFG="ims",1', wait=2)
    radio_cycle(r, full=True)
    print("   attached: %s" % wait_attached(r))
    state(r)
    r.close()
    print()
    print("DONE")


if __name__ == "__main__":
    main()
