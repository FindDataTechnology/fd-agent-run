"""The three rules, and proof that each one refuses what it should.

Every rule here came from a concrete failure in the translation experiment:
the first attempt filled four fields with plausible values that nobody knew
(`owner: "cron"`, `upstreams: []`, `exit_code: 0`, `seconds: 0`) and the result was
indistinguishable from a measurement. These tests exist so that the next producer
cannot do the same thing by accident.
"""

from __future__ import annotations

import unittest

from fd_agent_run.fact import UNKNOWN_MARKER, is_unknown, unknown, validate, validate_all


def good_fact(**overrides) -> dict:
    fact = {
        "task": "crew/r1/r1/1",
        "at": "2026-11-09T12:09:21+0800",
        "owner": "pid:1234",
        "upstreams": [],
        "predicate": {"kind": "write", "declared_at": "2026-11-09T12:05:00+0800"},
        "evidence": [{"path": "src/a.js", "sha256": "a" * 64, "bytes": 10}],
        "verdict": "committed",
        "reason": "",
        "attempt": 1,
    }
    fact.update(overrides)
    return fact


class AdmissibilityTests(unittest.TestCase):
    def test_a_complete_fact_is_admissible(self) -> None:
        self.assertEqual(validate(good_fact()), [])

    def test_missing_field_is_not_the_same_as_unknown(self) -> None:
        """Omitting a field says 'never considered'; unknown says 'looked, cannot tell'."""
        fact = good_fact()
        del fact["owner"]
        problems = validate(fact)
        self.assertTrue(any("missing core field" in p and "owner" in p for p in problems))
        self.assertTrue(any(UNKNOWN_MARKER in p for p in problems), "advice must name the marker")

    def test_unknown_is_admissible(self) -> None:
        fact = good_fact(owner=unknown(), upstreams=unknown())
        self.assertEqual(validate(fact), [])


class RuleOneUnknownIsNotEmptyTests(unittest.TestCase):
    def test_empty_owner_reads_as_nobody(self) -> None:
        problems = validate(good_fact(owner=""))
        self.assertTrue(any("reads as 'nobody'" in p for p in problems))
        self.assertTrue(validate(good_fact(owner=None)))

    def test_role_word_is_not_a_holder(self) -> None:
        """'cron' names a kind of runner, not which run this was.

        The first translation filled exactly this in for every crawler row. A role
        word looks like a value, passes an `if not owner` check, and tells a
        downstream reader nothing true about attribution.
        """
        for role in ("cron", "scheduler", "worker", "system", "Agent"):
            problems = validate(good_fact(owner=role))
            self.assertTrue(any("role word" in p for p in problems), role)

    def test_a_real_holder_is_fine(self) -> None:
        for owner in ("crawler-abc123", "job-7", "pid:1234", "platform-test@findmail.com"):
            self.assertEqual(validate(good_fact(owner=owner)), [], owner)

    def test_bare_string_unknown_is_refused(self) -> None:
        """A bare 'unknown' can be mistaken for a real holder and slips past `if not x`."""
        for value in ("unknown", "UNKNOWN", "  Unknown "):
            problems = validate(good_fact(owner=value))
            self.assertTrue(any("bare string" in p for p in problems), value)

    def test_empty_upstreams_is_a_legitimate_fact(self) -> None:
        """File-disjoint routes genuinely read nothing; [] is the honest answer.

        Rule 1 is about a producer emitting [] because it could not look. That
        cannot be detected here — it is prevented at the producer — so rejecting
        [] would only make the true answer unrepresentable.
        """
        self.assertEqual(validate(good_fact(upstreams=[])), [])
        self.assertEqual(validate(good_fact(upstreams=unknown())), [])

    def test_upstreams_must_not_contain_blanks(self) -> None:
        problems = validate(good_fact(upstreams=["https://a", ""]))
        self.assertTrue(any("empty/non-string" in p for p in problems))

    def test_a_real_list_is_fine(self) -> None:
        self.assertEqual(validate(good_fact(upstreams=["https://www.moa.gov.cn/x"])), [])

    def test_marker_is_a_dict_so_it_cannot_collide(self) -> None:
        self.assertTrue(is_unknown(UNKNOWN_MARKER and unknown()))
        self.assertFalse(is_unknown("unknown"))
        self.assertFalse(is_unknown([]))


