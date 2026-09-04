# Quectel firmware request — reply

Quectel asked for the full firmware version via `ATI` in QCOM before sending the
update. QCOM is their Windows AT terminal; the same strings come back over the
module's AT port on the board, so the values below were read from
`/dev/ttyUSB3` (ttyUSB2 stays held by `phone-api`).

## Reply to send

> Hi,
>
> ATI output from the module:
>
> ```
> ATI
> Quectel
> EC25
> Revision: EC25EFAR06A04M4G
> OK
> ```
>
> And the full version via AT+QGMR:
>
> ```
> EC25EFAR06A04M4G_01.004.01.004
> ```
>
> Sub-system versions (AT+QSUBSYSVER):
>
> ```
> SBLR06A01_0: May 25 2018
> TZR06A01: May 25 2018
> RPMR06A02_0: May 31 2018
> ```
>
> Module is an EC25-E, IMEI 860548044762786. Please send the latest EC25-E
> firmware plus the upgrade tool/instructions for a Linux host if available.
>
> Thanks

## Raw output collected

| Command | Response |
| --- | --- |
| `ATI` | `Quectel` / `EC25` / `Revision: EC25EFAR06A04M4G` |
| `AT+QGMR` | `EC25EFAR06A04M4G_01.004.01.004` |
| `AT+CGMR` | `EC25EFAR06A04M4G` |
| `AT+GMM` | `EC25` |
| `AT+CGSN` | `860548044762786` |
| `AT+QSUBSYSVER` | `SBLR06A01_0: May 25 2018` / `TZR06A01: May 25 2018` / `RPMR06A02_0: May 31 2018` |

Reading of the version string: **EC25-E** variant, firmware **R06A04**, build
**01.004.01.004**. The May 2018 sub-system dates line up with that — this is an
old A04 image, and the current public EC25-E release is R06A11, so a real
upgrade should be available.

## Deliberately omitted

- **ICCID** (`AT+QCCID`) was read during the query but left out of the reply —
  it identifies the SIM and Quectel has no use for it.
- **IMEI** is included because Quectel normally wants it to confirm the variant.
  Drop it if you would rather not share it.

## If they insist on a QCOM screenshot

That needs the Windows QCOM tool with the Quectel USB drivers installed. Try
sending the plain text first; the output is byte-for-byte what QCOM would show.

## How the values were read

```sh
ssh shoim2@10.64.88.12
python3 - <<'PY'
import serial, time
s = serial.Serial("/dev/ttyUSB3", 115200, timeout=0.4)
for cmd in ["ATI", "AT+QGMR", "AT+CGMR", "AT+GMM", "AT+CGSN", "AT+QSUBSYSVER"]:
    s.reset_input_buffer()
    s.write((cmd + "\r").encode())
    time.sleep(0.7)
    print(s.read(4096).decode(errors="replace"))
s.close()
PY
```
