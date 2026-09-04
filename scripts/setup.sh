#!/bin/bash
# Install the Orange Pi 3 LTS phone stack: drivers, LTE data session, API service.
# Idempotent — safe to re-run after redeploying code.
set -euo pipefail

APP_DIR="${APP_DIR:-/home/shoim2/phone}"
APP_USER="${APP_USER:-shoim2}"

if [ "$(id -u)" -ne 0 ]; then
    echo "re-running under sudo..."
    exec sudo -E "$0" "$@"
fi

log() { echo ""; echo "=== $* ==="; }

log "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends \
    python3 python3-flask python3-serial \
    libqmi-utils usb-modeswitch \
    || echo "WARN: some packages failed; continuing (they may already be present)"

log "Serial driver"
# Armbian's kernel ships CONFIG_USB_SERIAL_OPTION=n, so the proper driver is
# built from the matching kernel sources. It must be rebuilt after a kernel
# upgrade. Falls back to the generic-usbserial hack if the build fails.
if ! "$APP_DIR/scripts/build-option-driver.sh"; then
    echo "WARN: option.ko build failed, falling back to generic usbserial"
    cat > /etc/modprobe.d/ec25.conf <<'EOC'
# Fallback: no option.ko available, use the generic usbserial driver. This also
# claims the QMI interface, which ec25-bind.sh hands back to qmi_wwan.
options usbserial vendor=0x2c7c product=0x0125
EOC
    cat > /etc/modules-load.d/ec25.conf <<'EOC'
usbserial
qmi_wwan
EOC
fi

# Retire the previous ad-hoc configuration if present.
rm -f /etc/modprobe.d/quectel.conf /etc/modules-load.d/quectel.conf \
      /etc/udev/rules.d/99-quectel-ec25.rules

log "udev rules"
install -m 0644 "$APP_DIR/scripts/99-ec25.rules" /etc/udev/rules.d/99-ec25.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --subsystem-match=usb || true

log "Disabling ModemManager (this stack owns the AT port)"
systemctl disable --now ModemManager.service 2>/dev/null || true
systemctl mask ModemManager.service 2>/dev/null || true

log "Permissions"
usermod -aG dialout "$APP_USER" || true
chmod +x "$APP_DIR/scripts/"*.sh
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Observability: persistent logs and disk health"
# All of this exists because of a failure that left no evidence. The root
# filesystem stopped serving I/O, the board served 500s for an hour, and the
# restart that fixed it also destroyed the only record of what happened —
# /var/log was a zram disk and the journal was volatile.

apt-get install -y --no-install-recommends smartmontools \
    || echo "WARN: smartmontools install failed; SMART telemetry will be unavailable"

