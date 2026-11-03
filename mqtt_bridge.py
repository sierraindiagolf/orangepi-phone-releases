"""Publish board and modem telemetry to Home Assistant over MQTT.

Runs as a daemon thread inside the Flask process, and it has to: `app.py` owns
the serial port through a single reader thread, so a separate process could not
query the modem at all without fighting it for the port.

Design notes:

* **One retained JSON state topic**, with each entity extracting its own field
  via a discovery-time `value_template`. Publishing ~30 separate topics per cycle
  would be 30 round trips for data that is gathered in one pass anyway.
* **Discovery config is retained**, so entities survive a Home Assistant restart
  without waiting for the next publish.
* **A Last Will marks the board offline.** This matters more here than on a
  typical sensor: the modem has a documented crash loop, and a dashboard showing
  a stale but plausible signal reading is worse than one showing "unavailable".
* Publishing reuses whatever `app.py` already gathers for `/api/status`, so it
  adds no extra AT traffic beyond its own cycle, and the modem layer skips the
  expensive serving-cell query while a call is up.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("mqtt")

ENV_FILE = os.environ.get("PHONE_MQTT_ENV", "/etc/iris-mqtt.env")

DEVICE_ID = os.environ.get("PHONE_MQTT_ID", "iris")
DISCOVERY_PREFIX = os.environ.get("PHONE_MQTT_DISCOVERY", "homeassistant")
BASE = f"{DEVICE_ID}"
STATE_TOPIC = f"{BASE}/state"
AVAIL_TOPIC = f"{BASE}/availability"
INTERVAL = float(os.environ.get("PHONE_MQTT_INTERVAL", "30"))

# name, value_template source, unit, device_class, state_class, icon[, expr]
#
# `None` unit means a plain text sensor. The optional 7th field is a Jinja
# expression over `v` (the extracted value) for entities whose stored form is not
# their display form — bytes being the obvious case. Conversion happens here
# rather than in the API so `/api/status` keeps raw, unrounded values.
SENSORS = [
    # --- radio ---
    ("Operator",          "modem.operator",              None,  None, None, "mdi:sim"),
    ("Access technology", "modem.access_tech",           None,  None, None, "mdi:radio-tower"),
    ("Band",              "modem.serving_cell.band",     None,  None, None, "mdi:radio-tower"),
    ("Signal",            "modem.signal_percent",        "%",   None, "measurement", "mdi:signal"),
    ("RSSI",              "modem.signal.rssi_dbm",       "dBm", "signal_strength", "measurement", None),
    ("RSRP",              "modem.signal.rsrp_dbm",       "dBm", "signal_strength", "measurement", None),
    ("RSRQ",              "modem.signal.rsrq_db",        "dB",  None, "measurement", "mdi:signal-distance-variant"),
    ("SINR",              "modem.signal.sinr_db",        "dB",  None, "measurement", "mdi:signal-variant"),
    ("Cell ID",           "modem.serving_cell.cell_id",  None,  None, None, "mdi:transmission-tower"),
    ("EARFCN",            "modem.serving_cell.earfcn",   None,  None, None, "mdi:tune"),
    ("Registration",      "modem.registration",          None,  None, None, "mdi:check-network"),
    ("SIM",               "modem.sim",                   None,  None, None, "mdi:sim"),
    ("Mode",              "voice_state.mode",            None,  None, None, "mdi:swap-horizontal"),
    ("Modem temperature", "modem.modem_temp_c",          "°C",  "temperature", "measurement", None),
    ("LTE IP",            "lte.ip",                      None,  None, None, "mdi:ip-network"),

    # --- host ---
    ("CPU temperature",   "host.cpu_thermal_c",          "°C",  "temperature", "measurement", None),
    ("GPU temperature",   "host.gpu_thermal_c",          "°C",  "temperature", "measurement", None),
    ("Load 1m",           "host.load_1m",                None,  None, "measurement", "mdi:gauge"),
    ("Load 5m",           "host.load_5m",                None,  None, "measurement", "mdi:gauge"),
    ("Load 15m",          "host.load_15m",               None,  None, "measurement", "mdi:gauge"),
    ("Memory used",       "host.memory_used_percent",    "%",   None, "measurement", "mdi:memory"),
    ("CPU frequency",     "host.cpu_mhz",                "MHz", "frequency", "measurement", None),
    ("Uptime",            "host.uptime_seconds",         "s",   "duration", "measurement", None),
    ("Disk free",         "storage.free_bytes",          "GB",  "data_size", "measurement", None,
     "(v / 1073741824) | round(1)"),
    ("Disk used",         "storage.used_bytes",          "GB",  "data_size", "measurement", None,
     "(v / 1073741824) | round(1)"),
    ("Disk used percent", "storage.used_percent",        "%",   None, "measurement", "mdi:harddisk"),
    ("Memory available",  "host.memory_available_bytes", "MB",  "data_size", "measurement", None,
     "(v / 1048576) | round(0)"),

    # --- health ---
    ("USB disconnects last hour", "usb.usb_disconnects_last_hour", None, None, "measurement", "mdi:usb"),
    ("Modem enumerations",       "usb.modem_enumerations_since_boot", None, None, "total_increasing", "mdi:usb"),
    ("Unread messages",          "db.unread",           None,  None, "measurement", "mdi:message-badge"),

    # --- storage and system ---
    #
    # The board once served 500s for an hour with a filesystem that had stopped
    # accepting I/O, while every entity above it read perfectly normal. These are
    # the ones that would have said so.
    ("SSD temperature",   "storage.smart.temperature_c",         "°C", "temperature", "measurement", None),
    ("SSD life used",     "storage.smart.life_used_percent",     "%",  None, "measurement", "mdi:harddisk"),
    ("SSD spare",         "storage.smart.available_spare_percent", "%", None, "measurement", "mdi:harddisk-plus"),
    ("SSD media errors",  "storage.smart.media_errors",          None, None, "total_increasing", "mdi:alert-circle"),
    ("Unsafe shutdowns",  "storage.smart.unsafe_shutdowns",      None, None, "total_increasing", "mdi:power-plug-off"),
    ("Filesystem errors", "storage.ext4_errors",                 None, None, "total_increasing", "mdi:file-alert"),
    ("Write probe age",   "storage.write_checked_age_seconds",   "s",  "duration", "measurement", "mdi:pencil-off"),
    ("Failed services",   "system.services.failed",              None, None, None, "mdi:cog-off",
     "(v | join(', ')) if v else 'none'"),
]

# name, value source, device_class, icon, invert — rendered as on/off
#
# `invert` exists because device_class "problem" means ON = something is wrong.
# Publishing `usb.healthy` directly against that class would show a permanent
# false alarm on a perfectly healthy board, so the sense is flipped and the name
# says which way round it is.
BINARY_SENSORS = [
    ("Modem connected",   "modem.connected", "connectivity", None, False),
    ("Voice ready",       "voice_ready",     None, "mdi:phone-check", False),
    ("Call active",       "call_active",     None, "mdi:phone-in-talk", False),
    ("USB power problem", "usb.healthy",     "problem", None, True),

    # `Modem on USB bus` is deliberately separate from `Modem connected`: the
    # latter asks whether the AT port answers, and after the failure of
    # 2026-08-17 there was no port to answer — the hub was enumerated and the
    # module simply was not on the bus. One says "the modem is unwell", the other
    # says "there is no modem", and they call for different repairs.
    ("Modem on USB bus",  "usb.modem_on_bus", "connectivity", None, False),
    ("Storage problem",   "storage.healthy",  "problem", None, True),
    ("Filesystem read-only", "storage.read_only", "problem", None, False),
    ("Service problem",   "system.services.healthy", "problem", None, True),
    ("Clock synchronized", "system.clock_synchronized", None, "mdi:clock-check", False),
]


def _device_block():
    return {
        "identifiers": [DEVICE_ID],
        "name": "iris",
        "manufacturer": "Orange Pi / Quectel",
        "model": "Orange Pi 3 LTS + EC25-E",
        "configuration_url": "https://iris.bavariaduo.ovh/",
    }


def _slug(name):
    return name.lower().replace(" ", "_").replace("-", "_")


def _discovery_payloads():
    """(topic, payload) pairs for every entity, published retained."""
    out = []
    for entry in SENSORS:
        name, path, unit, dev_class, state_class, icon = entry[:6]
        expr = entry[6] if len(entry) > 6 else None
        uid = f"{DEVICE_ID}_{_slug(name)}"
        # `is defined` and the none check keep the entity 'unknown' rather than
        # publishing the literal string "None" when a field has not been reported
        # yet — and, for converted values, stop the arithmetic running on None.
        if expr:
            tmpl = ("{%% set v = value_json.%s if value_json.%s is defined else none %%}"
                    "{{ (%s) if v is not none else none }}" % (path, path, expr))
        else:
            tmpl = "{{ value_json.%s if value_json.%s is defined else none }}" % (path, path)
        cfg = {
            "name": name,
            "unique_id": uid,
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAIL_TOPIC,
            "value_template": tmpl,
            "device": _device_block(),
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dev_class:
            cfg["device_class"] = dev_class
        if state_class:
            cfg["state_class"] = state_class
        if icon:
            cfg["icon"] = icon
        out.append((f"{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{_slug(name)}/config", cfg))

    for name, path, dev_class, icon, invert in BINARY_SENSORS:
        uid = f"{DEVICE_ID}_{_slug(name)}"
        on, off = ("OFF", "ON") if invert else ("ON", "OFF")
        cfg = {
            "name": name,
            "unique_id": uid,
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAIL_TOPIC,
            "payload_on": "ON",
            "payload_off": "OFF",
            "value_template":
                "{{ '%s' if value_json.%s else '%s' }}" % (on, path, off),
            "device": _device_block(),
        }
        if dev_class:
            cfg["device_class"] = dev_class
        if icon:
            cfg["icon"] = icon
        out.append((f"{DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/{_slug(name)}/config", cfg))
    return out


def load_env(path=ENV_FILE):
    """Broker credentials, kept out of the repo and out of the process list."""
    try:
        with open(path) as fh:
            env = dict(
                line.strip().split("=", 1) for line in fh
                if "=" in line and not line.startswith("#")
            )
    except OSError:
        return None
    if not env.get("MQTT_HOST"):
        return None
    return env


class Bridge:
    def __init__(self, status_fn):
        """`status_fn` returns the same dict `/api/status` serves."""
        self.status_fn = status_fn
        self.client = None
        self.env = load_env()
        self.last_error = None
        self.published = 0

    def enabled(self):
        return self.env is not None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if hasattr(reason_code, "is_failure"):
            failed = reason_code.is_failure
        else:
            failed = (reason_code != 0)
        if failed:
            log.warning("mqtt: connect callback failed: %s", reason_code)
            return

        log.info("mqtt: connected to %s, %d entities announced",
                 self.env["MQTT_HOST"], len(_discovery_payloads()))
        try:
            for topic, payload in _discovery_payloads():
                client.publish(topic, json.dumps(payload), retain=True)
            client.publish(AVAIL_TOPIC, "online", retain=True)
        except Exception as exc:
            log.warning("mqtt: failed to publish discovery/availability: %s", exc)

    def _connect(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"{DEVICE_ID}-bridge")
        client.username_pw_set(self.env["MQTT_USER"], self.env["MQTT_PASS"])
        # The broker publishes this if the board stops talking, so entities go
        # 'unavailable' rather than sitting on a stale value.
        client.will_set(AVAIL_TOPIC, "offline", retain=True)
        client.on_connect = self._on_connect
        client.connect(self.env["MQTT_HOST"], int(self.env.get("MQTT_PORT", 1883)), 30)
        client.loop_start()
        self.client = client

    def publish_once(self):
        payload = self.status_fn()
        self.client.publish(STATE_TOPIC, json.dumps(payload, default=str),
                            retain=True)
        self.published += 1

    def run(self):
        backoff = 5
        while True:
            try:
                if self.client is None:
                    self._connect()
                    backoff = 5
                self.publish_once()
                self.last_error = None
                time.sleep(INTERVAL)
            except Exception as exc:
                # Never let a broker outage take the phone API down with it.
                self.last_error = str(exc)
                log.warning("mqtt: %s (retrying in %ss)", exc, backoff)
                try:
                    if self.client:
                        self.client.loop_stop()
                except Exception:
                    pass
                self.client = None
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)


_bridge = None


def start(status_fn):
    """Start the bridge if credentials exist. Returns the Bridge, or None."""
    global _bridge
    bridge = Bridge(status_fn)
    if not bridge.enabled():
        log.info("mqtt: no %s, telemetry publishing disabled", ENV_FILE)
        return None
    _bridge = bridge
    threading.Thread(target=bridge.run, daemon=True, name="mqtt-bridge").start()
    return bridge


def announce_offline(reason="shutting down"):
    """Publish `offline` deliberately, before the board goes down.

    The Last Will covers an unexpected death, but it fires only once the broker
    gives up on the keepalive — up to a minute later, during which the dashboard
    still shows live-looking values for a board that is already gone. Saying so
    on the way out makes a planned stop immediate and unambiguous, which on this
    board is a distinction worth keeping: unexplained power loss is the recurring
    fault here, and "it went away without warning" is evidence.
    """
    if _bridge is None or _bridge.client is None:
        return False
    try:
        info = _bridge.client.publish(AVAIL_TOPIC, "offline", retain=True)
        info.wait_for_publish(timeout=2)
        log.info("mqtt: announced offline (%s)", reason)
        return True
    except Exception as exc:
        log.warning("mqtt: could not announce offline: %s", exc)
        return False


def health():
    if _bridge is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "connected": _bridge.client is not None,
        "published": _bridge.published,
        "last_error": _bridge.last_error,
        "broker": _bridge.env.get("MQTT_HOST"),
        "interval_seconds": INTERVAL,
    }
