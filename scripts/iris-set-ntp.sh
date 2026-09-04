#!/bin/bash
# Set the NTP server(s) for systemd-timesyncd, or force a resync now.
#
# Install to /usr/local/sbin/iris-set-ntp, root-owned and 0755, with the
# matching sudoers drop-in (scripts/iris-ntp.sudoers). The API calls this rather
# than writing the config itself, for two reasons:
#
#  - It keeps the privileged surface to one auditable file with one job, instead
#    of the web application holding a general "write anything to /etc" power.
#  - shoim2 currently has blanket NOPASSWD sudo, which makes the narrow rule
#    redundant today. That is exactly why it is written narrowly: the day the
#    blanket rule is removed, this keeps working and nothing else gains root.
#
# The server list is validated here as well as in the caller. Not because the
# caller is untrusted, but because this runs as root and a validator that only
# exists on the far side of an HTTP handler is one refactor away from being
# skipped.

set -euo pipefail

DROPIN_DIR=/etc/systemd/timesyncd.conf.d
DROPIN="$DROPIN_DIR/10-iris.conf"
MAX_SERVERS=4

usage() {
    echo "usage: iris-set-ntp servers <host> [host ...]" >&2
    echo "       iris-set-ntp sync" >&2
    exit 2
}

# A hostname or an IPv4/IPv6 literal. Deliberately strict: no spaces, no shell
# metacharacters, no newlines, nothing that could break out of the config line
# it is about to be written into.
valid_server() {
    local s="$1"
    [ ${#s} -le 253 ] || return 1
    if [[ "$s" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
        return 0    # hostname or IPv4
    fi
    if [[ "$s" =~ ^[0-9A-Fa-f:]+$ ]]; then
        return 0    # IPv6 literal
    fi
    return 1
}

restart_timesyncd() {
    # set-ntp true is idempotent and covers the case where NTP was switched off
    # entirely; the restart is what forces an immediate poll rather than waiting
    # out the current interval (up to 34 minutes on this board).
    timedatectl set-ntp true >/dev/null 2>&1 || true
    systemctl restart systemd-timesyncd
}

case "${1:-}" in
    servers)
        shift
        [ $# -ge 1 ] || usage
        [ $# -le $MAX_SERVERS ] || {
            echo "at most $MAX_SERVERS servers" >&2; exit 2; }
        for s in "$@"; do
            valid_server "$s" || { echo "invalid server: $s" >&2; exit 2; }
        done
        mkdir -p "$DROPIN_DIR"
        # Written to a temporary file and moved into place, so a failure part way
        # through cannot leave timesyncd reading a half-written config.
        tmp=$(mktemp "$DROPIN.XXXXXX")
        {
            echo "# Managed by the iris phone API. Edits here are overwritten."
            echo "[Time]"
            echo "NTP=$*"
        } > "$tmp"
        chmod 0644 "$tmp"
        mv "$tmp" "$DROPIN"
        restart_timesyncd
        echo "ok: NTP=$*"
        ;;
    sync)
        restart_timesyncd
        echo "ok: resync requested"
        ;;
    *)
        usage
        ;;
esac
