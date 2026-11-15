"""Adapters read real substrates into facts; the validator judges them.

The purpose is a claim that can be re-run: two substrates, one shape, one check.
The dangerous failure mode is an adapter that *improves* its input — stamping a
timestamp, guessing a holder, filling an exit code — because the validator
downstream then cannot tell a measurement from an invention.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fd_agent_run import adapters as ad
from fd_agent_run.fact import is_unknown, validate


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="adapter-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def journal(self, events: list[dict]) -> Path:
        path = self.tmp / "journal.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        return path

    def crawl(self, report: dict) -> Path:
        path = self.tmp / "health.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path


class CrewAdapterTests(Base):
    def test_folds_transitions_into_one_fact_per_task(self) -> None:
        path = self.journal([
            {"kind": "task_state", "task": "a", "state": "pending"},
            {"kind": "task_produced", "task": "a",
             "evidence": [{"path": "x.js", "sha256": "a" * 64, "bytes": 5}]},
            {"kind": "task_committed", "task": "a"},
        ])
        facts = ad.from_crew_journal(path)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["verdict"], "committed")
        self.assertEqual(len(facts[0]["evidence"]), 1)

    def test_absent_declared_at_does_not_become_the_task_time(self) -> None:
        """An old journal has no declared_at; inventing one would hide the defect."""
        path = self.journal([
            {"t": "2026-11-09T12:00:00+0800", "kind": "task_state", "task": "a",
             "state": "pending", "predicate": {"kind": "write", "files": ["x"]}},
        ])
        fact = ad.from_crew_journal(path)[0]
        self.assertTrue(is_unknown(fact["predicate"]["declared_at"]))

    def test_predicate_failed_is_failed_not_pending(self) -> None:
        path = self.journal([{"kind": "task_predicate", "task": "a", "ok": False}])
        self.assertEqual(ad.from_crew_journal(path)[0]["verdict"], "failed")

    def test_abandoned_maps_to_cancelled(self) -> None:
        """A stopped task is not a broken one — the fourth verdict exists for this."""
        path = self.journal([{"kind": "task_abandoned", "task": "a", "reason": "human stop"}])
        fact = ad.from_crew_journal(path)[0]
        self.assertEqual(fact["verdict"], "cancelled")
        self.assertEqual(fact["reason"], "human stop")

    def test_explicit_empty_holder_reaches_the_validator(self) -> None:
        """`"holder": ""` is the writer saying "nobody" — do not launder it to unknown.

        `validate` refuses an empty owner for exactly this reason. The adapter used
        to gate on truthiness, so the empty string vanished and the fact came out
        `$unknown` (admissible): the rule was unreachable from this substrate.
        """
        path = self.journal([
            {"kind": "task_state", "task": "a", "state": "pending", "holder": ""},
        ])
        fact = ad.from_crew_journal(path)[0]
        self.assertEqual(fact["owner"], "")
        problems = validate(fact)
        self.assertTrue(any("owner is empty" in p for p in problems), problems)

    def test_absent_holder_is_still_unknown(self) -> None:
        """The pair for the test above: no key means "not looked", which is fine."""
        path = self.journal([{"kind": "task_state", "task": "a", "state": "pending"}])
        fact = ad.from_crew_journal(path)[0]
        self.assertTrue(is_unknown(fact["owner"]))
        self.assertEqual(validate(fact), [])

    def test_missing_file_is_empty_not_an_exception(self) -> None:
        self.assertEqual(ad.from_crew_journal(self.tmp / "nope.jsonl"), [])

    def test_corrupt_line_does_not_lose_the_rest(self) -> None:
        path = self.tmp / "j.jsonl"
        path.write_text(
            '{"kind":"task_state","task":"a","state":"pending"}\nnot json\n'
            '{"kind":"task_committed","task":"a"}\n',
            encoding="utf-8",
        )
        facts = ad.from_crew_journal(path)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["verdict"], "committed")


class CrawlAdapterTests(Base):
    REPORT = {
        "at": "2026-11-08T08:02:20+00:00",
        "units": [{
            "source": "moa-hog-price", "schedule": "58 8 * * 2", "data_period": "month",
            "telemetry": {"runs": 6, "last_run": "2026-11-06T10:01:34+00:00"},
            "observations": {"rows": 133, "age_days": 13, "latest": "2026-10-26"},
            "health": {"status": "healthy", "suggested_action": ""},
        }],
    }

    def test_one_fact_per_source(self) -> None:
        facts = ad.from_crawl_report(self.crawl(self.REPORT))
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["task"], "crawl/moa-hog-price/latest")
        self.assertEqual(facts[0]["verdict"], "committed")

    def test_owner_is_unknown_not_a_role_word(self) -> None:
        """The report never says which pod ran it; "cron" would be a fabrication."""
        fact = ad.from_crawl_report(self.crawl(self.REPORT))[0]
        self.assertTrue(is_unknown(fact["owner"]))

    def test_evidence_carries_only_observations(self) -> None:
        fact = ad.from_crawl_report(self.crawl(self.REPORT))[0]
        self.assertIn("observed", fact["evidence"][0])
        self.assertNotIn("exit_code", fact["evidence"][0], "no exit code was ever recorded")

    def test_freshness_rule_has_no_declaration_time(self) -> None:
        """The inspector evaluates after the fact, so the fact must say so."""
        fact = ad.from_crawl_report(self.crawl(self.REPORT))[0]
        self.assertTrue(is_unknown(fact["predicate"]["declared_at"]))

    def test_missing_or_bad_report_is_empty(self) -> None:
        self.assertEqual(ad.from_crawl_report(self.tmp / "nope.json"), [])
        bad = self.tmp / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        self.assertEqual(ad.from_crawl_report(bad), [])


class AdmissibilityTests(Base):
    def test_crawl_facts_are_admissible(self) -> None:
        for fact in ad.from_crawl_report(self.crawl(CrawlAdapterTests.REPORT)):
            self.assertEqual(validate(fact), [], fact["task"])

    def test_crew_fact_with_full_evidence_is_admissible(self) -> None:
        path = self.journal([
            {"t": "2026-11-09T12:00:00+0800", "kind": "task_state", "task": "a",
             "state": "pending",
             "predicate": {"kind": "write", "declared_at": "2026-11-09T11:59:00+0800"}},
            {"t": "2026-11-09T12:05:00+0800", "kind": "task_committed", "task": "a",
             "evidence": [{"path": "x.js", "sha256": "a" * 64, "bytes": 5}]},
        ])
        self.assertEqual(validate(ad.from_crew_journal(path)[0]), [])

    def test_unobserved_exit_code_in_a_journal_is_rejected(self) -> None:
        """The exact defect the fd-fleet run shipped: exit_code 0, never observed."""
        path = self.journal([
            {"t": "2026-11-09T12:00:00+0800", "kind": "task_state", "task": "a",
             "state": "pending",
             "predicate": {"kind": "verify", "declared_at": "2026-11-09T11:59:00+0800"}},
            {"t": "2026-11-09T12:05:00+0800", "kind": "task_committed", "task": "a",
             "evidence": [{"cmd": "node --test", "exit_code": 0, "stdout_lines": 5}]},
        ])
        problems = validate(ad.from_crew_journal(path)[0])
        self.assertTrue(any("cmd_observed" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