class RuleTwoCancelledIsNotFailedTests(unittest.TestCase):
    def test_four_verdicts_accepted(self) -> None:
        for verdict in ("committed", "failed", "cancelled", "pending"):
            self.assertEqual(validate(good_fact(verdict=verdict)), [], verdict)

    def test_other_verdicts_refused(self) -> None:
        for verdict in ("done", "success", "aborted", "", None):
            problems = validate(good_fact(verdict=verdict))
            self.assertTrue(any("verdict" in p for p in problems), verdict)

    def test_cancelled_and_failed_are_distinct_values(self) -> None:
        """The crawler stored both in free-text reason; here they are separate."""
        c = validate(good_fact(verdict="cancelled", reason="human stopped it"))
        f = validate(good_fact(verdict="failed", reason="site 502"))
        self.assertEqual(c, [])
        self.assertEqual(f, [])


class RuleThreePredicateMustPrecedeTests(unittest.TestCase):
    def test_declared_after_start_is_refused(self) -> None:
        fact = good_fact(at="2026-11-09T12:00:00+0800",
                         predicate={"kind": "write", "declared_at": "2026-11-09T12:30:00+0800"})
        problems = validate(fact)
        self.assertTrue(any("post-hoc rationalisation" in p for p in problems))

    def test_declared_before_start_is_fine(self) -> None:
        fact = good_fact(at="2026-11-09T12:00:00+0800",
                         predicate={"kind": "write", "declared_at": "2026-11-09T11:59:00+0800"})
        self.assertEqual(validate(fact), [])

    def test_missing_declared_at_is_refused(self) -> None:
        problems = validate(good_fact(predicate={"kind": "write"}))
        self.assertTrue(any("declared_at" in p for p in problems))

    def test_unknown_declared_at_is_allowed_but_explicit(self) -> None:
        """The crawler's rules live in the inspector, so they have no declared time.
        Saying that out loud is admissible; pretending it was declared is not."""
        fact = good_fact(predicate={"kind": "crawlFresh", "declared_at": unknown()})
        self.assertEqual(validate(fact), [])

    def test_unknown_predicate_is_allowed(self) -> None:
        self.assertEqual(validate(good_fact(predicate=unknown())), [])


class EvidenceTests(unittest.TestCase):
    def test_zero_byte_artifact_is_not_evidence(self) -> None:
        problems = validate(good_fact(evidence=[{"path": "a", "sha256": "x", "bytes": 0}]))
        self.assertTrue(any("zero-byte" in p for p in problems))

    def test_exit_code_zero_without_observation_is_refused(self) -> None:
        """A success nobody observed is indistinguishable from a real one."""
        problems = validate(good_fact(evidence=[{"cmd": "crawl", "exit_code": 0, "stdout_lines": 3}]))
        self.assertTrue(any("without cmd_observed" in p for p in problems))

    def test_exit_code_zero_with_observation_is_fine(self) -> None:
        fact = good_fact(evidence=[{"cmd": "crawl", "exit_code": 0, "stdout_lines": 3,
                                    "cmd_observed": True}])
        self.assertEqual(validate(fact), [])

    def test_command_without_exit_code_is_refused(self) -> None:
        """Exactly the crawler case: 'healthy' is recorded, the exit code never was."""
        problems = validate(good_fact(evidence=[{"cmd": "crawl", "stdout_lines": 133}]))
        self.assertTrue(any("exit_code" in p for p in problems))

    def test_observation_shaped_evidence_is_accepted(self) -> None:
        fact = good_fact(evidence=[{"observed": {"rows": 133, "ageDays": 13}}])
        self.assertEqual(validate(fact), [])

    def test_unrecognised_evidence_shape_is_refused(self) -> None:
        problems = validate(good_fact(evidence=[{"whatever": 1}]))
        self.assertTrue(any("neither file" in p for p in problems))


class SummaryTests(unittest.TestCase):
    def test_validate_all_counts_admissible(self) -> None:
        facts = [good_fact(), good_fact(owner=""), good_fact(verdict="done")]
        result = validate_all(facts)
        self.assertEqual(result["facts"], 3)
        self.assertEqual(result["admissible"], 1)
        self.assertEqual(result["totalProblems"], 2)

    def test_validate_never_raises_on_junk(self) -> None:
        for junk in (None, [], "x", 42, {}, {"task": 5}):
            try:
                validate(junk)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                self.fail(f"validate raised on {junk!r}: {exc}")


if __name__ == "__main__":
    unittest.main()
