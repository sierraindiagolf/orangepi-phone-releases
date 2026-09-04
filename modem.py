"""Thread-safe Quectel EC25-E AT controller with background URC monitoring.

One serial port carries both command responses and unsolicited result codes
(URCs), so a single reader thread owns the port: it classifies each inbound
line and either feeds it to the in-flight command or dispatches it as an event.
Commands are serialised behind a lock, which keeps call control, SMS, USSD and
the raw AT console from interleaving on the wire.

Operator settings are DIGI Romania (SMSC +40770000050, SMS over CS preferred),
carried over from the WG1608 deployment where they were established.
"""

import os
import queue
import re
import threading
import time

import serial

# /dev/ec25-at is a udev symlink to the modem's primary AT interface. It must be
# used in preference to /dev/ttyUSBn: this firmware resets itself on some
# operations, and after the USB re-enumeration the ttyUSB numbering can shift
# (ttyUSB2 has come back as ttyUSB4), whereas the symlink always follows
# interface 02.
AT_PORT = os.environ.get("PHONE_AT_PORT", "/dev/ec25-at")
BAUD = int(os.environ.get("PHONE_AT_BAUD", "115200"))

# Modes are retained as an escape hatch, but are no longer part of normal
# operation: with SMS_DOMAIN_PREF set below, VoLTE and SMS work simultaneously.
#
#   ims=1 (VoLTE)   calls stay on LTE and ring in 3-6 s; SMS goes over NAS
#   ims=2 (IMS off) calls fall back to GSM (CSFB) and ring in 6.5-12 s
#
# Both once appeared mutually exclusive with SMS. That was two separate mistakes:
# CSFB "resetting the modem" was USB brownout from a shared hub, and SMS being
# refused under VoLTE was the NV item below, not a network policy.
MODE_VOICE = "voice"
MODE_SMS = "sms"
_IMS_FOR_MODE = {MODE_VOICE: 1, MODE_SMS: 2}

# The mode the modem runs in. Voice (IMS on) is correct and there is no longer a
# reason to leave it: with SMS_DOMAIN_PREF set below, VoLTE and SMS work at the
# same time, so no switching is needed at all. MODE_SMS remains available as an
# escape hatch if IMS ever misbehaves.
RESTING_MODE = os.environ.get("PHONE_RESTING_MODE", MODE_VOICE)

# The setting that makes voice and SMS coexist.
#
# AT+CGSMS appears to accept a domain preference and is then ignored once IMS
# registers — because it does not write through to the Qualcomm NV item that
# actually decides. That item shipped as 01 (PS preferred), so every SMS was
# submitted over IMS, and DIGI refuses SMS over IMS (+CEER: 0,29). Writing it to
# 00 (CS preferred) sends SMS over NAS/SGs while IMS stays registered for VoLTE.
#
#   0 = CS preferred   1 = PS preferred   2 = CS only   3 = PS only
#
# It persists in NV across reboots and module resets, but is asserted on every
# configure() so a replacement module or an NV reset cannot silently regress to
# the broken behaviour.
NV_SMS_DOMAIN_PREF = "/nv/item_files/modem/mmode/sms_domain_pref"
SMS_DOMAIN_PREF = os.environ.get("PHONE_SMS_DOMAIN_PREF", "00")

# DIGI Romania operator profile.
#
# SMSC confirmed against a handset on the same SIM. cgsms is 1 (CS domain):
# with IMS off, SMS travels over SGs, which is signalled on LTE and needs no
# other radio. Measured either way — with ims=2 both cgsms 0 and 1 submit
# successfully, so the deciding setting is the IMS mode, not this one.
DIGI = {
    "smsc": "+40770000050",
    "smsc_type": 145,
    "csmp": "17,167,0,0",
    "storage": "ME",
    "cgsms": 1,
    "apn": "internet",
}

PROMPT = "__PROMPT__"

# Final result codes that end a command.
_FINAL_EXACT = {"OK", "ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"}
_FINAL_PREFIX = ("+CME ERROR:", "+CMS ERROR:")

# Codes that are always asynchronous, even while a command is in flight.
_ASYNC_PREFIX = (
    "RING", "+CRING:", "+CLIP:", "+CMTI:", "+CMT:", "+CDS:", "+CUSD:",
    "+QIND:", "+CIEV:", "+CGEV:",
)

# GSM 03.38 default alphabet — anything outside it forces a UCS2 send.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "\f^{}\\[~]|€"
)


def normalise_number(raw):
    """07xxxxxxxx -> +407xxxxxxxx. Short codes and USSD strings pass through."""
    if raw is None:
        return ""
    n = re.sub(r"[\s\-()./]", "", str(raw).strip())
    if not n:
        return ""
    if n.startswith("+"):
        return n
    if n.startswith("00"):
        return "+" + n[2:]
    if n.startswith("0") and len(n) == 10:      # national mobile/landline
        return "+40" + n[1:]
    if n.startswith("40") and len(n) == 11:
        return "+" + n
    return n                                     # short code, e.g. 1234


def is_gsm7(text):
    return all(ch in _GSM7 for ch in text)


def to_ucs2_hex(text):
    return text.encode("utf-16-be").hex().upper()


def looks_like_ucs2_hex(s):
    """Is this really UCS2 hex, or something that merely looks like it?

    Digits are valid hex, so a plain numeric string of the right length would
    otherwise be "decoded" into nonsense: the DIGI short code 68737173 became
    桳煳, and an 8-digit OTP in a message body would be mangled the same way.

    An all-decimal string is therefore only treated as UCS2 when it carries the
    signature of UCS2-encoded text — ASCII encodes as 00xx, so every 4-character
    group starts with "00". Anything containing hex letters cannot be a phone
    number and is decoded normally.
    """
    if len(s) < 4 or len(s) % 4 != 0:
        return False
    if re.fullmatch(r"[0-9A-Fa-f]+", s) is None:
        return False
    if s.isdigit():
        return all(s[i:i + 2] == "00" for i in range(0, len(s), 4))
    return True


