#!/bin/sh
# Check that Caddy rate-limits Matrix password logins per client IP.
#
#   scripts/check_login_limit.sh matrix.commons.theoutpost.network
#
# Sends wrong-password logins for a username that doesn't exist (so no real
# account is touched) until Caddy answers 429. Then this IP stays blocked
# for up to a minute; other IPs are unaffected.
#
# Note: Docker Desktop (macOS/Windows) may show every client to Caddy with the
# same address, so in that setup the limit is shared by all clients. On the
# Linux box Caddy sees each client's real IP.
set -eu

host="${1:?usage: $0 MATRIX_HOST}"
limit="${LIMIT:-10}"
url="https://$host/_matrix/client/v3/login"
body='{"type":"m.login.password","identifier":{"type":"m.id.user","user":"rate-limit-check-nobody"},"password":"not-a-real-password"}'

i=0
while [ "$i" -lt $((limit + 3)) ]; do
	i=$((i + 1))
	code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST \
		-H 'Content-Type: application/json' --data "$body" "$url")
	if [ "$code" = "429" ]; then
		if [ "$i" -le "$limit" ]; then
			echo "FAIL: blocked already at attempt $i (limit $limit; earlier attempts in the last minute count too)"
			exit 1
		fi
		retry=$(curl -s -o /dev/null -D - -m 10 -X POST -H 'Content-Type: application/json' \
			--data "$body" "$url" | tr -d '\r' | awk -F': ' 'tolower($1)=="retry-after"{print $2}')
		echo "ok: attempt $i got 429 (limit $limit per minute), Retry-After: ${retry:-missing}"
		exit 0
	fi
done
echo "FAIL: no 429 after $i attempts; login is not rate-limited"
exit 1
