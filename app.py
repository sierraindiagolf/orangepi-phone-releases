#!/usr/bin/env python3
"""Orange Pi 3 LTS phone API and web UI, driving a Quectel EC25-E.

Features: dialer with call register, SMS conversations with sending merged in,
USSD, phonebook, raw AT console, and a live API explorer.

Threading note: URC callbacks fire on the modem's reader thread. That thread is
the only consumer of the serial port, so it must never issue an AT command —
doing so would block waiting for a response it is itself responsible for
reading. Anything needing the modem is therefore pushed onto a work queue and
executed by a separate worker thread.
"""

import array
import datetime
import os
import queue
import re
import subprocess
import threading
import time
import wave
from collections import deque

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import balance
import db
from firmware import FirmwareManager
import modem as modem_mod
import mqtt_bridge

VERSION = "1.0.1"
FIRMWARE = FirmwareManager(current_version=VERSION)

PORT = int(os.environ.get("PHONE_PORT", "8080"))
BIND = os.environ.get("PHONE_BIND", "0.0.0.0")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=None)

# nginx terminates TLS and proxies to 127.0.0.1. Without this the app believes
# every request arrived over plain HTTP on localhost, so the Secure session cookie
# is never accepted by the browser and every login silently fails to stick.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Recent modem events, surfaced to the UI so it can react to calls and SMS.
EVENTS = deque(maxlen=200)
_event_seq = {"n": 0}
_event_lock = threading.Lock()

_work = queue.Queue()
_active_call_row = {"id": None}

# Call audio: the board has no speakers, so audio is recorded to a file instead
# of being played. The modem appears as a USB sound card (UAC enabled in
# usbcfg); its ALSA card number can change across re-enumeration, so it is
# looked up rather than hardcoded.
RECORDINGS_DIR = os.environ.get(
    "PHONE_RECORDINGS", os.path.expanduser("~/phone/recordings"))
_recorder = {"proc": None, "path": None}
_rec_lock = threading.Lock()


def usb_health():
    """Count USB disconnects since boot — the cheapest power-problem detector.

    An undersized or shared supply shows up as the modem (or the hub carrying it)
    re-enumerating: a transmit burst browns it out. On a shared hub this board
    logged 61 disconnects in one boot; on a dedicated supply, zero during normal
    operation. Every "firmware reset" originally blamed on the EC25 turned out to
    be this, so it is worth watching in deployment.
    """
    try:
        with open("/var/log/kern.log", "rb") as fh:      # may not exist
            pass
    except OSError:
        pass
    try:
        out = subprocess.run(["dmesg"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return None
    disconnects = out.count("USB disconnect")
    modem_re = out.count("GSM modem (1-port) converter detected")
    overcurrent = out.count("over-current")
    try:
        with open("/proc/uptime") as fh:
            uptime_s = float(fh.read().split()[0])
    except OSError:
        uptime_s = 0.0
    uptime_h = uptime_s / 3600.0

    # Counted over a recent window, not since boot.
    #
    # Dividing since-boot totals by total uptime is the obvious metric and it is
    # wrong: the stack switches the modem into its resting mode at startup, and
    # every deliberate CFUN=1,1 re-enumerates it. That startup burst poisons the
    # average for the life of the boot — this board sat at 6.78/h and "unhealthy"
    # for hours after 33 disconnects that all happened in the first 11 minutes,
    # with nothing since. A health signal has to answer "is it happening now".
    recent = 0
    for line in out.splitlines():
        if "USB disconnect" not in line:
            continue
        m = re.match(r"\[\s*(\d+\.\d+)\]", line)   # kernel seconds since boot
        if m and uptime_s - float(m.group(1)) <= 3600:
            recent += 1

    return {
        "usb_disconnects_since_boot": disconnects,
        "usb_disconnects_last_hour": recent,
        "modem_enumerations_since_boot": modem_re,
        "overcurrent_events": overcurrent,
        "uptime_hours": round(uptime_h, 2),
        "disconnects_per_hour": round(disconnects / uptime_h, 2) if uptime_h > 0.05 else None,
        # A mode switch or a firmware reset legitimately costs one or two
        # disconnects, so the threshold sits above that. The failure this exists
        # to catch — the supply not carrying transmit peaks — produces dozens per
        # minute, so it will trip this immediately and nothing else will.
        # There is no current sensor on this hardware (no power_supply, no hwmon
        # rails, no ADC), so this is the available proxy; use an inline USB power
        # meter to measure draw directly.
        "healthy": recent < 3 and overcurrent == 0,
    }


def _storage(disk):
    """Filesystem figures. Raw bytes stay in the API; display units are a
    presentation concern and are applied at the edge (UI, MQTT discovery)."""
    free = disk.f_bavail * disk.f_frsize
    total = disk.f_blocks * disk.f_frsize
    # f_bavail excludes the root reserve, so free + used will not equal total.
    # Percentage is computed against what a normal user can actually claim,
    # which is the number that matters when the database stops being writable.
    usable = disk.f_bavail * disk.f_frsize + (disk.f_blocks - disk.f_bfree) * disk.f_frsize
    return {
        "path": os.path.dirname(db.DB_PATH),
        "free_bytes": free,
        "total_bytes": total,
        "used_bytes": (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
        "used_percent": round(100.0 * (1 - free / usable), 1) if usable else None,
    }


def host_metrics():
    """Board health: temperatures, load, memory, uptime.

    All read straight from sysfs and procfs — no shelling out, because this runs
    on every status poll and on every MQTT publish.
    """
    out = {}
    try:
        for zone in sorted(os.listdir("/sys/class/thermal")):
            if not zone.startswith("thermal_zone"):
                continue
            base = os.path.join("/sys/class/thermal", zone)
            try:
                with open(os.path.join(base, "type")) as fh:
                    kind = fh.read().strip()
                with open(os.path.join(base, "temp")) as fh:
                    # millidegrees on this SoC
                    out[kind.replace("-", "_") + "_c"] = round(int(fh.read()) / 1000.0, 1)
            except OSError:
                continue
    except OSError:
        pass

    try:
        with open("/proc/loadavg") as fh:
            parts = fh.read().split()
        out["load_1m"], out["load_5m"], out["load_15m"] = (
            float(parts[0]), float(parts[1]), float(parts[2]))
    except (OSError, ValueError, IndexError):
        pass

    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    mem[k] = int(v.split()[0]) * 1024
        if mem.get("MemTotal"):
            out["memory_total_bytes"] = mem["MemTotal"]
            out["memory_available_bytes"] = mem.get("MemAvailable")
            out["memory_used_percent"] = round(
                100.0 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]), 1)
    except (OSError, ValueError):
        pass

    try:
        with open("/proc/uptime") as fh:
            out["uptime_seconds"] = int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        pass

    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as fh:
            out["cpu_mhz"] = round(int(fh.read()) / 1000)
    except (OSError, ValueError):
        pass

    return out


# --- storage and system health ---------------------------------------------
#
# Added after a failure that none of the existing telemetry could see. The root
# filesystem stopped serving I/O while every process stayed up: the API returned
# 500 on anything touching the database, static files came back 404 because
# os.path.isfile() swallows EIO and reports the file missing, and sshd closed new
# connections before authentication because it could not re-exec itself. Signal,
# load, memory and temperature all looked perfectly normal throughout, and the
# board kept publishing them. What follows watches the things that were actually
# broken, so the next occurrence is visible instead of being inferred from the
# shape of the HTTP errors.

# The root SSD is NVMe behind a JMicron JMS583 bridge (152d:a583), which does not
# pass SAT through — `-d sat` fails with "unsupported scsi opcode". The NVMe
# tunnel has to be named explicitly.
SMART_DEV = os.environ.get("PHONE_SMART_DEV", "/dev/sda")
SMART_TYPE = os.environ.get("PHONE_SMART_TYPE", "sntjmicron")
SMART_TTL = 900.0
_smart_cache = {"at": 0.0, "value": None}

MODEM_USB_ID = "2c7c:0125"

# unit -> the states that count as healthy for it.
WATCHED_UNITS = {
    "phone-api": ("active",),
    "ec25-lte": ("active",),
    "nginx": ("active",),
    # A oneshot without RemainAfterExit: it binds the drivers and exits, so
    # "inactive" is success and only "failed" is a problem. This unit sat failed
    # for over an hour — no modem on the USB bus at boot — while the UI answered
    # normally and nothing anywhere reported it.
    "ec25-bind": ("active", "inactive"),
}
UNITS_TTL = 20.0
_units_cache = {"at": 0.0, "value": None}


def _mount_info(target="/"):
    """Source device and mount options for a mountpoint, from /proc/mounts.

    The root filesystem is mounted `errors=remount-ro`, so ext4 flips it
    read-only the instant it hits an I/O error. That makes `ro` appearing here
    the earliest and cheapest storage alarm available — it is set by the kernel
    before any application notices its writes failing.
    """
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == target:
                    return {"source": parts[0], "options": parts[3].split(",")}
    except OSError:
        pass
    return {"source": None, "options": []}


def _ext4_error_count(source):
    """`s_error_count` from the superblock, exposed by sysfs and readable by any
    user — no root, no `dumpe2fs`.

    This survives reboots, which is the point: it separates "this filesystem has
    hit errors at some point" from "it has always been clean". One blind spot is
    worth stating, because it applies directly to the failure this was written
    for: if the device disappears from the bus entirely, ext4 cannot write the
    counter, so a zero here does not prove storage never failed. It only proves
    it never failed while still writable.
    """
    if not source or not source.startswith("/dev/"):
        return None
    try:
        with open(f"/sys/fs/ext4/{source[5:]}/errors_count") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


class _WriteProbe(threading.Thread):
    """Prove periodically that the data directory still accepts writes.

    Deliberately a thread rather than a check on the status call. When a
    USB-attached disk drops, filesystem syscalls do not fail fast — they block in
    uninterruptible sleep — so a write test on the request path would hang
    `/api/status`, and with it the MQTT publish loop, turning a detectable fault
    into a silent one. Out here the worst case is that this thread wedges and
    `checked_age_seconds` grows without bound, which is itself the alarm, and a
    more specific one than silence.

    The probe fsyncs. Without it a write lands in the page cache and returns
    success on a filesystem that can no longer reach its device.
    """

    INTERVAL = 30.0

    def __init__(self, directory):
        super().__init__(daemon=True, name="write-probe")
        self.directory = directory
        self.ok = None
        self.error = None
        self.at = None          # monotonic; None until the first attempt lands

    def _probe_once(self):
        path = os.path.join(self.directory, ".write-probe")
        fd = None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, b"iris\n")
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.unlink(path)
            return True, None
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False, str(exc)

    def run(self):
        while True:
            ok, error = self._probe_once()
            self.ok, self.error, self.at = ok, error, time.monotonic()
            time.sleep(self.INTERVAL)

    def snapshot(self):
        age = None if self.at is None else round(time.monotonic() - self.at, 1)
        return {
            "write_ok": self.ok,
            "write_error": self.error,
            "write_checked_age_seconds": age,
            # A probe that stopped reporting is as bad as one that failed, and it
            # is what a blocked-on-dead-disk board looks like from here.
            "write_probe_stale": age is None or age > 4 * self.INTERVAL,
        }


