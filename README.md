# COMMONS

A community-owned, locally hosted operating system for sharing resources and self-governance. It was first built for the Outpost community.

## Design principles

The rules COMMONS is built to, and that changes to it should keep.

### Runtime and hosting

- **On-premise, offline-capable runtime:** application services must run on community-owned hardware and remain usable without an internet connection after downloading and verifying necessary images, packages, and model weights during installation.

- **No hosted runtime dependencies:** no hosted AI APIs, external authentication, telemetry, analytics, error reporting, CDN scripts, styles, fonts, or other third-party runtime calls. Frontend assets must be included. The runtime model configuration must set `HF_HUB_OFFLINE=1`.

- **Network boundary:** only Caddy may publish host port 443. App, Matrix, bot, and model services must not publish host ports. Internal service traffic belongs on the Docker `internal: true` network. The model server must never be directly reachable from the LAN.

- **No public exposure:** use HTTPS on the local network and WireGuard for remote access. Do not port-forward the application to the public internet.

- **Supported footprint:** target Python 3.12 and modest x86 hardware with 8 GB RAM and no GPU. Keep images and dependencies compatible with arm64/Raspberry Pi 5.

### Security and data protection

- Every service runs as a non-root user with a read-only root filesystem. It has explicit tmpfs for writable temporary paths, all Linux capabilities dropped, `no-new-privileges`, memory and process limits, and a health check. No privileged containers or Docker socket mounts are allowed.

- Pin container images by SHA-256 digest and Python dependencies with lockfiles and hashes. Verify model weights against a recorded SHA-256 checksum.

- Never commit `.env`, secrets, signing keys, real member data, model weights, or production data. Store tokens as hashes where possible; never log secrets or include them in model prompts.

- Members sign in using their Matrix account (accounts are created with single-use admin invites; every account is a member; COMMONS does not store passwords). Use server-side sessions, CSRF protection, secure cookies, rate limits, and explicit server-side authorization checks on every protected route and internal API.

- Validate inputs, use parameterized database operations, escape rendered user data, and apply a strict Content Security Policy. Treat member messages and model outputs as untrusted.

- Collect only necessary personal information. Protect the host disk and maintain encrypted backups along with a tested restore process before handling real community data.

- The signed decision record is append-only. No updates or deletions to its rows are allowed. Each state change and its audit entry must be committed atomically.

### Product and architecture

- **Tailored to the community:** categories, units, thresholds, spaces, roles, voting rules, and badge rules are stored in the database, seeded from `community.yaml`. Do not hard-code Outpost-specific values in Python constants.

- **Small, open models only:** use a model with openly available weights and a training recipe from a research or public interest lab. Do not use models from Meta/Llama, Google/Gemma, Alibaba/Qwen, Microsoft/Phi, or Mistral. The planned default is SmolLM3-3B, served by `llama.cpp` `llama-server`. Forecasting will use Holt smoothing instead of an LLM.

- **The model suggests; application code decides:** model output must match a permit-listed, schema-constrained intent. Regular code resolves entities, validates inputs, checks the sender's permissions, and performs writes. No model tools, URL fetching, secret access, or access to unrelated members' data. If inference fails or is uncertain, use the rule-based fallback or ask the member.

- **Matrix is the messenger:** a self-hosted Synapse homeserver with Matrix Authentication Service (MAS), federation off, and a `mautrix-python` bot. The bot has no database access; it calls the app's internal API, and the app enforces the sending member's permissions.

- **One service path for writes:** web and Matrix callers use the same service-layer functions and permission checks. Record the acting member and channel (`web`, `matrix`, or `sensor`) for attributed actions.

- **Events, not polling:** services publish events after successful writes. SSE clients and the bot subscribe. Maintain one app worker while using the in-process event bus.

- **English UI and bot replies:** UI and bot strings are in `app/i18n/en.py`.

- **Governance stays structured:** proposals include a question and clear options, followed by member voting and a recorded result. No discussion threads, comments, or forums. Ballots are secret, can be changed until closing, and limited to one current vote per member/proposal.

### Data invariants

- Resource levels come from ledger entries, never edited as standalone balances.

- Resource entries can be edited by their author for 15 minutes; further changes are corrections connected to the original.

- Bookings for the same bookable item cannot overlap; PostgreSQL enforces this with an exclusion constraint.

- Categories and spaces with history are archived instead of deleted.

- Store timestamps in UTC and display them in `Europe/Stockholm`. Parse chat dates as day.month.year (for example, `01.12.26`).

- Votes have one current row per proposal/member; changing a vote is a one-way process.

- Decision-log verification checks contiguous sequence numbers, canonical hashes, previous-hash links, signatures, anchors, and tally replay.

- Every model change requires a new Alembic migration; do not modify a merged migration.

## Requirements

- Python 3.12

- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the locked Python environment

- Git and Make

- Docker Engine with the Docker Compose plugin for container deployment (not necessary for local checks)

On macOS with Homebrew, install uv with `brew install uv`. On macOS or Linux, use the standalone installer:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal if necessary, then check that the tools are available:

```sh
python3.12 --version

uv --version

git --version

make --version
```

## Set up the development environment

Clone the repository and install the exact versions from `uv.lock`:

