#!/usr/bin/env bash
# Install nginx as a TLS reverse proxy for the phone API and issue a Let's Encrypt
# certificate for iris.bavariaduo.ovh over OVH DNS-01.
#
# DNS-01 is not a preference, it is a requirement: the board has a private address
# and the name resolves only on the LAN (Unbound host override in OPNsense), so
# HTTP-01 can never reach it. DNS-01 proves control of the zone instead, and needs
# nothing open to the internet.
#
# acme.sh comes from Debian (acme.sh 3.1.1-1) rather than the upstream
# curl-into-shell installer: apt-signed, apt-updated, and no remote code executed
# at install time.
#
# Credentials are read from a root-only file so they never appear in a command
# line, a shell history or this repo. The file holds:
#
#   OVH_AK=...
#   OVH_AS=...
#   OVH_CK=...
#   OVH_END_POINT=ovh-eu
#
# Usage, as root on the board:
#
#   CREDS_FILE=/root/.ovh-env ./setup-tls.sh

set -euo pipefail

DOMAIN="${DOMAIN:-iris.bavariaduo.ovh}"
CERT_DIR="${CERT_DIR:-/etc/ssl/iris}"
CREDS_FILE="${CREDS_FILE:-/root/.ovh-env}"
ACME="${ACME:-/usr/bin/acme.sh}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

say() { printf '\n=== %s ===\n' "$1"; }

say "prerequisites"
command -v nginx >/dev/null || {
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx
}
[ -x "$ACME" ] || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq acme.sh
echo "  nginx:   $(nginx -v 2>&1)"
echo "  acme.sh: $($ACME --version 2>&1 | tr '\n' ' ')"

# The Debian package ships only the script — unlike the upstream installer it does
# not create the config home, and acme.sh then fails every read and write of
# account.conf (including --set-default-ca, which silently leaves the CA at
# ZeroSSL and produces a confusing "update your account with an email address").
install -d -m 700 /root/.acme.sh

say "credentials"
if [ -r "$CREDS_FILE" ]; then
    chmod 600 "$CREDS_FILE"
    set -a; . "$CREDS_FILE"; set +a
    export OVH_END_POINT="${OVH_END_POINT:-ovh-eu}"
    echo "  loaded from $CREDS_FILE (endpoint $OVH_END_POINT)"
else
    echo "  $CREDS_FILE absent — relying on values acme.sh already saved"
fi

# acme.sh defaults to ZeroSSL, which requires EAB credentials and a registered
# email. Let's Encrypt needs neither, and it is what shoim-1 already uses, so the
# chain matches across the estate.
"$ACME" --set-default-ca --server letsencrypt
"$ACME" --register-account --server letsencrypt \
        -m "${ACME_EMAIL:-admin@bavariaduo.ovh}" 2>&1 | tail -2 | sed 's/^/  /'

say "issuing $DOMAIN"
install -d -m 755 "$CERT_DIR"
# acme.sh polls the authoritative nameservers rather than sleeping blindly, so no
# --dnssleep is needed. Re-running is safe: an unexpired cert is skipped.
"$ACME" --issue --dns dns_ovh -d "$DOMAIN" "$@"

say "installing certificate"
"$ACME" --install-cert -d "$DOMAIN" \
    --key-file       "$CERT_DIR/privkey.pem" \
    --fullchain-file "$CERT_DIR/fullchain.pem" \
    --reloadcmd      "systemctl reload nginx"
chmod 600 "$CERT_DIR/privkey.pem"

say "automatic renewal"
# The Debian package ships no timer, so install acme.sh's own cron entry. Renewal
# is DNS-01 as well, so it keeps working with nothing exposed.
"$ACME" --install-cronjob >/dev/null 2>&1 || true
crontab -l 2>/dev/null | grep -c acme.sh | sed 's/^/  cron entries: /'

say "enabling the vhost"
install -m 644 "$HERE/nginx-iris.conf" /etc/nginx/sites-available/iris
ln -sf /etc/nginx/sites-available/iris /etc/nginx/sites-enabled/iris
# Debian's default vhost is the catch-all default_server: leaving it enabled means
# any other name pointed at this box still serves nginx's welcome page.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

say "result"
openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -issuer -dates | sed 's/^/  /'
"$ACME" --list | sed 's/^/  /'
echo
echo "Next: confirm https://$DOMAIN/ in a browser, then set PHONE_BIND=127.0.0.1"
echo "in phone-api.service so port 8080 is no longer directly exposed."