def decode_maybe_ucs2(s):
    """Decode a UCS2 hex payload if that is plainly what it is."""
    if not s or not looks_like_ucs2_hex(s):
        return s
    try:
        out = bytes.fromhex(s).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return s
    # Reject decodes full of control characters — those mean it was not UCS2.
    if any(ord(c) < 9 for c in out):
        return s
    return out


# Relevant GSM 24.008 / 24.011 cause values, for turning AT+CEER into something
# a human can act on.
_CEER_CAUSES = {
    1: "unallocated number",
    8: "operator determined barring",
    17: "user busy",
    21: "short message transfer rejected",
    27: "destination out of order",
    28: "unidentified subscriber / invalid number format",
    29: "facility rejected — the network refused the service",
    30: "unknown subscriber",
    31: "normal, unspecified",
    38: "network out of order",
    41: "temporary failure",
    42: "congestion",
    47: "resources unavailable",
    50: "requested facility not subscribed",
    69: "requested facility not implemented",
    95: "invalid message",
    96: "invalid mandatory information",
    111: "protocol error",
    127: "interworking, unspecified",
}


class ModemError(Exception):
    pass


class Modem:
    def __init__(self, port=AT_PORT, baud=BAUD, on_event=None):
        self.port_name = port
        self.baud = baud
        self.on_event = on_event or (lambda kind, data: None)

        self._ser = None
        self._cmd_lock = threading.RLock()   # one command at a time
        self._io_lock = threading.Lock()     # guards writes / port swap
        self._resp_q = queue.Queue()
        self._in_command = threading.Event()
        self._reader = None
        self._running = False
        self._pending_urc = None   # multi-line URC being accumulated

        self.connected = False
        self.last_error = None

        # Call state, maintained from URCs.
        self.call = None          # {'number','direction','state','started','id'}
        self._call_lock = threading.Lock()

        # USSD replies arrive asynchronously.
        self._ussd_q = queue.Queue()

    # --- lifecycle ----------------------------------------------------------

    def open(self):
        with self._io_lock:
            if self._ser and self._ser.is_open:
                return
            self._ser = serial.Serial(
                self.port_name, self.baud, timeout=0.3, write_timeout=5,
                rtscts=False, dsrdtr=False,
            )
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        self._running = True
        if not self._reader or not self._reader.is_alive():
            self._reader = threading.Thread(
                target=self._read_loop, name="modem-reader", daemon=True
            )
            self._reader.start()
        self.connected = True

    def close(self):
        self._running = False
        if self._reader:
            self._reader.join(timeout=2)
        with self._io_lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
        self.connected = False

    def reopen(self):
        """Recover from a port error (modem reset, USB re-enumeration)."""
        try:
            self.close()
        except Exception:
            pass
        time.sleep(1)
        self.open()
        self.configure()

    # --- reader -------------------------------------------------------------

    def _read_loop(self):
        buf = ""
        while self._running:
            try:
                with self._io_lock:
                    ser = self._ser
                    if not ser or not ser.is_open:
                        raise serial.SerialException("port closed")
                    pending = ser.in_waiting
                    data = ser.read(pending if pending else 1)
                if data:
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip("\r\n")
                        if line.strip():
                            self._handle_line(line.strip())
                    # The SMS "> " prompt arrives with no trailing newline.
                    if buf.strip() == ">":
                        buf = ""
                        self._handle_line(PROMPT)
                else:
                    if buf.strip() == ">":
                        buf = ""
                        self._handle_line(PROMPT)
            except serial.SerialException as exc:
                self.last_error = str(exc)
                self.connected = False
                time.sleep(2)
                try:
                    self.open()
                except Exception:
                    pass
            except Exception as exc:                     # keep the thread alive
                self.last_error = str(exc)
                time.sleep(0.5)

    def _handle_line(self, line):
        # A URC payload can span several lines: a USSD menu arrives as one
        # +CUSD: with embedded newlines inside the quoted string. Splitting on
        # newlines fragments it, so an unterminated quote is accumulated until
        # the quotes balance and only then dispatched.
        if self._pending_urc is not None:
            self._pending_urc += "\n" + line
            if self._pending_urc.count('"') % 2 == 0:
                full, self._pending_urc = self._pending_urc, None
                self._dispatch_urc(full)
            elif len(self._pending_urc) > 4096:      # runaway guard
                full, self._pending_urc = self._pending_urc, None
                self._dispatch_urc(full)
            return

        if line != PROMPT and line.startswith(_ASYNC_PREFIX):
            if line.count('"') % 2 == 1:
                self._pending_urc = line
                return
            self._dispatch_urc(line)
            return
        if self._in_command.is_set():
            self._resp_q.put(line)
        elif line != "OK":
            # Outside a command, "NO CARRIER" / "BUSY" / "NO ANSWER" are how the
            # modem reports that a call ended, so they must reach the dispatcher.
            # Only a bare "OK" is noise here.
            self._dispatch_urc(line)

    # --- URC handling -------------------------------------------------------

    def _dispatch_urc(self, line):
        try:
            if line.startswith(("RING", "+CRING:")):
                with self._call_lock:
                    if not self.call:
                        self.call = {
                            "number": "", "direction": "INCOMING",
                            "state": "RINGING", "started": None, "id": None,
                        }
                self.on_event("ring", {"raw": line})

            elif line.startswith("+CLIP:"):
                m = re.search(r'\+CLIP:\s*"([^"]*)"', line)
                number = normalise_number(m.group(1)) if m else ""
                with self._call_lock:
                    if not self.call:
                        self.call = {
                            "number": number, "direction": "INCOMING",
                            "state": "RINGING", "started": None, "id": None,
                        }
                    elif not self.call.get("number"):
                        self.call["number"] = number
                self.on_event("incoming_call", {"number": number})

            elif line.startswith("+CMTI:"):
                m = re.search(r"\+CMTI:\s*\"?([^\",]*)\"?\s*,\s*(\d+)", line)
                if m:
                    self.on_event("sms_stored",
                                  {"storage": m.group(1), "index": int(m.group(2))})

            elif line.startswith("+CMT:"):
                self.on_event("sms_direct", {"raw": line})

            elif line.startswith("+CUSD:"):
                self._ussd_q.put(line)
                self.on_event("ussd", {"raw": line})

            elif line.startswith("+CDS:"):
                self.on_event("sms_status_report", {"raw": line})

            elif line in ("NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"):
                with self._call_lock:
                    call = dict(self.call) if self.call else None
                    duration = 0
                    if call and call.get("started"):
                        duration = int(time.time() - call["started"])
                    self.call = None
                self.on_event("call_ended", {
                    "reason": line, "call": call, "duration": duration,
                })

            else:
                self.on_event("urc", {"raw": line})
        except Exception as exc:
            self.last_error = f"urc dispatch: {exc}"

    def notify_call_ended(self, reason="HANGUP"):
        """Called by the app layer once it has logged the call."""
        with self._call_lock:
            self.call = None
        self.on_event("call_ended", {"reason": reason})

    # --- command execution --------------------------------------------------

    def send(self, command, timeout=10, collect_prompt=False):
        """Run one AT command; return the list of response lines.

        Raises ModemError on ERROR / +CME ERROR / +CMS ERROR or timeout.
        """
        if not self._ser or not self._ser.is_open:
            self.open()

        with self._cmd_lock:
            try:
                return self._send_once(command, timeout, collect_prompt)
            except (serial.SerialException, OSError) as exc:
                # This firmware resets itself on some operations (CSFB, USSD),
                # which invalidates the file descriptor. Reopen through the udev
                # symlink — the tty number may have changed — and retry once.
                self.last_error = str(exc)
                self.connected = False
                try:
                    self._reopen_port()
                except Exception:
                    raise ModemError("modem port lost: %s" % exc)
                return self._send_once(command, timeout, collect_prompt)

    def _reopen_port(self):
        with self._io_lock:
            try:
                if self._ser:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None
        deadline = time.time() + 60
        last = None
        while time.time() < deadline:
            try:
                with self._io_lock:
                    self._ser = serial.Serial(
                        self.port_name, self.baud, timeout=0.3,
                        write_timeout=5, rtscts=False, dsrdtr=False,
                    )
                    self._ser.reset_input_buffer()
                self.connected = True
                self.on_event("modem_reopened", {"port": self.port_name})
                return
            except Exception as exc:
                last = exc
                time.sleep(3)
        raise ModemError("could not reopen %s: %s" % (self.port_name, last))

    def _send_once(self, command, timeout, collect_prompt):
        while not self._resp_q.empty():            # drop stale lines
            try:
                self._resp_q.get_nowait()
            except queue.Empty:
                break

        self._in_command.set()
        try:
            with self._io_lock:
                self._ser.write((command + "\r\n").encode())
                self._ser.flush()
            return self._collect(command, timeout, collect_prompt)
        finally:
            self._in_command.clear()

    def _collect(self, command, timeout, collect_prompt):
        deadline = time.time() + timeout
        lines = []
        cmd_stripped = command.strip()
        while time.time() < deadline:
            try:
                line = self._resp_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if line == PROMPT:
                if collect_prompt:
                    return lines            # caller now writes the payload
                continue
            if line == cmd_stripped:        # command echo, if ATE0 not applied
                continue
            if line in _FINAL_EXACT:
                if line == "OK":
                    return lines
                raise ModemError(f"{command} -> {line}")
            if line.startswith(_FINAL_PREFIX):
                raise ModemError(f"{command} -> {line}")
            lines.append(line)
        raise ModemError(f"{command} -> timeout after {timeout}s")

    def send_raw(self, command, timeout=10):
        """Console helper: never raises, returns (ok, text)."""
        try:
            lines = self.send(command, timeout=timeout)
            return True, "\n".join(lines + ["OK"]) if lines else "OK"
        except ModemError as exc:
            msg = str(exc)
            return False, msg.split("->", 1)[1].strip() if "->" in msg else msg
        except Exception as exc:
            return False, str(exc)

    def _write_payload(self, payload):
        with self._io_lock:
            self._ser.write(payload)
            self._ser.flush()

    # --- configuration ------------------------------------------------------

    def scan_mode(self):
        """Read AT+QCFG="nwscanmode": 0=auto, 1=GSM only, 2=WCDMA only, 3=LTE only."""
        try:
            for line in self.send('AT+QCFG="nwscanmode"', timeout=6):
                m = re.search(r'\+QCFG:\s*"nwscanmode",(\d+)', line)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def ensure_auto_scan_mode(self):
        """Undo a stranded RAT lock.

        A restricted nwscanmode persists in the modem across resets and USB
        re-enumeration. If something pinned it to GSM-only on a network that
        only offers LTE, the modem sits at "No Service" indefinitely and no
        amount of restarting the service helps. Forcing it back to automatic is
        the only recovery, so it is checked on every configure().
        """
        mode = self.scan_mode()
        if mode in (None, 0):
            return {"changed": False, "mode": mode}
        try:
            self.send('AT+QCFG="nwscanmode",0,1', timeout=15)
        except Exception as exc:
            return {"changed": False, "mode": mode, "error": str(exc)}
        return {"changed": True, "was": mode, "mode": 0}

    # --- voice / SMS mode ---------------------------------------------------

    def current_mode(self):
        """Which telephony mode the modem is in, derived from the IMS setting."""
        try:
            for line in self.send('AT+QCFG="ims"', timeout=8):
                m = re.search(r'\+QCFG:\s*"ims",(\d+)(?:,(\d+))?', line)
                if m:
                    ims = int(m.group(1))
                    # Only a forced enable (1) gives VoLTE. Value 0 means
                    # "follow the MBN profile", and ROW_Generic_3GPP leaves IMS
                    # unregistered here, so 0 behaves the same as 2 (disabled):
                    # SMS works, a call would CSFB and reset the modem.
                    return {
                        "mode": MODE_VOICE if ims == 1 else MODE_SMS,
                        "ims_setting": ims,
                        "ims_registered": (int(m.group(2))
                                           if m.group(2) is not None else None),
                    }
        except Exception:
            pass
        return {"mode": None, "ims_setting": None, "ims_registered": None}

    def has_service(self):
        """Is the radio attached to any network at all?

        `AT+QNWINFO` answers `No Service` while the module is still searching.
        This is the question that decides whether a repair is worth attempting: a
        module reset can fix an IMS registration that failed on a network that is
        actually there, but it cannot conjure coverage.

        Returns None when the query itself fails, which callers should read as
        "don't know" rather than "no service" — refusing to act on a failed AT
        command would disable the watchdog exactly when the modem is unwell.
        """
        try:
            for line in self.send("AT+QNWINFO", timeout=6):
                if "+QNWINFO:" not in line:
                    continue
                return "No Service" not in line
        except Exception:
            pass
        return None

    def wait_for_service(self, want="LTE", timeout=120):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                lines = self.send("AT+QNWINFO", timeout=6)
                last = lines[0] if lines else None
                if last and want in last:
                    return last
            except Exception:
                pass
            time.sleep(4)
        return last

    def set_mode(self, mode, timeout=120):
        """Switch between voice (VoLTE) and SMS (IMS off) mode.

        Changing the IMS setting only takes effect after the radio restarts, so
        this cycles AT+CFUN and waits for the LTE attach to come back. Takes
        roughly 20-40 s. LTE data drops briefly and the ec25-lte watchdog
        reconnects it.
        """
        if mode not in _IMS_FOR_MODE:
            raise ModemError("unknown mode %r" % mode)
        current = self.current_mode()

        if current.get("mode") == mode:
            # The setting already matches, but for voice that is not the same as
            # being ready: IMS must actually be registered or a call will CSFB
            # and reset the modem. Wait for it, and prod the radio if it does not
            # come up on its own.
            if mode != MODE_VOICE:
                return {"changed": False, "mode": mode, "network": None}
            if current.get("ims_registered") == 1:
                return {"changed": False, "mode": mode, "network": None,
                        "ims_registered": True}
            registered = self.wait_for_ims(timeout=30)
            if not registered:
                # A soft cycle will not fix this — the IMS PDN needs a module
                # reset to come up.
                self.full_reset()
                self.wait_for_service("LTE", timeout=timeout)
                registered = self.wait_for_ims(timeout=90)
            self.configure_sms_params()
            return {"changed": False, "mode": mode,
                    "network": self.wait_for_service("LTE", timeout=20),
                    "ims_registered": registered,
                    "state": self.current_mode()}

        self.send('AT+QCFG="ims",%d' % _IMS_FOR_MODE[mode], timeout=15)
        # A full module reset is used in both directions. A soft AT+CFUN=0/1
        # cycle is not enough: entering voice mode it leaves the IMS PDN down so
        # IMS never registers, and leaving voice mode it does not cleanly
        # deregister IMS, so the submission is still refused (+CEER 6,258).
        self.full_reset()
        network = self.wait_for_service("LTE", timeout=timeout)

        # Entering voice mode is not finished when LTE attaches — IMS registers
        # a little later. Dialling before that completes makes the modem attempt
        # CSFB, which resets its firmware, so wait for the registration.
        registered = None
        if mode == MODE_VOICE:
            registered = self.wait_for_ims(timeout=90)
        else:
            # Confirm IMS really went away; if it is still registered the
            # submission will go over IMS again and be refused.
            deadline = time.time() + 45
            while time.time() < deadline:
                if self.current_mode().get("ims_registered") != 1:
                    break
                time.sleep(5)
            registered = self.current_mode().get("ims_registered") == 1

        self.configure_sms_params()
        return {"changed": True, "mode": mode, "network": network,
                "ims_registered": registered, "state": self.current_mode()}

    def _radio_cycle(self):
        """Detach and reattach the radio so a changed IMS setting takes effect."""
        try:
            self.send("AT+CFUN=0", timeout=25)
            time.sleep(4)
            self.send("AT+CFUN=1", timeout=25)
        except ModemError:
            pass                      # the radio cycle sometimes answers late

    def full_reset(self, settle=25):
        """AT+CFUN=1,1 — reset the module and wait for it to come back.

        Needed when entering voice mode: a soft AT+CFUN=0/1 cycle leaves the IMS
        PDN context deactivated (AT+CGACT? shows cid 2 down, CGPADDR 0.0.0.0), so
        IMS never registers and any call would CSFB and reset the modem anyway.
        A full reset brings the IMS bearer up and IMS registers within seconds.

        The reset re-enumerates USB, so the port is reopened afterwards — via the
        udev symlink, since the tty number can change.
        """
        try:
            self.send("AT+CFUN=1,1", timeout=20)
        except Exception:
            pass
        with self._io_lock:
            try:
                if self._ser:
                    self._ser.close()
            except Exception:
                pass
            self._ser = None
        self.connected = False
        time.sleep(settle)
        self._reopen_port()
        try:
            self.send("ATE0", timeout=8)
            self.send("AT+CMEE=2", timeout=8)
        except Exception:
            pass
        # AT+QPCMV does not survive the reset; without this, call recording
        # would capture silence after every mode switch.
        self.enable_usb_audio()

    def wait_for_ims(self, timeout=90):
        """Wait until IMS reports registered. Returns True/False."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.current_mode().get("ims_registered") == 1:
                return True
            time.sleep(5)
        return False

    def cs_registered(self):
        """Is the circuit-switched domain attached? Required for a CSFB call."""
        try:
            for line in self.send("AT+CREG?", timeout=6):
                m = re.search(r"\+CREG:\s*\d+,(\d+)", line)
                if m:
                    return int(m.group(1)) in (1, 5)
        except Exception:
            pass
        return False

    def voice_ready(self):
        """Can a call be placed right now, and over which path?

        Two working paths, established by measurement on a stable supply:

          volte  IMS registered — the call stays on LTE, far end rings in ~2.5 s
          csfb   IMS off but the CS domain is attached — the modem falls back to
                 GSM for the call and returns to LTE straight after, far end
                 rings in ~6.5 s

        CSFB was previously believed to reset the modem. It does not; those
        resets were USB brownouts from a shared hub. With adequate power CSFB is
        reliable, which is what makes running with IMS off viable — that mode
        does voice *and* SMS with no mode switching at all.
        """
        state = self.current_mode()
        if state.get("ims_setting") == 1 and state.get("ims_registered") == 1:
            return True, dict(state, path="volte")
        if self.cs_registered():
            return True, dict(state, path="csfb")
        return False, dict(state, path=None)

    def enable_usb_audio(self):
        """Route call audio to the modem's USB sound card.

        UAC itself is enabled in NV (AT+QCFG="usbcfg" last flag = 1) and survives
        resets, but AT+QPCMV is volatile — it reverts to 0 on every module reset,
        and mode switching resets the module. Without re-applying it, recording
        silently yields digital silence, so it is asserted on every configure().
        """
        try:
            lines = self.send("AT+QPCMV?", timeout=8)
            if any("1,2" in l.replace(" ", "") for l in lines):
                return {"changed": False, "routed": True}
        except Exception:
            pass
        try:
            self.send("AT+QPCMV=1,2", timeout=10)
            return {"changed": True, "routed": True}
        except Exception as exc:
            return {"changed": False, "routed": False, "error": str(exc)}

    def audio_state(self):
        out = {}
        for cmd, key in (("AT+QPCMV?", "qpcmv"), ('AT+QCFG="usbcfg"', "usbcfg"),
                         ("AT+CLVL?", "volume")):
            try:
                lines = self.send(cmd, timeout=8)
                out[key] = lines[0] if lines else None
            except Exception:
                out[key] = None
        out["uac_enabled"] = bool(out.get("usbcfg")
                                  and out["usbcfg"].rstrip().endswith(",1"))
        out["routed_to_usb"] = bool(out.get("qpcmv")
                                    and "1,2" in out["qpcmv"].replace(" ", ""))
        return out

    def sms_domain_pref(self):
        """Read the NV item that decides whether SMS goes over IMS or NAS."""
        try:
            for line in self.send('AT+QNVFR="%s"' % NV_SMS_DOMAIN_PREF, timeout=10):
                m = re.search(r"\+QNVFR:\s*([0-9A-Fa-f]+)", line)
                if m:
                    return m.group(1)
        except Exception:
            pass
        return None

    def ensure_sms_domain(self):
        """Force SMS onto the CS/NAS domain so it works while VoLTE is registered.

        Returns a report rather than raising: on a module without QNVFW this is
        simply unavailable, and the stack still works via mode switching.
        """
        current = self.sms_domain_pref()
        if current is None:
            return {"supported": False, "current": None}
        if current.lower() == SMS_DOMAIN_PREF.lower():
            return {"supported": True, "current": current, "changed": False}
        try:
            self.send('AT+QNVFW="%s",%s' % (NV_SMS_DOMAIN_PREF, SMS_DOMAIN_PREF),
                      timeout=15)
        except Exception as exc:
            return {"supported": True, "current": current, "changed": False,
                    "error": str(exc)}
        # The write only takes effect after the module restarts.
        return {"supported": True, "was": current, "current": SMS_DOMAIN_PREF,
                "changed": True, "needs_reset": True}

    def configure_sms_params(self):
        """Re-apply the SMS parameters, which a radio cycle can clear."""
        for cmd in ["AT+CMGF=1", 'AT+CSCS="GSM"',
                    'AT+CSCA="%s",%d' % (DIGI["smsc"], DIGI["smsc_type"]),
                    "AT+CSMP=%s" % DIGI["csmp"],
                    'AT+CPMS="%s","%s","%s"' % ((DIGI["storage"],) * 3),
                    "AT+CNMI=2,1,0,0,0",
                    "AT+CGSMS=%d" % DIGI["cgsms"]]:
            try:
                self.send(cmd, timeout=8)
            except Exception:
                pass

    def configure(self):
        """Apply the DIGI Romania profile. Returns a per-step report."""
        steps = [
            ("ATE0", "echo off"),
            ("AT+CMEE=2", "verbose errors"),
            ('AT+QCFG="nwscanmode",0,1', "allow all RATs (avoid GSM stranding)"),
            ('AT+QURCCFG="urcport","usbat"', "URCs on this AT port"),
            ("AT+CLIP=1", "caller ID"),
            ("AT+COLP=0", "no connected-line presentation"),
            ("AT+CMGF=1", "SMS text mode"),
            ('AT+CSCS="GSM"', "GSM charset"),
            (f'AT+CSCA="{DIGI["smsc"]}",{DIGI["smsc_type"]}', "DIGI SMSC"),
            (f'AT+CSMP={DIGI["csmp"]}', "SMS parameters"),
            (f'AT+CPMS="{DIGI["storage"]}","{DIGI["storage"]}","{DIGI["storage"]}"',
             "message storage"),
            ("AT+CNMI=2,1,0,0,0", "new-message indications"),
            (f'AT+CGSMS={DIGI["cgsms"]}', "SMS over CS preferred"),
            ("AT+QPCMV=1,2", "call audio to the USB sound card"),
        ]
        # The NV item is what actually decides the SMS domain; AT+CGSMS above is
        # kept for completeness but does not write through to it.
        nv = self.ensure_sms_domain()
        report = [{"command": "nv:sms_domain_pref", "purpose":
                   "SMS over NAS so it works while VoLTE is registered",
                   "ok": nv.get("supported", False), **nv}]
        for cmd, why in steps:
            try:
                self.send(cmd, timeout=6)
                report.append({"command": cmd, "purpose": why, "ok": True})
            except Exception as exc:
                report.append({"command": cmd, "purpose": why, "ok": False,
                               "error": str(exc)})
        return report

    # --- status -------------------------------------------------------------

    def status(self):
        out = {
            "port": self.port_name,
            "connected": self.connected,
            "last_error": self.last_error,
        }

        def one(cmd, timeout=5):
            try:
                return self.send(cmd, timeout=timeout)
            except Exception:
                return []

        info = one("ATI")
        out["model"] = " ".join(l for l in info if not l.startswith("Revision")) or None
        rev = next((l for l in info if l.startswith("Revision:")), "")
        out["firmware"] = rev.replace("Revision:", "").strip() or None

        for line in one("AT+CPIN?"):
            m = re.search(r"\+CPIN:\s*(\S+)", line)
            if m:
                out["sim"] = m.group(1)

        for line in one("AT+COPS?"):
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)",(\d+)', line)
            if m:
                out["operator"] = m.group(1)
                out["access_tech"] = {
                    "0": "GSM", "2": "UTRAN", "3": "GSM/EGPRS",
                    "4": "UTRAN/HSDPA", "5": "UTRAN/HSUPA",
                    "6": "UTRAN/HSDPA+HSUPA", "7": "LTE",
                }.get(m.group(2), m.group(2))

        for line in one("AT+CSQ"):
            m = re.search(r"\+CSQ:\s*(\d+),(\d+)", line)
            if m:
                rssi = int(m.group(1))
                if rssi == 99:
                    out["rssi_dbm"] = None
                    out["signal_percent"] = 0
                else:
                    out["rssi_dbm"] = -113 + 2 * rssi
                    out["signal_percent"] = min(100, round(rssi / 31 * 100))
                ber = int(m.group(2))
                out["ber"] = None if ber == 99 else ber

        for line in one("AT+CEREG?"):
            m = re.search(r"\+CEREG:\s*\d+,(\d+)", line)
            if m:
                out["registration"] = {
                    "0": "not registered", "1": "registered (home)",
                    "2": "searching", "3": "denied", "4": "unknown",
                    "5": "registered (roaming)",
                }.get(m.group(1), m.group(1))

        for line in one("AT+QNWINFO"):
            m = re.search(r'\+QNWINFO:\s*"([^"]*)","([^"]*)","([^"]*)",(\d+)', line)
            if m:
                out["network"] = {
                    "tech": m.group(1), "operator_code": m.group(2),
                    "band": m.group(3), "channel": int(m.group(4)),
                }

        for line in one("AT+CNUM"):
            m = re.search(r'\+CNUM:\s*"[^"]*","([^"]*)"', line)
            if m:
                out["own_number"] = m.group(1)

        out["activity"] = self.phone_activity()
        mode = self.scan_mode()
        out["scan_mode"] = {
            "code": mode,
            "text": {0: "automatic", 1: "GSM only", 2: "WCDMA only",
                     3: "LTE only"}.get(mode, "unknown"),
        }
        with self._call_lock:
            out["active_call"] = dict(self.call) if self.call else None
        out.update(self.radio_detail())
        return out

    # Serving-cell detail costs two extra AT round trips, so it is cached briefly
    # and skipped outright during a call: the modem is fragile under load, and a
    # gate-opening call must never queue behind a diagnostic query.
    _RADIO_TTL = 20.0

    def radio_detail(self):
        """RSRP/RSRQ/SINR, serving-cell identity, and module temperature.

        `AT+QCSQ` is deliberately not used. On this firmware it returns
        `+QCSQ: "LTE",80,-106,122,-15` — a positive RSSI and a SINR of 122, both
        scaled encodings whose formulas are not documented for this model.
        `AT+QENG="servingcell"` reports the same quantities in plain dBm/dB, and
        its RSSI agrees exactly with `AT+CSQ` (17 -> -79 dBm), so it is the one
        that can still be trusted when someone reads the graph a year from now.
        """
        now = time.time()
        cached = getattr(self, "_radio_cache", None)
        with self._call_lock:
            in_call = bool(self.call)
        if cached and (in_call or now - cached[0] < self._RADIO_TTL):
            return cached[1]

        def num(s):
            return int(s) if s.lstrip("-").isdigit() else None

        out = {"signal": None, "serving_cell": None, "modem_temp_c": None}
        try:
            for line in self.send('AT+QENG="servingcell"', timeout=6):
                if not line.startswith("+QENG:"):
                    continue
                f = [p.strip().strip('"') for p in line[6:].split(",")]
                # f: servingcell,state,rat,duplex,mcc,mnc,cellid,pcid,earfcn,
                #    band,ul_bw,dl_bw,tac,rsrp,rsrq,rssi,sinr,cqi
                if len(f) < 18 or f[2] != "LTE":
                    # GSM and WCDMA use an entirely different field layout. Report
                    # the mode and the raw line rather than mislabelling values.
                    out["serving_cell"] = {"rat": f[2] if len(f) > 2 else None,
                                           "raw": line}
                    break
                out["signal"] = {
                    "rsrp_dbm": num(f[13]), "rsrq_db": num(f[14]),
                    "rssi_dbm": num(f[15]), "sinr_db": num(f[16]),
                    "cqi": num(f[17]),
                }
                out["serving_cell"] = {
                    "rat": f[2], "duplex": f[3], "mcc": f[4], "mnc": f[5],
                    "cell_id": f[6], "pci": num(f[7]), "earfcn": num(f[8]),
                    "band": num(f[9]), "tac": f[12], "state": f[1],
                }
                break
        except Exception:
            pass

        try:
            for line in self.send("AT+QTEMP", timeout=6):
                m = re.search(r"\+QTEMP:\s*(-?\d+),(-?\d+),(-?\d+)", line)
                if m:
                    temps = [int(g) for g in m.groups()]
                    # PA, PMIC and crystal. The PA runs hottest and is what
                    # throttles, so that is the figure worth alerting on.
                    out["modem_temp_c"] = max(temps)
                    out["modem_temps_c"] = temps
                    break
        except Exception:
            pass

        self._radio_cache = (now, out)
        return out

    def extended_error(self):
        """AT+CEER — the network's own reason for the last failure.

        Far more useful than the generic "+CMS ERROR: unknown error": cause 29
        is "facility rejected", i.e. the network refusing the service outright.
        """
        try:
            for line in self.send("AT+CEER", timeout=8):
                m = re.search(r"\+CEER:\s*(\d+),(-?\d+)", line)
                if m:
                    cause = int(m.group(2))
                    return {
                        "category": int(m.group(1)),
                        "cause": cause,
                        "text": _CEER_CAUSES.get(cause),
                        "raw": line,
                    }
        except Exception:
            pass
        return None

    def band_config(self):
        """Report the band mask — confirms the EMEA profile without changing it."""
        try:
            lines = self.send('AT+QCFG="band"', timeout=6)
            return lines[0] if lines else None
        except Exception as exc:
            return f"error: {exc}"

    # --- voice --------------------------------------------------------------

    def dial(self, number, force=False):
        num = normalise_number(number)
        if not num:
            raise ModemError("no number given")
        ready, state = self.voice_ready()
        if not ready and not force:
            raise ModemError(
                "not ready for voice: neither IMS nor the CS domain is "
                "available (ims setting=%s registered=%s). Wait for the modem "
                "to attach, or switch to voice mode for VoLTE."
                % (state.get("ims_setting"), state.get("ims_registered")))
        self.send(f"ATD{num};", timeout=20)
        with self._call_lock:
            self.call = {
                "number": num, "direction": "OUTGOING", "state": "DIALING",
                "started": time.time(), "id": None,
            }
        return num

    def answer(self):
        self.send("ATA", timeout=15)
        with self._call_lock:
            if self.call:
                self.call["state"] = "ACTIVE"
                self.call["started"] = self.call.get("started") or time.time()

    def hangup(self):
        duration = 0
        with self._call_lock:
            if self.call and self.call.get("started"):
                duration = int(time.time() - self.call["started"])
        try:
            self.send("AT+CHUP", timeout=10)
        except ModemError:
            self.send("ATH", timeout=10)     # fallback for stubborn states
        with self._call_lock:
            self.call = None
        return duration

    def send_dtmf(self, digits):
        for d in str(digits):
            if d.strip():
                self.send(f'AT+VTS="{d}"', timeout=6)

    def call_status(self):
        """Parse AT+CLCC into a list of *voice* calls.

        On this firmware +CLCC also lists active PDP contexts as calls with
        <mode>=1 (data) — with the QMI data session up that is two phantom
        entries — so anything but <mode>=0 (voice) is filtered out.
        """
        try:
            lines = self.send("AT+CLCC", timeout=6)
        except ModemError:
            return []
        states = {0: "ACTIVE", 1: "HELD", 2: "DIALING", 3: "ALERTING",
                  4: "INCOMING", 5: "WAITING"}
        calls = []
        for line in lines:
            m = re.search(
                r"\+CLCC:\s*(\d+),(\d+),(\d+),(\d+),(\d+)(?:,\"([^\"]*)\")?", line
            )
            if not m:
                continue
            if m.group(4) != "0":          # 0=voice, 1=data, 2=fax
                continue
            calls.append({
                "index": int(m.group(1)),
                "direction": "INCOMING" if m.group(2) == "1" else "OUTGOING",
                "state": states.get(int(m.group(3)), m.group(3)),
                "number": normalise_number(m.group(6) or ""),
            })

        if not calls:
            self._reconcile_ended_call()
        return calls

    def _reconcile_ended_call(self):
        """Close out a call the modem no longer has but we still think is up.

        Call state was originally driven purely by URCs, which is not reliable:
        a VoLTE call ended by the far end does not always produce NO CARRIER, and
        the tracked call then stays ACTIVE forever — blocking the dialler and
        leaving the register wrong. The modem is authoritative, so whenever it
        reports no voice call, any tracked call is closed here instead.
        """
        with self._call_lock:
            stale = dict(self.call) if self.call else None
        if not stale:
            return
        activity = self.phone_activity()
        if activity and activity.get("code") not in (0, None):
            return                       # modem still busy — not stale after all
        with self._call_lock:
            if not self.call:
                return                   # someone else cleared it first
            self.call = None
        duration = (int(time.time() - stale["started"])
                    if stale.get("started") else 0)
        self.on_event("call_ended", {
            "reason": "remote hangup", "call": stale, "duration": duration,
            "reconciled": True,
        })

    def phone_activity(self):
        """AT+CPAS — authoritative voice activity, unaffected by data bearers."""
        try:
            lines = self.send("AT+CPAS", timeout=6)
        except ModemError:
            return None
        for line in lines:
            m = re.search(r"\+CPAS:\s*(\d+)", line)
            if m:
                return {
                    "code": int(m.group(1)),
                    "text": {0: "ready", 1: "unavailable", 2: "unknown",
                             3: "ringing", 4: "call in progress",
                             5: "asleep"}.get(int(m.group(1)), m.group(1)),
                }
        return None

    # --- SMS ----------------------------------------------------------------

    def send_sms(self, number, text, auto_mode=False, restore_mode=True):
        """Send one SMS. Uses GSM text mode, falling back to UCS2 when needed.

        With sms_domain_pref set to CS (see ensure_sms_domain) this works while
        IMS is registered, so no mode switching is needed and auto_mode defaults
        to off. It is kept as an escape hatch for a module whose NV cannot be
        written, where SMS still requires IMS to be off.
        """
        num = normalise_number(number)
        if not num:
            raise ModemError("no recipient given")
        if not text:
            raise ModemError("empty message")

        switched_from = None
        if auto_mode:
            state = self.current_mode()
            if state.get("mode") and state["mode"] != MODE_SMS:
                switched_from = state["mode"]
                self.set_mode(MODE_SMS)
        try:
            return self._send_sms_now(num, text)
        finally:
            if switched_from and restore_mode:
                try:
                    self.set_mode(switched_from)
                except Exception:
                    pass

    def _send_sms_now(self, num, text):
        # A module reset (AT+CFUN=1,1 — which every mode switch performs) drops
        # the modem back to PDU mode, and AT+CMGS then fails with "invalid PDU
        # mode parameter". Assert the text-mode settings on every send rather
        # than trusting that configure() ran since the last reset.
        for attempt in range(4):
            try:
                self.send("AT+CMGF=1", timeout=8)
                lines = self.send("AT+CMGF?", timeout=8)
                if any("+CMGF: 1" in l for l in lines):
                    break
            except Exception:
                pass
            time.sleep(1.5)
        else:
            raise ModemError("could not put the modem into text mode (CMGF=1); "
                             "it is still in PDU mode after a reset")
        for cmd in ('AT+CSCS="GSM"',
                    'AT+CSCA="%s",%d' % (DIGI["smsc"], DIGI["smsc_type"])):
            try:
                self.send(cmd, timeout=8)
            except Exception:
                pass

        unicode_mode = not is_gsm7(text)
        with self._cmd_lock:
            try:
                if unicode_mode:
                    # In UCS2 charset the recipient must be hex-encoded too.
                    self.send('AT+CSCS="UCS2"', timeout=6)
                    self.send("AT+CSMP=17,167,0,8", timeout=6)
                    target = to_ucs2_hex(num)
                    payload = to_ucs2_hex(text)
                else:
                    target = num
                    payload = text

                self.send(f'AT+CMGS="{target}"', timeout=15, collect_prompt=True)
                self._in_command.set()
                try:
                    self._write_payload(payload.encode("ascii", errors="replace")
                                        + b"\x1a")
                    lines = self._collect("AT+CMGS payload", 60, False)
                finally:
                    self._in_command.clear()

                ref = None
                for line in lines:
                    m = re.search(r"\+CMGS:\s*(\d+)", line)
                    if m:
                        ref = int(m.group(1))
                return {"reference": ref, "unicode": unicode_mode,
                        "recipient": num}
            finally:
                if unicode_mode:
                    try:
                        self.send('AT+CSCS="GSM"', timeout=6)
                        self.send(f'AT+CSMP={DIGI["csmp"]}', timeout=6)
                    except Exception:
                        pass

    def read_messages(self, which="ALL"):
        """List stored messages via AT+CMGL. Returns dicts, newest last."""
        try:
            lines = self.send(f'AT+CMGL="{which}"', timeout=30)
        except ModemError:
            return []

        messages = []
        current = None
        header = re.compile(
            r'\+CMGL:\s*(\d+),"([^"]*)","([^"]*)",(?:"([^"]*)")?,"([^"]*)"'
        )
        for line in lines:
            m = header.match(line)
            if m:
                if current:
                    messages.append(current)
                stat = m.group(2)
                current = {
                    "index": int(m.group(1)),
                    "status": stat,
                    "number": normalise_number(decode_maybe_ucs2(m.group(3))),
                    "timestamp": m.group(5),
                    "direction": "OUT" if "SENT" in stat.upper() else "IN",
                    "is_read": 0 if "UNREAD" in stat.upper() else 1,
                    "body": "",
                }
            elif current is not None:
                current["body"] = (current["body"] + "\n" + line).strip() \
                    if current["body"] else line
        if current:
            messages.append(current)

        for msg in messages:
            msg["body"] = decode_maybe_ucs2(msg["body"])
            msg["ts_iso"] = _cmgl_ts_to_iso(msg.get("timestamp"))
        return messages

    def read_message(self, index):
        try:
            lines = self.send(f"AT+CMGR={index}", timeout=15)
        except ModemError:
            return None
        head = next((l for l in lines if l.startswith("+CMGR:")), None)
        if not head:
            return None
        m = re.search(
            r'\+CMGR:\s*"([^"]*)","([^"]*)",(?:"([^"]*)")?,"([^"]*)"', head
        )
        body_lines = [l for l in lines if not l.startswith("+CMGR:")]
        body = decode_maybe_ucs2("\n".join(body_lines).strip())
        if not m:
            return {"index": index, "body": body}
        stat = m.group(1)
        return {
            "index": index,
            "status": stat,
            "number": normalise_number(decode_maybe_ucs2(m.group(2))),
            "timestamp": m.group(4),
            "ts_iso": _cmgl_ts_to_iso(m.group(4)),
            "direction": "OUT" if "SENT" in stat.upper() else "IN",
            "is_read": 0 if "UNREAD" in stat.upper() else 1,
            "body": body,
        }

    def delete_message(self, index):
        self.send(f"AT+CMGD={index}", timeout=15)

    # --- USSD ---------------------------------------------------------------

    def send_ussd(self, code, timeout=45):
        """Send a USSD code and wait for the asynchronous +CUSD reply."""
        while not self._ussd_q.empty():
            try:
                self._ussd_q.get_nowait()
            except queue.Empty:
                break

        self.send(f'AT+CUSD=1,"{code}",15', timeout=15)
        try:
            raw = self._ussd_q.get(timeout=timeout)
        except queue.Empty:
            raise ModemError(f"no USSD response within {timeout}s")

        m = re.search(r'\+CUSD:\s*(\d+)(?:,"(.*)",\s*(\d+))?', raw, re.S)
        if not m:
            return {"raw": raw, "text": raw, "status": None}
        status = int(m.group(1))
        text = m.group(2) or ""
        dcs = int(m.group(3)) if m.group(3) else None
        if dcs in (72, 8) or looks_like_ucs2_hex(text):
            text = decode_maybe_ucs2(text)
        return {
            "raw": raw,
            "text": text,
            "status": status,
            "dcs": dcs,
            "session_open": status == 1,
            "status_text": {
                0: "completed", 1: "further action required", 2: "terminated",
                3: "local client responded", 4: "not supported",
                5: "network timeout",
            }.get(status, str(status)),
        }

    def cancel_ussd(self):
        self.send("AT+CUSD=2", timeout=10)

    # --- SIM phonebook ------------------------------------------------------

    def sim_phonebook(self):
        """Read the SIM phonebook (for import into the local one)."""
        try:
            self.send('AT+CPBS="SM"', timeout=6)
            info = self.send("AT+CPBS?", timeout=6)
            total = 250
            for line in info:
                m = re.search(r'\+CPBS:\s*"[^"]*",\d+,(\d+)', line)
                if m:
                    total = int(m.group(1))
            lines = self.send(f"AT+CPBR=1,{total}", timeout=30)
        except ModemError:
            return []
        entries = []
        for line in lines:
            m = re.search(r'\+CPBR:\s*(\d+),"([^"]*)",(\d+),"([^"]*)"', line)
            if m:
                entries.append({
                    "index": int(m.group(1)),
                    "number": normalise_number(m.group(2)),
                    "name": decode_maybe_ucs2(m.group(4)),
                })
        return entries


def _cmgl_ts_to_iso(ts):
    """'26/08/14,16:30:12+12' -> '2026-08-14T16:30:12'."""
    if not ts:
        return None
    m = re.match(r"(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})", ts)
    if not m:
        return None
    yy, mm, dd, hh, mi, ss = m.groups()
    return f"20{yy}-{mm}-{dd}T{hh}:{mi}:{ss}"
