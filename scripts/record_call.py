#!/usr/bin/env python3
"""Place a call and record its audio to a WAV file.

The board has no speakers, so call audio is captured instead of played. The
modem is exposed as a USB sound card by enabling UAC in AT+QCFG="usbcfg" and
routing voice to it with AT+QPCMV=1,2; the capture side is S16_LE mono 8 kHz.

Usage: record_call.py [number] [--seconds N] [--answer-wait N] [--no-dial]

  --no-dial   record only, without placing a call (use to capture an *incoming*
              call: start this, then answer on the modem side)
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

CARD = os.environ.get("PHONE_AUDIO_DEV", "hw:2,0")
OUTDIR = os.path.expanduser("~/phone/recordings")


def arg(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return default


def find_capture_device():
    """Locate the modem's USB audio card, whose number can change."""
    try:
        with open("/proc/asound/cards") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0].isdigit() and "USB-Audio" in line:
                    return "hw:%s,0" % parts[0]
    except OSError:
        pass
    return CARD


def main():
    number = next((a for a in sys.argv[1:] if not a.startswith("--")), "0771324635")
    seconds = arg("--seconds", 20)
    answer_wait = arg("--answer-wait", 30)
    do_dial = "--no-dial" not in sys.argv

    os.makedirs(OUTDIR, exist_ok=True)
    dev = find_capture_device()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUTDIR, "call-%s.wav" % stamp)

    m = modem.Modem()
    m.open()
    m.send_raw("ATE0")
    m.send_raw("AT+CMEE=2")

    # Voice must be over VoLTE; without IMS a call CSFBs and resets the modem.
    ready, state = m.voice_ready()
    print("voice ready: %s  (%s)" % (ready, state))
    if not ready and do_dial:
        print("NOT voice-ready — switching to voice mode first")
        print(m.set_mode(modem.MODE_VOICE))
        ready, state = m.voice_ready()
        if not ready:
            print("still not voice-ready, aborting:", state)
            return 1

    print("routing audio to USB: %s" % m.send_raw("AT+QPCMV=1,2", timeout=10)[1])
    print("capture device: %s" % dev)
    print("output file   : %s" % path)

    rec = subprocess.Popen(
        ["arecord", "-D", dev, "-f", "S16_LE", "-c", "1", "-r", "8000", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(1)
    if rec.poll() is not None:
        print("arecord failed to start:", rec.stderr.read().decode()[:200])
        return 1

    connected_at = None
    try:
        if do_dial:
            num = modem.normalise_number(number)
            print()
            print(">>> DIALLING %s — ANSWER THE CALL AND SAY SOMETHING <<<" % num)
            m.dial(num)
        else:
            print()
            print(">>> RECORDING ONLY (no dial) <<<")

        deadline = time.time() + answer_wait
        while time.time() < deadline:
            calls = m.call_status()
            act = m.phone_activity() or {}
            states = [c.get("state") for c in calls]
            print("   %5.1fs  CPAS=%-18s CLCC=%s"
                  % (time.time() - (deadline - answer_wait),
                     act.get("text"), states or "-"))
            if "ACTIVE" in states:
                connected_at = time.time()
                print("   >>> ANSWERED — recording for %ds <<<" % seconds)
                break
            if do_dial and not calls and act.get("code") == 0:
                print("   call ended before being answered")
                break
            time.sleep(3)

        if connected_at:
            while time.time() - connected_at < seconds:
                time.sleep(2)
        elif do_dial:
            print("   not answered within %ds" % answer_wait)
    finally:
        try:
            dur = m.hangup()
            print("   hung up (%ss)" % dur)
        except Exception as exc:
            print("   hangup: %s" % exc)
        time.sleep(1)
        rec.terminate()
        try:
            rec.wait(timeout=10)
        except Exception:
            rec.kill()
        m.close()

    print()
    subprocess.run(["python3", os.path.expanduser("~/phone/scripts/wavstats.py"),
                    path])
    print()
    print("saved: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
