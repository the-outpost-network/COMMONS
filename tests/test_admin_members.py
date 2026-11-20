"""Admin members page, member service invariants and the admin command line."""

from __future__ import annotations

import dataclasses
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from tests.conftest import SERVER, sign_in

from app import cli, decision_log, invites, members
from app.i18n import en
from app.models import AuthEvent, Invite, Member, Session, utcnow

ADMIN = f"@ada:{SERVER}"
NORA = f"@nora:{SERVER}"
# How the account menu shows it: a break opportunity after the colon.
NORA_SHOWN = f"<span>@nora:</span><wbr><span>{SERVER}</span>"


def csrf(client: TestClient) -> str:
    page = client.get("/").text
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', page).group(1)


def as_user(client, fake_matrix, user_id: str) -> str:
    fake_matrix.user_id = user_id
    assert sign_in(client, fake_matrix).status_code == 303
    return csrf(client)


@pytest.fixture
def log(log_signer) -> dict:
    """Keyword arguments for member changes made directly in a test."""
    return {"signer": log_signer, "channel": "cli"}


@pytest.fixture
def ada(db, log_signer):
    m = members.add_member(db, ADMIN, "Ada", ("admin",), signer=log_signer)
    db.commit()
    return m


@pytest.fixture
def admin_client(client, fake_matrix, ada, nora):
    token = as_user(client, fake_matrix, ADMIN)
    client.token = token
    return client


def reload(db, m: Member) -> Member:
    db.expire_all()
    return db.get(Member, m.id)


# Who may call which admin route: tests/test_authz_matrix.py.


def test_home_links_admin_page_only_for_admins(client, fake_matrix, ada, nora):
    # The Settings tab (resources, things to book, members) shows for admins only.
    as_user(client, fake_matrix, NORA)
    assert 'href="/admin/resources"' not in client.get("/").text
    as_user(client, fake_matrix, ADMIN)
    assert 'href="/admin/resources"' in client.get("/").text
    assert 'href="/admin/members"' in client.get("/admin/resources").text


def test_admin_sees_members(admin_client):
    page = admin_client.get("/admin/members")
    assert page.status_code == 200
    assert NORA in page.text
    assert ADMIN in page.text


@pytest.mark.parametrize("token", [None, "", "0" * 64])
def test_admin_posts_need_csrf(admin_client, db, nora, token):
    data = {"role": "board"} if token is None else {"role": "board", "csrf_token": token}
    r = admin_client.post(f"/admin/members/{nora.id}/grant", data=data)
    assert r.status_code == 403
    assert reload(db, nora).role_names == {"member"}


# -- invites -------------------------------------------------------------------


def create_invite(client, note: str = "for Bob"):
    r = client.post("/admin/invites", data={"note": note, "csrf_token": client.token})
    assert r.status_code == 200, r.text  # after the redirect to /admin/members
    assert r.history[0].status_code == 303
    link = re.search(r'id="invite-link"[^>]*>(https://[^<]+/join/([A-Z0-9-]+))</code>', r.text)
    return r, link.group(1), link.group(2)


def test_create_invite_shows_link_once_and_stores_only_a_hash(admin_client, fake_matrix, db, ada):
    _, link, code = create_invite(admin_client)
    assert invites.CODE_RE.match(code)
    assert link.endswith(f"/join/{code}")
    # MAS got a single-use token with an expiry.
    (token,) = fake_matrix.reg_tokens.values()
    assert token["token"] == code
    assert token["usage_limit"] == 1
    expires = datetime.fromisoformat(token["expires_at"])
    assert timedelta(hours=23) < expires - utcnow() <= timedelta(hours=24)
    # COMMONS keeps the hash, never the code.
    invite = db.scalar(select(Invite))
    assert invite.code_hash == invites.hash_code(code)
    assert invite.created_by == ada.id
    assert invite.note == "for Bob"
    dump = " ".join(str(v) for v in db.execute(text("SELECT * FROM invite")).one())
    assert code not in dump
    # The list shows the invite, but not the link again.
    page = admin_client.get("/admin/members").text
    assert "for Bob" in page
    assert code not in page


