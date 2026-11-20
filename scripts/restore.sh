#!/bin/sh
# Restore a backup into a FRESH clone (docs/BACKUP.md):
#   scripts/restore.sh /mnt/commons-backup/commons-20261115T021500Z.tar.gz.age --identity ~/commons-backup.key
# Refuses to run where secrets/ exists or a database or volume already holds data.
set -eu
cd "$(dirname "$0")/.."
# Python 3.11+ (standard library only): the project's venv if there is one.
py=python3
[ -x .venv/bin/python ] && py=.venv/bin/python
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
	echo "commons-backup: needs Python 3.11 or newer ($py)" >&2
	exit 1
}
exec "$py" scripts/backup.py restore "$@"
