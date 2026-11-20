#!/bin/sh
# Point the COMMONS names at this machine's LAN address via the deSEC API.
#
#   scripts/update_dns.sh            # detect the LAN IP
#   scripts/update_dns.sh 10.0.0.5   # or give it explicitly
#   scripts/update_dns.sh "$(tailscale ip -4)"   # VPN address (dev remote access)
#
# Sets the A records for $COMMONS_DOMAIN, matrix.$COMMONS_DOMAIN and auth.$COMMONS_DOMAIN
# (auth: Matrix Authentication Service, Synapse trial).
# The token is read from secrets/desec_token and passed to curl on stdin,
# so it never shows up in the process list, the shell history or the output.
set -eu

domain="${COMMONS_DOMAIN:-commons.theoutpost.network}"
token_file="${DESEC_TOKEN_FILE:-secrets/desec_token}"

[ -s "$token_file" ] || { echo "error: $token_file is missing or empty" >&2; exit 1; }

ip="${1:-}"
if [ -z "$ip" ]; then
	if command -v ipconfig >/dev/null 2>&1; then      # macOS
		ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
	else                                              # Linux
		ip=$(hostname -I 2>/dev/null | awk '{print $1}')
	fi
fi
echo "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || { echo "error: no usable IPv4 address ('$ip'); pass one as argument" >&2; exit 1; }

# Private LAN ranges, plus 100.64.0.0/10 (Tailscale / CGNAT addresses).
case "$ip" in
	10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;;
	100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) ;;
	*) echo "warning: $ip is not a private or VPN address" >&2 ;;
esac

body=$(printf '[{"subname":"","type":"A","ttl":3600,"records":["%s"]},{"subname":"matrix","type":"A","ttl":3600,"records":["%s"]},{"subname":"auth","type":"A","ttl":3600,"records":["%s"]}]' "$ip" "$ip" "$ip")

# PATCH creates or updates only these two record sets; other records are untouched.
status=$(printf 'Authorization: Token %s' "$(tr -d '\r\n' < "$token_file")" |
	curl -sS -o /dev/null -w '%{http_code}' -X PATCH \
		-H @- -H 'Content-Type: application/json' \
		--data "$body" \
		"https://desec.io/api/v1/domains/$domain/rrsets/")

case "$status" in
	200) echo "ok: $domain, matrix.$domain and auth.$domain -> $ip (may take a few minutes to show up everywhere)" ;;
	401|403) echo "error: deSEC rejected the token (HTTP $status)" >&2; exit 1 ;;
	404) echo "error: deSEC has no zone '$domain' for this token (HTTP 404); check COMMONS_DOMAIN and that the token can access the delegated zone" >&2; exit 1 ;;
	*) echo "error: deSEC returned HTTP $status" >&2; exit 1 ;;
esac
