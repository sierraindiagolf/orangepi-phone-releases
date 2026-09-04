# Power, the crash loop, and recovering the modem

Everything in this document was learned the hard way on this deployment. Most
"firmware" problems here turned out to be power problems, and one failure mode
cannot be recovered from software at all.

---

## The crash loop

The worst failure looks like the modem being *absent*, but it is not. It is
enumerating and dying, over and over:

```
usb 3-1.4: new high-speed USB device number 13
  ... ttyUSB0-3 + cdc-wdm0 + wwan0 all created successfully ...
usb 3-1.4: USB disconnect, device number 13     <- 0.6s later
  ~12 s gap (the module rebooting itself)
usb 3-1.4: new high-speed USB device number 14
  ... and so on
```

Measured survival times across ten consecutive cycles: **0.32, 0.55, 0.58, 0.60,
0.61, 0.81, 0.82, 0.83, 0.83, 1.11 seconds**. 14 enumerations, 13 disconnects,
100 % failure. No over-current reported, no kernel power complaints.

The shape is diagnostic: enumeration is a low-current operation and always
succeeds; the module dies moments later when the radio initialises and current
demand jumps. The ~12 s gap is the module's own boot time.

### How to recognise it

Not by "the modem is missing" — that is also normal for ~25 s after any
deliberate `AT+CFUN=1,1`. The signature is **many enumerations, none surviving**:

```bash
dmesg | grep -c 'GSM modem (1-port) converter detected'   # climbing fast
dmesg | grep -c 'USB disconnect'                          # climbing with it
```

`GET /api/status` reports both as `modem_enumerations_since_boot` and
`usb_disconnects_since_boot`. Enumerations climbing while the modem never becomes
usable means a crash loop, not a slow start.

Health is judged on **`usb_disconnects_last_hour`**, not on disconnects divided by
uptime. Deliberate re-enumerations at startup make the since-boot average useless:
33 disconnects in the first 11 minutes left this board reading 6.78/h and
"unhealthy" for hours after everything had gone quiet.

### The other shape: absent, not looping

Seen on 2026-08-17 after the board was hard power-cycled. This is *not* the crash
loop and the counters that detect the loop all read zero, because nothing ever
enumerated:

```bash
lsusb | grep 2c7c              # nothing — the hub at 0bda:5411 is there, the modem is not
ls /sys/bus/usb/devices/       # 3-1 present, 3-1.4 absent
dmesg | grep -c 'GSM modem'    # 0 — not "climbing", zero
```

The only thing that reported it was `ec25-bind`, which had exited at boot with
*"no EC25 (2c7c:0125) found on the USB bus"* and then sat `failed` for over an
hour while the web UI answered normally and every other metric looked healthy.
Nothing surfaced that, which is why systemd unit state and a `Modem on USB bus`
entity are now published — see the README's telemetry section.

Distinguishing the two matters because they read identically from the API's point
of view (`modem.connected` is false either way):

| | Crash loop | Absent |
|---|---|---|
| Enumerations | climbing fast | zero |
| `USB disconnect` | climbing with them | zero |
| `3-1.4` in sysfs | appears and vanishes | never appears |

Recovery was the same, though: replugging the module brought it back
immediately — all four `ttyUSB` ports, `cdc-wdm0` and `qmi_wwan` within a second,
and `ec25-bind` + `ec25-lte` then started cleanly. A board reboot alone had not
done it.

### Recovery: only physical power removal works

Confirmed on this board, in this order — everything except the last failed:

| Attempt | Result |
|---|---|
| `AT+CFUN=1,1` | can't — the port dies before a command completes |
| Rebinding the USB device | re-enumerates the same wedged module |
| Board reboot | no effect — the module keeps its state |
| Hub `authorized` 0/1 toggle | no effect |
| Moving between the hub's 3 A and 4 A ports | no effect, identical failure |
| **Physically unplugging the modem** | **recovered** |

The module has to lose power completely. That is the single most important fact
for designing any watchdog: **software cannot fix this.**

### What triggers it

USSD. It forces the CS domain, which drops the modem to GSM, and GSM transmit
bursts are the highest-current state the module ever enters (~577 µs bursts
against a declared `bMaxPower` of 500 mA). USSD has now taken the modem down
twice. It is disabled or diagnostic-only for that reason, not because it doesn't
work — it does, and returns DIGI's full menu.

---

## The underlying problem, and the fix

The modem draws its transmit peaks through the same USB connector that carries
data. A board port, or a hub port shared with other devices, cannot deliver that
cleanly. Evidence from this deployment:

| Arrangement | USB disconnects |
|---|---|
| Hub shared with a charging phone | **61 in one boot** — the *hub itself* dropped, taking the modem with it |
| Hub alone on a dedicated 3.5 A supply | 0 during a full stress test |
| 30 W brick, modem on the 3 A A-port | 0 across 4 CSFB calls, 4 SMS and USSD |

### The recommended wiring: split power from data

