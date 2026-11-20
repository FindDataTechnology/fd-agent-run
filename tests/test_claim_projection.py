"""The claim lane's reducer (harden-agent-runtime-spine, agent-run-store 6.2).

`heartbeats do not grow the row count` is the rule these pin. It lives in a
pure function (`projector.reduce_claims`) precisely so it can be checked without
a database — a rule only verifiable by connecting to Postgres is a rule nobody
re-checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from fd_agent_run.projector import reduce_claims  # noqa: E402


def claim(ref, **over):
    row = {"k": "claim", "turn_ref": ref, "run_id": "r", "turn_id": "t", "started_at": 1000}
    row.update(over)
    return row


def test_one_row_per_attempt_even_after_many_heartbeats():
    # Three observations of the SAME attempt: start, a mid beat, a late beat.
    objects = [
        claim("platform|cell:aa|r|t", heartbeat_at=1000),
        claim("platform|cell:aa|r|t", heartbeat_at=1030),
        claim("platform|cell:aa|r|t", heartbeat_at=1060),
    ]
    out = reduce_claims(objects)
    assert len(out) == 1, "a long turn is one row, not one per heartbeat"
    assert out["platform|cell:aa|r|t"]["heartbeat_at"] == 1060, "the latest picture wins"


def test_two_attempts_stay_two_rows():
    out = reduce_claims([claim("platform|cell:aa|r|t1"), claim("platform|cell:aa|r|t2")])
    assert set(out) == {"platform|cell:aa|r|t1", "platform|cell:aa|r|t2"}


def test_non_claim_objects_are_ignored():
    out = reduce_claims([{"k": "turn", "turn_ref": "x"}, {"k": "run"}, claim("a|b|c|d")])
    assert set(out) == {"a|b|c|d"}, "only claim rows feed this lane"


def test_a_claim_without_an_address_is_dropped_not_invented():
    out = reduce_claims([{"k": "claim", "run_id": "r"}, claim("")])
    assert out == {}, "an attempt with no address cannot be projected — and must not get a made-up one"


def test_close_state_survives_a_later_beat():
    # The last observation in the batch is the closed one; a reader must see the
    # close, not the beat that preceded it.
    objects = [
        claim("a|b|c|d", heartbeat_at=1050, closed_at=None, close_reason=None),
        claim("a|b|c|d", heartbeat_at=1060, closed_at=1070, close_reason="interrupted"),
    ]
    out = reduce_claims(objects)["a|b|c|d"]
    assert out["closed_at"] == 1070
    assert out["close_reason"] == "interrupted"