```sh
git clone https://github.com/the-outpost-network/commons.git

cd commons

uv sync --locked --all-groups --no-install-project
```

`uv sync` creates `.venv` and installs runtime and development dependencies, including Ruff, pytest, pip-audit, and Bandit. Use `uv run` to execute a command in this environment:

```sh
uv run --locked --no-sync ruff check .

uv run --locked --no-sync pytest

uv run --locked --no-sync python scripts/preflight.py .

uv run --locked --no-sync pip-audit

uv run --locked --no-sync bandit -r app bot
```

The preflight check will exit with an error if it finds an `ERROR`; warnings will not cause a failure. Live-LLM tests are excluded by default and need a running local model server.

Equivalent Make targets are available after installing the dependencies:

```sh
make lint

make test

make preflight

make audit
```

## Run the stack

Everything operates from the single `docker-compose.yml` on one box. Docker Engine and the Compose plugin are required.

| Service | What it is |

|---|---|

| `caddy` | Edge proxy with TLS, the only published port (443) |

| `matrix` | Synapse homeserver, federation off |

| `mas` | Matrix Authentication Service: accounts, login, tokens |

| `postgres` | PostgreSQL 17 with separate databases and roles for COMMONS, Synapse, and MAS |

| `app` | COMMONS: web pages, internal API, chat assistant |

| `llm` | SmolLM3-3B in llama.cpp, internal network only |

| `bot` | Matrix bot `@commons`, forwards chat to the app |

| `egress` | Allowlist proxy, the only way out (push to `matrix.org`, SMHI weather) |

The deployment uses `commons.theoutpost.network` as the permanent Matrix server name and web hostname, plus `matrix.commons.theoutpost.network` for Synapse and `auth.commons.theoutpost.network` for MAS. The script `scripts/update_dns.sh` sets A records for these three hosts in the delegated deSEC zone `commons.theoutpost.network`. Personal developer domains should not be used as the deployment or Matrix server name.

**First start** (`make init` preserves existing files when re-run after a successful initialization; Docker Engine must be running since it uses the pinned MAS image to create MAS keys):

```bash
make init # app + Synapse/MAS bootstrap secrets and local config

make fetch-model # the model (1.9 GB), checked against deploy/models.sha256

scripts/update_dns.sh # set DNS to the detected LAN IP (or pass an IP)

docker compose up -d --build matrix mas postgres # the bot account needs MAS running

scripts/bot_token.sh # create bot account/token; requires Matrix and MAS running

COMMONS_TLS=desec COMMONS_HOSTS=commons.theoutpost.network \

COMMONS_MATRIX_HOST=matrix.commons.theoutpost.network \

COMMONS_AUTH_HOST=auth.commons.theoutpost.network make up

make bootstrap-admin NAME=yourname # your Matrix account + the first COMMONS admin
```

`make init` handles both pre-start Matrix setup scripts. It creates the Synapse/MAS/PostgreSQL secrets and configuration under `secrets/matrix/`, and sets up the COMMONS MAS admin client. You do not need to run `scripts/synapse_trial_init.py` or `scripts/commons_admin_client.py` separately. Python dependencies must already be installed, and Docker must be ready to generate MAS keys with the pinned image. If initialization fails partway through, examine the reported files before retrying; the scripts do not overwrite partially generated files by design.

For remote testing over Tailscale, first install and sign in to Tailscale. Then run `scripts/update_dns.sh "$(tailscale ip -4)"`. Alternatively, use the automatic LAN-IP detection mentioned above, or provide the server's LAN IPv4 address directly, like this: `scripts/update_dns.sh 192.168.1.20`. The token in `secrets/desec_token` must give access to the delegated `commons.theoutpost.network` zone. If you receive a deSEC HTTP 404 error, the requested zone is not available for that token; check that the token has access to that exact zone. Do not paste or share the token.

**Matrix server name is permanent.** Set up `commons.theoutpost.network` before starting Synapse for the first time. Matrix IDs will take the form of `@name:commons.theoutpost.network`. If Synapse has already initialized a database with a different server name, stop and back it up before proceeding. Changing the YAML file alone will not migrate existing Matrix accounts or rooms. If you have already run `scripts/synapse_trial_init.py` but Synapse hasn’t started yet, refresh the generated MAS URLs while keeping its secrets by running `.venv/bin/python scripts/synapse_trial_init.py --refresh-hostnames`.

Running `make init` also creates the app secrets and copies the `community.yaml` and `.env` example files. You cannot create the bot access token until MAS is running, so do this in the previous step. The app generates the local decision-log signing key on its first start (`/data/keys`, mode 0600; make sure to back it up). Upon first start, the app also checks `community.yaml` and seeds the community configuration into the database, recording each section in the decision log. If there is an issue with the file, the app refuses to start and lists problems with their locations (e.g., `resource_categories[1] (firewood).unit: is required`). After this, the database becomes the source of truth, and editing `community.yaml` won’t change anything. After bootstrapping the bot, use `make up`, `make logs`, and `make down` (with the same three variables) to start, monitor, and stop the stack. The `make up` command will not start if a required secret or the model is missing.

`secrets/`, `community.yaml`, and `.env` are ignored by git. Compose mounts secrets as read-only at `/run/secrets/<name>`, not as environment variables. The app refuses to start if any secret is missing or empty. On a Linux host, run `sudo chown 10001:10001 secrets/*` once after `make init` since the containers run as uid 10001. Docker Desktop on macOS does not require this.

