#!/bin/sh
# Create the bot's Matrix account and its secrets.
#
#   scripts/bot_token.sh          # once, with matrix, mas and postgres running
#   scripts/bot_token.sh --renew  # new access token (same device, keeps encryption keys)
#
# Writes into secrets/matrix/ (mode 0600, values never printed):
#   bot_access_token  MAS compatibility token for @commons, device COMMONSBOT
#   bot_pickle_key    encrypts the bot's stored encryption keys
#
# The account has no password: nobody can sign in as the bot, it only has this token.
set -eu

out="secrets/matrix"
user="${COMMONS_BOT_USERNAME:-commons}"
device="${COMMONS_BOT_DEVICE_ID:-COMMONSBOT}"
compose="docker compose"
mas="$compose exec -T mas mas-cli manage --config /run/secrets/mas_config"

[ -d "$out" ] || { echo "error: $out is missing; run make init first" >&2; exit 1; }
umask 077

if [ -s "$out/bot_pickle_key" ]; then
	echo "keep     $out/bot_pickle_key"
else
	openssl rand -hex 32 > "$out/bot_pickle_key.tmp" && mv "$out/bot_pickle_key.tmp" "$out/bot_pickle_key"
	echo "created  $out/bot_pickle_key"
fi

if [ -s "$out/bot_access_token" ] && [ "${1:-}" != "--renew" ]; then
	echo "keep     $out/bot_access_token (use --renew for a new one)"
	exit 0
fi

# Register the account (no password, not an admin); fine if it already exists.
if $mas register-user --yes --no-admin --display-name COMMONS "$user" >/dev/null 2>&1; then
	echo "created  Matrix account $user"
else
	echo "keep     Matrix account $user (already exists)"
fi

# MAS writes the token into a log line; pick it out without ever showing it.
log=$($mas issue-compatibility-token "$user" "$device" 2>&1) || {
	echo "error: could not issue a token for $user" >&2; exit 1; }
token=$(printf '%s\n' "$log" | grep -oE 'mct_[A-Za-z0-9_]+' | head -n 1 || true)
unset log
[ -n "$token" ] || { echo "error: no token in the MAS output" >&2; exit 1; }
printf '%s\n' "$token" > "$out/bot_access_token.tmp" && mv "$out/bot_access_token.tmp" "$out/bot_access_token"
unset token
echo "created  $out/bot_access_token (device $device)"
echo "Start or restart the bot: $compose up -d bot"
