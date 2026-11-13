"""Contract tests for non-execution in the ledger.

Not-executed work is the state this change adds, and the reason it must be its own
state: folding it into `committed` would claim work that never ran, and folding it
into `predicate_failed` would report a defect where the graph deliberately
branchéd away. Both read as success or failure from the outside, which is exactly
how the incidents this package exists for looked.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fd_agent_run import cli, dispatch_adapter as da, journal, state as st, view as vw


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fd-run-ne-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proj"
        (self.project / "src").mkdir(parents=True)
        (self.project / ".crew").mkdir()

    def _main(self, argv):
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _start(self, task: str = "ship/1", node: str = "ship") -> None:
        code, _, err = self._main(
            [
                "start",
                "--dir",
                str(self.project),
                "--run",
                "r1",
                "--task",
                task,
                "--predicate",
                json.dumps({"task": task, "kind": "write", "files": ["src/a.txt"]}),
            ]
        )
        self.assertEqual(code, 0, err)

    def _record_not_executed(self, task: str, reason: str):
        return self._main(
            [
                "record",
                "--dir",
                str(self.project),
                "--run",
                "r1",
                "--task",
                task,
                "--state",
                st.NOT_EXECUTED,
                "--reason",
                reason,
            ]
        )


class StateTest(Base):
    def test_not_executed_is_a_known_state(self):
        self.assertIn(st.NOT_EXECUTED, st.STATES)
        self.assertTrue(st.is_terminal(st.NOT_EXECUTED))

    def test_an_unknown_state_is_still_refused(self):
        with self.assertRaises(st.TransitionError):
            st.check_transition("t", st.PENDING, "skipped")

    def test_not_executed_accepts_no_further_transition(self):
        for target in (st.WORKING, st.PRODUCED, st.COMMITTED, st.PENDING):
            with self.assertRaises(st.TransitionError):
                st.check_transition("t", st.NOT_EXECUTED, target)


class ReasonTest(Base):
    def test_a_write_without_a_reason_is_refused(self):
        self._start()
        code, _, err = self._record_not_executed("ship/1", "")
        self.assertEqual(code, 1)
        self.assertIn("needs a reason", err)

    def test_a_write_with_an_invented_reason_is_refused(self):
        self._start()
        code, _, err = self._record_not_executed("ship/1", "because the model said so")
        self.assertEqual(code, 1)
        self.assertIn("when_false", err)

    def test_each_of_the_three_reasons_is_accepted(self):
        for index, reason in enumerate(st.REASONS):
            project = self.tmp / f"p{index}"
            (project / "src").mkdir(parents=True)
            (project / ".crew").mkdir()
            self.project = project
            self._start()
            code, _, err = self._record_not_executed("ship/1", reason)
            self.assertEqual(code, 0, f"{reason}: {err}")
            view = vw.rebuild(project, "r1")
            self.assertEqual(view.tasks["ship/1"].state, st.NOT_EXECUTED)
            self.assertEqual(view.tasks["ship/1"].reason, reason)


class CountTest(Base):
    def test_not_executed_is_neither_committed_nor_failed(self):
        self._start()
        self._record_not_executed("ship/1", st.REASON_WHEN_FALSE)
        view = vw.rebuild(self.project, "r1")
        self.assertEqual(view.committed, [])
        self.assertEqual(view.by_state(st.PREDICATE_FAILED), [])
        self.assertEqual([t.task for t in view.by_state(st.NOT_EXECUTED)], ["ship/1"])

    def test_status_counts_it_under_its_own_key(self):
        self._start()
        self._record_not_executed("ship/1", st.REASON_UPSTREAM_NOT_EXECUTED)
        code, out, _ = self._main(
            ["status", "--dir", str(self.project), "--run", "r1", "--json"]
        )
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["counts"], {st.NOT_EXECUTED: 1})
        self.assertNotIn(st.COMMITTED, payload["counts"])
        self.assertNotIn(st.PREDICATE_FAILED, payload["counts"])


class ResumeTest(Base):
    def test_resume_does_not_requeue_not_executed_work(self):
        self._start()
        self._record_not_executed("ship/1", st.REASON_WHEN_FALSE)
        code, out, err = self._main(
            ["resume", "--dir", str(self.project), "--run", "r1", "--json"]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["requeued"], [])


class AdapterTest(Base):
    def record(self, outcomes) -> Path:
        path = self.tmp / "g.dispatch.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "graph": "g",
                    "fingerprint": "sha256:abc",
                    "outcomes": outcomes,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_a_not_executed_outcome_reaches_the_journal(self):
        self._start()
        path = self.record(
            [{"node": "ship", "status": "not_executed", "reason": st.REASON_WHEN_FALSE}]
        )
        result = da.apply_record(self.project, "r1", path)
        self.assertEqual(result["applied"], ["ship/1"])
        view = vw.rebuild(self.project, "r1")
        self.assertEqual(view.tasks["ship/1"].state, st.NOT_EXECUTED)

    def test_a_node_with_no_task_is_reported_not_invented(self):
        self._start()
        path = self.record(
            [{"node": "ghost", "status": "not_executed", "reason": st.REASON_WHEN_FALSE}]
        )
        result = da.apply_record(self.project, "r1", path)
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["skipped"][0]["node"], "ghost")
        self.assertNotIn("ghost/1", vw.rebuild(self.project, "r1").tasks)

    def test_an_invented_reason_is_refused(self):
        self._start()
        path = self.record([{"node": "ship", "status": "not_executed", "reason": "nope"}])
        with self.assertRaises(da.DispatchAdapterError) as ctx:
            da.apply_record(self.project, "r1", path)
        self.assertIn("legal reasons", str(ctx.exception))

    def test_a_terminal_task_is_not_rewritten(self):
        self._start()
        self._record_not_executed("ship/1", st.REASON_WHEN_FALSE)
        path = self.record(
            [{"node": "ship", "status": "not_executed", "reason": st.REASON_WHEN_UNKNOWN}]
        )
        result = da.apply_record(self.project, "r1", path)
        self.assertEqual(result["applied"], [])
        self.assertIn("already", result["skipped"][0]["reason"])
        # One ending per task: the second reason must not have been appended.
        events = journal.read(self.project, "r1")
        self.assertEqual(
            len([e for e in events if e.get("kind") == "task_not_executed"]), 1
        )

    def test_a_record_without_outcomes_is_refused(self):
        path = self.tmp / "verdict-only.dispatch.json"
        path.write_text(json.dumps({"graph": "g", "dispatch": {"permitted": False}}), encoding="utf-8")
        with self.assertRaises(da.DispatchAdapterError) as ctx:
            da.apply_record(self.project, "r1", path)
        self.assertIn("records a verdict, not a run", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