def test_reloading_after_creating_an_invite_creates_no_second_one(admin_client, fake_matrix, db):
    r = admin_client.post(
        "/admin/invites",
        data={"note": "once", "csrf_token": admin_client.token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/members"
    first = admin_client.get("/admin/members").text
    assert "/join/" in first
    # Reloading is a GET: no new invite, and the link is not shown again.
    again = admin_client.get("/admin/members").text
    assert "/join/" not in again
    assert len(fake_matrix.reg_tokens) == 1
    assert db.scalar(select(func.count()).select_from(Invite)) == 1


def test_new_invite_link_is_only_for_the_admin_who_made_it(app, admin_client, fake_matrix, db):
    admin_client.post(
        "/admin/invites", data={"csrf_token": admin_client.token}, follow_redirects=False
    )
    with TestClient(app, base_url="https://commons.example.test") as other:
        as_user(other, fake_matrix, ADMIN)  # same admin, another browser
        assert "/join/" not in other.get("/admin/members").text
    assert "/join/" in admin_client.get("/admin/members").text


def test_create_invite_when_mas_is_down(admin_client, fake_matrix, db):
    fake_matrix.fail_admin_api = True
    r = admin_client.post("/admin/invites", data={"csrf_token": admin_client.token})
    assert r.status_code == 502
    assert db.scalar(select(func.count()).select_from(Invite)) == 0


def test_invite_note_is_limited_and_escaped(admin_client, fake_matrix, db):
    r = admin_client.post(
        "/admin/invites", data={"note": "x" * 201, "csrf_token": admin_client.token}
    )
    assert r.status_code == 400
    create_invite(admin_client, "<script>x</script>")
    assert "<script>x" not in admin_client.get("/admin/members").text


def test_revoke_invite(admin_client, fake_matrix, db):
    _, _, code = create_invite(admin_client)
    invite = db.scalar(select(Invite))
    r = admin_client.post(
        f"/admin/invites/{invite.id}/revoke",
        data={"csrf_token": admin_client.token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert fake_matrix.reg_tokens["RT-1"]["revoked"]
    db.refresh(invite)
    assert invite.revoked_at is not None
    assert admin_client.get(f"/join/{code}").status_code == 410
    assert "for Bob" not in admin_client.get("/admin/members").text
    # Revoking twice is harmless.
    r = admin_client.post(
        f"/admin/invites/{invite.id}/revoke", data={"csrf_token": admin_client.token}
    )
    assert r.status_code == 200


def test_revoke_unknown_invite_is_404(admin_client):
    r = admin_client.post("/admin/invites/9999/revoke", data={"csrf_token": admin_client.token})
    assert r.status_code == 404


# -- the join page -------------------------------------------------------------


@pytest.fixture
def mas_on_subdomain(app):
    """Like production: MAS at auth.commons.<domain>, below COMMONS' host."""
    app.state.settings = dataclasses.replace(
        app.state.settings, matrix_auth_public_url="https://auth.commons.example.test"
    )


def test_join_link_goes_straight_to_sign_up_with_the_code(
    app, admin_client, fake_matrix, mas_on_subdomain
):
    _, _, code = create_invite(admin_client)
    with TestClient(app, base_url="https://commons.example.test") as newcomer:
        r = newcomer.get(f"/join/{code}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/auth/login?signup=1"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"commons_invite={code};")
        # For the parent domain, so MAS' page (auth.commons.example.test) can read it.
        for part in ("Domain=commons.example.test", "Max-Age=3600", "Secure", "SameSite=lax"):
            assert part in cookie
        assert "HttpOnly" not in cookie
        r = newcomer.get("/auth/login?signup=1", follow_redirects=False)
        query = parse_qs(httpx.URL(r.headers["location"]).query.decode())
        assert query["prompt"] == ["create"]
        # Signing in afterwards removes the cookie.
        r = sign_in(newcomer, fake_matrix)
        assert r.status_code == 303
        assert 'commons_invite=""' in r.headers.get("set-cookie", "") or (
            "commons_invite=;" in r.headers.get("set-cookie", "")
        )


def test_join_shows_the_code_when_mas_is_on_another_domain(app, admin_client):
    _, _, code = create_invite(admin_client)
    with TestClient(app, base_url="https://commons.example.test") as newcomer:
        r = newcomer.get(f"/join/{code}")
        assert r.status_code == 200
        assert code in r.text
        assert "/auth/login?signup=1" in r.text
        assert "for Bob" not in r.text  # the admin note stays with the admins
        assert "commons_invite" not in r.headers.get("set-cookie", "")


def test_cookie_domain():
    assert (
        invites.cookie_domain("https://commons.x.net", "https://auth.commons.x.net")
        == "commons.x.net"
    )
    assert invites.cookie_domain("https://commons.x.net", "https://auth.x.net") is None
    assert invites.cookie_domain("https://x.net", "https://evilx.net") is None


def test_plain_sign_in_does_not_ask_for_sign_up(client, fake_matrix):
    r = client.get("/auth/login", follow_redirects=False)
    assert "prompt" not in parse_qs(httpx.URL(r.headers["location"]).query.decode())


def test_join_lowercase_code_works(admin_client):
    _, _, code = create_invite(admin_client)
    assert admin_client.get(f"/join/{code.lower()}").status_code == 200


@pytest.mark.parametrize("code", ["AAAA-BBBB-CCCC-DDDD-EEEE", "nonsense", "x" * 300])
def test_join_unknown_code(client, code):
    r = client.get(f"/join/{code}")
    assert r.status_code == 410
    assert "Ask an admin for a new one" in r.text


def test_join_used_code(admin_client, fake_matrix):
    _, _, code = create_invite(admin_client)
    fake_matrix.reg_tokens["RT-1"]["times_used"] = 1  # someone signed up with it
    assert admin_client.get(f"/join/{code}").status_code == 410


def test_join_expired_code(admin_client, db):
    _, _, code = create_invite(admin_client)
    invite = db.scalar(select(Invite))
    invite.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert admin_client.get(f"/join/{code}").status_code == 410


# -- every account on the homeserver is a member --------------------------------


def test_new_account_becomes_a_member_on_first_sign_in(client, fake_matrix, db, ada):
    fake_matrix.profiles[f"@bob:{SERVER}"] = "Bob"
    r = as_user(client, fake_matrix, f"@bob:{SERVER}")
    assert r
    bob = members.get_by_matrix_id(db, f"@bob:{SERVER}")
    assert bob.role_names == {"member"}
    assert bob.display_name == "Bob"
    # Extra roles only from an admin, and the new member cannot reach admin pages.
    assert client.get("/admin/members").status_code == 403


def test_ensure_member_ignores_other_servers(db):
    assert members.ensure_member(db, "@eve:evil.example", SERVER) is None
    assert members.ensure_member(db, "not an id", SERVER) is None
    assert members.list_members(db) == []


def test_ensure_member_keeps_deactivated_members_deactivated(db, nora, log):
    members.deactivate(db, nora, **log)
    db.commit()
    m = members.ensure_member(db, NORA, SERVER)
    assert m.id == nora.id
    assert not m.is_active


def test_two_first_sign_ins_at_once_make_one_member(app, db):
    first = app.state.sessionmaker()
    second = app.state.sessionmaker()
    try:
        a = members.ensure_member(first, f"@bob:{SERVER}", SERVER)
        result = {}

        def other():
            with second.begin():
                second.execute(text("SET LOCAL lock_timeout = '5s'"))
                result["m"] = members.ensure_member(second, f"@bob:{SERVER}", SERVER)
                result["id"] = result["m"].id

        t = threading.Thread(target=other)
        t.start()
        first.commit()
        t.join(10)
        assert result["id"] == a.id
    finally:
        first.close()
        second.close()
    assert len(members.list_members(db)) == 1


# -- roles and deactivation ----------------------------------------------------


def test_grant_and_revoke_role(admin_client, db, nora):
    tok = admin_client.token
    r = admin_client.post(
        f"/admin/members/{nora.id}/grant",
        data={"role": "board", "csrf_token": tok},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert reload(db, nora).role_names == {"member", "board"}
    admin_client.post(f"/admin/members/{nora.id}/revoke", data={"role": "board", "csrf_token": tok})
    assert reload(db, nora).role_names == {"member"}


def test_member_role_cannot_be_revoked(admin_client, db, nora):
    r = admin_client.post(
        f"/admin/members/{nora.id}/revoke",
        data={"role": "member", "csrf_token": admin_client.token},
    )
    assert r.status_code == 400
    assert "member" in reload(db, nora).role_names


def test_unknown_member_is_404(admin_client):
    r = admin_client.post("/admin/members/9999/deactivate", data={"csrf_token": admin_client.token})
    assert r.status_code == 404


def test_deactivate_ends_sessions_and_reactivate(app, admin_client, fake_matrix, db, nora):
    with TestClient(app, base_url="https://commons.example.test") as other:
        as_user(other, fake_matrix, NORA)
        assert NORA_SHOWN in other.get("/").text
        r = admin_client.post(
            f"/admin/members/{nora.id}/deactivate", data={"csrf_token": admin_client.token}
        )
        assert r.status_code == 200
        assert "Nora is deactivated: signed out, and their chat account is locked." in r.text
        assert not reload(db, nora).is_active
        assert NORA_SHOWN not in other.get("/").text  # signed out
        live = db.scalars(
            select(Session).where(Session.member_id == nora.id, Session.revoked_at.is_(None))
        ).all()
        assert live == []
        assert fake_matrix.locked == {"ULID-nora"}  # the chat account too
        assert sign_in(other, fake_matrix).status_code == 403
        r = admin_client.post(
            f"/admin/members/{nora.id}/reactivate", data={"csrf_token": admin_client.token}
        )
        assert "Nora is active again, and their chat account is unlocked." in r.text
        assert reload(db, nora).is_active
        assert fake_matrix.locked == set()
        assert sign_in(other, fake_matrix).status_code == 303


def test_deactivate_changes_nothing_when_mas_is_down(admin_client, fake_matrix, db, nora):
    fake_matrix.fail_admin_api = True
    r = admin_client.post(
        f"/admin/members/{nora.id}/deactivate", data={"csrf_token": admin_client.token}
    )
    assert r.status_code == 502
    assert reload(db, nora).is_active


def test_deactivate_member_without_a_mas_account(admin_client, fake_matrix, db, nora):
    fake_matrix.unknown_users.add("nora")
    r = admin_client.post(
        f"/admin/members/{nora.id}/deactivate",
        data={"csrf_token": admin_client.token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert not reload(db, nora).is_active


# -- deleting ------------------------------------------------------------------


def delete_url(m: Member) -> str:
    return f"/admin/members/{m.id}/delete"


def test_delete_asks_first(admin_client, db, nora):
    page = admin_client.get("/admin/members").text
    assert delete_url(nora) in page
    r = admin_client.get(delete_url(nora))
    assert r.status_code == 200
    assert "for good" in r.text
    assert "never be used again" in r.text
    assert members.get(db, nora.id) is not None  # asking changes nothing


def test_delete_member_and_erase_chat_account(app, admin_client, fake_matrix, db, nora):
    nora_id = nora.id
    with TestClient(app, base_url="https://commons.example.test") as other:
        as_user(other, fake_matrix, NORA)
        assert NORA_SHOWN in other.get("/").text
        r = admin_client.post(
            delete_url(nora), data={"csrf_token": admin_client.token}, follow_redirects=False
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/members?done=deleted"
        assert NORA_SHOWN not in other.get("/").text  # signed out
    db.expire_all()
    assert members.get_by_matrix_id(db, NORA) is None
    assert db.scalars(select(Session).where(Session.member_id == nora_id)).all() == []
    assert (
        db.scalar(select(func.count()).select_from(AuthEvent).where(AuthEvent.member_id == nora_id))
        == 0
    )
    assert fake_matrix.erased == ["ULID-nora"]
    assert "Member deleted." in admin_client.get("/admin/members?done=deleted").text


def test_delete_needs_csrf(admin_client, fake_matrix, db, nora):
    assert admin_client.post(delete_url(nora), data={}).status_code == 403
    assert members.get(db, nora.id) is not None
    assert fake_matrix.erased == []


def test_member_with_history_cannot_be_deleted(admin_client, fake_matrix, db, nora, log):
    members.grant_role(db, nora, "admin", **log)
    db.add(
        Invite(
            code_hash="1" * 64,
            mas_token_id="RT-9",
            created_by=nora.id,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    db.commit()
    r = admin_client.post(delete_url(nora), data={"csrf_token": admin_client.token})
    assert r.status_code == 409
    assert "Deactivate them instead" in r.text
    assert reload(db, nora).role_names == {"member", "admin"}  # nothing removed
    assert fake_matrix.erased == []  # MAS not touched


def test_cannot_delete_yourself_or_the_last_admin(admin_client, fake_matrix, db, ada):
    assert delete_url(ada) not in admin_client.get("/admin/members").text
    r = admin_client.post(delete_url(ada), data={"csrf_token": admin_client.token})
    assert r.status_code == 400
    assert members.get(db, ada.id) is not None
    other = members.add_member(db, f"@bob:{SERVER}")
    with pytest.raises(members.LastAdminError):
        members.delete_member(db, ada, other)
    assert fake_matrix.erased == []


def test_delete_changes_nothing_when_mas_is_down(admin_client, fake_matrix, db, nora):
    fake_matrix.fail_admin_api = True
    r = admin_client.post(delete_url(nora), data={"csrf_token": admin_client.token})
    assert r.status_code == 502
    assert reload(db, nora) is not None


def test_deleted_account_does_not_come_back_as_a_member_while_mas_is_out_of_sync(
    admin_client, db, nora, log
):
    """After deletion the Matrix account is deactivated, so it can't sign in; if it
    somehow did, it would join again as a plain member, never with old roles."""
    members.grant_role(db, nora, "board", **log)
    db.commit()
    nora_id, url = nora.id, delete_url(nora)
    admin_client.post(url, data={"csrf_token": admin_client.token})
    db.expire_all()
    again = members.ensure_member(db, NORA, SERVER)
    assert again.id != nora_id
    assert again.role_names == {"member"}


# -- the last admin ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "form"),
    [("/admin/members/{id}/revoke", {"role": "admin"}), ("/admin/members/{id}/deactivate", {})],
)
def test_last_admin_cannot_be_removed(admin_client, db, ada, path, form):
    r = admin_client.post(path.format(id=ada.id), data={**form, "csrf_token": admin_client.token})
    assert r.status_code == 409
    assert "last active admin" in r.text
    ada = reload(db, ada)
    assert "admin" in ada.role_names
    assert ada.is_active


def test_admin_can_step_down_when_another_admin_exists(admin_client, db, ada, nora, log):
    members.grant_role(db, nora, "admin", **log)
    db.commit()
    r = admin_client.post(
        f"/admin/members/{ada.id}/revoke",
        data={"role": "admin", "csrf_token": admin_client.token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert reload(db, ada).role_names == {"member"}
    # Ada is no admin any more: the next admin action is refused.
    assert admin_client.get("/admin/members").status_code == 403


def test_deactivated_admin_does_not_count(db, ada, nora, log):
    members.grant_role(db, nora, "admin", **log)
    db.commit()
    members.deactivate(db, nora, **log)
    db.commit()
    with pytest.raises(members.LastAdminError):
        members.revoke_role(db, ada, "admin", **log)


def test_two_admins_removing_each_other_at_once_leave_one(app, db, ada, nora, log):
    """Row locks serialize the two changes: the second sees the first and is refused."""
    members.grant_role(db, nora, "admin", **log)
    db.commit()
    first = app.state.sessionmaker()
    second = app.state.sessionmaker()
    result: dict[str, object] = {}
    try:
        members.revoke_role(first, first.get(Member, ada.id), "admin", **log)  # holds the locks

        def other():
            try:
                second.execute(text("SET lock_timeout = '5s'"))
                members.revoke_role(second, second.get(Member, nora.id), "admin", **log)
                second.commit()
                result["second"] = "ok"
            except members.LastAdminError:
                second.rollback()
                result["second"] = "refused"

        t = threading.Thread(target=other)
        t.start()
        t.join(0.5)
        assert t.is_alive(), "the second change should wait for the first"
        first.commit()
        t.join(10)
    finally:
        first.close()
        second.close()
    assert result["second"] == "refused"
    db.expire_all()
    assert members.has_active_admin(db)


# -- command line --------------------------------------------------------------


def run_cli(app, *argv: str) -> int:
    return cli.main(
        list(argv),
        sessionmaker=app.state.sessionmaker,
        server_name=SERVER,
        key_dir=app.state.settings.key_dir,
    )


def test_bootstrap_admin_only_once(app, db, capsys):
    assert run_cli(app, "bootstrap-admin", "alex", "--check") == 0
    assert members.get_by_matrix_id(db, f"@alex:{SERVER}") is None  # --check writes nothing
    assert run_cli(app, "bootstrap-admin", "alex") == 0
    db.expire_all()
    assert members.get_by_matrix_id(db, f"@alex:{SERVER}").role_names == {"member", "admin"}
    assert run_cli(app, "bootstrap-admin", "mallory") == 1
    assert run_cli(app, "bootstrap-admin", "mallory", "--check") == 1
    assert "already has an admin" in capsys.readouterr().err
    assert members.get_by_matrix_id(db, f"@mallory:{SERVER}") is None


def test_bootstrap_promotes_an_existing_member(app, db, nora):
    assert run_cli(app, "bootstrap-admin", NORA) == 0
    assert reload(db, nora).role_names == {"member", "admin"}


def test_bootstrap_after_the_only_admin_was_deactivated_is_refused(app, db, ada):
    # The last admin can't be deactivated, so there is always an admin.
    assert run_cli(app, "deactivate", "ada") == 1
    assert run_cli(app, "bootstrap-admin", "mallory") == 1


def test_cli_roles_and_deactivation(app, db, ada, nora, capsys):
    assert run_cli(app, "grant-role", "nora", "board") == 0
    assert run_cli(app, "revoke-role", "nora", "board") == 0
    assert run_cli(app, "revoke-role", "ada", "admin") == 1
    assert run_cli(app, "revoke-role", "nora", "member") == 1
    assert run_cli(app, "deactivate", "nora") == 0
    assert run_cli(app, "list-members") == 0
    out = capsys.readouterr().out
    assert f"{NORA}  member  (deactivated)" in out
    assert run_cli(app, "reactivate", "nora") == 0
    assert run_cli(app, "deactivate", "@nobody:" + SERVER) == 1
    assert run_cli(app, "add-member", "@eve:evil.example") == 1
    assert reload(db, nora).is_active


def test_normalize_user_id():
    assert members.normalize_user_id(" Nora ", SERVER) == NORA
    assert members.normalize_user_id(NORA, SERVER) == NORA
    for bad in ("", "@nora", "@nora:other.test", "ni na", "@Nora:" + SERVER):
        with pytest.raises(members.InvalidUserId):
            members.normalize_user_id(bad, SERVER)


# -- decision log (invites, role changes, deactivation) -------------------------


def log_entries(db) -> list[dict]:
    db.expire_all()
    return decision_log.load_entries(db)


def membership_entries(db) -> list[dict]:
    kinds = {"member.invited", "role.granted", "role.revoked", "member.deactivated"}
    return [e for e in log_entries(db) if e["kind"] in kinds]


def assert_chain_verifies(db) -> None:
    db.expire_all()
    result = decision_log.verify_db(db)
    assert result.ok, result


def test_granting_a_role_produces_a_verifiable_entry(admin_client, db, ada, nora):
    before = len(log_entries(db))
    admin_client.post(
        f"/admin/members/{nora.id}/grant",
        data={"role": "board", "csrf_token": admin_client.token},
    )
    entries = log_entries(db)
    assert len(entries) == before + 1
    entry = entries[-1]
    assert entry["kind"] == "role.granted"
    assert entry["actor"] == ada.id
    assert entry["payload"] == {
        "member_id": nora.id,
        "matrix_user_id": NORA,
        "role": "board",
        "channel": "web",
    }
    assert_chain_verifies(db)


def test_revoking_a_role_is_logged(admin_client, db, ada, nora, log):
    members.grant_role(db, nora, "board", **log)
    db.commit()
    admin_client.post(
        f"/admin/members/{nora.id}/revoke",
        data={"role": "board", "csrf_token": admin_client.token},
    )
    entry = log_entries(db)[-1]
    assert entry["kind"] == "role.revoked"
    assert entry["actor"] == ada.id
    assert entry["payload"]["role"] == "board"
    assert entry["payload"]["member_id"] == nora.id
    assert_chain_verifies(db)


def test_deactivation_is_logged(admin_client, db, ada, nora):
    admin_client.post(
        f"/admin/members/{nora.id}/deactivate", data={"csrf_token": admin_client.token}
    )
    entry = log_entries(db)[-1]
    assert entry["kind"] == "member.deactivated"
    assert entry["actor"] == ada.id
    assert entry["payload"] == {"member_id": nora.id, "matrix_user_id": NORA, "channel": "web"}
    assert_chain_verifies(db)


def test_invite_is_logged_without_code_or_note(admin_client, db, ada):
    _, _, code = create_invite(admin_client)
    invite = db.scalar(select(Invite))
    entry = log_entries(db)[-1]
    assert entry["kind"] == "member.invited"
    assert entry["actor"] == ada.id
    assert entry["payload"] == {
        "invite_id": invite.id,
        "expires_at": decision_log.format_ts(invite.expires_at),
        "channel": "web",
    }
    dump = str(entry)
    assert code not in dump
    assert invite.code_hash not in dump
    assert "for Bob" not in dump  # the admin's note stays out of the permanent log
    assert_chain_verifies(db)


def test_failed_changes_leave_no_entry(admin_client, fake_matrix, db, ada, nora):
    before = log_entries(db)
    tok = admin_client.token
    # Refused by the service: the last admin, the member role, a role nobody holds.
    admin_client.post(f"/admin/members/{ada.id}/revoke", data={"role": "admin", "csrf_token": tok})
    admin_client.post(f"/admin/members/{ada.id}/deactivate", data={"csrf_token": tok})
    admin_client.post(
        f"/admin/members/{nora.id}/revoke", data={"role": "member", "csrf_token": tok}
    )
    admin_client.post(f"/admin/members/{nora.id}/revoke", data={"role": "board", "csrf_token": tok})
    # Already held: nothing changes, nothing is logged.
    admin_client.post(f"/admin/members/{ada.id}/grant", data={"role": "admin", "csrf_token": tok})
    # MAS down: the change and its entry roll back together.
    fake_matrix.fail_admin_api = True
    admin_client.post(f"/admin/members/{nora.id}/deactivate", data={"csrf_token": tok})
    admin_client.post("/admin/invites", data={"csrf_token": tok})
    assert log_entries(db) == before
    assert reload(db, nora).is_active


def test_first_sign_in_is_not_logged(client, fake_matrix, db, ada):
    """The automatic `member` role comes with the account; its invite is the record."""
    before = log_entries(db)
    as_user(client, fake_matrix, f"@bob:{SERVER}")
    assert members.get_by_matrix_id(db, f"@bob:{SERVER}") is not None
    assert log_entries(db) == before


def test_extra_roles_need_the_signer(db):
    with pytest.raises(decision_log.DecisionLogError):
        members.add_member(db, f"@bob:{SERVER}", roles=("board",))
    assert members.get_by_matrix_id(db, f"@bob:{SERVER}") is None
    members.add_member(db, f"@bob:{SERVER}")  # plain membership needs no signer


def test_cli_bootstrap_on_a_fresh_install_logs_the_first_admin(app, db):
    key_file = app.state.settings.key_dir / decision_log.KEY_FILE
    assert not key_file.exists()
    assert run_cli(app, "bootstrap-admin", "alex") == 0
    assert key_file.stat().st_mode & 0o777 == 0o600
    (entry,) = log_entries(db)
    alex = members.get_by_matrix_id(db, f"@alex:{SERVER}")
    assert entry["seq"] == 1
    assert entry["kind"] == "role.granted"
    assert entry["actor"] is None
    assert entry["payload"] == {
        "member_id": alex.id,
        "matrix_user_id": f"@alex:{SERVER}",
        "role": "admin",
        "channel": "cli",
    }
    assert_chain_verifies(db)


def test_cli_and_app_share_one_key_and_one_chain(app, db, fake_matrix):
    assert run_cli(app, "bootstrap-admin", "ada") == 0
    with TestClient(app, base_url="https://commons.example.test") as client:
        client.token = as_user(client, fake_matrix, ADMIN)
        create_invite(client)
    assert run_cli(app, "add-member", "nora", "--role", "board") == 0
    assert run_cli(app, "revoke-role", "nora", "board") == 0
    assert run_cli(app, "deactivate", "nora") == 0
    assert [e["kind"] for e in membership_entries(db)] == [
        "role.granted",
        "member.invited",
        "role.granted",
        "role.revoked",
        "member.deactivated",
    ]
    assert len({e["key_id"] for e in log_entries(db)}) == 1
    assert_chain_verifies(db)


def test_cli_refuses_to_start_a_second_key(app, db, tmp_path, capsys):
    assert run_cli(app, "bootstrap-admin", "ada") == 0
    lost = cli.main(
        ["grant-role", "ada", "board"],
        sessionmaker=app.state.sessionmaker,
        server_name=SERVER,
        key_dir=tmp_path / "empty",
    )
    assert lost == 1
    assert "signing key" in capsys.readouterr().err
    assert len(log_entries(db)) == 1


def test_message_pages_keep_the_header_and_say_what_happened(client, fake_matrix, ada):
    # Signed out: a heading, no tabs.
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert f'<h1 class="page-title">{en.MESSAGE_TITLES[404]}</h1>' in r.text
    assert 'aria-label="Main"' not in r.text
    # Signed in, but not an admin: the normal header stays.
    as_user(client, fake_matrix, f"@bob:{SERVER}")
    r = client.get("/admin/members")
    assert r.status_code == 403
    assert f'<h1 class="page-title">{en.MESSAGE_TITLES[403]}</h1>' in r.text
    assert 'aria-label="Main"' in r.text
    assert 'href="/calendar"' in r.text
    assert en.FORBIDDEN.replace("'", "&#39;") in r.text


def test_set_guide_replaces_the_guide_and_logs_it(app, db, tmp_path, capsys):
    from app import community

    def guide_file(text: str) -> Path:
        f = tmp_path / "community.yaml"
        f.write_text(
            "resource_categories:\n  - {slug: water, name: Water, unit: l, kind: stock}\n"
            f"guide:\n  - {{title: Sauna, text: {text}}}\n",
            encoding="utf-8",
        )
        return f

    f = guide_file("Sit on your own towel.")
    assert run_cli(app, "set-guide", "--file", str(f)) == 0
    assert "guide replaced (1 topics)" in capsys.readouterr().out
    assert run_cli(app, "set-guide", "--file", str(guide_file("Bring your own wood."))) == 0
    db.expire_all()
    assert community.guide_for_prompt(db) == "## Sauna\nBring your own wood."
    entries = [e for e in log_entries(db) if e["kind"] == "config.changed"]
    assert [e["payload"]["source"] for e in entries] == ["cli", "cli"]
    assert entries[0]["payload"]["old"] is None
    assert entries[1]["payload"]["old"] == [{"title": "Sauna", "text": "Sit on your own towel."}]
    assert entries[1]["actor"] is None
    assert decision_log.verify_db(db).ok

    assert run_cli(app, "set-guide", "--file", str(f.with_name("missing.yaml"))) == 1
    assert "not found" in capsys.readouterr().err
    assert run_cli(app, "set-guide", "--file", str(guide_file("Bring your own wood."))) == 0
    assert "unchanged" in capsys.readouterr().out
    assert len([e for e in log_entries(db) if e["kind"] == "config.changed"]) == 2


def test_set_badges_updates_adds_and_archives_and_logs_it(app, db, tmp_path, capsys):
    from sqlalchemy import select

    from app import categories as cats
    from app import impact, ledger
    from app.models import BadgeRule

    def badges_file(*lines: str) -> Path:
        f = tmp_path / "community.yaml"
        f.write_text(
            "resource_categories:\n  - {slug: water, name: Water, unit: l, kind: stock}\n"
            "badges:\n" + "".join(f"  - {line}\n" for line in lines),
            encoding="utf-8",
        )
        return f

    first = "{slug: first-log, name: First log, rule: {count: ledger_entries, min: 1}}"
    f = badges_file(first, "{slug: regular, name: Regular, rule: {count: ledger_entries, min: 5}}")
    assert run_cli(app, "set-badges", "--file", str(f)) == 0
    assert "2 badge rules set" in capsys.readouterr().out

    # Nora earns "First log"; then the file drops it and makes "Regular" stricter.
    nora = members.add_member(db, f"@nora:{SERVER}", "Nora")
    water = cats.create(db, cats.CategorySpec(slug="water", name="Water", unit="l", kind="stock"))
    ledger.add_entry(db, nora, water, "reading", 500, channel="matrix")
    impact.sync(db, nora)
    db.commit()
    f = badges_file(
        "{slug: regular, name: Regular, rule: {count: ledger_entries, min: 10}}",
        "{slug: streak, name: Week, rule: {count: ledger_days_in_a_row, min: 7}}",
    )
    assert run_cli(app, "set-badges", "--file", str(f)) == 0
    db.expire_all()
    rules = {r.slug: r for r in db.scalars(select(BadgeRule))}
    assert rules["first-log"].archived_at is not None  # archived, not deleted
    assert rules["regular"].rule_json == {"count": "ledger_entries", "min": 10}
    assert [
        (r.slug, r.position)
        for r in sorted(rules.values(), key=lambda r: r.position)
        if r.archived_at is None
    ] == [("regular", 0), ("streak", 1)]
    views = {v.rule.slug: v for v in impact.sync(db, nora)}
    assert views["first-log"].earned  # still hers

    entries = [e for e in log_entries(db) if e["payload"].get("key") == "badges"]
    assert [e["payload"]["source"] for e in entries] == ["cli", "cli"]
    assert [b["slug"] for b in entries[1]["payload"]["old"]] == ["first-log", "regular"]
    assert [b["slug"] for b in entries[1]["payload"]["new"]] == ["regular", "streak"]
    assert decision_log.verify_db(db).ok

    assert run_cli(app, "set-badges", "--file", str(f)) == 0
    assert "unchanged" in capsys.readouterr().out
    bad = badges_file("{slug: big, name: Big, rule: {count: litres_used, min: 1}}")
    assert run_cli(app, "set-badges", "--file", str(bad)) == 1
    assert len([e for e in log_entries(db) if e["payload"].get("key") == "badges"]) == 2
