#!/usr/bin/env python3
"""Place a call, inject an audio file into it, and record the call at the same time.

The modem's USB Audio Class interface is full duplex: the capture side carries
what the far end says, the playback side is injected as our microphone. So a WAV
written to the playback device is heard by the person on the other end.

The card only accepts S16_LE mono 8 kHz, so anything else is converted with sox
first.

Usage: play_into_call.py [number] [--file PATH] [--loop] [--seconds N]
       play_into_call.py --no-dial --file PATH     # inject into an existing call
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, "/home/shoim2/phone")
import modem  # noqa: E402

AUDIO_DIR = os.path.expanduser("~/phone/audio")
REC_DIR = os.path.expanduser("~/phone/recordings")
DEFAULT_FILE = os.path.join(AUDIO_DIR, "playback-test.wav")


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def find_card():
    try:
        with open("/proc/asound/cards") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0].isdigit() and "USB-Audio" in line:
                    return "hw:%s,0" % parts[0]
    except OSError:
        pass
    return None


def prepare(path):
    """Ensure the clip is S16_LE mono 8 kHz; convert via sox if not."""
    import wave
    try:
        with wave.open(path, "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2):
                return path
    except Exception:
        pass                     # not a plain WAV — let sox deal with it
    out = "/tmp/inject-8k.wav"
    subprocess.run(["sox", path, "-r", "8000", "-c", "1", "-b", "16",
                    "-e", "signed-integer", out], check=True)
    print("   converted to 8 kHz mono: %s" % out)
    return out


def main():
    number = next((a for a in sys.argv[1:]
                   if not a.startswith("--")
                   and not a.endswith(".wav")), "0771324635")
    path = opt("--file", DEFAULT_FILE)
    loop = "--loop" in sys.argv
    max_seconds = int(opt("--seconds", "40"))
    do_dial = "--no-dial" not in sys.argv

    if not os.path.exists(path):
        print("no such file:", path)
        return 1

    card = find_card()
    if not card:
        print("no USB audio device — is UAC enabled in AT+QCFG=\"usbcfg\"?")
        return 1
    clip = prepare(path)

    m = modem.Modem()
    m.open()
    m.send_raw("ATE0")
    m.send_raw("AT+CMEE=2")

    ready, state = m.voice_ready()
    if not ready and do_dial:
        print("switching to voice mode first...")
        m.set_mode(modem.MODE_VOICE)
        ready, state = m.voice_ready()
        if not ready:
            print("not voice-ready:", state)
            return 1
    print("voice ready:", state)
    print("audio routing:", m.enable_usb_audio())
    print("card:", card, "| clip:", clip)

    os.makedirs(REC_DIR, exist_ok=True)
    rec_path = os.path.join(REC_DIR, "duplex-%s.wav" % time.strftime("%Y%m%d-%H%M%S"))
    rec = player = None
    try:
        if do_dial:
            num = modem.normalise_number(number)
            print()
            print(">>> DIALLING %s — ANSWER AND LISTEN <<<" % num)
            m.dial(num)

        # Wait for the far end to answer before injecting, otherwise the clip
        # plays into a call that is still ringing and is never heard.
        answered = False
        deadline = time.time() + 35
        while time.time() < deadline:
            states = [c.get("state") for c in m.call_status()]
            act = m.phone_activity() or {}
            print("   CPAS=%-18s CLCC=%s" % (act.get("text"), states or "-"))
            if "ACTIVE" in states:
                answered = True
                break
            if do_dial and not states and act.get("code") == 0:
                print("   call ended before answer")
                break
            time.sleep(3)

        if not answered and do_dial:
            print("   not answered — nothing to inject")
            return 1

        print()
        print(">>> ANSWERED — injecting audio and recording simultaneously <<<")
        rec = subprocess.Popen(
            ["arecord", "-D", card, "-f", "S16_LE", "-c", "1", "-r", "8000",
             rec_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = ["aplay", "-D", card, clip]
        started = time.time()
        while True:
            player = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE)
            _, err = player.communicate()
            if player.returncode != 0:
                print("   aplay error:", err.decode(errors="replace")[:200])
                break
            print("   clip finished (%.1fs elapsed)" % (time.time() - started))
            if not loop or time.time() - started > max_seconds:
                break
        # Let a little trailing audio be captured.
        time.sleep(2)
    finally:
        for p in (player, rec):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except Exception:
                    p.kill()
        try:
            print("   hung up (%ss)" % m.hangup())
        except Exception as exc:
            print("   hangup:", exc)
        m.close()

    print()
    if os.path.exists(rec_path):
        subprocess.run(["python3", os.path.expanduser("~/phone/scripts/wavstats.py"),
                        rec_path])
        print("   (this capture is the far end's side while we injected ours)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
