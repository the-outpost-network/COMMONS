"""Verify a decision log: the chain, the anchors and every vote result.

Pure: only the standard library and `cryptography`, never the database or the
rest of the app. The app (`app.decision_log`) and the offline verifier
(`scripts/verify_log.py`, which loads this file on a member's own laptop) run
exactly this code, so the box can't judge its own log differently from what
members check at home. The entry format is frozen (v1).

Verification, in order:

1. `seq` starts at 1 and is contiguous; each entry's hash matches its canonical
   body, `prev_hash` links to the entry before it, and the signature is valid
   under the entry's key.
2. Every known anchor (a `seq` and hash posted to the community room) is in
   the chain unchanged.
3. Tally replay. Ballots are secret: `vote.*` entries say who voted, never what
   they chose, so single ballots can't be recounted. Per proposal the replay
   checks that votes only come between its opening and its close or
   cancellation, that a member's first vote is `vote.cast` and later ones
   `vote.changed`, that the logged turnout matches the members who voted (a
   member deactivated after voting may drop out of it), that the counts add
   up, that the rules match the ones it opened with, and that `tally` over the
   logged counts gives the logged outcome. This catches a forged outcome and
   invented or dropped voters, but not votes moved from one option to another:
   that is the price of secret ballots.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GENESIS_HASH = "0" * 64
NO_DECISION = ("no_votes", "no_quorum", "tie", "below_threshold")
VOTE_KINDS = frozenset(
    {"proposal.opened", "vote.cast", "vote.changed", "proposal.closed", "proposal.cancelled"}
)
TRIGGERS = ("window_ended", "all_voted")


# --- canonical form (frozen v1) ---------------------------------------------


def canonical(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8. NaN/Infinity are not JSON and are refused."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def entry_body(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The seven hashed envelope fields of an exported entry."""
    return {
        "seq": entry["seq"],
        "ts": entry["ts"],
        "actor": entry["actor"],
        "kind": entry["kind"],
        "payload": entry["payload"],
        "prev_hash": entry["prev_hash"],
        "key_id": entry["key_id"],
    }


def entry_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(body))).hexdigest()


# --- tally (the rules a vote closes by) -------------------------------------


@dataclass(frozen=True)
class Result:
    outcome: str  # "decided" or one of NO_DECISION
    option_id: int | None  # the winning option when decided
    counts: dict[int, int]  # option id -> current votes, every option listed
    abstained: int
    eligible: int
    quorum_met: bool

    @property
    def cast(self) -> int:
        """Votes cast for an option; abstentions are not votes cast."""
        return sum(self.counts.values())

    @property
    def turnout(self) -> int:
        """Members who voted or abstained."""
        return self.cast + self.abstained

    @property
    def decided(self) -> bool:
        return self.outcome == "decided"


def tally(
    counts: Mapping[int, int],
    eligible: int,
    quorum_pct: Decimal | None,
    threshold_pct: Decimal,
    abstained: int = 0,
) -> Result:
    """The outcome of `counts` under a rule set; pure, so a recount can reuse it.

    Integer cross-multiplication with exact decimals: with a 50 % threshold,
    3 of 6 votes is not enough and 4 of 6 is.
    """
    counts = dict(counts)
    cast = sum(counts.values())
    voters = cast + abstained
    quorum_met = quorum_pct is None or voters * 100 >= quorum_pct * eligible

    def result(outcome: str, option_id: int | None = None) -> Result:
        return Result(outcome, option_id, counts, abstained, eligible, quorum_met)

    if cast == 0:
        return result("no_votes")
    if not quorum_met:
        return result("no_quorum")
    top = max(counts.values())
    leaders = [option_id for option_id, n in counts.items() if n == top]
    if len(leaders) > 1:
        return result("tie")
    if top * 100 <= threshold_pct * cast:
        return result("below_threshold")
    return result("decided", leaders[0])


# --- verification -------------------------------------------------------------


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    # Last good entry (0 and the genesis hash for an empty log).
    head_seq: int
    head_hash: str
    # First failing seq and why; None when ok.
    failed_seq: int | None = None
    reason: str | None = None
    # Closed votes whose result was recounted (0 when the replay didn't run).
    tallies: int = 0