The right fix is the one used by old 2.5" HDD enclosures — a **split / Y USB
cable**: the data leg goes to the board, and a separate leg takes power from its
own supply. The modem then draws its transmit peaks from a supply sized for it,
while the board's port only carries D+/D− and ground.

Two details worth getting right:

- **Prefer a data-only leg to the board.** If both legs carry VBUS you are
  paralleling two 5 V sources, which is not ideal — they will fight slightly and
  the lower one sinks current. The clean arrangement is VBUS from the supply only,
  with the board's VBUS not connected.
- **Grounds must be common.** The data leg's ground provides that; do not cut it.

The Mcuzone-style carrier board implements exactly this properly, with a switch:
*"when the switch is facing down, power is supplied through the auxiliary power
supply interface, and the USB-C or 1.25 4p interface is only for communication"*.
That is the same idea done on the PCB instead of in a cable, and it is the
preferred route once that board is available.

### What does not help

- **More watts.** A 60 W USB-C PD brick still only offers **5 V/3 A** — the 60 W
  is at 20 V. Check the 5 V rating specifically.
- **A passive USB splitter for power.** No bulk capacitance, thin shared stem,
  and it cannot negotiate PD, so the source may cap at 1.5 A regardless.
- **Total current headroom alone.** The 180 W unit that failed had enormous
  headroom. The problem is transient voltage sag, not average draw.

If a cheap improvement is wanted without rewiring: a **4700 µF low-ESR capacitor
across 5 V at the modem's input**. A GSM burst is ~577 µs and repeats every
4.6 ms, so the supply only has to provide the average while the capacitor covers
the peak — `C = I·Δt/ΔV` gives ~4300 µF for 1.5 A of shortfall at 0.2 V droop.

---

## Watchdog design (for later)

Not built yet. Recorded so the design work is not repeated.

**Detection** — reuse `usb_health()` in `app.py`, which already counts
enumerations, disconnects and over-current events. A crash loop is *enumerations
climbing while nothing stays alive more than a couple of seconds*. Do not trigger
on absence alone: a deliberate mode switch removes the modem for ~25 s.

**Actuator** — must physically remove power. In order of preference:

1. **HomeAssistant smart plug.** HA already drives this board, so the watchdog can
   ask HA to cycle the modem's supply. No wiring, no extra parts, and HA can watch
   the board itself — which matters, since a wedged board cannot rescue anything.
2. **The carrier board's reset pin.** That board advertises three reset
   interfaces. Wire one to a GPIO — `gpioset` is installed and `/dev/gpiochip0`
   exists — and the module can be reset directly, faster than a power cycle.
3. **A GPIO-driven relay or high-side switch** on the modem's 5 V line. Works
   today, a few euro, but more wiring.
4. **`uhubctl`** — the hub at `3-1` (Realtek `0bda:5411`) does advertise
   `Per-port power switching`. **But this only helps if the modem is bus-powered
   from that port.** Once the modem has its own supply — which is the goal — cutting
   the port's VBUS removes nothing, and the module stays wedged. So this is the
   least useful option precisely *because* the wiring is being fixed.

**Rate limiting is not optional.** A watchdog that power-cycles hardware in a loop
is its own hazard. One attempt, wait, a second attempt, then stop and alert.

---

## Safety rules for this board

**Never unbind USB controllers.** The root filesystem is a USB SSD:

```
/dev/sda3  ->  JMicron 152d:a583, alone on its own root hub
               (which bus number that is varies — see below)
```

Unbinding `ehci-platform` drivers to reset USB took out the controller hosting it,
after which nothing could be exec'd — `sleep`, `lsusb` and `grep` all vanished
mid-script — and the board needed a physical power cycle. The filesystem came back
clean (`Filesystem state: clean`, journal replayed), but there was no reason to
risk it. If a USB reset is genuinely needed, act on the specific device
(`/sys/bus/usb/devices/<id>/authorized`), never on a driver's `bind`/`unbind`.

**The SSD and the modem are on separate controllers. Confirm which is which
every time — never from memory, and never from a bus number.**

Bus numbers are enumeration order, not a property of the hardware. On
2026-08-17 they swapped and swapped back within two hours across a few replugs.
An earlier version of this document stated "the SSD is on Bus 4; the modem's hub
is on Bus 3" as a fact to verify — which for part of that day was exactly
inverted. A pre-flight check that silently reverses is worse than none: you
confirm "the SSD is on Bus 4", act on Bus 3, and hit your own root filesystem.

Ask the kernel instead, using identifiers that do not move:

```bash
readlink -f /sys/block/sda        # .../usb4/4-1/... — the bus holding root, now
lsusb -t                          # topology; 2c7c:0125 is the modem
                                  #           152d:a583 is the root SSD
```

The USB layout is not fixed either. It has been the modem behind a Realtek
RTS5411 hub, and it has been the modem straight into a board port on a Y cable
with no hub present at all. Read the tree; do not assume one.
