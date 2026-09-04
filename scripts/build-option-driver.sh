#!/bin/bash
# Build and install the proper USB serial driver for the Quectel EC25.
#
# Armbian's sunxi64 kernel ships CONFIG_USB_SERIAL_OPTION=n, so option.ko and
# usb_wwan.ko simply do not exist:
#
#   CONFIG_USB_SERIAL_GENERIC=y
#   # CONFIG_USB_SERIAL_OPTION is not set
#
# Without them the only way to get AT ports is the generic usbserial driver with
# an explicit vendor/product match, which the kernel itself describes as "only
# for testing and one-off prototypes" — and which also swallows the QMI
# interface, leaving no data path.
#
# Both are ordinary in-tree drivers, so they can be compiled out-of-tree against
# the installed headers. The sources are fetched at the exact tag of the running
# kernel.
#
# NOTE: a kernel upgrade replaces /lib/modules and these modules must be rebuilt.
# Re-run this script after any kernel update.
set -euo pipefail

KVER="$(uname -r)"
WORK="${WORK:-/tmp/ec25-option-build}"
DST="/lib/modules/$KVER/kernel/drivers/usb/serial"
BASE="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/usb/serial"

log() { echo "[option-build] $*"; }

if [ "$(id -u)" -ne 0 ]; then
    exec sudo -E "$0" "$@"
fi

if modinfo option >/dev/null 2>&1; then
    log "option.ko already present for $KVER, nothing to do"
    exit 0
fi

if [ ! -d "/lib/modules/$KVER/build" ]; then
    log "ERROR: kernel headers missing. Install linux-headers-current-sunxi64"
    exit 1
fi

# Kernel version without any local suffix, e.g. 6.18.33-current-sunxi64 -> 6.18.33
BASEVER="$(echo "$KVER" | sed 's/-.*//')"

rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

fetched=""
for tag in "v$BASEVER" "v${BASEVER%.*}"; do
    log "trying kernel tag $tag"
    ok=1
    for f in option.c usb_wwan.c usb-wwan.h; do
        curl -sfL --max-time 60 -o "$f" "$BASE/$f?h=$tag" || { ok=0; break; }
        [ -s "$f" ] || { ok=0; break; }
    done
    if [ "$ok" = "1" ]; then fetched="$tag"; break; fi
done

if [ -z "$fetched" ]; then
    log "ERROR: could not fetch driver sources"
    exit 1
fi
log "fetched sources at $fetched"

cat > Makefile <<'MK'
obj-m += usb_wwan.o
obj-m += option.o
KDIR := /lib/modules/$(shell uname -r)/build
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules
MK

log "building"
make >/dev/null

log "installing into $DST"
install -d "$DST"
install -m 0644 usb_wwan.ko option.ko "$DST/"
depmod -a

# The generic-usbserial hack must go, or it races the real driver for the ports.
rm -f /etc/modprobe.d/ec25.conf /etc/modprobe.d/quectel.conf
printf 'option\nqmi_wwan\n' > /etc/modules-load.d/ec25.conf

log "loading"
modprobe -r option 2>/dev/null || true
modprobe -r usbserial 2>/dev/null || true
sleep 2
modprobe option
modprobe qmi_wwan
sleep 4

log "result:"
echo "   modules: $(lsmod | grep -E '^option|^usb_wwan' | awk '{print $1}' | tr '\n' ' ')"
echo "   ttyUSB : $(ls /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
echo "   qmi    : $(ls /dev/cdc-wdm* 2>/dev/null || echo MISSING)"
for i in /sys/bus/usb/devices/*:1.*/; do
    [ -e "$i/driver" ] || continue
    case "$(readlink -f "$i/driver")" in
        *option|*qmi_wwan)
            echo "   $(basename "$i") -> $(basename "$(readlink -f "$i/driver")")" ;;
    esac
done
