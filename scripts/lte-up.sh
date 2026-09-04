#!/bin/bash
# Bring up (and keep up) the DIGI Romania LTE data session over QMI.
#
# Routing policy: by default this does NOT install a default route into the main
# table, so the board keeps using Ethernet/Wi-Fi for its own traffic and the SSH
# management path is never disturbed. The LTE link is reachable by binding to it
# (curl --interface, SO_BINDTODEVICE) via source-based rules in table 100.
# Set LTE_DEFAULT_ROUTE=1 to also install a high-metric default route so LTE
# acts as a failover uplink.
set -u

APN="${LTE_APN:-internet}"
QMI_DEV="${QMI_DEV:-/dev/cdc-wdm0}"
RT_TABLE="${LTE_RT_TABLE:-100}"
DEFAULT_ROUTE="${LTE_DEFAULT_ROUTE:-0}"
DEFAULT_METRIC="${LTE_DEFAULT_METRIC:-700}"
CHECK_INTERVAL="${LTE_CHECK_INTERVAL:-60}"
CHECK_HOST="${LTE_CHECK_HOST:-1.1.1.1}"

log() { echo "[lte-up] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

find_netdev() {
    local d
    for d in /sys/class/net/*/; do
        if [ -d "$d/device" ] && \
           [ "$(basename "$(readlink -f "$d/device/driver" 2>/dev/null)" 2>/dev/null)" = "qmi_wwan" ]; then
            basename "$d"
            return 0
        fi
    done
    return 1
}

# Self-heal: if the modem re-enumerated, usbserial will have grabbed the QMI
# interface again and there is no netdev. Re-run the binding rather than
# relying solely on udev firing.
ensure_bound() {
    local dev
    dev=$(find_netdev) && { echo "$dev"; return 0; }
    log "no qmi_wwan netdev, re-running ec25-bind"
    "$(dirname "$0")/ec25-bind.sh" || true
    sleep 2
    dev=$(find_netdev) && { echo "$dev"; return 0; }
    return 1
}

qmi() { qmicli -d "$QMI_DEV" --device-open-proxy "$@" 2>&1; }

start_session() {
    local out
    out=$(qmi --wds-start-network="apn=$APN,ip-type=4" --client-no-release-cid)
    if echo "$out" | grep -q "NoEffect"; then
        log "data session already active"
        return 0
    fi
    if echo "$out" | grep -qi "successfully"; then
        log "data session started"
        return 0
    fi
    log "start-network output: $(echo "$out" | tr '\n' ' ' | cut -c1-200)"
    # NoEffect and success both fine; anything else may still have an IP, so
    # let the caller decide based on get-current-settings.
    return 0
}

has_ipv4() {
    qmi --wds-get-current-settings | grep -q "IPv4 address"
}

# Sessions are started with --client-no-release-cid so they outlive qmicli. If
# the modem resets, the session is reported as still active ("NoEffect") while
# the WDS client that owned it is gone, so no address can be read and no new
# session can be started. Resetting the WDS service clears that stale state.
bring_up_session() {
    start_session
    if has_ipv4; then
        return 0
    fi
    log "session reported active but carries no IPv4 — resetting WDS"
    qmi --wds-reset >/dev/null 2>&1
    sleep 3
    start_session
    if has_ipv4; then
        log "recovered after WDS reset"
        return 0
    fi
    log "still no IPv4 after WDS reset; stopping any autoconnect and retrying"
    qmi --wds-stop-network=disable-autoconnect >/dev/null 2>&1
    sleep 3
    start_session
    has_ipv4
}

# qmi_wwan defaults a freshly created netdev to 802.3 mode. In that mode the
# kernel presents an Ethernet interface with a MAC while the modem sends raw IP
# frames, so the address and routes look perfectly correct and not a single
# packet passes. A modem reset recreates the netdev and silently reverts this,
# so it is enforced on every configuration pass rather than only at startup.
ensure_raw_ip() {
    local dev="$1" path="/sys/class/net/$1/qmi/raw_ip"
    [ -f "$path" ] || return 0
    if [ "$(cat "$path")" != "Y" ]; then
        log "raw_ip was $(cat "$path"), enabling (802.3 mode passes no traffic)"
        ip link set "$dev" down
        echo Y > "$path"
    fi
    ip link set "$dev" up
}

apply_config() {
    local dev="$1" settings ip mask gw mtu prefix

    ensure_raw_ip "$dev"

    settings=$(qmi --wds-get-current-settings)
    ip=$(echo "$settings"      | awk -F': ' '/IPv4 address/        {print $2}' | tr -d ' ')
    mask=$(echo "$settings"    | awk -F': ' '/IPv4 subnet mask/    {print $2}' | tr -d ' ')
    gw=$(echo "$settings"      | awk -F': ' '/IPv4 gateway address/{print $2}' | tr -d ' ')
    mtu=$(echo "$settings"     | awk -F': ' '/MTU/                 {print $2}' | tr -d ' ')

    if [ -z "$ip" ] || [ "$ip" = "unknown" ]; then
        log "ERROR: no IPv4 address from QMI"
        return 1
    fi

    # Netmask -> prefix length.
    prefix=0
    local IFS=.
    for octet in $mask; do
        case $octet in
            255) prefix=$((prefix+8));; 254) prefix=$((prefix+7));;
            252) prefix=$((prefix+6));; 248) prefix=$((prefix+5));;
            240) prefix=$((prefix+4));; 224) prefix=$((prefix+3));;
            192) prefix=$((prefix+2));; 128) prefix=$((prefix+1));;
            0) ;; *) ;;
        esac
    done
    unset IFS
    [ "$prefix" -eq 0 ] && prefix=30

    log "IP=$ip/$prefix gw=$gw mtu=${mtu:-1400} dev=$dev"

    ip addr flush dev "$dev" 2>/dev/null
    ip addr add "$ip/$prefix" dev "$dev"
    ip link set "$dev" mtu "${mtu:-1400}" up

    # Source-based routing so the link is usable without owning the default route.
    ip route replace "$gw" dev "$dev" scope link table "$RT_TABLE"
    ip route replace default via "$gw" dev "$dev" table "$RT_TABLE"
    # The operator hands out a different address on each attach, so drop every
    # stale source rule for this table instead of only the current address —
    # otherwise they accumulate one per reconnect.
    while ip rule show | grep -q "lookup $RT_TABLE"; do
        ip rule del table "$RT_TABLE" 2>/dev/null || break
    done
    ip rule add from "$ip" table "$RT_TABLE"

    if [ "$DEFAULT_ROUTE" = "1" ]; then
        log "installing failover default route (metric $DEFAULT_METRIC)"
        ip route replace default via "$gw" dev "$dev" metric "$DEFAULT_METRIC"
    fi

    echo "$ip" > /run/lte-ip
    return 0
}

link_ok() {
    local dev="$1"
    curl -s --interface "$dev" --max-time 10 -o /dev/null \
         "http://$CHECK_HOST/" 2>/dev/null && return 0
    # Some check hosts refuse plain HTTP; fall back to a TCP connect test.
    timeout 8 bash -c "exec 3<>/dev/tcp/$CHECK_HOST/443" 2>/dev/null && return 0
    return 1
}

# --- main -------------------------------------------------------------------

dev=$(ensure_bound) || { log "ERROR: no qmi_wwan netdev found"; exit 1; }
log "using netdev $dev, APN=$APN"

# raw-ip is required for qmi_wwan on modern kernels.
if [ -f "/sys/class/net/$dev/qmi/raw_ip" ]; then
    if [ "$(cat "/sys/class/net/$dev/qmi/raw_ip")" != "Y" ]; then
        log "enabling raw_ip"
        ip link set "$dev" down
        echo Y > "/sys/class/net/$dev/qmi/raw_ip"
    fi
fi
ip link set "$dev" up

bring_up_session || log "WARN: no IPv4 yet, the watchdog will keep trying"
if ! apply_config "$dev"; then
    # Do not exit: exiting makes systemd restart-loop every 10s, which is
    # noisier and no more effective than letting the watchdog retry on its own
    # cadence.
    log "initial configuration incomplete, entering watchdog to retry"
fi
log "LTE bring-up finished on $dev"

# Watchdog: re-establish the session if it drops.
while true; do
    sleep "$CHECK_INTERVAL"
    if [ ! -e "/sys/class/net/$dev" ]; then
        log "netdev $dev vanished (modem re-enumerated), rebinding"
        newdev=$(ensure_bound) || { log "rebind failed, will retry"; continue; }
        dev="$newdev"
        if [ -f "/sys/class/net/$dev/qmi/raw_ip" ] && \
           [ "$(cat "/sys/class/net/$dev/qmi/raw_ip")" != "Y" ]; then
            ip link set "$dev" down
            echo Y > "/sys/class/net/$dev/qmi/raw_ip"
        fi
        ip link set "$dev" up
        start_session && apply_config "$dev"
        continue
    fi
    if ! ip addr show "$dev" 2>/dev/null | grep -q "inet "; then
        log "interface has no IPv4, reconnecting"
        bring_up_session && apply_config "$dev"
        continue
    fi
    status=$(qmi --wds-get-packet-service-status | grep -i "status:" | head -1)
    if ! echo "$status" | grep -qi "connected"; then
        log "packet service not connected ($status), reconnecting"
        qmi --wds-stop-network=disable-autoconnect >/dev/null 2>&1
        sleep 2
        bring_up_session && apply_config "$dev"
    fi
done
