"""Admin command line.

A member is given by Matrix id or by username on this homeserver ("nora").

python -m app.cli bootstrap-admin alex          # first start only
python -m app.cli add-member nora --name Nora
python -m app.cli add-member @adelia:commons.theoutpost.network --role board
python -m app.cli grant-role nora board
python -m app.cli revoke-role nora board
python -m app.cli deactivate nora
python -m app.cli reactivate nora
python -m app.cli list-members
python -m app.cli set-guide                        # guide: from /config/community.yaml
python -m app.cli set-badges                       # badge rules: from /config/community.yaml
python -m app.cli verify-log                       # check the decision log (after a restore)
python -m app.cli anchor-log                       # post the log's fingerprint now (a release)

Role changes and deactivations are written to the decision log (actor null,
channel "cli"), so the command line loads the log signing key from the data
volume, or creates it on a fresh install, just like the app does at startup.
Run it inside the app container so both use the same key.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app import anchors, community, decision_log, members
from app.models import ROLES


class CliError(Exception):
    """A command that ran but found a problem (exit 1)."""


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser(
        "bootstrap-admin", help="make the first admin (refused once an active admin exists)"
    )
    b.add_argument("user")
    b.add_argument("--name", help="display name")
    b.add_argument(
        "--check", action="store_true", help="only check that bootstrapping is still possible"
    )
    a = sub.add_parser("add-member", help="add a member by Matrix id or username")
    a.add_argument("user")
    a.add_argument("--name", help="display name")
    a.add_argument(
        "--role",
        action="append",
        choices=ROLES,
        default=[],
        help="extra role (repeatable); every member gets 'member'",
    )
    g = sub.add_parser("grant-role", help="give an existing member a role")
    g.add_argument("user")
    g.add_argument("role", choices=ROLES)
    r = sub.add_parser("revoke-role", help="take a role away (never the last admin)")
    r.add_argument("user")
    r.add_argument("role", choices=ROLES)
    d = sub.add_parser("deactivate", help="end a membership; history stays attributed")
    d.add_argument("user")
    e = sub.add_parser("reactivate", help="undo a deactivation")
    e.add_argument("user")
    sub.add_parser("list-members", help="list members and roles")
    gd = sub.add_parser("set-guide", help="replace the guide with the one in community.yaml")
    gd.add_argument("--file", type=Path, default=Path("/config/community.yaml"))
    sub.add_parser("verify-log", help="verify the decision log's hash chain and signatures")
    sub.add_parser("anchor-log", help="post the decision log's fingerprint to the community room")
    bd = sub.add_parser("set-badges", help="make the badge rules the ones in community.yaml")
    bd.add_argument("--file", type=Path, default=Path("/config/community.yaml"))
    return p


def _roles(m) -> str:
    return ", ".join(sorted(m.role_names))


def run(
    args: argparse.Namespace, db: Session, server_name: str, signer: decision_log.Signer
) -> str:
    """Execute one command; returns the line to print. Raises MemberError."""
    log = {"signer": signer, "channel": "cli"}
    if args.cmd == "list-members":
        lines = []
        for m in members.list_members(db):
            state = "" if m.is_active else "  (deactivated)"
            lines.append(f"{m.matrix_user_id}  {_roles(m)}{state}")
        return "\n".join(lines) or "no members yet"
    if args.cmd == "anchor-log":
        entry = anchors.anchor(db, signer, "manual")
        if entry is None:
            raise CliError("no community room is set (or the log is empty); nothing was posted")
        db.commit()
        p = entry.payload_json
        return f"posted {p['anchored_seq']}:{p['anchored_hash']} to the community room"
    if args.cmd == "verify-log":
        result = decision_log.verify_db(db)
        if not result.ok:
            raise CliError(
                f"the decision log does not verify at entry {result.failed_seq}: {result.reason}"
            )
        return f"ok: {result.head_seq} entries verify; head {result.head_hash}"
    if args.cmd == "set-guide":
        config = community.load(args.file)
        changed = community.replace_guide(db, config, signer)
        db.commit()
        n = len(config.guide)
        return f"ok: guide replaced ({n} topics)" if changed else "ok: the guide is unchanged"
    if args.cmd == "set-badges":
        config = community.load(args.file)
        changed = community.replace_badges(db, config, signer)
        db.commit()
        n = len(config.badges)
        return f"ok: {n} badge rules set" if changed else "ok: the badge rules are unchanged"

    user_id = members.normalize_user_id(args.user, server_name)
    existing = members.get_by_matrix_id(db, user_id)

    if args.cmd == "bootstrap-admin":
        if members.has_active_admin(db):
            raise members.MemberError(
                "COMMONS already has an admin; an admin adds members on the admin page"
            )
        if existing is not None and not existing.is_active:
            raise members.MemberError(f"{user_id} is deactivated; reactivate it first")
        if args.check:
            return f"ok: {user_id} can become the first admin"
        if existing is None:
            m = members.add_member(db, user_id, args.name, ("admin",), **log)
        else:
            m = existing
            members.grant_role(db, m, "admin", **log)
        members.queue_welcome(db, m)  # the bot opens a private chat and says hello
        db.commit()
        return f"ok: {m.matrix_user_id} is admin ({_roles(m)})"

    if args.cmd == "add-member":
        m = members.add_member(db, user_id, args.name, tuple(args.role), **log)
        members.queue_welcome(db, m)
        db.commit()
        return f"ok: added {m.matrix_user_id} ({_roles(m)})"

    if existing is None:
        raise members.MemberError(f"{user_id} is not a member")
    m = existing
    if args.cmd == "grant-role":
        members.grant_role(db, m, args.role, **log)
    elif args.cmd == "revoke-role":
        members.revoke_role(db, m, args.role, **log)
    elif args.cmd == "deactivate":
        members.deactivate(db, m, **log)
    elif args.cmd == "reactivate":
        members.reactivate(db, m)
    db.commit()
    state = "" if m.is_active else ", deactivated"
    out = f"ok: {m.matrix_user_id} ({_roles(m)}{state})"
    if args.cmd in ("deactivate", "reactivate"):
        # The web page does this itself; the command line has no MAS admin token.
        verb = "lock-user" if args.cmd == "deactivate" else "unlock-user"
        localpart = m.matrix_user_id[1:].split(":", 1)[0]
        out += (
            f"\nalso {verb.split('-')[0]} the chat account: docker compose exec mas "
            f"mas-cli manage {verb} --config /run/secrets/mas_config {localpart}"
        )
    return out


def main(
    argv: list[str] | None = None,
    *,
    sessionmaker: Callable[[], Session] | None = None,
    server_name: str | None = None,
    key_dir: Path | None = None,
) -> int:
    """Tests pass `sessionmaker`, `server_name` and `key_dir`; otherwise they come
    from the environment."""
    args = _parser().parse_args(argv)
    if sessionmaker is None or server_name is None or key_dir is None:
        from app.config import load_settings, read_secret
        from app.db import make_engine, make_sessionmaker, upgrade_database

        settings = load_settings()
        engine = make_engine(settings.database_url, lambda: read_secret("database_password"))
        upgrade_database(engine)
        sessionmaker = make_sessionmaker(engine)
        server_name = settings.matrix_server_name
        key_dir = settings.key_dir
    with sessionmaker() as db:
        try:
            with db.begin():
                signer = decision_log.init_signer(db, key_dir)
        except decision_log.DecisionLogError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        try:
            print(run(args, db, server_name, signer))
        except (members.MemberError, community.CommunityConfigError, CliError) as e:
            db.rollback()
            print(f"error: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
