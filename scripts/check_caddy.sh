#!/bin/sh
# Smoke test for the edge proxy. Run against a running stack:
#
#   scripts/check_caddy.sh HOST [ROOT_CRT]
#
#   Let's Encrypt (COMMONS_TLS=desec):  scripts/check_caddy.sh commons.theoutpost.network
#   Local CA (COMMONS_TLS=internal):    scripts/check_caddy.sh 192.168.0.179 commons-root.crt
#
# Without ROOT_CRT the certificate must be trusted by the system's normal CA list.
# CHECK_IP=192.168.0.179 connects to that address instead of resolving HOST via DNS.
#
# Checks: HTTPS is trusted, HSTS is set, /internal is blocked (including path
# tricks), the Server header is hidden and plain HTTP on port 80 is closed.
set -u

host="${1:?usage: check_caddy.sh HOST [ROOT_CRT]}"
ca="${2:-}"
base="https://$host"
fail=0

# curl options shared by every request
set --
[ -n "$ca" ] && set -- "$@" --cacert "$ca"
[ -n "${CHECK_IP:-}" ] && set -- "$@" --resolve "$host:443:$CHECK_IP" --resolve "$host:80:$CHECK_IP"

ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

status() { curl -s "$@" --path-as-is -o /dev/null -w '%{http_code}'; }

# 1. Trusted HTTPS (curl verifies the chain and the host name against the CA).
code=$(status "$@" "$base/")
[ "$code" = 200 ] && ok "HTTPS trusted ${ca:+with $ca }($code)" || bad "HTTPS / returned $code"

# 2. HSTS header present.
headers=$(curl -s "$@" -D - -o /dev/null "$base/")
echo "$headers" | grep -qi '^strict-transport-security: max-age=' && ok "HSTS header set" || bad "HSTS header missing"
echo "$headers" | grep -qi '^server:' && bad "Server header exposed" || ok "Server header hidden"

# 3. /internal blocked, including case and encoding variants.
for p in /internal /internal/ /internal/assistant /INTERNAL/assistant /Internal \
         /%69nternal/assistant //internal/assistant /x/../internal/assistant /internal%2Fassistant; do
	code=$(status "$@" "$base$p")
	if [ "$code" = 404 ] || [ "$code" = 400 ]; then ok "blocked $p ($code)"; else bad "$p returned $code"; fi
done

# 4. Paths that merely start with "internal" are not blocked by Caddy: the request
#    reaches the backend (which may answer 200 or its own 404 with a body), whereas
#    Caddy's block answers 404 with an empty body.
size=$(curl -s "$@" --path-as-is -o /dev/null -w '%{size_download}' "$base/internalize")
[ "${size:-0}" -gt 0 ] && ok "/internalize passes through to the backend" || bad "/internalize was blocked"

# 5. Local CA only: without it the certificate must NOT be trusted (proves it is ours).
if [ -n "$ca" ]; then
	resolve=""; [ -n "${CHECK_IP:-}" ] && resolve="--resolve $host:443:$CHECK_IP"
	# shellcheck disable=SC2086
	curl -s $resolve -o /dev/null "$base/" 2>/dev/null && bad "trusted without the local CA?" || ok "untrusted without the local CA"
fi

# 6. Plain HTTP is not served.
curl -s -m 3 "$@" -o /dev/null "http://$host/" 2>/dev/null && bad "port 80 answers" || ok "port 80 closed"

[ "$fail" = 0 ] && echo "all checks passed" || echo "some checks FAILED"
exit "$fail"
