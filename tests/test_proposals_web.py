"""The Votes tab in the browser: propose, one-tap vote, secret ballots, results, log."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from tests.conftest import SERVER, VOTING, sign_in

from app import governance, members, proposals, textfmt
from app.models import ConfigKV, DecisionLog, Member, Proposal, Vote, utcnow

BASE = "https://commons.example.test"


@pytest.fixture
def voting(db):
    db.add(ConfigKV(key="voting", value_json=VOTING))
    db.commit()


class Browser:
    """One signed-in member with their own cookies and CSRF token."""

    def __init__(self, app, fake_matrix, db, signer, name: str, roles=()):
        self.member = members.add_member(
            db, f"@{name}:{SERVER}", name.title(), roles, signer=signer
        )
        db.commit()
        self.client = TestClient(app, base_url=BASE)
        fake_matrix.user_id = self.member.matrix_user_id
        assert sign_in(self.client, fake_matrix).status_code == 303
        page = self.client.get("/").text
        self.csrf = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page).group(1)

    def get(self, url: str):
        return self.client.get(url, follow_redirects=False)

    def post(self, url: str, data: dict, csrf: bool = True):
        if csrf:
            data = {**data, "csrf_token": self.csrf}
        return self.client.post(url, data=data, follow_redirects=False)

    def vote(self, pid: int, choice, back: str = "detail"):
        return self.post(f"/proposals/{pid}/vote", {"choice": str(choice), "back": back})


@pytest.fixture
def people(app, client, fake_matrix, db, log_signer, voting):
    def browser(name, *roles):
        return Browser(app, fake_matrix, db, log_signer, name, roles)

    return {"board": browser("bo", "board"), "anna": browser("anna"), "ben": browser("ben")}


NEW = {
    "vote_type": "rules",
    "question": "Raise the firewood alert to 8 baskets?",
    "why": "This week the alert at 5 came a day too late.",
    "option_1": "Yes",
    "option_2": "No",
    "option_3": "",
    "option_4": "",
}


def open_via_web(who: Browser, **changes) -> int:
    r = who.post("/proposals", {**NEW, **changes})
    assert r.status_code == 303, r.text
    return int(re.match(r"/proposals/(\d+)\?done=opened", r.headers["location"]).group(1))


def option_ids(db, proposal_id: int) -> list[int]:
    return [o.id for o in db.get(Proposal, proposal_id).options]


def unescape(text: str) -> str:
    return text.replace("&#39;", "'").replace("&#34;", '"').replace("&amp;", "&")


# --- proposing -----------------------------------------------------------------


def test_any_member_proposes_and_others_vote_with_one_tap(db, people):
    anna, ben = people["anna"], people["ben"]
    pid = open_via_web(anna)
    page = unescape(ben.get(f"/proposals/{pid}").text)
    assert "Raise the firewood alert to 8 baskets?" in page
    assert "This week the alert at 5 came a day too late." in page
    # Sign-in takes names from the Matrix profile; the fake one has none, so ids show.
    assert f"Proposed by @anna:{SERVER}" in page
    assert "Secret ballot · live counts" in page
    assert 'name="choice" value="abstain"' in page

    yes, no = option_ids(db, pid)
    r = ben.vote(pid, yes, back="list")
    assert r.status_code == 303
    assert r.headers["location"] == f"/proposals?done=voted&p={pid}#vote-{pid}"
    page = unescape(ben.get(r.headers["location"]).text)
    assert "Your vote on “Raise the firewood alert to 8 baskets?” is in: Yes." in page
    assert f'value="{yes}" aria-pressed="true">✓ Yes' in page

    assert ben.vote(pid, no).status_code == 303
    assert anna.vote(pid, "abstain").status_code == 303
    db.expire_all()
    current = db.scalars(select(Vote).where(Vote.superseded_at.is_(None))).all()
    assert {(v.member_id, v.option_id, v.channel) for v in current} == {
        (ben.member.id, no, "web"),
        (anna.member.id, None, "web"),
    }
    assert db.query(Vote).count() == 3  # ben's first vote is kept, superseded
    kinds = list(db.scalars(select(DecisionLog.kind).order_by(DecisionLog.seq)))
    assert kinds[-4:] == ["proposal.opened", "vote.cast", "vote.changed", "vote.cast"]


def test_new_form_offers_the_types_and_yes_no(people):
    page = unescape(people["anna"].get("/proposals/new").text)
    for name in ("Spending", "Rules", "Events"):
        assert f">{name}</option>" in page
    assert page.count('name="option_') == 4  # 2 + extra_options
    assert 'name="option_1" value="Yes"' in page
    assert 'name="option_2" value="No"' in page
    assert 'name="amount_unknown"' in page
    # The passing share starts at the default and can't go below a majority.
    assert 'name="threshold_pct" value="50" type="number"' in page
    assert 'min="50"' in page
    assert "duration_days" not in page  # the type sets the closing time


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"option_2": ""}, "2 to 4 options"),
        ({"question": "  "}, "question can't be empty"),
        ({"question": "x" * 141}, "at most 140"),
        ({"option_2": "yes"}, "different"),
        ({"option_2": "Abstain"}, "always offered"),
        ({"vote_type": "elections"}, "not one of the vote types"),
        ({"amount": "100"}, "Rules votes have no amount"),
        ({"vote_type": "spending"}, "give the amount"),
        ({"vote_type": "spending", "amount": "lots"}, "must be a number"),
        ({"vote_type": "spending", "amount": "5", "amount_unknown": "1"}, "not both"),
        ({"threshold_pct": "49"}, "at least 50 %"),
        ({"threshold_pct": "100"}, "under 100 %"),
        ({"threshold_pct": "most"}, "must be a number"),
    ],
)
def test_bad_proposals_show_the_form_again(db, people, changes, message):
    r = people["anna"].post("/proposals", {**NEW, **changes})
    assert r.status_code == 400
    assert message in unescape(r.text)
    assert "This week the alert at 5 came a day too late." in unescape(r.text)
    assert db.scalar(select(Proposal.id)) is None


def test_the_proposer_sets_the_passing_share(db, people):
    anna = people["anna"]
    pid = open_via_web(anna, threshold_pct="66.67")
    assert db.get(Proposal, pid).threshold_pct == Decimal("66.67")
    page = unescape(anna.get(f"/proposals/{pid}").text)
    assert "Passes over 66.67 %" in page
    assert "Passes with more than 66.67 % of the votes cast" in page
    # Shares are rounded down to 2 decimals: 2 of 3 is not above 66.67 %, so it reads 66.66.
    yes, no = option_ids(db, pid)
    for who, choice in ((anna, yes), (people["ben"], yes), (people["board"], no)):
        assert who.vote(pid, choice).status_code == 303
    page = unescape(anna.get(f"/proposals/{pid}").text)
    assert "<b>2</b> · 66.66 %" in page
    assert "<b>1</b> · 33.33 %" in page
    assert "Rejected · 66.66 %" in page
    # Left empty, the community's default applies.
    default = open_via_web(anna, question="Keep the default?", threshold_pct="")
    assert db.get(Proposal, default).threshold_pct == Decimal(50)


@pytest.mark.parametrize(("n", "of", "text"), [
    (2, 3, "66.66"), (1, 3, "33.33"), (1, 2, "50"), (3, 4, "75"), (0, 0, "0"), (5, 5, "100"),
])  # fmt: skip
def test_shares_are_rounded_down_to_two_decimals(n, of, text):
    assert proposals.share_text(n, of) == text


def test_spending_votes_show_their_amount(db, people):
    anna = people["anna"]
    priced = open_via_web(anna, vote_type="spending", question="Buy a chainsaw?", amount="6,500")
    unknown = open_via_web(anna, vote_type="spending", question="Order wood?", amount_unknown="1")
    assert "6,500 SEK" in anna.get(f"/proposals/{priced}").text
    assert "Amount unknown" in anna.get(f"/proposals/{unknown}").text
    p = db.get(Proposal, priced)
    assert (p.vote_type, str(p.amount), p.currency) == ("spending", "6500.00", "SEK")


def test_overlong_fields_are_refused(db, people):
    r = people["anna"].post("/proposals", {**NEW, "question": "x" * 5000})
    assert r.status_code == 400
    assert db.scalar(select(Proposal.id)) is None


def test_closing_time_is_set_by_the_type_and_shown_in_local_time(db, people):
    now = utcnow()
    pid = open_via_web(people["anna"], vote_type="events", question="Host the choir?")
    p = db.get(Proposal, pid)
    assert timedelta(days=2) <= p.closes_at - now <= timedelta(days=3)
    local = p.closes_at.astimezone(ZoneInfo("Europe/Stockholm")).strftime("%d.%m.%y %H:%M")
    page = people["ben"].get(f"/proposals/{pid}").text
    assert local.endswith(" 23:59")
    shown = textfmt.web_time(p.closes_at, ZoneInfo("Europe/Stockholm"))
    assert f"Closes {shown}" in unescape(page)
    assert "Europe/Stockholm time" in page


# --- secret ballots --------------------------------------------------------------


def test_nobody_sees_who_voted_what(db, people):
    board, anna, ben = people["board"], people["anna"], people["ben"]
    pid = open_via_web(board)
    yes, _ = option_ids(db, pid)
    anna.vote(pid, yes)
    for who in (board, ben):  # not even the board
        for url in (f"/proposals/{pid}", "/proposals", "/"):
            page = who.get(url).text
            assert "@anna" not in page, url
    page = unescape(board.get(f"/proposals/{pid}").text)
    assert "<b>1</b> · 100 %" in page  # anonymous counts
    assert "You haven't voted yet" not in page  # live type: no reminder box
    assert "your vote" not in page
    mine = unescape(anna.get(f"/proposals/{pid}").text)
    assert "Yes</b> · your vote" in mine


def test_after_close_types_show_only_turnout_while_open(db, people, log_signer):
    board, anna, ben = people["board"], people["anna"], people["ben"]
    pid = open_via_web(board, vote_type="events", question="Host the choir weekend?")
    yes, _ = option_ids(db, pid)
    anna.vote(pid, yes)
    page = unescape(ben.get(f"/proposals/{pid}").text)
    assert "Secret ballot · counts after the close" in page
    assert '<b class="big">1</b>' in page
    assert " · 100 %" not in page
    assert "abstained (not counted)" not in page
    assert "The counts are shown when the vote closes." in page
    assert "You haven't voted yet." in page
    assert "You voted Yes." in unescape(anna.get(f"/proposals/{pid}").text)
    assert "You voted Yes." not in page
    # Closed: the counts from the log are shown.
    governance.close_if_due(db, pid, signer=log_signer, now=utcnow() + timedelta(days=5))
    db.commit()
    page = unescape(ben.get(f"/proposals/{pid}").text)
    assert "<b>1</b> · 100 %" in page
    assert "Passed · 100 %" in page


def test_log_page_lists_the_vote_but_no_ballots(db, people):
    board, anna = people["board"], people["anna"]
    pid = open_via_web(board, option_1="Definitely", option_2="Never")
    anna.vote(pid, option_ids(db, pid)[1])
    page = unescape(board.get("/log").text)
    assert "Opened the vote “Raise the firewood alert to 8 baskets?”" in page
    assert "Voted on" not in page
    assert "vote.cast" not in page


# --- closing and results ---------------------------------------------------------


def test_last_vote_closes_and_shows_the_result(db, people):
    board, anna, ben = people["board"], people["anna"], people["ben"]
    pid = open_via_web(board)
    yes, no = option_ids(db, pid)
    for who, choice in ((board, yes), (anna, yes), (ben, "abstain")):
        assert who.vote(pid, choice).status_code == 303
    page = unescape(anna.get(f"/proposals/{pid}").text)
    assert "Passed · 100 %" in page
    assert "1 abstained (not counted)" in page
    assert f'action="/proposals/{pid}/vote"' not in page
    assert "You voted Yes. Only you see this." in page
    r = anna.vote(pid, no)
    assert r.status_code == 400
    assert "votes are no longer taken" in r.text


@pytest.mark.parametrize(
    ("choices", "result"),
    [((1, 2, "abstain"), "Rejected · tie"), (("abstain",) * 3, "Rejected · no votes")],
)
def test_rejections_are_explained(db, people, choices, result):
    pid = open_via_web(people["board"])
    ids = option_ids(db, pid)
    for who, choice in zip(people.values(), choices, strict=True):
        who.vote(pid, choice if choice == "abstain" else ids[choice - 1])
    assert result in people["anna"].get(f"/proposals/{pid}").text


def test_past_votes_filter_and_link_to_the_log(db, people, log_signer):
    anna = people["anna"]
    old = utcnow() - timedelta(days=10)
    for question, vote_type in (("Old rule?", "rules"), ("Old party?", "events")):
        p = governance.open_proposal(
            db, anna.member, question, ("Yes", "No"), vote_type_slug=vote_type,
            signer=log_signer, channel="web", now=old,
        )  # fmt: skip
        db.commit()
        governance.close_if_due(db, p.id, signer=log_signer)
        db.commit()
    open_via_web(anna, question="Still open?")
    page = anna.get("/proposals").text
    assert page.index("Still open?") < page.index("Past votes") < page.index("Old rule?")
    assert "Old party?" in page
    closed_seq = db.scalar(
        select(DecisionLog.seq).where(DecisionLog.kind == "proposal.closed").limit(1)
    )
    assert f'href="/log?before={closed_seq + 1}#entry-{closed_seq}"' in page
    filtered = anna.get("/proposals?type=events&outcome=rejected").text
    past = filtered[filtered.index("Past votes") :]
    assert "Old party?" in past
    assert "Old rule?" not in past
    none = anna.get("/proposals?outcome=passed").text
    assert "No past votes match this filter." in none


def test_vote_needs_an_option_of_this_proposal(db, people):
    board, anna = people["board"], people["anna"]
    pid, other = open_via_web(board), open_via_web(board)
    for data in ({}, {"choice": "maybe"}, {"choice": "-1"}):
        r = anna.post(f"/proposals/{pid}/vote", data)
        assert r.status_code == 400
        assert "Please choose one of the options." in r.text
    r = anna.vote(pid, option_ids(db, other)[0])
    assert r.status_code == 400
    assert "not one of this proposal" in unescape(r.text)
    assert db.query(Vote).count() == 0


def test_forms_need_the_csrf_token(db, people):
    board, anna = people["board"], people["anna"]
    assert anna.post("/proposals", NEW, csrf=False).status_code == 403
    pid = open_via_web(anna)
    yes, _ = option_ids(db, pid)
    assert anna.post(f"/proposals/{pid}/vote", {"choice": str(yes)}, csrf=False).status_code == 403
    assert board.post(f"/proposals/{pid}/cancel", {"reason": "x"}, csrf=False).status_code == 403
    db.expire_all()
    assert db.query(Vote).count() == 0
    assert db.get(Proposal, pid).status == "open"


def test_unknown_proposal_is_404(people):
    assert people["anna"].get("/proposals/999999").status_code == 404
    assert people["anna"].vote(999999, 1).status_code == 404


# --- cancelling --------------------------------------------------------------------


def test_board_cancels_with_a_reason(db, people):
    board = people["board"]
    pid = open_via_web(people["anna"])
    assert f"/proposals/{pid}/cancel" in board.get(f"/proposals/{pid}").text
    assert f"/proposals/{pid}/cancel" not in people["anna"].get(f"/proposals/{pid}").text
    r = board.post(f"/proposals/{pid}/cancel", {"reason": ""})
    assert r.status_code == 400
    assert db.get(Proposal, pid).status == "open"
    r = board.post(f"/proposals/{pid}/cancel", {"reason": "Wrong numbers in the question"})
    assert r.status_code == 303
    page = people["anna"].get(f"/proposals/{pid}").text
    assert "Reason: Wrong numbers in the question" in page
    db.expire_all()
    assert db.get(Proposal, pid).status == "cancelled"


def test_member_cannot_cancel(db, people):
    anna = people["anna"]
    pid = open_via_web(anna)
    assert anna.post(f"/proposals/{pid}/cancel", {"reason": "no"}).status_code == 403
    db.expire_all()
    assert db.get(Proposal, pid).status == "open"


# --- board tab, nav, log page --------------------------------------------------


def test_board_tab_shows_the_open_vote_closing_first(db, people):
    anna = people["anna"]
    assert "No open votes." in anna.get("/").text
    later = open_via_web(anna, question="Later one?")  # rules: 3 days
    sooner = open_via_web(anna, vote_type="spending", question="Sooner one?", amount="800")
    page = anna.get("/").text
    assert "Sooner one?" in page
    assert "Later one?" not in page
    yes = option_ids(db, sooner)[0]
    r = anna.vote(sooner, yes, back="board")
    assert (r.status_code, r.headers["location"]) == (303, "/#open-vote")
    assert later  # still open, shown on the Votes tab


def test_log_page_shows_chain_status_and_links_to_the_entry(db, people):
    pid = open_via_web(people["anna"])
    page = people["anna"].get("/log").text
    assert "Chain verified: all" in page
    seq = db.scalar(select(DecisionLog.seq).where(DecisionLog.kind == "proposal.opened"))
    assert f'id="entry-{seq}"' in page
    detail = people["anna"].get(f"/proposals/{pid}").text
    assert f'href="/log?before={seq + 1}#entry-{seq}"' in detail


def test_anonymous_must_sign_in(app, people):
    with TestClient(app, base_url=BASE) as anonymous:
        for url in ("/proposals", "/proposals/new", "/proposals/1"):
            r = anonymous.get(url, follow_redirects=False)
            assert r.status_code == 401
            assert "Please sign in first." in r.text


def test_questions_and_labels_are_escaped(db, people):
    anna = people["anna"]
    pid = open_via_web(anna, question="<script>alert(1)</script>", option_1="<b>Yes</b>")
    for url in ("/proposals", f"/proposals/{pid}", "/", "/log"):
        page = anna.get(url).text
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page
        assert "<b><b>Yes</b></b>" not in page


def test_message_the_proposer_link(db, log_signer, people):
    anna, ben = people["anna"], people["ben"]
    pid = open_via_web(anna)
    link = f'class="btn line sm" href="matrix:u/anna:{SERVER}?action=chat"'
    assert link in ben.get(f"/proposals/{pid}").text
    assert link in ben.get("/proposals").text
    assert link not in anna.get(f"/proposals/{pid}").text  # not to yourself
    governance.close_if_due(db, pid, signer=log_signer, now=utcnow() + timedelta(days=5))
    db.commit()
    assert link not in ben.get(f"/proposals/{pid}").text  # only while the vote is open


def test_anonymous_proposer_is_not_shown_anywhere(db, people):
    anna, ben = people["anna"], people["ben"]
    assert 'name="anonymous"' in anna.get("/proposals/new").text
    pid = open_via_web(anna, anonymous="1")
    p = db.get(Proposal, pid)
    assert p.anonymous
    assert p.created_by == anna.member.id  # still attributed in the database
    for url in (f"/proposals/{pid}", "/proposals", "/"):
        page = ben.get(url).text
        assert "Proposed by an anonymous member" in page
        assert "Anna" not in page
        assert "action=chat" not in page
    entry = db.scalar(select(DecisionLog).where(DecisionLog.kind == "proposal.opened"))
    assert entry.actor_member_id is None
    assert entry.payload_json["anonymous"] is True
    assert "anna" not in str(entry.payload_json).lower()
    assert "Anna" not in ben.get("/log").text


def test_anonymous_choice_is_fixed_by_the_database(db, people):
    p = db.get(Proposal, open_via_web(people["anna"]))
    p.anonymous = True
    with pytest.raises(DBAPIError, match="fixed once it is opened"):
        db.commit()
    db.rollback()


def test_no_message_link_to_a_deactivated_proposer(db, people):
    anna, ben = people["anna"], people["ben"]
    pid = open_via_web(anna)
    governance.get(db, pid).creator.deactivated_at = utcnow()
    db.commit()
    assert "action=chat" not in ben.get(f"/proposals/{pid}").text


def test_chat_link_encodes_the_user_id():
    member = Member(matrix_user_id=f"@a/b+c:{SERVER}")
    assert proposals.chat_link(member) == f"matrix:u/a%2Fb%2Bc:{SERVER}?action=chat"


def test_nav_says_votes(people):
    page = people["anna"].get("/").text
    assert 'href="/proposals"' in page
    assert ">Votes</a>" in page
