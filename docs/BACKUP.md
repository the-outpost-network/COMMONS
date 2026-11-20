# Backups and restore

A stolen box or a dead disk must not cost the community its history. Every night the box writes one encrypted backup to a second disk. With that file and a private key held off the box, a fresh stack can be brought back exactly as it was. The decision log verifies there, and members sign in with their usual accounts.

## What is in a backup

One file per night, `commons-YYYYMMDDTHHMMSSZ.tar.gz.age` (the time is UTC). The name holds nothing else.

| Part | From | Why |
|---|---|---|
| `commons.dump`, `synapse.dump`, `mas.dump` | `pg_dump -Fc` of the three databases | Ledger, bookings, votes, decision log; rooms and messages; accounts and passwords |
| `app-data.tar` | `app-data` volume | The **decision-log signing key** (`/data/keys`) and the app's OAuth client |
| `synapse-data.tar` | `synapse-data` volume | Homeserver media |
| `bot-data.tar` | `bot-data` volume | The bot's encryption keys (crypto store) and cross-signing recovery key. The bot is stopped for a few seconds while this is copied |
| `caddy-data.tar` | `caddy-data` volume (`/data/caddy`) | TLS certificates and, in `tls internal` mode, the local CA that members' phones trust |
| `host-files.tar` | `secrets/`, `community.yaml`, `.env` | Matrix, MAS and database keys and passwords. The dumps are useless without them: MAS encrypts data with keys in `secrets/matrix/mas.yaml`, and the bot's store needs `bot_pickle_key` |
| `manifest.json` | generated | Format, time, git commit and the SHA-256 of every part. A restore checks every part against it |

Not included: the model weights (`make fetch-model` downloads and verifies them again), Caddy's autosaved config, and Docker images (built from the checkout).

## How it is protected