WRITE_PROBE = _WriteProbe(os.path.dirname(db.DB_PATH))


def disk_smart():
    """SMART health for the root SSD, cached — it costs a subprocess and the
    numbers move over weeks, not seconds.

    Requires a sudoers rule for smartctl; without one this reports unavailable
    rather than failing, so the rest of the status payload is unaffected.
    """
    now = time.monotonic()
    if _smart_cache["value"] is not None and now - _smart_cache["at"] < SMART_TTL:
        return _smart_cache["value"]

    try:
        out = subprocess.run(
            ["sudo", "-n", "smartctl", "-d", SMART_TYPE, "-H", "-A", SMART_DEV],
            capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        result = {"available": False, "error": str(exc)}
        _smart_cache.update(at=now, value=result)
        return result

    # smartctl's exit status is a bitfield where several bits are informational,
    # so the output is parsed instead of the return code.
    fields = {
        "Temperature": ("temperature_c", int),
        "Available Spare": ("available_spare_percent", int),
        "Percentage Used": ("life_used_percent", int),
        "Media and Data Integrity Errors": ("media_errors", int),
        "Error Information Log Entries": ("error_log_entries", int),
        "Unsafe Shutdowns": ("unsafe_shutdowns", int),
    }
    result = {"available": False, "passed": None}
    for line in out.splitlines():
        if "overall-health" in line:
            result["available"] = True
            result["passed"] = line.strip().endswith("PASSED")
            continue
        key, sep, value = line.partition(":")
        spec = fields.get(key.strip())
        if not sep or not spec:
            continue
        name, cast = spec
        # Values arrive as "47 Celsius", "100%", "265,663" — take the leading
        # number and drop the thousands separators.
        m = re.match(r"\s*([\d,]+)", value)
        if m:
            try:
                result[name] = cast(m.group(1).replace(",", ""))
            except ValueError:
                pass
    if not result["available"]:
        result["error"] = "smartctl produced no health line (sudoers rule missing?)"
    _smart_cache.update(at=now, value=result)
    return result


def storage_health(disk):
    """`_storage` plus the answers to 'can it still be written' and 'has it ever
    reported an error'."""
    out = _storage(disk)
    mount = _mount_info("/")
    out["device"] = mount["source"]
    out["read_only"] = "ro" in mount["options"]
    out["ext4_errors"] = _ext4_error_count(mount["source"])
    out.update(WRITE_PROBE.snapshot())
    out["smart"] = disk_smart()
    smart = out["smart"]
    out["healthy"] = bool(
        not out["read_only"]
        and out["write_ok"] is not False
        and not out["write_probe_stale"]
        and not out["ext4_errors"]
        # `passed is None` means SMART could not be read, which is not evidence
        # of a fault and must not raise a permanent alarm.
        and smart.get("passed") is not False
        and not smart.get("media_errors")
    )
    return out


def service_health():
    """State of the units the phone depends on, cached briefly.

    `systemctl is-active` takes every unit in one call and answers in order, so
    this is a single subprocess regardless of how many are watched.
    """
    now = time.monotonic()
    if _units_cache["value"] is not None and now - _units_cache["at"] < UNITS_TTL:
        return _units_cache["value"]

    names = list(WATCHED_UNITS)
    try:
        proc = subprocess.run(["systemctl", "is-active", *names],
                              capture_output=True, text=True, timeout=10)
        states = proc.stdout.split()
    except Exception:
        states = []

    units, healthy = {}, True
    for i, name in enumerate(names):
        state = states[i] if i < len(states) else "unknown"
        ok = state in WATCHED_UNITS[name]
        units[name.replace("-", "_")] = state
        healthy = healthy and ok
    result = {"units": units, "healthy": healthy,
              "failed": [n for i, n in enumerate(names)
                         if (states[i] if i < len(states) else "unknown")
                         not in WATCHED_UNITS[n]]}
    _units_cache.update(at=now, value=result)
    return result


def modem_on_bus():
    """Is the EC25 enumerated at all?

    Distinct from `modem.connected`, which asks whether the AT port answers, and
    the distinction is the whole point: after the last failure the hub was
    present and the modem simply was not on the bus, so there was no port to fail
    to answer. Reads sysfs directly — `lsusb` would be a subprocess on every
    poll, and this runs on the status path.
    """
    vendor, product = MODEM_USB_ID.split(":")
    try:
        for name in os.listdir("/sys/bus/usb/devices"):
            base = os.path.join("/sys/bus/usb/devices", name)
            try:
                with open(os.path.join(base, "idVendor")) as fh:
                    if fh.read().strip() != vendor:
                        continue
                with open(os.path.join(base, "idProduct")) as fh:
                    if fh.read().strip() == product:
                        return True
            except OSError:
                continue
    except OSError:
        return None
    return False


def system_health():
    """Board-level facts that are not radio, load or temperature."""
    return {
        "services": service_health(),
        # systemd-timesyncd drops this file once the clock is trustworthy. There
        # is no RTC battery, so after a power cut the board comes up with a stale
        # clock and jumps forward when NTP lands — which is why log timestamps
        # around a boot cannot be trusted, and why this is worth surfacing.
        "clock_synchronized": os.path.exists("/run/systemd/timesync/synchronized"),
    }


def find_audio_device():
    try:
        with open("/proc/asound/cards") as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0].isdigit() and "USB-Audio" in line:
                    return "hw:%s,0" % parts[0]
    except OSError:
        pass
    return None


