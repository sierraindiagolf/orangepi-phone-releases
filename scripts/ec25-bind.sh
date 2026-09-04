#!/bin/bash
# Bind the Quectel EC25-E USB interfaces to the right drivers.
#
# The Armbian sunxi64 kernel (6.18.x) ships no option.ko / usb_wwan.ko, so the
# only way to get AT ports is the generic usbserial driver with an explicit
# vendor/product match. That driver claims *every* vendor-specific interface,
# including interface 4 (QMI), which leaves no data path. So we let it grab
# them all, then hand interface 4 back to qmi_wwan.
#
# Resulting layout: ttyUSB0=DM/diag  ttyUSB1=NMEA  ttyUSB2=AT  ttyUSB3=AT(spare)
#                   cdc-wdm0 + wwan netdev = QMI data
set -u

VID=2c7c
PID=0125
QMI_IFACE_NUM=4

log() { echo "[ec25-bind] $*"; }

modprobe usbserial vendor=0x$VID product=0x$PID 2>/dev/null || true
modprobe qmi_wwan 2>/dev/null || true

# Locate the modem's USB device directory by vendor/product.
usbdev=""
for d in /sys/bus/usb/devices/*/; do
    [ -f "$d/idVendor" ] && [ -f "$d/idProduct" ] || continue
    if [ "$(cat "$d/idVendor")" = "$VID" ] && [ "$(cat "$d/idProduct")" = "$PID" ]; then
        usbdev="$d"
        break
    fi
done

if [ -z "$usbdev" ]; then
    log "ERROR: no EC25 ($VID:$PID) found on the USB bus"
    exit 1
fi

busid=$(basename "$usbdev")
qmi_iface="${busid}:1.${QMI_IFACE_NUM}"
log "found EC25 at $busid, QMI interface $qmi_iface"

# Wait for usbserial to finish claiming interfaces so we don't race it.
for _ in $(seq 1 20); do
    [ -e "/sys/bus/usb/devices/$qmi_iface" ] && break
    sleep 0.25
done

if [ ! -e "/sys/bus/usb/devices/$qmi_iface" ]; then
    log "ERROR: interface $qmi_iface never appeared"
    exit 1
fi

current=$(basename "$(readlink -f "/sys/bus/usb/devices/$qmi_iface/driver" 2>/dev/null)" 2>/dev/null)
log "interface $qmi_iface currently on driver: ${current:-none}"

if [ "$current" = "qmi_wwan" ]; then
    log "already bound to qmi_wwan, nothing to do"
else
    if [ -n "$current" ]; then
        log "unbinding from $current"
        echo -n "$qmi_iface" > "/sys/bus/usb/drivers/$current/unbind" 2>/dev/null || \
            log "WARN: unbind failed"
        sleep 1
    fi
    log "binding to qmi_wwan"
    echo -n "$qmi_iface" > /sys/bus/usb/drivers/qmi_wwan/bind 2>/dev/null || \
        log "WARN: bind failed"
    sleep 2
fi

# Report the outcome.
if [ -e /dev/cdc-wdm0 ]; then
    log "OK: /dev/cdc-wdm0 present"
else
    log "ERROR: /dev/cdc-wdm0 missing after bind"
    exit 1
fi

netdev=$(ls "/sys/bus/usb/devices/$qmi_iface/net/" 2>/dev/null | head -1)
log "QMI netdev: ${netdev:-none}"
log "AT ports: $(ls /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