**Certificates** (`COMMONS_TLS`, details in [docs/SECURITY.md](docs/SECURITY.md)):

- **`desec`: Let's Encrypt, trusted by every phone and Element X.** Requires a deSEC token for the zone in `secrets/desec_token` (`pbpaste > secrets/desec_token && chmod 600 secrets/desec_token`).

- **`internal`: Caddy's own CA, fully offline.** Members install the root certificate ([docs/phone-certificate-setup.md](docs/phone-certificate-setup.md)). Element X does not accept it, so it is utilized only as a fallback. Start with `COMMONS_HOSTS=<ip> COMMONS_DEFAULT_SNI=<ip> DESEC_TOKEN_FILE=/dev/null make up`, then export the certificate with `scripts/caddy_ca.sh commons-root.crt`.

Checks can be performed with: `scripts/check_caddy.sh commons.theoutpost.network` (TLS and headers), and `scripts/check_login_limit.sh matrix.commons.theoutpost.network` (login rate limit).

**Backups:** Every night, `scripts/backup.sh` (via a systemd timer) saves the three databases, the decision-log signing key, media, the bot's keys, Caddy's certificates, and `secrets/` to a second disk, all encrypted with `age` using public keys whose private halves never reside on the box. Fourteen backups are kept, and a failure message goes out to every admin. Use `scripts/restore.sh` to restore a backup to a fresh clone. For setup, the step-by-step restore process and the drill log, see [docs/BACKUP.md](docs/BACKUP.md).

**Usage report:** `make usage-report` generates anonymous counts from the running box as Markdown (use `FORMAT=csv` for CSV; `SINCE=YYYY-MM-DD` excludes setup and test days). It uses a read-only database transaction, does not include demo data, and outputs no names, Matrix IDs, or notes. Undone entries and cancelled bookings do not count as usage.

**Demo data:** `make seed-demo` rebuilds the separate `commons_demo` database with three days of made-up use, written through the actual services and with its own signing key (the running app container must be built from this version). It uses the resources, bookable things and vote types from `community.yaml`: readings and use of every resource (one close to its warning level, a corrected typo and an undone double entry), bookings past and ahead, and three votes (one passed, one cancelled, one still open). Three days are too few for a forecast. Members are fictional unless a git-ignored `demo-members.json` lists real users: `[{"localpart": "ana", "name": "Ana", "roles": ["admin", "board"]}]`. To show it, start the stack with `COMMONS_ENV=demo COMMONS_DB_NAME=commons_demo COMMONS_DATA_DIR=/data/demo make up`, and mark it as demo data. The app will not accept any other combination of mode and database, and the usage report will not include this database. The open vote works like a real one: the bot reminds members who haven't voted on the closing day (with `demo-members.json`, that messages real accounts) and it closes on its own. Decision-log entries are signed during seeding, so `/log` shows the seeding time for them.

**Hardening:** Only Caddy publishes a port. Every other service is on the `internal` network, which has no route out; only `egress` also connects to `outbound`. All services operate as non-root users with read-only root filesystems, dropped capabilities, `no-new-privileges`, and limited memory and processes; registry images are pinned by digest.

## Accounts and members

Each member has one account: their Matrix account. **Every account on our homeserver is a COMMONS member,** with no other way in: federation is off, and signing up requires a single-use invite code from an admin. A new account becomes a member (role `member`) the first time it signs in to COMMONS or communicates with the bot.

The dashboard features "Sign in with Matrix" (OAuth with MAS). COMMONS does not store passwords, only the identities of members and their roles. Roles include: `member` (everyone), `board`, `admin`.

**First admin.** In a fresh install, `make bootstrap-admin NAME=yourname` creates the Matrix account in MAS (you set the password in the MAS prompt) and makes it the first COMMONS admin. Use `MAS_ACCOUNT=existing` if the Matrix account already exists. This command will not work once COMMONS has an active admin, and it verifies that before touching MAS.

**Inviting someone.** Sign in as an admin, go to **Manage members** (`/admin/members`), and click on **Create invite** (you can include an optional note like "for Nora"). Send the link to the person; it appears only once, works once, and expires after 24 hours (`COMMONS_INVITE_HOURS`). The link leads directly to MAS's "Create an account" page, where they can choose their username and password, and the invite code is filled in automatically (COMMONS provides it to MAS in a short-lived cookie; MAS's registration-token page is replaced by our version in `deploy/mas/`, which submits it). MAS then signs them in to COMMONS. The code is also visible to the admin for someone who signs up through Element's "Create account" screen. No email is required. Unused invites can be retracted on the same page.

**Managing members.** Through **Manage members**, admins can grant or revoke the `board` and `admin` roles and deactivate or reactivate members. Deactivating ends the member's COMMONS sessions immediately and locks their Matrix account in MAS (no sign-in, no chat); reactivating unlocks it. Their history remains attributed to them. **Delete** (with a confirmation page) is reserved for test accounts and mistakes: it removes the member from COMMONS and permanently closes their chat account (MAS deactivates it and Synapse deletes its profile and messages; MAS does not truly delete accounts, and the username can never be reused). This only applies to members for whom COMMONS has no records (e.g., no invites created, no roles assigned); all others will be deactivated instead. COMMONS always maintains at least one active admin: the last admin cannot lose their role or be deactivated, so appoint someone else as admin first. The same actions can be taken from the command line (a member is identified by username or Matrix ID); `deactivate` and `reactivate` commands print the `mas-cli` command that locks or unlocks the chat account:

