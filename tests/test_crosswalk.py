"""The crosswalk is a total function, and the two calls that are not one-to-one.

These tests exist because the failure they prevent is silent: two vocabularies that
match on nothing produce an aggregate of zero, and zero failures reads like good
news. Every case here is a value that a naive `.get(status, "failed")` would turn
into a confident lie.
"""

from __future__ import annotations

import datetime as dt
import unittest

from fd_agent_run import crosswalk as cw
from fd_agent_run.fact import is_unknown


class CrewStateMapping(unittest.TestCase):
    def test_every_known_state_maps(self) -> None:
        expected = {
            "committed": "committed",
            "abandoned": "cancelled",
            "produced": "pending",
            "pending": "pending",
            "leased": "pending",
            "working": "pending",
            "predicate_failed": "failed",
        }
        for state, verdict in expected.items():
            self.assertEqual(cw.crew_verdict(state), verdict, state)

    def test_unmapped_state_is_unknown_not_pending(self) -> None:
        """A state nobody has seen before must not be reported as "not started yet"."""
        result = cw.crew_verdict("superceded_by_newer_runtime")
        self.assertTrue(is_unknown(result))

    def test_non_string_state_is_unknown(self) -> None:
        self.assertTrue(is_unknown(cw.crew_verdict(None)))


class PlatformStatusMapping(unittest.TestCase):
    def test_ok_and_failed(self) -> None:
        self.assertEqual(cw.platform_verdict("ok"), "committed")
        self.assertEqual(cw.platform_verdict("failed"), "failed")
        self.assertEqual(cw.platform_verdict("pending"), "pending")

    def test_skipped_produces_no_fact(self) -> None:
        """A skipped turn is the absence of an attempt, not a failed attempt.

        Folding it into `failed` inflates the failure rate with rounds that never
        ran; folding it into `cancelled` claims somebody stopped work that was
        never started. Neither is true, so it yields no fact at all.
        """
        self.assertIs(cw.platform_verdict("skipped"), cw.NO_FACT)

    def test_interrupted_without_stop_reason_is_failed(self) -> None:
        """The conservative side of the known bias: a crash must never read as success."""
        self.assertEqual(cw.platform_verdict("interrupted"), "failed")

    def test_interrupted_with_earlier_stop_reason_is_cancelled(self) -> None:
        self.assertEqual(
            cw.platform_verdict(
                "interrupted",
                stop_reason="owner cancelled",
                stopped_at=1763000000000,        # millis, as the platform stores it
                ended_at=1763000060000,
            ),
            "cancelled",
        )

    def test_interrupted_with_later_stop_reason_is_failed(self) -> None:
        """A stop recorded after the turn ended cannot explain this turn's ending."""
        self.assertEqual(
            cw.platform_verdict(
                "interrupted",
                stop_reason="owner cancelled",
                stopped_at=1763000600000,
                ended_at=1763000000000,
            ),
            "failed",
        )

    def test_unmapped_status_is_unknown(self) -> None:
        self.assertTrue(is_unknown(cw.platform_verdict("quarantined")))

    def test_empty_status_is_unknown(self) -> None:
        self.assertTrue(is_unknown(cw.platform_verdict("")))

    def test_unknown_is_a_active_marker_not_a_string(self) -> None:
        """`"unknown"` as a string can be mistaken for a holder; the marker cannot."""
        self.assertNotIsInstance(cw.platform_verdict("nope"), str)
        self.assertTrue(is_unknown(cw.platform_verdict("nope")))


class TimestampParsing(unittest.TestCase):
    def test_millis_seconds_and_iso_agree(self) -> None:
        millis = 1763000000000
        seconds = millis // 1000
        # Derive the ISO text from the epoch rather than writing it by hand: a
        # hand-written literal that disagrees with the number tests the literal.
        iso = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat()
        a, b, c = cw._as_dt(millis), cw._as_dt(seconds), cw._as_dt(iso)
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_garbage_is_none_not_now(self) -> None:
        self.assertIsNone(cw._as_dt(""))
        self.assertIsNone(cw._as_dt("not a date"))
        self.assertIsNone(cw._as_dt(None))


if __name__ == "__main__":
    unittest.main()