def start_recording(tag="call"):
    with _rec_lock:
        if _recorder["proc"] and _recorder["proc"].poll() is None:
            return {"recording": True, "path": _recorder["path"],
                    "note": "already recording"}
        dev = find_audio_device()
        if not dev:
            return {"recording": False,
                    "error": "no USB audio device — is UAC enabled in usbcfg?"}
        MODEM.enable_usb_audio()
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        path = os.path.join(
            RECORDINGS_DIR,
            "%s-%s.wav" % (tag, time.strftime("%Y%m%d-%H%M%S")))
        proc = subprocess.Popen(
            ["arecord", "-D", dev, "-f", "S16_LE", "-c", "1", "-r", "8000",
             path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.6)
        if proc.poll() is not None:
            return {"recording": False, "error": "arecord exited immediately"}
        _recorder.update(proc=proc, path=path)
        push_event("recording_started", {"path": os.path.basename(path)})
        return {"recording": True, "path": path, "device": dev}


def stop_recording():
    with _rec_lock:
        proc, path = _recorder["proc"], _recorder["path"]
        _recorder.update(proc=None, path=None)
    if not proc:
        return None
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    info = {"path": path, "file": os.path.basename(path) if path else None}
    info.update(wav_stats(path) or {})
    push_event("recording_stopped", info)
    return info


AUDIO_DIR = os.environ.get("PHONE_AUDIO", os.path.expanduser("~/phone/audio"))
_player = {"proc": None, "file": None, "loop": False}
_play_lock = threading.Lock()


def prepare_playback(path):
    """The card only accepts S16_LE mono 8 kHz; convert anything else via sox."""
    try:
        with wave.open(path, "rb") as w:
            if (w.getframerate(), w.getnchannels(),
                    w.getsampwidth()) == (8000, 1, 2):
                return path
    except Exception:
        pass                      # not a plain WAV — hand it to sox
    out = "/tmp/inject-8k.wav"
    subprocess.run(["sox", path, "-r", "8000", "-c", "1", "-b", "16",
                    "-e", "signed-integer", out],
                   check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return out


def resolve_audio_file(name):
    """Look a playable file up by bare name in the audio and recordings dirs."""
    if os.path.basename(name) != name:
        return None
    for directory in (AUDIO_DIR, RECORDINGS_DIR):
        candidate = os.path.join(directory, name)
        if os.path.exists(candidate):
            return candidate
    return None


def start_playback(name, loop=False):
    """Inject an audio file into the call as the board's microphone."""
    path = resolve_audio_file(name)
    if not path:
        return {"playing": False, "error": "no such audio file: %s" % name}
    dev = find_audio_device()
    if not dev:
        return {"playing": False, "error": "no USB audio device"}
    try:
        clip = prepare_playback(path)
    except Exception as exc:
        return {"playing": False, "error": "conversion failed: %s" % exc}

    with _play_lock:
        if _player["proc"] and _player["proc"].poll() is None:
            return {"playing": True, "file": _player["file"],
                    "note": "already playing"}
        MODEM.enable_usb_audio()
        _player.update(file=os.path.basename(path), loop=loop)

        def run():
            while True:
                proc = subprocess.Popen(
                    ["aplay", "-D", dev, clip],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with _play_lock:
                    _player["proc"] = proc
                proc.wait()
                if proc.returncode != 0 or not _player.get("loop"):
                    break
                # Stop looping once the call is gone, otherwise this spins.
                if not MODEM.call_status():
                    break
            with _play_lock:
                _player.update(proc=None, file=None, loop=False)
            push_event("playback_finished", {"file": os.path.basename(path)})

        threading.Thread(target=run, daemon=True).start()
        push_event("playback_started",
                   {"file": os.path.basename(path), "loop": loop})
    time.sleep(0.4)
    return {"playing": True, "file": os.path.basename(path), "loop": loop,
            "device": dev, "converted": clip != path}


def stop_playback():
    with _play_lock:
        proc, name = _player["proc"], _player["file"]
        _player["loop"] = False
    if not proc or proc.poll() is not None:
        return None
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    push_event("playback_stopped", {"file": name})
    return {"stopped": name}


def play_when_answered(name, loop=False, wait=40):
    """Wait for the far end to answer, then inject. Runs on a worker thread.

    Injecting while the call is still ringing would play the clip into a call
    nobody is listening to, so the answer is waited for.
    """
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            if any(c.get("state") == "ACTIVE" for c in MODEM.call_status()):
                push_event("playback_on_answer", start_playback(name, loop))
                return
        except Exception:
            pass
        time.sleep(2)
    push_event("playback_on_answer",
               {"playing": False, "error": "call not answered within %ds" % wait})


def wav_stats(path):
    """Levels, so a caller can tell real audio from silence without listening."""
    try:
        with wave.open(path, "rb") as w:
            frames, rate, width = w.getnframes(), w.getframerate(), w.getsampwidth()
            raw = w.readframes(frames)
    except Exception:
        return None
    if width != 2 or not raw:
        return {"seconds": 0, "rms": 0, "peak": 0, "has_audio": False}
    samples = array.array("h")
    samples.frombytes(raw[:len(raw) // 2 * 2])
    if not samples:
        return {"seconds": 0, "rms": 0, "peak": 0, "has_audio": False}
    peak = max(max(samples), -min(samples))
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    return {
        "seconds": round(frames / rate, 2),
        "rms": round(rms),
        "peak": peak,
        "bytes": os.path.getsize(path),
        "has_audio": rms > 50,
    }


def push_event(kind, data):
    with _event_lock:
        _event_seq["n"] += 1
        EVENTS.append({
            "seq": _event_seq["n"],
            "ts": db.now_iso(),
            "kind": kind,
            "data": data,
        })


# --- modem event handling ---------------------------------------------------

def on_modem_event(kind, data):
    """Runs on the reader thread: record it, and defer any AT work."""
    push_event(kind, data)

    if kind == "incoming_call":
        number = data.get("number") or ""
        if number and _active_call_row["id"] is None:
            try:
                _active_call_row["id"] = db.add_call(
                    number, "INCOMING", "RINGING", 0, None
                )
            except Exception:
                pass
        if VOICEMAIL.get("enabled"):
            _work.put(("voicemail", {"number": number}))

    elif kind == "ring" and VOICEMAIL.get("enabled"):
        # RING arrives without caller ID; +CLIP follows. Only act if no +CLIP
        # turned up, so the number is used when it is available.
        _work.put(("voicemail_maybe", data))

    elif kind == "call_ended":
        _work.put(("call_ended", data))

    elif kind == "sms_stored":
        _work.put(("sync_sms", data))

    elif kind == "sms_direct":
        _work.put(("sync_sms", data))


# --- voicemail ---------------------------------------------------------------
#
# Everything this needs already exists: incoming calls surface as +CLIP/RING URCs,
# ATA answers, playback injects audio as the microphone, and recording captures
# the far end. This just sequences them.
#
# It runs on the worker thread, never on the reader thread — answering and
# recording both issue AT commands, and the reader thread is the only consumer of
# the port, so issuing a command there would deadlock.

VOICEMAIL_DEFAULTS = {
    "enabled": os.environ.get("PHONE_VOICEMAIL", "0") not in ("0", "", "false"),
    "ring_seconds": int(os.environ.get("PHONE_VM_RING_SECONDS", "5")),
    "greeting": os.environ.get("PHONE_VM_GREETING", "greeting.wav"),
    "max_seconds": int(os.environ.get("PHONE_VM_MAX_SECONDS", "30")),
}
# Held in memory for speed but persisted in the settings table: the service
# restarts on every deploy and on every modem reset, and an in-memory-only
# setting silently reverts. Voicemail was switched on and later found off again
# for exactly that reason.
VOICEMAIL = dict(VOICEMAIL_DEFAULTS)


def load_voicemail_config():
    stored = db.get_setting("voicemail")
    if isinstance(stored, dict):
        VOICEMAIL.update({k: v for k, v in stored.items()
                          if k in VOICEMAIL_DEFAULTS})
    return VOICEMAIL


def save_voicemail_config():
    db.set_setting("voicemail", dict(VOICEMAIL))
    return VOICEMAIL
_vm_busy = threading.Lock()


def take_voicemail(number):
    """Answer an incoming call, play the greeting, record the caller, hang up."""
    if not _vm_busy.acquire(blocking=False):
        return                                  # already handling one
    try:
        # RING and +CLIP each queue a job, and more arrive while ringing, so
        # check there is actually a call here before announcing anything.
        if not MODEM.call_status() and (MODEM.phone_activity() or {}).get("code") != 3:
            return
        tag = "voicemail-%s" % (number.lstrip("+") or "unknown")
        push_event("voicemail_started", {"number": number})

        # Let it ring a little first, so a caller who changes their mind can.
        time.sleep(max(0, VOICEMAIL["ring_seconds"]))
        # Accept either an incoming call in CLCC or CPAS reporting "ringing" —
        # on VoLTE the two do not always agree, and requiring both meant the
        # answer never fired.
        calls = MODEM.call_status()
        activity = MODEM.phone_activity() or {}
        ringing = (any(c.get("state") in ("INCOMING", "WAITING") for c in calls)
                   or activity.get("code") == 3)
        if not ringing:
            push_event("voicemail_aborted",
                       {"number": number, "reason": "no longer ringing",
                        "calls": calls, "activity": activity})
            return

        MODEM.answer()
        started = time.time()
        # Reuse the row the incoming-call handler already created rather than
        # logging the same call twice.
        row = _active_call_row["id"]
        if row is None:
            row = db.add_call(number or "unknown", "INCOMING", "VOICEMAIL", 0,
                              "voicemail")
            _active_call_row["id"] = row

        rec = start_recording(tag)
        if not rec.get("recording"):
            push_event("voicemail_error", rec)

        greeting = VOICEMAIL.get("greeting")
        if greeting and resolve_audio_file(greeting):
            start_playback(greeting)
            # Let the greeting finish before the caller starts talking.
            deadline = time.time() + 30
            while time.time() < deadline:
                with _play_lock:
                    playing = bool(_player["proc"]
                                   and _player["proc"].poll() is None)
                if not playing:
                    break
                time.sleep(0.5)

        # Record until the caller hangs up or the limit is reached.
        deadline = time.time() + VOICEMAIL["max_seconds"]
        while time.time() < deadline:
            if not MODEM.call_status():
                break
            time.sleep(1)

        info = stop_recording()
        try:
            MODEM.hangup()
        except Exception:
            pass
        # Measure locally: hangup() reports 0 when the caller hung up first,
        # because the call was already reconciled away by then.
        duration = int(time.time() - started)
        db.update_call(row, status="VOICEMAIL", duration=duration,
                       notes="voicemail: %s" % (info or {}).get("file", "no audio"))
        _active_call_row["id"] = None
        push_event("voicemail_saved",
                   {"number": number, "duration": duration, **(info or {})})
    except Exception as exc:
        push_event("voicemail_error", {"number": number, "error": str(exc)})
        try:
            stop_playback()
            stop_recording()
            MODEM.hangup()
        except Exception:
            pass
    finally:
        _vm_busy.release()


def worker():
    """Serialises follow-up modem work off the reader thread."""
    while True:
        try:
            job, data = _work.get()
        except Exception:
            continue
        try:
            if job == "sync_sms":
                sync_sms_from_modem()
            elif job == "call_ended":
                finish_call_log(data)
            elif job == "voicemail":
                take_voicemail(data.get("number") or "")
            elif job == "voicemail_maybe":
                time.sleep(2)          # give +CLIP a chance to arrive first
                if not _vm_busy.locked():
                    calls = MODEM.call_status()
                    num = next((c.get("number") for c in calls
                                if c.get("direction") == "INCOMING"), "")
                    if calls:
                        take_voicemail(num or "")
        except Exception as exc:
            push_event("worker_error", {"job": job, "error": str(exc)})


def finish_call_log(data):
    call = data.get("call") or {}
    duration = int(data.get("duration") or 0)
    reason = data.get("reason") or "HANGUP"
    row_id = _active_call_row["id"]
    if row_id:
        db.update_call(row_id, status=reason, duration=duration)
        _active_call_row["id"] = None
    elif call.get("number"):
        db.add_call(call["number"], call.get("direction", "INCOMING"),
                    reason, duration, None)


def sync_sms_from_modem():
    """Pull every stored message into the database, then clear the modem store.

    The modem store is small, so messages are removed once persisted. Duplicate
    inserts are prevented by a UNIQUE constraint, making this safe to re-run.
    """
    imported = 0
    for msg in MODEM.read_messages("ALL"):
        number = msg.get("number") or "unknown"
        body = msg.get("body") or ""
        if not body:
            continue
        row = db.add_sms(
            number,
            msg.get("direction", "IN"),
            body,
            status="RECEIVED" if msg.get("direction") == "IN" else "SENT",
            ts=msg.get("ts_iso"),
            sim_index=msg.get("index"),
            is_read=msg.get("is_read", 0),
        )
        if row:
            imported += 1
        idx = msg.get("index")
        if idx is not None:
            try:
                MODEM.delete_message(idx)
            except Exception:
                pass
    if imported:
        push_event("sms_imported", {"count": imported})
    return imported


MODEM = modem_mod.Modem(on_event=on_modem_event)


# --- helpers ---------------------------------------------------------------

def ok(**payload):
    out = {"success": True}
    out.update(payload)
    return jsonify(out)


def fail(message, status=400, **extra):
    out = {"success": False, "error": str(message)}
    out.update(extra)
    return jsonify(out), status


def body():
    return request.get_json(silent=True) or {}


def decorate(rows, key="number"):
    """Attach phonebook names to a list of rows."""
    names = db.contact_names()
    for row in rows:
        row["name"] = names.get(row.get(key))
    return rows


# --- static UI -------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


# --- authentication ---------------------------------------------------------
#
# The gate itself lives in auth.py and is installed as a before_request hook at
# the bottom of this file. These are the endpoints it deliberately lets through.

@app.route("/api/auth/state")
def api_auth_state():
    """What the login page needs to know before anyone has authenticated."""
    return ok(
        configured=auth.has_user(),
        authenticated=bool(session.get("user")),
        user=session.get("user"),
        recovery_configured=bool(auth.recovery_number()),
        locked=auth.login_locked(),
    )


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or auth.DEFAULT_USER).strip()
    password = data.get("password") or ""

    if not auth.has_user():
        # First run: whoever reaches the box first sets the password. There is no
        # safe alternative that does not ship a default credential.
        if len(password) < 8:
            return fail("choose a password of at least 8 characters")
        auth.set_password(password, username)
        session.permanent = True
        session["user"] = username
        return ok(user=username, created=True)

    if auth.login_locked():
        return fail("too many failed attempts — locked for "
                    f"{auth.LOGIN_WINDOW_MIN} minutes", 429)
    if not auth.verify_password(username, password):
        auth.note_login_failure(request.remote_addr)
        return fail("incorrect username or password", 401)

    auth.note_login_success(request.remote_addr)
    session.permanent = True
    session["user"] = username
    return ok(user=username)


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return ok()


@app.route("/api/auth/password", methods=["POST"])
def api_auth_password():
    data = request.get_json(silent=True) or {}
    new = data.get("new_password") or ""
    if not auth.verify_password(session.get("user"), data.get("current_password") or ""):
        return fail("current password is incorrect", 403)
    if len(new) < 8:
        return fail("choose a password of at least 8 characters")
    auth.set_password(new, session.get("user"))
    return ok()


@app.route("/api/auth/recovery")
def api_auth_recovery_get():
    return ok(number=auth.recovery_number())


@app.route("/api/auth/recovery", methods=["POST"])
def api_auth_recovery_set():
    data = request.get_json(silent=True) or {}
    # Changing where reset codes go is equivalent to changing the password, so it
    # is guarded the same way.
    if not auth.verify_password(session.get("user"), data.get("current_password") or ""):
        return fail("current password is incorrect", 403)
    number = (data.get("number") or "").strip()
    db.set_setting("recovery_number", number)
    return ok(number=number)


@app.route("/api/auth/reset/request", methods=["POST"])
def api_auth_reset_request():
    number = auth.recovery_number()
    if auth.reset_rate_exceeded():
        return fail("too many reset requests — try again later", 429)
    # The response is identical whether or not a recovery number is configured,
    # so this endpoint cannot be used to probe the board's configuration.
    if number:
        code = auth.create_reset_code(request.remote_addr)
        try:
            MODEM.send_sms(number, f"iris reset code: {code} "
                                   f"(valid {auth.RESET_CODE_MINUTES} min)")
        except Exception as exc:
            push_event("reset_sms_failed", {"error": str(exc)})
    return ok(sent=True)


@app.route("/api/auth/reset/confirm", methods=["POST"])
def api_auth_reset_confirm():
    data = request.get_json(silent=True) or {}
    new = data.get("new_password") or ""
    if len(new) < 8:
        return fail("choose a password of at least 8 characters")
    good, reason = auth.consume_reset_code(data.get("code") or "")
    if not good:
        return fail(reason, 403)
    user = db.get_user()
    auth.set_password(new, user["username"] if user else None)
    db.clear_auth_events("login_fail")
    return ok()


# --- API tokens -------------------------------------------------------------

@app.route("/api/auth/tokens")
def api_tokens_list():
    return ok(tokens=db.list_tokens(), scopes=auth.SCOPES,
              scope_help=auth.SCOPE_HELP)


@app.route("/api/auth/tokens", methods=["POST"])
def api_tokens_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return fail("give the token a name — it is how you will recognise it later")
    scopes = data.get("scopes") or []
    if not scopes:
        return fail("choose at least one scope")
    token_id, value = auth.create_token(name, scopes, data.get("expires_days"))
    # The only time the value exists outside the caller's hands.
    return ok(token=db.get_token(token_id), value=value)


@app.route("/api/auth/tokens/<int:token_id>/regenerate", methods=["POST"])
def api_tokens_regenerate(token_id):
    old = db.get_token(token_id)
    if not old:
        return fail("no such token", 404)
    db.revoke_token(token_id)
    new_id, value = auth.create_token(old["name"], old["scopes"])
    return ok(token=db.get_token(new_id), value=value, replaced=token_id)


@app.route("/api/auth/tokens/<int:token_id>", methods=["DELETE"])
def api_tokens_delete(token_id):
    if request.args.get("purge") == "1":
        row = db.get_token(token_id)
        if row and not row["revoked_at"]:
            return fail("revoke the token before deleting it")
        return ok(deleted=db.delete_token(token_id))
    return ok(revoked=db.revoke_token(token_id))


# --- status ----------------------------------------------------------------

def status_payload():
    """The status dict, separated from the HTTP layer.

    The MQTT bridge publishes exactly what the UI sees, so a value on a Home
    Assistant dashboard and a value in the web UI can never disagree.
    """
    try:
        status = MODEM.status()
    except Exception as exc:
        status = {"connected": False, "error": str(exc),
                  "port": MODEM.port_name}
    lte = {}
    try:
        with open("/run/lte-ip") as fh:
            lte["ip"] = fh.read().strip()
    except OSError:
        lte["ip"] = None
    disk = os.statvfs(os.path.dirname(db.DB_PATH))
    try:
        ready, vstate = MODEM.voice_ready()
    except Exception:
        ready, vstate = False, {}
    usb = usb_health() or {}
    usb["modem_on_bus"] = modem_on_bus()
    return dict(
        modem=status,
        # Can a call be placed right now without a 40 s mode switch? This is the
        # question that matters for the gate-opener use case.
        voice_ready=ready,
        voice_state=vstate,
        usb=usb,
        host=host_metrics(),
        lte=lte,
        storage=storage_health(disk),
        system=system_health(),
        db=db.stats(),
        # Flattened for consumers that cannot express "is this dict non-empty" in
        # a template — Home Assistant's value_template being the one that counts.
        call_active=bool(status.get("active_call")),
        mqtt=mqtt_bridge.health(),
    )


# --- board power ------------------------------------------------------------

POWER_COMMANDS = {
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
    "shutdown": ["sudo", "-n", "systemctl", "poweroff"],
}
# Long enough for the HTTP response to reach the browser, short enough that
# nobody wonders whether the button worked.
POWER_DELAY = 2.0


@app.route("/api/system/power", methods=["POST"])
def api_power():
    """Reboot or power the board off cleanly.

    This exists because the alternative was being used: pulling the plug. The
    SSD has logged 29 unsafe shutdowns, and an unclean stop is how a filesystem
    ends up replaying a journal — or worse — on a board whose storage is already
    the least trustworthy part of it.

    Split by action, not by route (see `auth.TOKEN_POWER_ACTIONS`). A `reboot`
    comes back on its own, so a token may ask for one — that is what the Home
    Assistant button on the network dashboard uses. A `shutdown` has no remote
    undo on this hardware: there is no working wake-on-LAN, so it can only be
    reversed by someone physically standing next to the board, or by cutting and
    restoring mains upstream. That one stays session-only.
    """
    payload = body()
    action = str(payload.get("action") or "").strip().lower()
    if action not in POWER_COMMANDS:
        return fail("action must be one of: %s" % ", ".join(sorted(POWER_COMMANDS)))

    if (g.identity or "").startswith("token:") and action not in auth.TOKEN_POWER_ACTIONS:
        db.add_auth_event("power_denied", f"{g.identity} {action}", request.remote_addr)
        return fail("%s requires an interactive session" % action, 403)

    # This board is a phone whose entire point is that a call can happen at any
    # moment — including the gate opener. Cutting one off mid-call has to be a
    # decision, not an accident.
    try:
        call_active = bool(MODEM.status().get("active_call"))
    except Exception:
        call_active = False
    if call_active and not payload.get("force"):
        return fail("a call is in progress — resend with force to override",
                    409, call_active=True)

    push_event("system_power", {"action": action, "by": g.identity,
                                "forced": bool(call_active)})
    # Say goodbye before going, so Home Assistant shows this as a deliberate stop
    # instead of waiting out the keepalive on the Last Will.
    mqtt_bridge.announce_offline(f"{action} requested by {g.identity}")

    def go():
        time.sleep(POWER_DELAY)
        try:
            subprocess.run(POWER_COMMANDS[action], timeout=30)
        except Exception as exc:                      # pragma: no cover
            push_event("system_power_failed", {"action": action, "error": str(exc)})

    threading.Thread(target=go, daemon=True, name="power").start()
    return ok(action=action, in_seconds=POWER_DELAY, call_interrupted=call_active)


@app.route("/api/status")
def api_status():
    return ok(**status_payload())


@app.route("/api/events")
def api_events():
    since = request.args.get("since", type=int, default=0)
    with _event_lock:
        items = [e for e in EVENTS if e["seq"] > since]
    return ok(events=items, latest=_event_seq["n"])


@app.route("/api/modem/configure", methods=["POST"])
def api_configure():
    return ok(steps=MODEM.configure())


@app.route("/api/modem/band")
def api_band():
    return ok(band=MODEM.band_config())


@app.route("/api/carrier")
def api_carrier():
    """Everything about how the modem is configured for this operator."""
    def one(cmd):
        try:
            lines = MODEM.send(cmd, timeout=8)
            return lines[0] if lines else None
        except Exception:
            return None

    def val(raw, pattern):
        if not raw:
            return None
        m = re.search(pattern, raw)
        return m.group(1) if m else None

    smsc_raw = one("AT+CSCA?")
    return ok(
        settings={
            # The setting that lets VoLTE and SMS coexist. AT+CGSMS does not
            # write through to it, so it is edited directly.
            "sms_domain_pref": MODEM.sms_domain_pref(),
            "sms_domain_pref_meaning": {
                "00": "CS preferred — SMS over NAS, works alongside VoLTE",
                "01": "PS preferred — SMS over IMS, refused by DIGI",
                "02": "CS only", "03": "PS only",
            },
            "smsc": val(smsc_raw, r'\+CSCA:\s*"([^"]*)"'),
            "smsc_type": val(smsc_raw, r'\+CSCA:\s*"[^"]*",(\d+)'),
            "cgsms": val(one("AT+CGSMS?"), r"\+CGSMS:\s*(\d+)"),
            "csmp": val(one("AT+CSMP?"), r"\+CSMP:\s*(.+)"),
            "storage": val(one("AT+CPMS?"), r'\+CPMS:\s*"([^"]*)"'),
            "ims": val(one('AT+QCFG="ims"'), r'"ims",(\d+)'),
            "scan_mode": val(one('AT+QCFG="nwscanmode"'), r'"nwscanmode",(\d+)'),
            "band": one('AT+QCFG="band"'),
            "mbn_profile": val(one('AT+QMBNCFG="select"'),
                               r'"Select",(\S+)'),
            "mbn_autosel": val(one('AT+QMBNCFG="autosel"'), r'"AutoSel",(\d+)'),
            "apn": modem_mod.DIGI["apn"],
        },
        network={
            "operator": val(one("AT+COPS?"), r'\+COPS:\s*\d+,\d+,"([^"]*)"'),
            "imsi": one("AT+CIMI"),
            "iccid": val(one("AT+QCCID"), r"\+QCCID:\s*(\S+)"),
            "firmware": val(one("ATI"), r"Revision:\s*(\S+)"),
            "registration": one("AT+CEREG?"),
            "info": one("AT+QNWINFO"),
        },
        # Changing these needs a module reset before they take effect.
        needs_reset=["sms_domain_pref", "ims"],
    )


@app.route("/api/carrier", methods=["POST"])
def api_carrier_set():
    data = body()
    applied, errors, reset_needed = {}, {}, False

    def run(key, cmd, resets=False):
        nonlocal reset_needed
        try:
            MODEM.send(cmd, timeout=15)
            applied[key] = cmd
            if resets:
                reset_needed = True
        except Exception as exc:
            errors[key] = str(exc)

    if "sms_domain_pref" in data:
        v = str(data["sms_domain_pref"]).zfill(2)
        if v not in ("00", "01", "02", "03"):
            return fail("sms_domain_pref must be 00, 01, 02 or 03")
        run("sms_domain_pref",
            'AT+QNVFW="%s",%s' % (modem_mod.NV_SMS_DOMAIN_PREF, v), resets=True)
    if "smsc" in data:
        run("smsc", 'AT+CSCA="%s",%s'
            % (data["smsc"], data.get("smsc_type", 145)))
    if "cgsms" in data:
        run("cgsms", "AT+CGSMS=%s" % int(data["cgsms"]))
    if "csmp" in data:
        run("csmp", "AT+CSMP=%s" % data["csmp"])
    if "storage" in data:
        run("storage", 'AT+CPMS="%s","%s","%s"' % ((data["storage"],) * 3))
    if "scan_mode" in data:
        run("scan_mode", 'AT+QCFG="nwscanmode",%s,1' % int(data["scan_mode"]))
    if "ims" in data:
        run("ims", 'AT+QCFG="ims",%s' % int(data["ims"]), resets=True)

    if reset_needed and data.get("reset"):
        try:
            MODEM.full_reset()
            MODEM.wait_for_service("LTE", timeout=120)
            MODEM.configure_sms_params()
            applied["reset"] = "module reset applied"
            reset_needed = False
        except Exception as exc:
            errors["reset"] = str(exc)

    push_event("carrier_settings_changed", {"applied": applied})
    return ok(applied=applied, errors=errors or None,
              reset_required=reset_needed,
              note=("Some changes only take effect after a module reset — "
                    "POST again with {\"reset\": true}" if reset_needed else None))


@app.route("/api/voicemail")
def api_voicemail_get():
    with _vm_busy:
        busy = False
    return ok(**VOICEMAIL, busy=busy,
              greeting_available=bool(resolve_audio_file(VOICEMAIL["greeting"]))
              if VOICEMAIL.get("greeting") else False)


@app.route("/api/voicemail", methods=["POST"])
def api_voicemail_set():
    data = body()
    if "enabled" in data:
        VOICEMAIL["enabled"] = bool(data["enabled"])
    if "ring_seconds" in data:
        VOICEMAIL["ring_seconds"] = max(0, min(int(data["ring_seconds"]), 60))
    if "max_seconds" in data:
        VOICEMAIL["max_seconds"] = max(5, min(int(data["max_seconds"]), 300))
    if "greeting" in data:
        VOICEMAIL["greeting"] = data["greeting"] or None
    save_voicemail_config()
    push_event("voicemail_config", dict(VOICEMAIL))
    return ok(**VOICEMAIL)


@app.route("/api/modem/mode")
def api_get_mode():
    """Which telephony mode the modem is in.

    This firmware cannot do voice and SMS under one configuration on DIGI, so
    the two are exposed as modes. LTE data works in both.
    """
    return ok(**MODEM.current_mode(), modes={
        modem_mod.MODE_VOICE: "calls over VoLTE; SMS is refused by the network",
        modem_mod.MODE_SMS: "SMS over LTE NAS; a call would reset the modem",
    })


@app.route("/api/modem/mode", methods=["POST"])
def api_set_mode():
    mode = (body().get("mode") or "").strip().lower()
    if mode not in (modem_mod.MODE_VOICE, modem_mod.MODE_SMS):
        return fail("mode must be %r or %r"
                    % (modem_mod.MODE_VOICE, modem_mod.MODE_SMS))
    try:
        result = MODEM.set_mode(mode)
    except Exception as exc:
        return fail(exc, 502)
    push_event("mode_changed", result)
    return ok(**result)


# --- voice -----------------------------------------------------------------

@app.route("/api/call/dial", methods=["POST"])
def api_dial():
    data = body()
    number = data.get("number")
    if not number:
        return fail("number is required")
    try:
        num = MODEM.dial(number)
    except Exception as exc:
        db.add_call(modem_mod.normalise_number(number), "OUTGOING", "FAILED",
                    0, str(exc))
        return fail(exc, 502)
    row = db.add_call(num, "OUTGOING", "DIALED", 0, data.get("notes"))
    _active_call_row["id"] = row
    recording = start_recording("call-%s" % num.lstrip("+")) \
        if data.get("record") else None
    # Optionally inject a clip once the far end answers — an outgoing greeting
    # or announcement. Waits for the answer on a worker thread so this returns
    # immediately.
    play = data.get("play")
    if play:
        threading.Thread(target=play_when_answered,
                         args=(play, bool(data.get("play_loop"))),
                         daemon=True).start()
    return ok(call_id=row, number=num, message="Dialing",
              recording=recording, play_on_answer=play)


@app.route("/api/call/ring", methods=["POST"])
def api_ring():
    """Dial, hold for a few seconds, then hang up.

    This is the gate-opener primitive: many gate controllers open on caller ID
    alone and never answer, so the call only has to reach the far end and ring.
    From a warm voice-mode idle that takes 2-3 s. Auto-hangup means an unanswered
    call is not left ringing.
    """
    data = body()
    number = data.get("number")
    if not number:
        return fail("number is required")
    seconds = min(int(data.get("seconds", 8)), 30)

    ready, state = MODEM.voice_ready()
    if not ready:
        return fail("not ready for voice (ims %s/%s) — the modem must be in "
                    "voice mode with IMS registered"
                    % (state.get("ims_setting"), state.get("ims_registered")),
                    409, state=state)

    started = time.time()
    try:
        num = MODEM.dial(number)
    except Exception as exc:
        db.add_call(modem_mod.normalise_number(number), "OUTGOING", "FAILED",
                    0, "ring: %s" % exc)
        return fail(exc, 502)

    row = db.add_call(num, "OUTGOING", "RINGING", 0, data.get("notes") or "ring")
    _active_call_row["id"] = row

    alerting_after = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if any(c.get("state") in ("ALERTING", "ACTIVE")
                   for c in MODEM.call_status()):
                alerting_after = round(time.time() - started, 1)
                break
        except Exception:
            break
        time.sleep(0.5)

    # Hold the call ringing for the remainder of the requested window.
    while time.time() < deadline:
        time.sleep(0.5)
    try:
        duration = MODEM.hangup()
    except Exception:
        duration = int(time.time() - started)
    db.update_call(row, status="RUNG", duration=duration)
    _active_call_row["id"] = None
    return ok(number=num, call_id=row, seconds=seconds,
              ringing_after_seconds=alerting_after,
              total_seconds=round(time.time() - started, 1))


@app.route("/api/call/answer", methods=["POST"])
def api_answer():
    try:
        MODEM.answer()
    except Exception as exc:
        return fail(exc, 502)
    return ok(message="Answered")


@app.route("/api/call/hangup", methods=["POST"])
def api_hangup():
    try:
        duration = MODEM.hangup()
    except Exception as exc:
        stop_playback()
        stop_recording()
        return fail(exc, 502)
    row = _active_call_row["id"]
    if row:
        db.update_call(row, status="HANGUP", duration=duration)
        _active_call_row["id"] = None
    stop_playback()
    return ok(duration_seconds=duration, recording=stop_recording())


# --- call audio -------------------------------------------------------------

@app.route("/api/audio")
def api_audio():
    """Audio routing state, and whether a USB sound card is present.

    The modem's UAC interface is full duplex: the capture side carries the far
    end's audio, the playback side is injected as the board's microphone.
    """
    with _rec_lock:
        rec_active = bool(_recorder["proc"] and _recorder["proc"].poll() is None)
        rec_path = _recorder["path"]
    with _play_lock:
        play_active = bool(_player["proc"] and _player["proc"].poll() is None)
        play_file = _player["file"]
    return ok(
        device=find_audio_device(),
        recording=rec_active,
        current_file=os.path.basename(rec_path) if rec_path else None,
        playing=play_active,
        playing_file=play_file,
        modem=MODEM.audio_state(),
    )


@app.route("/api/audio/files")
def api_audio_files():
    """Clips available to inject — from the audio dir and past recordings."""
    out = []
    for label, directory in (("audio", AUDIO_DIR),
                             ("recording", RECORDINGS_DIR)):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".wav"):
                continue
            entry = {"file": name, "source": label}
            entry.update(wav_stats(os.path.join(directory, name)) or {})
            out.append(entry)
    return ok(files=out, audio_dir=AUDIO_DIR, recordings_dir=RECORDINGS_DIR)


@app.route("/api/audio/files/<name>", methods=["PUT"])
def api_audio_upload(name):
    """Upload a clip to inject. Body is the raw file; any sox-readable format."""
    if os.path.basename(name) != name:
        return fail("invalid name", 400)
    data = request.get_data()
    if not data:
        return fail("empty body", 400)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    path = os.path.join(AUDIO_DIR, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return ok(file=name, bytes=len(data), stats=wav_stats(path))


@app.route("/api/audio/play", methods=["POST"])
def api_play():
    data = body()
    name = data.get("file")
    if not name:
        return fail("file is required (see GET /api/audio/files)")
    result = start_playback(name, bool(data.get("loop")))
    if not result.get("playing"):
        return fail(result.get("error", "could not start playback"), 502)
    return ok(**result)


@app.route("/api/audio/play", methods=["DELETE"])
def api_stop_play():
    info = stop_playback()
    if not info:
        return fail("nothing is playing", 409)
    return ok(**info)


@app.route("/api/audio/record", methods=["POST"])
def api_start_recording():
    return ok(**start_recording(body().get("tag") or "manual"))


@app.route("/api/audio/record", methods=["DELETE"])
def api_stop_recording():
    info = stop_recording()
    if not info:
        return fail("nothing is being recorded", 409)
    return ok(**info)


@app.route("/api/recordings")
def api_recordings():
    if not os.path.isdir(RECORDINGS_DIR):
        return ok(recordings=[])
    items = []
    for name in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
        if not name.endswith(".wav"):
            continue
        full = os.path.join(RECORDINGS_DIR, name)
        entry = {"file": name,
                 "modified": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime(os.path.getmtime(full))),
                 "url": "/api/recordings/" + name}
        entry.update(wav_stats(full) or {})
        items.append(entry)
    return ok(recordings=items, directory=RECORDINGS_DIR)


@app.route("/api/recordings/<name>")
def api_recording_download(name):
    # Guard against path traversal — only plain .wav names in the directory.
    if os.path.basename(name) != name or not name.endswith(".wav"):
        return fail("invalid recording name", 400)
    if not os.path.exists(os.path.join(RECORDINGS_DIR, name)):
        return fail("recording not found", 404)
    return send_from_directory(RECORDINGS_DIR, name, as_attachment=True)


@app.route("/api/recordings/<name>", methods=["DELETE"])
def api_recording_delete(name):
    if os.path.basename(name) != name or not name.endswith(".wav"):
        return fail("invalid recording name", 400)
    full = os.path.join(RECORDINGS_DIR, name)
    if not os.path.exists(full):
        return fail("recording not found", 404)
    os.remove(full)
    return ok(deleted=name)


@app.route("/api/call/dtmf", methods=["POST"])
def api_dtmf():
    digits = body().get("digits")
    if not digits:
        return fail("digits is required")
    try:
        MODEM.send_dtmf(digits)
    except Exception as exc:
        return fail(exc, 502)
    return ok(sent=digits)


@app.route("/api/call/status")
def api_call_status():
    try:
        return ok(calls=MODEM.call_status(), activity=MODEM.phone_activity(),
                  tracked=MODEM.call)
    except Exception as exc:
        return fail(exc, 502)


@app.route("/api/call/register")
def api_call_register():
    rows, total = db.list_calls(
        limit=request.args.get("limit", type=int, default=100),
        offset=request.args.get("offset", type=int, default=0),
        search=request.args.get("search"),
        direction=request.args.get("direction"),
    )
    return ok(calls=decorate(rows), total=total)


@app.route("/api/call/register", methods=["DELETE"])
def api_clear_calls():
    return ok(deleted=db.clear_calls())


# --- SMS -------------------------------------------------------------------

@app.route("/api/sms/conversations")
def api_conversations():
    return ok(conversations=db.list_conversations(),
              unread=db.unread_count())


@app.route("/api/sms/conversation")
def api_conversation():
    number = request.args.get("number")
    if not number:
        return fail("number is required")
    num = modem_mod.normalise_number(number)
    messages = db.get_conversation(num)
    db.mark_conversation_read(num)
    contact = db.find_contact_by_number(num)
    return ok(number=num, messages=messages,
              name=contact["name"] if contact else None)


@app.route("/api/sms/conversation", methods=["DELETE"])
def api_delete_conversation():
    number = request.args.get("number")
    if not number:
        return fail("number is required")
    return ok(deleted=db.delete_conversation(
        modem_mod.normalise_number(number)))


@app.route("/api/sms/send", methods=["POST"])
def api_send_sms():
    data = body()
    message = data.get("message")
    recipients = data.get("numbers") or (
        [data["number"]] if data.get("number") else [])
    if not recipients:
        return fail("number (or numbers) is required")
    if not message:
        return fail("message is required")

    # No mode switching: with the NV SMS domain set to CS, sending works while
    # IMS is registered for VoLTE. auto_mode remains available for a module whose
    # NV cannot be written, where SMS still needs IMS off.
    auto_mode = data.get("auto_mode", False)
    restore_mode = data.get("restore_mode", True)
    switched_from = None
    if auto_mode:
        try:
            state = MODEM.current_mode()
            if state.get("mode") and state["mode"] != modem_mod.MODE_SMS:
                switched_from = state["mode"]
                push_event("mode_changed", MODEM.set_mode(modem_mod.MODE_SMS))
        except Exception as exc:
            return fail("could not switch to SMS mode: %s" % exc, 502)

    results = []
    for raw in recipients:
        num = modem_mod.normalise_number(raw)
        try:
            res = MODEM.send_sms(num, message, auto_mode=False)
            db.add_sms(num, "OUT", message, status="SENT")
            results.append({"number": num, "success": True,
                            "reference": res.get("reference"),
                            "unicode": res.get("unicode")})
        except Exception as exc:
            # "+CMS ERROR: unknown error" says nothing; AT+CEER carries the
            # network's actual reason, so record and return that too.
            cause = MODEM.extended_error()
            detail = str(exc)
            if cause and cause.get("text"):
                detail = "%s (%s)" % (detail, cause["text"])
            db.add_sms(num, "OUT", message, status="FAILED", error=detail)
            results.append({"number": num, "success": False,
                            "error": detail, "cause": cause})

    if switched_from and restore_mode:
        try:
            push_event("mode_changed", MODEM.set_mode(switched_from))
        except Exception as exc:
            push_event("mode_restore_failed", {"error": str(exc)})

    any_ok = any(r["success"] for r in results)
    payload = {"success": any_ok, "results": results,
               "mode_switched": bool(switched_from)}
    if not any_ok:
        # Surface the network's refusal verbatim rather than a generic message.
        payload["error"] = results[0].get("error")
        return jsonify(payload), 502
    return jsonify(payload)


@app.route("/api/sms/sync", methods=["POST"])
def api_sync_sms():
    try:
        return ok(imported=sync_sms_from_modem())
    except Exception as exc:
        return fail(exc, 502)


@app.route("/api/sms/inbox")
def api_inbox():
    """Raw view of whatever is currently in the modem's message store."""
    try:
        return ok(messages=MODEM.read_messages(
            request.args.get("filter", "ALL")))
    except Exception as exc:
        return fail(exc, 502)


@app.route("/api/sms/read", methods=["POST"])
def api_mark_read():
    number = body().get("number")
    if not number:
        return fail("number is required")
    return ok(updated=db.mark_conversation_read(
        modem_mod.normalise_number(number)))


# --- USSD ------------------------------------------------------------------

@app.route("/api/ussd/send", methods=["POST"])
def api_ussd():
    code = body().get("code")
    if not code:
        return fail("code is required")
    try:
        res = MODEM.send_ussd(code, timeout=body().get("timeout", 45))
        db.add_ussd_history(code, res.get("text"), True)
        return ok(**res)
    except Exception as exc:
        db.add_ussd_history(code, str(exc), False)
        return fail(exc, 502)


@app.route("/api/ussd/cancel", methods=["POST"])
def api_ussd_cancel():
    try:
        MODEM.cancel_ussd()
    except Exception as exc:
        return fail(exc, 502)
    return ok(message="Session cancelled")


@app.route("/api/ussd/history")
def api_ussd_history():
    return ok(history=db.list_ussd_history(
        request.args.get("limit", type=int, default=50)))


# --- time and NTP ----------------------------------------------------------
#
# This board has no RTC battery, so the clock is whatever it was at shutdown
# until NTP lands. That is not a cosmetic problem: TLS certificate validation,
# the prepaid reminder's date arithmetic and every log timestamp depend on it.
# Hence a way to see the sync state and to point it at a different server —
# useful here because the LAN has its own resolver and gateway that answer NTP
# faster and keep working when the WAN does not.

NTP_HELPER = "/usr/local/sbin/iris-set-ntp"

# Hostname, IPv4, or IPv6 literal. Mirrored in the helper script, which is the
# one that actually runs as root.
RE_NTP_SERVER = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]+)$")
NTP_MAX_SERVERS = 4


def _timesync_info():
    """What timesyncd is doing, parsed from `timedatectl show-timesync`."""
    info = {}
    try:
        out = subprocess.run(["timedatectl", "show-timesync"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return info
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in ("ServerName", "ServerAddress", "PollIntervalUSec",
                   "RootDistanceMaxUSec"):
            info[key] = value.strip()
        elif key == "NTPMessage":
            # The whole struct is one line; the only field worth surfacing is
            # when the last exchange actually happened.
            m = re.search(r"DestinationTimestamp=([^,}]+)", value)
            if m:
                info["last_sync"] = m.group(1).strip()
    return info


def _configured_ntp():
    """Servers from our drop-in, or None if it has never been written."""
    path = "/etc/systemd/timesyncd.conf.d/10-iris.conf"
    try:
        with open(path) as fh:
            for line in fh:
                if line.strip().upper().startswith("NTP="):
                    return line.split("=", 1)[1].strip().split()
    except OSError:
        pass
    return None


def _run_ntp_helper(args):
    proc = subprocess.run(["sudo", "-n", NTP_HELPER] + args,
                          capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or "exit %d" % proc.returncode)
    return (proc.stdout or "").strip()


def _time_payload():
    now = datetime.datetime.now().astimezone()
    return {
        "local": now.isoformat(timespec="seconds"),
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"),
        "timezone": str(now.tzname()),
        "synchronized": os.path.exists("/run/systemd/timesync/synchronized"),
        "configured_servers": _configured_ntp(),
        "timesync": _timesync_info(),
        "helper_installed": os.path.exists(NTP_HELPER),
    }


@app.route("/api/time")
def api_time():
    return ok(**_time_payload())


@app.route("/api/time/ntp", methods=["POST"])
def api_time_ntp():
    """Point timesyncd at one or more NTP servers."""
    raw = body().get("servers")
    if isinstance(raw, str):
        servers = raw.replace(",", " ").split()
    elif isinstance(raw, list):
        servers = [str(s).strip() for s in raw if str(s).strip()]
    else:
        servers = []

    if not servers:
        return fail("give at least one server")
    if len(servers) > NTP_MAX_SERVERS:
        return fail("at most %d servers" % NTP_MAX_SERVERS)
    bad = [s for s in servers if not RE_NTP_SERVER.match(s)]
    if bad:
        return fail("not a valid hostname or address: %s" % ", ".join(bad))

    try:
        detail = _run_ntp_helper(["servers"] + servers)
    except Exception as exc:
        return fail("could not set NTP server: %s" % exc, 500)
    push_event("ntp_configured", {"servers": servers, "by": g.identity})
    return ok(detail=detail, **_time_payload())


@app.route("/api/time/sync", methods=["POST"])
def api_time_sync():
    """Force a resync now, rather than waiting out the poll interval.

    The interval on this board stretches to 34 minutes once the clock is stable,
    which is a long time to sit looking at a wrong clock after a power cut.
    """
    try:
        detail = _run_ntp_helper(["sync"])
    except Exception as exc:
        return fail("could not trigger a resync: %s" % exc, 500)
    push_event("ntp_resync", {"by": g.identity})
    return ok(detail=detail, **_time_payload())


# --- prepaid balance -------------------------------------------------------

@app.route("/api/balance")
def api_balance():
    return ok(**balance.status())


@app.route("/api/balance", methods=["POST"])
def api_balance_config():
    try:
        return ok(config=balance.set_config(**body()))
    except ValueError as exc:
        return fail(str(exc))


@app.route("/api/balance/check", methods=["POST"])
def api_balance_check():
    """Run a check now, in the background.

    Not synchronous: the USSD walk plus waiting for DIGI's five SMS replies runs
    to a couple of minutes, which is longer than nginx's proxy_read_timeout and
    far longer than anyone holds a request open. Poll GET /api/balance for the
    result — `last_result.ran_at` tells you whether it is this run's.
    """
    threading.Thread(
        target=balance.run_check, args=(MODEM,),
        kwargs={"send_alert": bool(body().get("send_alert", True))},
        daemon=True, name="balance-check").start()
    return ok(started=True, poll="/api/balance")


# --- firmware & version check ----------------------------------------------

@app.route("/api/firmware/check")
def api_firmware_check():
    """Check for new software/firmware release tags on GitHub."""
    return ok(**FIRMWARE.check_updates())


# --- phonebook -------------------------------------------------------------

@app.route("/api/phonebook")
def api_phonebook():
    return ok(contacts=db.list_contacts(request.args.get("search")))


@app.route("/api/phonebook", methods=["POST"])
def api_add_contact():
    data = body()
    name, number = data.get("name"), data.get("number")
    if not name or not number:
        return fail("name and number are required")
    try:
        row = db.add_contact(name, modem_mod.normalise_number(number),
                             data.get("notes"))
    except Exception as exc:
        return fail("could not add contact: %s" % exc, 409)
    return ok(id=row)


@app.route("/api/phonebook/<int:contact_id>", methods=["PUT"])
def api_update_contact(contact_id):
    data = body()
    number = data.get("number")
    try:
        updated = db.update_contact(
            contact_id, data.get("name"),
            modem_mod.normalise_number(number) if number else None,
            data.get("notes"),
        )
    except Exception as exc:
        return fail("could not update contact: %s" % exc, 409)
    if not updated:
        return fail("contact not found", 404)
    return ok(updated=updated)


@app.route("/api/phonebook/<int:contact_id>", methods=["DELETE"])
def api_delete_contact(contact_id):
    if not db.delete_contact(contact_id):
        return fail("contact not found", 404)
    return ok(deleted=1)


@app.route("/api/phonebook/import-sim", methods=["POST"])
def api_import_sim():
    try:
        entries = MODEM.sim_phonebook()
    except Exception as exc:
        return fail(exc, 502)
    added = 0
    for entry in entries:
        if not entry.get("number"):
            continue
        try:
            db.add_contact(entry.get("name") or entry["number"],
                           entry["number"], "imported from SIM")
            added += 1
        except Exception:
            pass          # already present
    return ok(found=len(entries), added=added)


# --- AT console ------------------------------------------------------------

@app.route("/api/at", methods=["POST"])
def api_at():
    command = (body().get("command") or "").strip()
    if not command:
        return fail("command is required")
    success, response = MODEM.send_raw(
        command, timeout=body().get("timeout", 15))
    db.add_at_history(command, response, success)
    return jsonify({"success": success, "command": command,
                    "response": response})


@app.route("/api/at/history")
def api_at_history():
    return ok(history=db.list_at_history(
        request.args.get("limit", type=int, default=100)))


# --- OpenAPI spec ----------------------------------------------------------

def _spec():
    def op(summary, tag, params=None, req=None):
        entry = {"summary": summary, "tags": [tag],
                 "responses": {"200": {"description": "OK"}}}
        if params:
            entry["parameters"] = [
                {"name": p, "in": "query", "schema": {"type": "string"}}
                for p in params
            ]
        if req:
            entry["requestBody"] = {
                "content": {"application/json": {
                    "schema": {"type": "object", "properties": {
                        k: {"type": "string", "example": v}
                        for k, v in req.items()}}}}
            }
        return entry

    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Orange Pi 3 LTS Phone API",
            "version": "1.0.1",
            "description": "Voice, SMS, USSD, phonebook and AT access for a "
                           "Quectel EC25-E on DIGI Romania.",
        },
        "servers": [{"url": "/"}],
        "paths": {
            "/api/status": {"get": op("Modem, LTE, storage and DB status",
                                      "status")},
            "/api/events": {"get": op("Recent modem events", "status",
                                      params=["since"])},
            "/api/modem/configure": {"post": op("Re-apply the DIGI profile",
                                                "status")},
            "/api/modem/band": {"get": op("Band configuration", "status")},

            "/api/call/dial": {"post": op("Place a call", "voice",
                                          req={"number": "0771324635",
                                               "notes": "test"})},
            "/api/call/answer": {"post": op("Answer the ringing call", "voice")},
            "/api/call/hangup": {"post": op("Hang up", "voice")},
            "/api/call/dtmf": {"post": op("Send DTMF digits", "voice",
                                          req={"digits": "123#"})},
            "/api/call/status": {"get": op("Live call state", "voice")},
            "/api/call/register": {
                "get": op("Call history", "voice",
                          params=["limit", "offset", "search", "direction"]),
                "delete": op("Clear call history", "voice"),
            },

            "/api/sms/conversations": {"get": op("Conversation list", "sms")},
            "/api/sms/conversation": {
                "get": op("Messages with one correspondent", "sms",
                          params=["number"]),
                "delete": op("Delete a conversation", "sms",
                             params=["number"]),
            },
            "/api/sms/send": {"post": op("Send an SMS", "sms",
                                         req={"number": "0771324635",
                                              "message": "Hello"})},
            "/api/sms/sync": {"post": op("Import messages from the modem",
                                         "sms")},
            "/api/sms/inbox": {"get": op("Raw modem message store", "sms",
                                         params=["filter"])},
            "/api/sms/read": {"post": op("Mark a conversation read", "sms",
                                         req={"number": "0771324635"})},

            "/api/ussd/send": {"post": op("Send a USSD code", "ussd",
                                          req={"code": "*118#"})},
            "/api/ussd/cancel": {"post": op("Cancel the USSD session", "ussd")},
            "/api/ussd/history": {"get": op("USSD history", "ussd",
                                            params=["limit"])},

            "/api/time": {"get": op("Clock and NTP state", "time")},
            "/api/time/ntp": {"post": op("Set the NTP server(s)", "time",
                                         req={"servers": "10.64.88.1"})},
            "/api/time/sync": {"post": op("Resync the clock now", "time")},

            "/api/balance": {
                "get": op("Prepaid option, credit and quotas", "balance"),
                "post": op("Configure the reminder", "balance",
                           req={"balance_alert_number": "0771324635",
                                "balance_alert_days": "3",
                                "balance_check_at": "09:15",
                                "balance_enabled": "true"}),
            },
            "/api/balance/check": {"post": op("Run a balance check now",
                                              "balance")},

            "/api/firmware/check": {"get": op("Check for software updates",
                                              "system")},

            "/api/phonebook": {
                "get": op("List contacts", "phonebook", params=["search"]),
                "post": op("Create a contact", "phonebook",
                           req={"name": "Test", "number": "0771324635"}),
            },
            "/api/phonebook/{id}": {
                "put": op("Update a contact", "phonebook",
                          req={"name": "New name"}),
                "delete": op("Delete a contact", "phonebook"),
            },
            "/api/phonebook/import-sim": {
                "post": op("Import the SIM phonebook", "phonebook")},

            "/api/at": {"post": op("Run a raw AT command", "at",
                                   req={"command": "AT+CSQ"})},
            "/api/at/history": {"get": op("AT command history", "at",
                                          params=["limit"])},
        },
    }


@app.route("/api/spec")
def api_spec():
    return jsonify(_spec())


# --- startup ---------------------------------------------------------------

VOICE_WATCHDOG_INTERVAL = int(os.environ.get("PHONE_VOICE_WATCHDOG", "120"))

# Edge-triggered so "waiting for coverage" is announced once per outage rather
# than every poll. A watchdog that logs the same line every two minutes is one
# nobody reads.
_watchdog = {"waiting_for_coverage": False}


def voice_watchdog():
    """Keep the modem ready to place a call at any moment.

    The primary use of this board is ringing a gate on arrival, so a call must
    never be the thing that discovers IMS has dropped — recovering costs a module
    reset and ~40 s. This checks periodically and repairs proactively instead.

    It deliberately does nothing while a call is up, while a recording or
    playback is running, or while the modem is intentionally in SMS mode.
    """
    while True:
        time.sleep(VOICE_WATCHDOG_INTERVAL)
        try:
            with _rec_lock:
                busy = bool(_recorder["proc"] and _recorder["proc"].poll() is None)
            with _play_lock:
                busy = busy or bool(_player["proc"]
                                    and _player["proc"].poll() is None)
            if busy or _active_call_row["id"] is not None:
                continue
            if MODEM.call_status():
                continue                      # a call is up after all
            state = MODEM.current_mode()

            # Return to the resting mode if something left the modem elsewhere —
            # an interrupted SMS send, or a failed restore.
            if state.get("mode") != modem_mod.RESTING_MODE:
                push_event("mode_drifted", state)
                push_event("mode_restored",
                           MODEM.set_mode(modem_mod.RESTING_MODE))
                continue

            # In voice mode, being in the right mode is not enough — IMS must
            # actually be registered or a call falls back to CSFB.
            if (modem_mod.RESTING_MODE == modem_mod.MODE_VOICE
                    and state.get("ims_registered") != 1):
                # But only reset the module when a reset could actually help.
                #
                # Repairing means AT+CFUN=1,1, which re-enumerates the module.
                # With no coverage that is futile and expensive: measured in a
                # basement, this loop produced 14 disconnects and 15
                # enumerations in 85 minutes, every repair reporting
                # `changed: false`. It drives the module through its
                # highest-current state on a 7-minute cycle — the state
                # docs/power-and-recovery.md identifies as what invites the
                # crash loop — and it pins `usb_disconnects_last_hour` high
                # enough that the USB power alarm never clears, burying the real
                # brownouts it exists to catch.
                #
                # No backoff, deliberately: the radio attaches by itself when
                # signal returns and IMS follows, so polling stays at the normal
                # interval. A backoff would delay voice readiness by however long
                # it had grown to, and ringing the gate is why this board exists.
                served = MODEM.has_service()
                if served is False:
                    if not _watchdog["waiting_for_coverage"]:
                        _watchdog["waiting_for_coverage"] = True
                        push_event("voice_waiting_for_coverage", state)
                    continue
                _watchdog["waiting_for_coverage"] = False
                push_event("voice_not_ready", state)
                push_event("voice_repaired",
                           MODEM.set_mode(modem_mod.MODE_VOICE))
            else:
                _watchdog["waiting_for_coverage"] = False
        except Exception as exc:
            push_event("voice_watchdog_error", {"error": str(exc)})


def bootstrap():
    db.init()
    load_voicemail_config()
    threading.Thread(target=worker, name="phone-worker", daemon=True).start()
    threading.Thread(target=voice_watchdog, name="voice-watchdog",
                     daemon=True).start()
    WRITE_PROBE.start()
    try:
        MODEM.open()
        MODEM.configure()
        push_event("modem_ready", {"port": MODEM.port_name})
    except Exception as exc:
        push_event("modem_error", {"error": str(exc)})

    def initial_sync():
        time.sleep(3)
        try:
            sync_sms_from_modem()
        except Exception:
            pass
        # Put the modem into the resting mode at startup so the first call does
        # not have to discover it is in the wrong one. Takes ~40 s when a switch
        # is actually needed, so it runs in the background.
        try:
            state = MODEM.current_mode()
            if state.get("mode") != modem_mod.RESTING_MODE:
                push_event("mode_restored",
                           MODEM.set_mode(modem_mod.RESTING_MODE))
            elif (modem_mod.RESTING_MODE == modem_mod.MODE_VOICE
                    and state.get("ims_registered") != 1):
                push_event("voice_repaired",
                           MODEM.set_mode(modem_mod.MODE_VOICE))
        except Exception as exc:
            push_event("resting_mode_failed", {"error": str(exc)})

    threading.Thread(target=initial_sync, daemon=True).start()


bootstrap()

# Installed after every route is registered, so nothing can slip in behind it.
# The hook is fail-closed: a route that is not classified in auth.RULES requires
# the `full` scope, which keeps the UI working (a session is full access) while
# tokens are denied until the route is deliberately classified.
auth.install(app)

# Telemetry to Home Assistant. This has to live in-process: app.py owns the
# serial port through a single reader thread, so a separate publisher could not
# query the modem without fighting it for the port. It is a no-op when the
# credentials file is absent.
mqtt_bridge.start(status_payload)

# Prepaid option reminder. In-process for the same reason as the bridge above,
# and scheduled internally rather than by cron: a cron job would need its own way
# to the modem, and the only one available is this process.
balance.start(MODEM)

if __name__ == "__main__":
    # Bound to 0.0.0.0 for a direct run; the service sets PHONE_BIND=127.0.0.1 so
    # that nginx is the only way in once TLS is in front.
    app.run(host=BIND, port=PORT, threaded=True, use_reloader=False)