# Armbian keeps /var/log on a 50 MB zram to spare SD cards from write wear, and
# syncs it to /var/log.hdd on *clean* shutdown only. This board's root is a
# 128 GB NVMe SSD (0% life used after 136 GB written), so the wear argument does
# not apply — and the sync-on-clean-shutdown behaviour loses the logs in exactly
# the case worth investigating, an unplanned power cut.
if [ -f /etc/default/armbian-ramlog ] && grep -q '^ENABLED=true' /etc/default/armbian-ramlog; then
    # Order matters: the script begins with `[ "$ENABLED" != true ] && exit 0`,
    # so flipping the flag first would turn `stop` into a no-op and lose whatever
    # is still only in RAM. Stop it while it is still enabled, then disable.
    systemctl stop armbian-ramlog || true
    sed -i 's/^ENABLED=true/ENABLED=false/' /etc/default/armbian-ramlog
    systemctl disable armbian-ramlog || true
    mountpoint -q /var/log && umount -l /var/log || true
    mountpoint -q /var/log.hdd && umount -l /var/log.hdd || true
    # Installing ramlog rewrites every logrotate config to the /var/log.hdd copy.
    # Left that way with the bind gone, `missingok` makes logrotate silently do
    # nothing while rsyslog writes to /var/log — unbounded growth where the 50 MB
    # zram used to be the ceiling.
    cp -a /etc/logrotate.d "/root/logrotate.d.bak-ramlog.$(date +%s)" || true
    sed -i 's#/var/log\.hdd/#/var/log/#g' /etc/logrotate.d/* || true
    echo "armbian-ramlog disabled; /var/log is now on the SSD"
fi

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/10-persistent.conf <<'EOC'
# Survive reboots. Without this the journal lives in /run and every restart —
# including the one that recovers the board — erases the evidence.
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
EOC
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal || true
systemctl restart systemd-journald
journalctl --flush || true

# The root SSD is NVMe behind a JMicron JMS583 bridge, so smartctl needs the
# NVMe tunnel and root. Exactly one command is granted, with its arguments fixed.
cat > /etc/sudoers.d/iris-smartctl <<EOC
$APP_USER ALL=(root) NOPASSWD: /usr/sbin/smartctl -d sntjmicron -H -A /dev/sda
EOC
chmod 0440 /etc/sudoers.d/iris-smartctl
if ! visudo -c -f /etc/sudoers.d/iris-smartctl >/dev/null; then
    echo "WARN: sudoers snippet rejected, removing it"
    rm -f /etc/sudoers.d/iris-smartctl
fi

# Clean reboot and shutdown from the UI, so the alternative — pulling the plug —
# stops being the only option. Two fixed commands, no arguments to smuggle
# anything through. The API restricts this to a browser session, never a token.
cat > /etc/sudoers.d/iris-power <<EOC
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
EOC
chmod 0440 /etc/sudoers.d/iris-power
if ! visudo -c -f /etc/sudoers.d/iris-power >/dev/null; then
    echo "WARN: sudoers snippet rejected, removing it"
    rm -f /etc/sudoers.d/iris-power
fi

# Setting the NTP server and forcing a resync from the UI. There is no RTC
# battery, so the clock is wrong until NTP lands, and TLS validation and the
# prepaid reminder's date arithmetic both depend on it. One helper with one job,
# which validates its own arguments because it is the thing running as root.
install -m 0755 -o root -g root "$APP_DIR/scripts/iris-set-ntp.sh" \
    /usr/local/sbin/iris-set-ntp
cat > /etc/sudoers.d/iris-ntp <<EOC
$APP_USER ALL=(root) NOPASSWD: /usr/local/sbin/iris-set-ntp
EOC
chmod 0440 /etc/sudoers.d/iris-ntp
if ! visudo -c -f /etc/sudoers.d/iris-ntp >/dev/null; then
    echo "WARN: sudoers snippet rejected, removing it"
    rm -f /etc/sudoers.d/iris-ntp
fi

log "systemd units"
UNITS="ec25-bind.service ec25-lte.service"
# The API unit is only installed once the application itself is deployed, so the
# driver/LTE layer can be set up and validated on its own.
if [ -f "$APP_DIR/app.py" ]; then
    UNITS="$UNITS phone-api.service"
else
    echo "NOTE: app.py not deployed yet, skipping phone-api.service"
fi
for unit in $UNITS; do
    install -m 0644 "$APP_DIR/scripts/$unit" "/etc/systemd/system/$unit"
    echo "installed $unit"
done
systemctl daemon-reload
systemctl enable $UNITS

log "Starting services"
systemctl restart ec25-bind.service
sleep 3
systemctl restart ec25-lte.service
sleep 8
if [ -f "$APP_DIR/app.py" ]; then
    systemctl restart phone-api.service
    sleep 2
fi

log "Status"
for unit in $UNITS; do
    printf '%-20s %s\n' "$unit" "$(systemctl is-active $unit)"
done
echo ""
echo "AT ports : $(ls /dev/ttyUSB* 2>/dev/null | tr '\n' ' ')"
echo "QMI      : $(ls /dev/cdc-wdm* 2>/dev/null || echo 'MISSING')"
echo "LTE IP   : $(cat /run/lte-ip 2>/dev/null || echo 'not up yet')"
echo ""
echo "Web UI   : http://$(hostname -I | awk '{print $1}'):8080"
