#!/bin/sh
# Export Caddy's local root CA certificate (the PUBLIC part) and print its
# SHA-256 fingerprint, so members can check they install the right one.
#
#   scripts/caddy_ca.sh [output-file]      default: ./commons-root.crt
#
# Never export root.key: it can impersonate any website on members' devices.
set -eu

out="${1:-commons-root.crt}"
compose="docker compose"

$compose cp caddy:/data/caddy/pki/authorities/local/root.crt "$out"
chmod 0644 "$out"

echo "Wrote $out"
openssl x509 -in "$out" -noout -subject -enddate
openssl x509 -in "$out" -noout -fingerprint -sha256