- **Encrypted with [age](https://age-encryption.org) to public keys.** The box only has `age1…` public keys, so it can write backups but never read them. The private keys live with people, off the box. The script refuses a recipients file that contains a private key (`AGE-SECRET-KEY-…`).
- **Use two keys.** Give one to the admin who would do the restore, and keep the other offline (printed, or on a USB stick in a sealed envelope with the board). Losing one key then doesn't lose the backups. Losing both does: nobody, not even the team, can open a backup without a private key.
- **Plaintext never reaches the backup disk.** The parts are staged in a 0700 temp directory on the box's (LUKS-encrypted) disk, streamed through `age`, and the temp directory is removed on every exit. Backup files are 0600, written under a `.partial` name and renamed once complete, and an existing backup is never overwritten.
- **Another device.** The script refuses a backup directory on the same disk as the checkout or Docker's data. An unplugged USB disk therefore fails loudly instead of quietly filling the box's SSD.
- **Logs** hold the file name, size and SHA-256 only, never paths inside secrets, values or raw output. A failure sends every admin a private chat message with a fixed reason, for example "the backup disk or the backup keys are not set up (is the disk plugged in?)".

## Set up on the box (once)

1. **age and Python 3.11+** on the host: `sudo apt install age` (Debian 12, Ubuntu 24.04 and Raspberry Pi OS ship Python 3.11 or newer).

2. **Keys.** Each key holder runs this on their own computer, not on the box:

   ```bash
   age-keygen -o commons-backup.key      # private: keep it safe, never copy it to the box
   age-keygen -y commons-backup.key      # prints the public key, age1…
   ```

   On the box, put the public keys, one per line, in `/etc/commons/backup-recipients.txt`. Lines starting with `#` are comments, e.g. `# Alex, 15 Nov 2026`.

3. **Backup disk.** Format a USB disk or second SSD (LUKS is optional, since the files are already encrypted) and mount it permanently at `/mnt/commons-backup`, e.g. through `/etc/fstab` with `nofail`. Make it writable for the account that runs the stack: `sudo chown commons: /mnt/commons-backup`.

4. **Settings:** `sudo cp deploy/backup/backup.env.example /etc/commons/backup.env` and adjust it. It holds no secrets.

5. **Timer:** edit `User=` and `WorkingDirectory=` in `deploy/backup/commons-backup.service` to match the account and checkout that run the stack. That account must be in the `docker` group, which is equivalent to root on the host. Then:

   ```bash
   sudo cp deploy/backup/commons-backup.service deploy/backup/commons-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now commons-backup.timer
   sudo systemctl start commons-backup.service     # one backup now
   journalctl -u commons-backup -n 20              # "wrote commons-…tar.gz.age (… bytes, sha256 …)"
   ```

The timer runs at 03:15 Stockholm time. A night the box was off is made up at the next boot. The newest 14 backups are kept (`COMMONS_BACKUP_KEEP`), and older ones are deleted only after the new one is written. A manual run is `make backup` with the same three variables set.

**Check it now and then:** the newest file in `/mnt/commons-backup` should be from last night. A key holder can check that a backup opens, without restoring anything:

```bash
python3 scripts/backup.py extract commons-20261115T021500Z.tar.gz.age \
  --identity commons-backup.key --to /tmp/commons-check   # "ok: 8 parts match the manifest"
rm -rf /tmp/commons-check                                 # it holds every secret in plaintext
```

## Restore into a fresh stack, step by step

You need a backup file, one private key, and a machine with Docker, git, age and Python 3.11+. That can be the repaired box, a new box, or a laptop for a drill.

1. **Fresh checkout of the version that wrote the backup.** `manifest.json` names the commit, and the restore prints it:

   ```bash
   git clone https://github.com/the-outpost-network/commons.git && cd commons
   ```

   Don't run `make init`: the restore brings back the original secrets. A new set would not match the databases.

2. **Same host as a running COMMONS?** That only applies to a drill. `docker-compose.yml` fixes the project name, so give the drill its own name, or it would touch the running box. The restore refuses otherwise.

   ```bash
   export COMPOSE_PROJECT_NAME=commons-restore
   ```

3. **Restore:**

   ```bash
   scripts/restore.sh /mnt/commons-backup/commons-20261115T021500Z.tar.gz.age --identity ~/commons-backup.key
   ```

   It decrypts into a temp directory and checks every part against the manifest. Then it puts back `secrets/`, `community.yaml` and `.env`, starts `postgres` (whose init scripts create the roles and empty databases with the restored passwords) and restores the three databases in one transaction each. Last, it builds the images and fills the `app`, `matrix`, `bot` and `caddy` volumes. It refuses to run where `secrets/`, `community.yaml` or `.env` already exist, or where a database or volume already holds data. It never overwrites a box.

4. **Start and check:**

   ```bash
   make fetch-model
   COMMONS_TLS=desec COMMONS_HOSTS=commons.theoutpost.network \
   COMMONS_MATRIX_HOST=matrix.commons.theoutpost.network \
   COMMONS_AUTH_HOST=auth.commons.theoutpost.network make up
   docker compose exec app python -m app.cli verify-log    # "ok: N entries verify; head …"
   docker compose exec app python -m app.cli list-members
   ```

   Compare the head hash with the newest anchor in the COMMONS room or the `/log` page, check that the levels on the board match, and sign in as a member. On a new box, point DNS at it with `scripts/update_dns.sh`. Linux hosts need `sudo chown 10001:10001 secrets/*` once, as after `make init`.

5. **Drill only:** stop it and delete its volumes, then delete the checkout. Its `secrets/` is in plaintext.

   ```bash
   docker compose -p commons-restore down -v
   ```

   For a drill on the box itself, start only `postgres mas matrix app` (`docker compose up -d --no-deps …`). A restored bot and Synapse would otherwise talk in the real rooms' copies and send pushes to members' phones.

## Restore drill log

| Date | From | To | Result |
|---|---|---|---|
| 15 Nov 2026 | Test stack (Mac, Docker Desktop) | Fresh clone, project `commons-restore`, same Mac | Restore in 7 s. `verify-log` ok (14 entries, same head). Members, ledger entries, bookings and decision-log hashes identical to the source; levels identical (water 530 l). MAS issued tokens with the restored keys and listed all 7 accounts; Synapse up; Caddy's certificates restored. Not checked: a browser sign-in, because the drill had no hostnames; repeat on the real box (#7) |