def verify_chain(
    entries: Iterable[Mapping[str, Any]],
    keys: Mapping[str, str],
    anchors: Iterable[tuple[int, str]] = (),
    *,
    replay: bool = True,
) -> VerifyResult:
    """Check the chain, the anchors and (with `replay`) every vote result.

    ``keys`` maps key_id to the raw Ed25519 public key in hex. ``anchors`` are
    (seq, entry_hash) pairs members hold independently (e.g. posted to Matrix).
    Returns the first failing seq; a missing entry is reported as its seq.
    """
    entries = list(entries)
    head_seq, head_hash = 0, GENESIS_HASH
    seen: dict[int, str] = {}

    def fail(seq: int, reason: str) -> VerifyResult:
        return VerifyResult(False, head_seq, head_hash, seq, reason)

    loaded: dict[str, Ed25519PublicKey | None] = {}
    for entry in entries:
        expected = head_seq + 1
        try:
            seq = entry["seq"]
            body = entry_body(entry)
            stored_hash = entry["entry_hash"]
            signature = entry["signature"]
        except (KeyError, TypeError):
            return fail(expected, "entry is missing envelope fields")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return fail(expected, "seq is not an integer")
        if seq != expected:
            if seq > expected:
                return fail(expected, f"entry {expected} is missing (next entry is {seq})")
            return fail(seq, f"seq {seq} is out of order (expected {expected})")
        try:
            computed = entry_hash(body)
        except (TypeError, ValueError):
            return fail(seq, "entry is not valid JSON")
        if computed != stored_hash:
            return fail(seq, "entry hash does not match its contents")
        if body["prev_hash"] != head_hash:
            return fail(seq, "prev_hash does not link to the previous entry")
        key_id = body["key_id"]
        if not isinstance(key_id, str):
            return fail(seq, "key id is not a string")
        if key_id not in loaded:
            loaded[key_id] = _load_public_key(keys.get(key_id))
        public = loaded[key_id]
        if public is None:
            return fail(seq, f"unknown signing key {key_id!r}")
        try:
            public.verify(bytes.fromhex(signature), bytes.fromhex(computed))
        except (InvalidSignature, ValueError, TypeError):
            return fail(seq, "signature is not valid")
        seen[seq] = computed
        head_seq, head_hash = seq, computed

    for a_seq, a_hash in sorted(anchors):
        if a_seq not in seen:
            return fail(a_seq, f"anchored entry {a_seq} is missing (log ends at {head_seq})")
        if seen[a_seq] != a_hash.lower():
            return fail(a_seq, f"entry {a_seq} does not match its anchor")

    if not replay:
        return VerifyResult(True, head_seq, head_hash)
    replayed = replay_tallies(entries)
    if replayed.failed_seq is not None:
        return fail(replayed.failed_seq, replayed.reason or "vote replay failed")
    return VerifyResult(True, head_seq, head_hash, tallies=replayed.tallies)


def _load_public_key(public_hex: str | None) -> Ed25519PublicKey | None:
    if not isinstance(public_hex, str) or not public_hex:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    except ValueError:
        return None


# --- tally replay -------------------------------------------------------------


class _Bad(ValueError):
    """A vote entry that doesn't add up; the message is the reason shown."""


@dataclass
class _Vote:
    """What the log says about one proposal so far."""

    options: dict[int, str]
    quorum_pct: Any
    threshold_pct: Any
    status: str = "open"
    voted: dict[int, int] = field(default_factory=dict)  # member id -> seq of last vote


@dataclass(frozen=True)
class Replay:
    tallies: int
    failed_seq: int | None = None
    reason: str | None = None


def _int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Bad(f"{what} is not a whole number")
    return value


def _count(value: Any, what: str) -> int:
    n = _int(value, what)
    if n < 0:
        raise _Bad(f"{what} is negative")
    return n


