#!/usr/bin/env python3
"""Probe a Fibocom FG621 to decide whether it can replace the Quectel EC25.

Three questions decide it:
  1. does it support voice at all (the EC25's only real advantage)
  2. can it do VoLTE and SMS at the same time — the EC25 cannot, which is what
     forces the ~75 s mode switch
  3. can it expose USB audio, which is what our call recording and injection ride on

Its serial ports assert no carrier, so they must be opened non-blocking with
CLOCAL set — a plain pyserial open() hangs forever.
"""
import os
import select
import sys
import termios
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"


class AT:
    def __init__(self, path=PORT):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[4] = a[5] = termios.B115200
        a[2] = a[2] | termios.CLOCAL | termios.CREAD
        a[0] = a[1] = a[3] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, a)

    def cmd(self, text, wait=8.0):
        # Drain anything still arriving from the previous command, otherwise a
        # slow reply is read as the answer to this one.
        end = time.time() + 0.4
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if not r:
                break
            try:
                os.read(self.fd, 4096)
            except BlockingIOError:
                break
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        os.write(self.fd, (text + "\r").encode())
        out = b""
        end = time.time() + wait
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.25)
            if r:
                try:
                    out += os.read(self.fd, 4096)
                except BlockingIOError:
                    pass
            if b"OK" in out or b"ERROR" in out:
                break
        return out.decode(errors="replace").strip().replace("\n", " | ")

    def close(self):
        os.close(self.fd)


def section(at, title, commands):
    print()
    print("=== %s ===" % title)
    for c in commands:
        print("  %-28s %s" % (c, at.cmd(c)[:150]))


def main():
    at = AT()
    at.cmd("ATE0")
    at.cmd("AT+CMEE=2")

    section(at, "identity", ["ATI", "AT+CGMI", "AT+CGMM", "AT+CGMR", "AT+GCAP",
                             "AT+CNUM"])
    section(at, "SIM and registration", ["AT+CPIN?", "AT+CIMI", "AT+COPS?",
                                         "AT+CREG?", "AT+CGREG?", "AT+CEREG?",
                                         "AT+C5GREG?", "AT+CSQ"])
    # 1. Voice
    section(at, "VOICE support", ["AT+CVHU?", "AT+CLCC", "AT+CPAS", "AT+CLIP?",
                                  "AT+CSSN?", "AT+VTS=?", "AT+CHUP"])
    # 2. SMS and IMS
    section(at, "SMS and IMS", ["AT+CMGF=?", "AT+CSCA?", "AT+CGSMS?",
                                "AT+CGSMS=?", "AT+CPMS?", "AT+CSMS?",
                                "AT+CIREG?", "AT+CIREP?"])
    # 3. Audio, and the vendor command set
    section(at, "audio and vendor commands", [
        "AT+XACT?", "AT+XCFG?", 'AT+GTUSBMODE?', "AT+GTAUDIO?",
        "AT+GTPCMV?", "AT+CLVL?", "AT+GTCSFB?", "AT+GTIMSCFG?"])
    print()
    print("DONE — port %s" % PORT)
    at.close()


if __name__ == "__main__":
    main()
