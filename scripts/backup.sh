#!/bin/sh
# Nightly encrypted backup of the COMMONS box (docs/BACKUP.md). Settings come from
# the environment (the systemd unit's EnvironmentFile, /etc/commons/backup.env):
#   COMMONS_BACKUP_DIR         the backup disk, e.g. /mnt/commons-backup (another device)
#   COMMONS_BACKUP_RECIPIENTS  file with age public keys (age1…); never a private key
#   COMMONS_BACKUP_KEEP        how many backups to keep (default 14)
set -eu
cd "$(dirname "$0")/.."
# Python 3.11+ (standard library only): the project's venv if there is one.
py=python3
[ -x .venv/bin/python ] && py=.venv/bin/python
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
	echo "commons-backup: needs Python 3.11 or newer ($py)" >&2
	exit 1
}
exec "$py" scripts/backup.py backup "$@"