def _pct(value: Any, what: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _Bad(f"{what} is not a number")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise _Bad(f"{what} is not a number") from None


def replay_tallies(entries: Iterable[Mapping[str, Any]]) -> Replay:
    """Recount every closed vote from the log alone (see the module docstring).

    Expects entries whose chain already verified; reports the first entry whose
    vote record doesn't add up.
    """
    votes: dict[int, _Vote] = {}
    deactivated: dict[int, int] = {}  # member id -> seq of their latest deactivation
    tallies = 0
    for entry in entries:
        seq, kind = entry["seq"], entry["kind"]
        payload = entry["payload"]
        try:
            if kind == "member.deactivated":
                if isinstance(payload, Mapping) and isinstance(payload.get("member_id"), int):
                    deactivated[payload["member_id"]] = seq
                continue
            if kind not in VOTE_KINDS:
                continue
            if not isinstance(payload, Mapping):
                raise _Bad(f"{kind} has no payload object")
            pid = _int(payload.get("proposal_id"), f"{kind}: proposal_id")
            if kind == "proposal.opened":
                if pid in votes:
                    raise _Bad(f"vote {pid} is opened a second time")
                votes[pid] = _opened(payload)
                continue
            vote = votes.get(pid)
            if vote is None:
                raise _Bad(f"{kind} for vote {pid}, which the log never opened")
            if vote.status != "open":
                raise _Bad(f"{kind} for vote {pid} after it was {vote.status}")
            if kind in ("vote.cast", "vote.changed"):
                _ballot(kind, payload, entry.get("actor"), vote, seq)
            elif kind == "proposal.cancelled":
                vote.status = "cancelled"
            else:
                _closed(payload, vote, deactivated)
                vote.status = "closed"
                tallies += 1
        except _Bad as e:
            return Replay(tallies, seq, f"vote replay: {e}")
        except (KeyError, TypeError, ValueError, AttributeError):
            return Replay(tallies, seq, f"vote replay: {kind} entry is malformed")
    return Replay(tallies)


def _opened(payload: Mapping[str, Any]) -> _Vote:
    options: dict[int, str] = {}
    for o in payload["options"]:
        oid = _int(o["option_id"], "option id")
        if oid in options:
            raise _Bad(f"option {oid} is listed twice")
        options[oid] = o["label"]
    if len(options) < 2:
        raise _Bad("a vote needs at least two options")
    threshold = _pct(payload["threshold_pct"], "threshold_pct")
    if threshold is None:
        raise _Bad("threshold_pct is missing")
    return _Vote(options, payload.get("quorum_pct"), payload["threshold_pct"])


def _ballot(kind: str, payload: Mapping[str, Any], actor: Any, vote: _Vote, seq: int) -> None:
    member = _int(payload.get("member_id"), f"{kind}: member_id")
    if actor != member:
        raise _Bad(f"{kind} is signed for member {actor}, not the voter {member}")
    if kind == "vote.cast" and member in vote.voted:
        raise _Bad(f"member {member} has a second vote.cast (a change is vote.changed)")
    if kind == "vote.changed" and member not in vote.voted:
        raise _Bad(f"member {member} changes a vote they never cast")
    vote.voted[member] = seq


def _closed(payload: Mapping[str, Any], vote: _Vote, deactivated: Mapping[int, int]) -> None:
    if payload["trigger"] not in TRIGGERS:
        raise _Bad(f"unknown closing trigger {payload['trigger']!r}")
    counts: dict[int, int] = {}
    for row in payload["tally"]:
        oid = _int(row["option_id"], "tally option id")
        if oid not in vote.options:
            raise _Bad(f"the tally counts option {oid}, which the vote didn't offer")
        if oid in counts:
            raise _Bad(f"the tally counts option {oid} twice")
        if row["label"] != vote.options[oid]:
            raise _Bad(f"option {oid} is labelled differently than when the vote opened")
        counts[oid] = _count(row["votes"], f"votes for option {oid}")
    if set(counts) != set(vote.options):
        raise _Bad("the tally leaves out an option")
    cast = _count(payload["cast"], "cast")
    abstained = _count(payload["abstained"], "abstained")
    turnout = _count(payload["turnout"], "turnout")
    eligible = _count(payload["eligible"], "eligible")
    if cast != sum(counts.values()):
        raise _Bad(f"cast is {cast}, but the options add up to {sum(counts.values())}")
    if turnout != cast + abstained:
        raise _Bad(f"turnout is {turnout}, not cast plus abstained ({cast + abstained})")
    voters = len(vote.voted)
    gone = sum(1 for m, at in vote.voted.items() if deactivated.get(m, 0) > at)
    if not voters - gone <= turnout <= voters:
        raise _Bad(f"turnout is {turnout}, but {voters} members voted in the log")
    if turnout > eligible:
        raise _Bad(f"turnout {turnout} is more than the {eligible} members who could vote")
    if payload["trigger"] == "all_voted" and turnout != eligible:
        raise _Bad("closed because everyone voted, but not everyone did")
    if payload["quorum_pct"] != vote.quorum_pct or payload["threshold_pct"] != vote.threshold_pct:
        raise _Bad("the rules at the close differ from the ones it opened with")
    result = tally(
        counts,
        eligible,
        _pct(vote.quorum_pct, "quorum_pct"),
        _pct(vote.threshold_pct, "threshold_pct"),
        abstained,
    )
    if payload["outcome"] != result.outcome or payload["outcome_option_id"] != result.option_id:
        raise _Bad(f"the logged outcome doesn't follow from the counts (recount: {result.outcome})")
    if payload["quorum_met"] is not result.quorum_met:
        raise _Bad("quorum_met doesn't follow from the counts")
    if payload["result"] != ("passed" if result.decided else "rejected"):
        raise _Bad("the logged result doesn't follow from the outcome")
    label = vote.options.get(result.option_id) if result.option_id is not None else None
    if payload["outcome_label"] != label:
        raise _Bad("the winning option's label doesn't match")
