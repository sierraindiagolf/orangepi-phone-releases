# Fibocom FG621-EA evaluation

Evaluated as a replacement for the Quectel EC25-E, to remove the ~75 s voice/SMS
mode switch. **Conclusion: not a replacement — it cannot place calls.** Keep the
EC25.

```
Module    Fibocom FG621-EA          2cb7:0a07
Firmware  16121.1034.00.01.01.09
SIM       the same DIGI SIM         IMSI 226050073421577, registered LTE
```

## Result

| | EC25-E | FG621-EA |
|---|---|---|
| LTE data | QMI, works | MBIM, works |
| SMS | works, but needs a ~75 s mode switch | **works instantly, no switching** |
| Voice calls | works over VoLTE | **fails** — no IMS *and* no 2G radio |
| Call audio (record / inject) | works via UAC | no audio interface exposed |

The FG621 fixes the SMS annoyance completely and loses the phone. For a project
whose core is a dialer, call register and call audio, that is a bad trade.

## Why voice fails

Calls reach `+CLCC` state 2 (DIALING), never ALERTING, and end with:

```
+CEER: "CC setup error",34,"no circuit channel available"
```

There is no CS channel and IMS never registers, so there is no voice path at all.
**Both** are confirmed, which is what makes this final:

* **No 2G radio.** Forced to GSM only (`AT+GTACT=1`) it reports `No Service` for
  two minutes with `+CREG: 0,0`, and a network scan in that mode returns an empty
  list `+COPS: (,,,,)`. The EC25 on the same SIM and site *does* fall back to
  GSM 900, so the network has 2G — this module cannot use it. So no CSFB target.
* **No IMS registration**, below. So no VoLTE either. IMS gets remarkably close but does not complete:

* `AT+PCSCF?` returns `10.13.87.6` — the network **does** offer IMS to this SIM
* the IMS PDN activates and gets an address once created and forced up
  (`AT+CGDCONT=2,"IPV4V6","ims"` then `AT+CGACT=1,2` → `10.51.8.245`)
* `AT+CISRVCC?` reports `1`
* but `AT+CIREG?` stays `2,0,0` — SIP registration never completes

Tried without success: `AT+IMSEN=1`, `AT+CEVDP` at 2 (IMS only), 3 (CS preferred,
the default) and 4 (IMS preferred), `AT+CIREG=2`, defining and manually
activating the IMS APN, and several `AT+CFUN=0/1` cycles. The bearer comes up;
the IMS client does not register. That points at the firmware's VoLTE stack or a
missing carrier configuration, not at anything reachable over AT.

## Useful things learned

**`AT+CEVDP` is the vendor-neutral voice-domain preference** — the standard
equivalent of the EC25's `AT+QCFG="ims"`. Values: 1 = CS only, 2 = IMS only,
3 = CS preferred, 4 = IMS preferred. The FG621 ships at 3, which is why it
attempted CSFB. Worth remembering for any module.

**`AT+GCAP` is far more informative than guessing vendor commands.** This
firmware does not support `AT+CLAC`, but `AT+GCAP` returned the entire supported
command set in one line, which is how `+CEVDP`, `+IMSEN`, `+PCSCF`, `+CISRVCC`
and `+CIREG` were found after `AT+GTIMSCFG`, `AT+XIMSCFG`, `AT+GTVOLTE` and
`AT+IMSCFG` had all come back "not supported".

**Its serial ports assert no carrier.** A plain `serial.Serial()` open blocks
forever. They must be opened `O_NONBLOCK` with `CLOCAL` set — see
`scripts/fibocom_probe.py`. This cost some time and looked like "the modem does
not answer AT" at first.

**The `option.ko` built for the EC25 also drives this module** — it claimed all
four vendor-specific interfaces unprompted, with `cdc_mbim` taking the data path.
So `scripts/build-option-driver.sh` is useful beyond the Quectel.

**USB composition is switchable** via `AT+GTUSBMODE` (currently 40, range 34-41,
8 compositions). Not explored — pointless without voice, but one of them might
expose audio if the voice problem were ever solved.

## State left on the module

Changed during evaluation and persisted in the module's NV — reset these if it is
ever reused as a data/SMS device:

```
AT+IMSEN=1
AT+CEVDP=4                      (shipped as 3)
AT+CGDCONT=2,"IPV4V6","ims"     (context 2 did not exist before)
```

## If it were used as an SMS/data gateway

It is a good fit for that: SMS worked first try (`+CMGS: 77`) with the correct
SMSC already set, no IMS involvement and no mode switching, and MBIM data works.
Supporting it would take roughly an hour — the AT layer in `modem.py` is
portable, but every vendor command differs (`AT+QCFG`, `AT+QPCMV`, `AT+QMBNCFG`,
`AT+QNWINFO` have no direct equivalents), and call audio would have to be
dropped.