```bash
docker compose exec app python -m app.cli grant-role nora admin

docker compose exec app python -m app.cli revoke-role nora board

docker compose exec app python -m app.cli deactivate nora

docker compose exec app python -m app.cli reactivate nora

docker compose exec app python -m app.cli list-members
```

Roles: `member`, `board`, `admin`. The COMMONS database has its own role and does not access Matrix databases. The app applies committed Alembic migrations before serving requests. Migration `0001` creates the PostgreSQL 17 core schema (identity, configuration, decision log). Migration `0002` adds invites, resource categories, and the ledger.

Passwords: Members can change their passwords in their account settings at `auth.<domain>`. There is no email system, so there is no option to "forgot password." An admin can reset a password using `docker compose exec mas mas-cli manage set-password --config /run/secrets/mas_config <username>` (the command will ask for the password).

Invites, role changes, and deactivations are recorded in the signed decision log, from both the admin page and via `python -m app.cli` (reactivation is not yet logged). The COMMONS database has its own role and no access to Matrix databases. Each model change is shipped as a new Alembic migration.

Tests run against a temporary PostgreSQL container: `make test` starts it (`make test-db`, port 127.0.0.1:55432) and runs pytest.

## Community settings and resources

`community.yaml` outlines the community: its name, the resources it tracks, the items members can book (spaces, vehicles, tools), roles, voting rules, badges, and a brief **guide** (house rules, chores, waste disposal, how to get there). The chat assistant reads the entire guide with each message, allowing members to ask questions like "where does the cardboard go?" or "when is dinner?" Keep it under 8,000 characters. A box created before the guide was available reads that section from the file on its next start, just once. If you need to change the guide later (there isn't an admin page for it yet), edit `community.yaml` and run `docker compose exec app python -m app.cli set-guide`. Both the old and new text will go into the decision log. Badge rules work the same way: edit the `badges:` section and run `docker compose exec app python -m app.cli set-badges`. Rules missing from the file will be archived, while badges members have already earned will remain theirs. Badges only account for participation (logged entries, consecutive days, completed bookings) and not how much anyone uses them. Each member can see their badges in **My contributions** (`/me`, which includes a download of all their own data) or by asking the bot "my badges". Run `make init` to copy it from `community.yaml.example`, and please review it before the first start. Compose mounts it as read-only into the app.

**It is read once.** On the first start, the app checks the file and copies it into the database (`config_kv` remembers this). If there is a problem with the file, the app will stop and display a message indicating the issue, for example, `resource_categories[1] (firewood).kind: must be stock or flow`. After that, the database becomes the authoritative source, and the file won’t be read again. Editing the file will not change anything. To date, the community name, resources, bookable items, and booking settings have been copied over. Voting and badges will follow with their features. A box created before bookings existed will read the file one more time, but only for the bookings sections. There is one exception: voting rules saved before vote types were established, or those missing because the box was created before the voting rules were copied, will be replaced once at startup by the `voting:` section in the file (marked as `config.changed`).

**Resources** (water, firewood, etc.) are managed by admins under **Community settings → Resources** (`/admin/resources`). This allows them to add, edit, archive, restore, reorder, and delete resources. Each resource has a name, a fixed ID (`slug`, used in chat and the history, which can't be changed), a unit, a type (`stock`: a level that is monitored and replenished; `flow`: only records use and refill events), an optional "warn below" level, an optional capacity, a forecast horizon, and chat words (aliases like "wood" for firewood). Each word (ID, name, chat word) can only represent one resource. A resource that has a history cannot be deleted, only archived, to keep its history accessible; archived resources will no longer appear in chat or on the board. The board (`/`) displays the active resources along with their levels.

**Usage history** (admins only): **History** is available next to each resource, including archived ones (`/admin/resources/<id>/history`). It shows a period (default is the last 90 days, but can be up to 366 days): the level at the end of each day (or daily use for `flow` resources; weekly for periods over 92 days) presented as a chart, along with the start and end levels, total amounts used and added, and each entry detailing what it counts for today, the level after it, and any corrections noted below. The numbers come from the same calculations used for the level displayed in chat. **Download CSV** provides the same rows (`entry_id, occurred_at, logged_at, type, amount, unit, counted_amount, status, corrects_id, level_after, member, channel, note`, with times in the community's time zone). This file includes member names, so it should only be shared with admins.

**The ledger** (`app/ledger.py`) is the sole code path that writes resource data. Chat, the web board, and later sensors all utilize it, as do members who log entries. An entry consists of a `reading` (the current level), a `use`, an `add` (refill), or a `correction`. Amounts can be at most twice the capacity (or 1,000,000 if there is none), for up to 30 days in the past and never in the future; a `flow` resource doesn’t require readings. Deactivated members and archived resources will not be accepted. The level is not stored; instead, it is calculated as the latest reading plus the adds minus the uses that follow. Until a resource gets its first reading, the level is counted from zero (adds minus uses); the board and the bot will indicate "counted from 0" and suggest logging a reading. The author can edit an entry for 15 minutes; thereafter, either the author or an admin can make a correction, which will replace the counted amount while keeping the original visible. An undo action creates a `void` correction: the entry stays in history but no longer counts (subsequent corrections with a value will restore it). `ledger.fix_entry` determines whether an edit or correction is made, ensuring consistency between chat and the web. Nothing is ever deleted, and a PostgreSQL trigger prevents deletions and late edits from outside the app. After each committed write, the ledger publishes an event (`app/events.py`) for the community room and alerts.

**Bookings** (`app/calendar.py`) operate like a library. The web calendar and chat booking rely on them. A member can book a bookable item now or for a future date. Indicating an end date is optional and serves only as a guideline: COMMONS cannot compel anyone to return a thing but serves as a reminder. A booking may include a note (e.g., "cutting firewood"). For vehicle bookings, members can specify their destination, allowing others to request an errand or join. If two members try to book the same thing simultaneously, the database will prevent overlaps. A booking without a specified end blocks the item for 2 hours from its start (this is a community setting); afterwards, others can book it, and COMMONS will notify anyone who still has it. Late returns are recorded, but extensions are never done automatically. Members can change, extend (when the time is available), return, or cancel their own bookings; admins have the authority to change or cancel anyone's bookings, which will be logged. Bookings are never deleted (a trigger prevents this), and every booking and change gets logged in the COMMONS room. **Reminders** (`app/reminders.py`): when a vehicle or tool with an end has not been returned `reminder_every_hours` after that end (usually 2 hours), COMMONS sends the member a private message in their chat with the bot. This message states when it was due, who booked it next, and instructions for returning or extending the item. Extending the booking creates a new end date with its own reminder; spaces do not require reminders (a space is available again after its end). No messages are sent during quiet hours (22:00–8:00 by default; if a reminder is due then, it is sent at 8:00) or for items overdue by more than `reminders_stop_after_hours`. All of these are community settings under `bookings:` in `community.yaml`. Repeating the reminder every 2 hours, reminders for bookings without an end, and notifying admins after 24 hours have not been implemented yet.

**Calendar** (`/calendar`, every member): This view offers a month and week overview of all bookings, filterable by type (spaces, vehicles, tools) or by items, in the community's time zone. "Out right now" displays who has what. Members can book on the web using the same rules and restrictions as in chat (e.g., "Saw is with Ana since 14:00"). They can change, return, or cancel their own bookings on the booking's page; admins can change or cancel anyone's bookings (this action is logged). Every booking is visible to all members.

**Phone calendar** (`/calendar/phone`): Each member can create one secret link (`/calendar/ics/<token>.ics`) to subscribe to in their phone's calendar. This link currently only shows their own bookings. It will only appear once; only the SHA-256 is stored, it never shows in the access log, and it stops working if the member cancels it, creates a new one, or is deactivated. This link is only functional in areas where COMMONS is available (LAN or VPN), which means Google Calendar's "subscribe by URL" does not work with it, but iPhone, Mac, and Android devices using ICSx⁵ can retrieve it from the device.

**Bookable items** are managed by admins under **Community settings → Bookable** (`/admin/bookable`). This includes a form with a name, a type (space, vehicle, or tool), the location, and a description. The ID is generated from the name and never changes; active names must be unique (regardless of case), allowing chat to identify a thing without confusion. A thing with bookings cannot be deleted, only archived; archived items cannot be booked.

**Every settings change is logged.** Adding, editing, archiving, restoring, or deleting a resource or a bookable item creates a signed `config.changed` entry in the same transaction, listing the admin as the actor along with the old and new values (key, e.g., `bookable.saw`). Saving a form with no changes logs nothing; the order of resources is not recorded.

On the board, each resource card displays the level, a bar if the capacity is known, "OK" or "Running low", the last recorded level, and who logged it. There is also a **Log** button that opens the form (current level / used / added, amount, time, and an optional note). The time section is pre-filled with the current date and time in the community’s time zone; if left unchanged, the entry will record the moment it is saved, meaning that leaving the page open for hours won't backdate it. Saving will show a dismissible snackbar at the bottom for eight seconds; hovering, keyboard focus, or a hidden tab will pause it. Refreshing or navigating back does not repeat the confirmation. If JavaScript is not enabled, the confirmation will remain viewable inline. Validation errors will appear below the affected field, which will be highlighted and linked to its message for screen readers, while general errors will appear otherwise. Rejected submissions keep the form open and preserve the entered values. Below the cards: the recent entries, corrections included.

The web pages use one stylesheet with design tokens (`app/static/app.css`, served by the app itself) and one small first-party script (`app/static/app.js`, which closes the account menu on a click outside it and adds a Copy button next to one-time links; everything works without it), and work on phones: settings tables turn into one card per row, and the calendar's month view becomes a list of the days with bookings. The fonts (Public Sans for text, Newsreader for the wordmark and titles) are vendored under `app/static/vendor/fonts` with their OFL licences and checksums; nothing is loaded from elsewhere. The footer links to the source code, as the AGPL asks.

## Forecasts

`app/forecast.py` forecasts each active resource with a small classical model in numpy, no language model: exponential smoothing with a damped trend (Gardner and McKenzie; Holt's linear method when the damping is off, simple exponential smoothing for short series). It was among the most accurate methods in the M3 forecasting competition and runs in milliseconds. What it forecasts is daily use, not the level: use is a logged `use`, or the drop between a reading and the level before it, spread over the days in between (a reading above the previous level is an unlogged refill, not negative use). The series is the full days between the first and the latest log; with fewer than 4 it refuses and says why. The smoothing parameters come from a small grid search on one-step-ahead errors. For a `stock` resource with a warning level, the level is projected forward from the latest log, and the forecast gives the day it likely drops below that level with an 80 % range; `flow` resources get expected use per day. Every run is stored in `forecast_run` with its data basis (days, entries, period), parameters and result. The app reruns a forecast when new logs arrive (checked every 15 minutes, `COMMONS_FORECAST_CHECK_SECONDS`) and at least daily. A new likely shortage goes to the COMMONS room once, as an alert, and again only after the forecast cleared or the date moved more than 2 days earlier. Members see the forecast when they ask the bot about levels.

## Votes

Any member proposes a vote under **Votes** (`/proposals`); every member votes in the browser with one tap. There is no discussion in COMMONS; on an open vote, other members get a link that opens a private chat with the proposer in their own Matrix app (a `matrix:` link, nothing is stored here). A proposal is a vote type, a question (max 140 characters), an optional short "why", Yes / No plus up to `voting.extra_options` alternatives, and for types that ask for one (spending) an amount or "amount unknown". The type (`voting.types` in the configuration: spending 1 day, rules & thresholds 3 days, use of space / events 2 days, all drafts for the board to confirm) sets when voting closes: at 23:59 community time on the day that full period ends, so a 1-day vote opened Monday at 10:00 closes Tuesday at 23:59 (always at least the full period). Once published nothing can change; the board or an admin can only cancel it, with a reason.

A proposer can tick **Propose anonymously**: their name then isn't shown on the vote, in chat or in the decision log, and there is no message link. The database still records who proposed it, so someone with direct database access can find out. Each member has one current vote, for an option or **Abstain**, and can change it until the vote closes; the old vote is kept, marked as replaced. **Ballots are secret**: pages show anonymous counts (for event votes only after the close) and turnout, and each member sees only their own choice, not even the board or the admins see more. Times are shown in the community's timezone (Europe/Stockholm). The Board tab shows the open vote that closes first.

The rules are copied from `voting` when the vote opens, so later changes don't affect a running vote. A vote closes when its window ends (the app checks every minute, so within a minute of 23:59) or as soon as every active member has voted or abstained. It **passes** when one option has more than the vote's passing share of the votes cast: `threshold_pct` (50 %) unless the proposer set a higher one when opening it (e.g. 66.67 for two thirds; never below 50 %, which the database also refuses), shown on every vote card; abstentions don't count as cast, and there is no quorum unless `quorum_pct` is set. Otherwise (a tie, no majority, nobody voted) it is **rejected** and can be proposed again right away. Past votes can be filtered by type and result, and link to their decision-log entries.

Opening, every vote and change, closing (with the counts) and cancelling are written to the signed decision log in the same transaction as the change itself. The `/log` page lists only votes opening, closing with their outcome, and being cancelled; it verifies the whole chain, including the entries it doesn't list.

**Checking the log.** Verifying the chain (on every load of `/log`, with its **Verify chain** button, `GET /api/log/verify`, and `python -m app.cli verify-log`) checks the hashes, links and signatures and also recounts every closed vote from the log alone (`app/chain_verify.py`): votes only between opening and close or cancellation, a first `vote.cast` and later `vote.changed` per member, a turnout that matches the members who voted, counts that add up, the rules it opened with, and an outcome that `tally` gives from the logged counts. Ballots are secret, so the replay can't tell whether a vote was moved from one option to another without changing the outcome; the page says so. Any member can download the whole log (`GET /api/log/export`, JSON Lines) and the public keys (`GET /api/log/keys`) and check them on their own computer with the same code, which needs only Python and `cryptography`: `python3 scripts/verify_log.py commons-decision-log.jsonl --pubkeys commons-log-keys.json [--anchor seq:hash ...]` (or `make verify-log LOG=… KEYS=…`). It exits 0 when the log verifies, 1 when it doesn't, 2 when the files can't be read. Tests: `tests/test_verify_log.py`.

**Checkpoints in the community room** (`app/anchors.py`). After every closed vote, and once a day when the quiet hours end (`bookings.quiet_hours_end`, 08:00 by default), COMMONS posts the log's latest entry number and fingerprint in the community room as `Decision log checkpoint 412:ab34…`, the form `verify_log.py --anchor` takes. Members' phones then hold copies the box can't take back: a cut or rewritten log, even one re-signed with the box's own key, fails against them. Each post is a signed `log.anchored` entry written in the same transaction as the room line; with no community room set nothing is posted. `python -m app.cli anchor-log` posts one by hand (e.g. at a release). Once a day a `ledger.digest` entry also seals the previous day's ledger entries (count, first and last id, SHA-256 of their canonical JSON; days without entries too, missed days caught up), so the ledger is tamper-evident as well. `/log` shows the last checkpoint and puts it into the verify command. Tests: `tests/test_anchors.py`. Vote entries say who voted, never what they chose. The pages call `app/governance.py`, the same code the Matrix bot will use; PostgreSQL itself refuses a second current vote, a rewritten or deleted vote, an option from another proposal, and changes to a proposal's question, amount or rules. Tests: `tests/test_governance.py`, `tests/test_governance_schema.py`, `tests/test_proposals_web.py`.

**Welcome.** When someone becomes a member (the first admin with `make bootstrap-admin`, an admin adding them, or their first sign-in), the bot opens an encrypted private chat with them and sends a welcome: what they can do there, how votes reach them, and "help". It waits up to 3 days for them to accept the invite, because an encrypted message can only be read by those already in the room. If a COMMONS room is set, the bot invites them to it at the same time. The welcome goes through the same outbox as the vote messages (`chat_notification`, kind `welcome`), so a bot restart loses none.

**Votes in the COMMONS room and taps.** When a vote opens, the bot posts it in the COMMONS room with an `@room` mention: the question, the options, the closing time and a pointer to the private chat. It has no emojis to tap, because the room never takes ballots: reactions there are public. It notifies everyone only if the bot may use `@room` there (power level 50). The result, or the reason for a cancellation, follows as a reply to that post. Each member also gets the vote, and the reminders, in their private chat with the bot, with the option emojis already added. Tapping one, or replying to that message with the number, the option or "abstain", is their ballot, sent to `POST /internal/votes` and cast with the same `governance.cast_vote` as the web (channel `matrix`). Another tap or reply changes the vote until it closes. Ballots and their confirmations are kept out of the chat history given to the local model. Removing a tap keeps the vote, and the bot says how to change it. Taps from non-members, deactivated members, after the close or too many per minute (`COMMONS_TAP_RATE_PER_MINUTE`, default 20) are refused politely. The app keeps which Matrix message shows which vote in `proposal_message`, so a tap only counts on the vote message sent to that member. The COMMONS room is never treated as a private chat, even if only one member and the bot are in it: no vote messages go there, and taps, replies and typed votes from it are not ballots.

**Votes in Matrix.** In a direct chat with `@commons`, ask "which votes are open?" and vote with `vote 12 1` (option number), `vote 12 yes`, `vote 12 abstain` or, with one open vote, `vote yes`; send it again to change your vote. This calls the same `governance.cast_vote` as the web (channel `matrix`). Only these exact forms cast a ballot; the model can only show the list. Board members and admins can write `cancel vote 12 because ...`; the number and the reason are both required, and the same `governance.cancel_proposal` permission check and decision-log entry apply as on the web. The decision log keeps the reason exactly as written; the chat message to members may show a clearer wording from the local model, marked as reworded, and falls back to the original if that wording could change the meaning. Group rooms can list votes but ballots are refused there, because the message would reveal the choice, and ballots are kept out of the assistant's conversation memory. The bot also messages each member directly: when a vote opens (everyone, including the proposer), on the closing day at `voting.reminder_times` (default 09:00 and 18:00 Swedish time, only members who haven't voted, and never outside `voting.reminder_window`, default 08:00 to 22:00) and when it closes or is cancelled (with the signed result). Each message states the vote's question as its title. Code writes every fact in it; the local model only adds one short neutral opening sentence, which is refused if it holds a link, mention, number, result or recommendation, and then replaced by a fixed sentence. A member who has no direct chat with the bot gets an encrypted invite and receives messages once they accept it. The messages wait in the `chat_notification` table, written in the same transaction as the change, so a bot restart loses none; the bot checks for new ones every 15 seconds through the internal API.

## Chat assistant with the local model

Members chat with `@commons:<domain>` in Element. The bot (`bot/`) only relays messages to the COMMONS app (`POST /internal/assistant`). The app (`app/assistant.py`) decides what to do, using SmolLM3-3B in llama.cpp to understand the message. The model only picks one allowed intent; code checks it and acts with the sender's own rights. This is how all future chat requests (bookings, resource entries, votes) will work.

**The model reads the chat.** With each message the bot sends the last 10 messages of that chat as they are in Matrix (the member's and COMMONS', reminders included), so "I returned it" after a reminder about the saw means the saw. COMMONS keeps no copy of the conversation. Every message goes to the model first; fixed phrasings (rules) answer only when the model server is down. A change is done at once when you named the thing ("I returned the saw"); when the model had to guess it from the chat ("I returned it") or took the time from an earlier message, COMMONS asks first ("Shall I …? (yes/no)"). "yes", "jep", "ja", 👍 confirm, "no", "nein", 👎 decline.

What it can do so far:

- **Log resources** ("we used 2 m³ of firewood", "water tank reading 800 l", "added 3 m³ wood", "we burned 1,5 m³ wood yesterday at 21:00"). The resource is found by its name or chat words, the unit is checked, and the entry is logged through the ledger as the sender. The bot repeats what it logged with the new level, warns below the threshold, and asks back instead of guessing when the resource, amount, type or unit is unclear. Questions are never logged.
- **Answer about the levels** ("how much water do we have?", "what's left?", "levels", "how much is left in the tank?"): the current level of one or all resources, how old the last reading is and who logged it, a warning below the threshold and a hint when the last reading is more than two days old. **"last 5 firewood entries"** lists the newest entries with their numbers (`#12`), who logged them, corrections and notes (at most 20). Every number is written by code from the ledger; the model at most picks the question and the resource word.
- **Undo and correct** ("undo", "that was wrong, it was 1.5", "no, the firewood was 1.5", "#42 should be 2", "undo #42"). Without a number it means the sender's own newest entry (of the resource they name) from the last 24 hours; older entries are fixed by their number from the history. Inside the 15 minutes the author's entry is edited; later, or when an admin fixes someone else's entry by number, a correction is added and the original stays. Other members can't change your entries. The bot repeats the entry with its value before and after and the new level. The model can't invent an entry number or an amount: both must appear in the message, and without an amount it undoes only when the member said so.
- **Book spaces, vehicles and tools** ("I want to use the theatre from 01.12.26 to 13.12.26 for a workshop", "I'm taking the saw", "I need the car tomorrow 9:00 to 13:00 to drive to town for shopping"), **ask** ("who has the saw?", "is the workshop free on Saturday?", "where is the car?", "what can I book?", "my bookings"), **give back** ("brought the saw back"), **keep longer** ("keep the saw until 18:00") and **cancel** ("cancel my theatre booking", "cancel booking #12", "cancel that" right after booking). Dates are day.month.year; times are always read by code from the message, never taken from the model. Everything goes through the same booking service as the web calendar, as the sender (`channel=matrix`), so the same rules and refusals apply. The bot repeats what it booked; on a conflict it says who has the thing and offers the nearest free time of the same length ("yes" books it). When the model had to guess the thing or the time, COMMONS asks "Shall I …? (yes/no)" first. Booking for someone else ("for Frida", "for @ana") is refused.
- **Change your own name**, in COMMONS and in the chat ("change my name to Nora", "call me Al from now on"). Requests about someone else's name are refused.
- **Tell you who you are**: name, account and roles ("who am I?").
- **The weather at the Outpost** ("how's the weather?", "will it snow this week?"): now, the next 12 hours, tomorrow and three more days, from SMHI open data. The app fetches it every 30 minutes through the egress proxy; only the coordinates leave the box (see `docs/SECURITY.md`).
- **Small talk.** `help` lists the commands.

### The COMMONS room

One shared Matrix room where everyone sees what happens, without being pinged for it:

- Every ledger write (log, edit, correction, undo) becomes one line in the `event_feed` table, in the same transaction as the change (`app/feed.py`). Code writes the line from the database; the model never does.
- The bot follows `GET /internal/events`, a Server-Sent Events stream with the service token (blocked by Caddy like all of `/internal`). It reconnects with `Last-Event-ID` and the app replays what it missed, so a bot restart loses nothing; its position is kept in the bot's volume. The bot never reads the database.
- Updates are posted as **notices** (`m.notice`), which Matrix' default push rules don't notify for: members see an unread count, not a ping. Lines within 20 seconds go into one message; a fix to an entry the bot posted in the last 15 minutes **edits that message** instead of posting again; later fixes get their own line. After a long outage, replayed lines older than a day are skipped.
- **Low-level alerts:** when a write takes a resource below its `shortage_threshold`, the room gets one normal message (so members are notified): "⚠️ Water is running low: 900 l left, below 1,000 l." No more alerts until the level is back at or above the threshold, which gets a quiet line; the next drop alerts again. Edits, corrections and undos count like any write. The alert state is the newest `ledger.level_low` / `ledger.level_ok` row in `event_feed`, so it survives restarts.
- The **room topic** shows the current levels ("COMMONS levels: Water 1,850 l · Firewood 1 m³ (from 0) (low)"). The bot needs moderator rights in the room for that and says so once if it doesn't have them.

Setting it up: create a room, invite `@commons:<domain>`, give it moderator rights (Room settings → Roles & Permissions), and as an admin write **"COMMONS, use this room for updates"** in it. Only admins can choose the room; the choice is stored in `config_kv` and signed into the decision log as `config.changed`. Tests: `tests/test_feed.py`, `tests/test_bot_feed.py`.

Model answers take about 1–6 seconds on an M-series MacBook, and the model uses about 2.4 GB of RAM. Tests: `tests/test_assistant.py`, `tests/test_chat_ledger.py`, `tests/test_chat_booking.py`, `tests/test_bot.py`; `tests/intent_cases.yaml` holds real-style messages, checked against the rules in every run and against the live model with `pytest -m llm` (set `COMMONS_TEST_LLM_URL` and `COMMONS_TEST_LLM_KEY`).

## Documentation

- Security controls and threat model: [docs/SECURITY.md](docs/SECURITY.md)
- Backups and restore: [docs/BACKUP.md](docs/BACKUP.md)
- Data model diagram: [`docs/data-model.drawio`](docs/data-model.drawio)
- Network overview: [docs/network-diagram.md](docs/network-diagram.md)
- Trusting the internal certificate on a phone: [docs/phone-certificate-setup.md](docs/phone-certificate-setup.md)

## License

Copyright (C) 2026 The Outpost Network and COMMONS contributors.

COMMONS is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.

SPDX-License-Identifier: `AGPL-3.0-or-later`
