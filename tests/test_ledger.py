"""Contract tests for the run ledger.

The rule applied throughout: a test that only shows the happy path proves nothing.
Each invariant here has a case that must be REFUSED, because every incident this
package exists for looked like success from the outside.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fd_agent_run import evidence as ev
from fd_agent_run import cli, journal, predicate as pred, state as st, view as vw


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fd-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proj"
        (self.project / "src").mkdir(parents=True)
        (self.project / ".crew").mkdir()

    def file(self, rel: str, content: str = "x\n") -> Path:
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class JournalTests(Base):
    def test_append_and_read_roundtrip(self) -> None:
        for i in range(3):
            journal.append(self.project, "r1", {"kind": "task_state", "task": f"t/{i}", "state": "pending"})
        events = journal.read(self.project, "r1")
        self.assertEqual(len(events), 3)
        self.assertTrue(all("t" in e for e in events), "every event must carry a timestamp")

    def test_missing_journal_is_empty_not_error(self) -> None:
        self.assertEqual(journal.read(self.project, "nope"), [])

    def test_unwritable_journal_raises_loudly(self) -> None:
        """A ledger that silently fails to record is worse than no ledger."""
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        (blocked / ".run").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(journal.JournalError):
            journal.append(blocked, "r1", {"kind": "run_started"})

    def test_corrupt_line_raises(self) -> None:
        path = journal.journal_path(self.project, "r1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"kind":"run_started"}\nnot json\n', encoding="utf-8")
        with self.assertRaises(journal.JournalError):
            journal.read(self.project, "r1")

    def test_event_needs_a_kind(self) -> None:
        with self.assertRaises(journal.JournalError):
            journal.append(self.project, "r1", {"task": "t"})


class StateMachineTests(unittest.TestCase):
    def test_legal_chain(self) -> None:
        chain = [st.PENDING, st.LEASED, st.WORKING, st.PRODUCED]
        for current, target in zip(chain, chain[1:]):
            st.check_transition("t", current, target)  # must not raise

    def test_terminal_states_accept_nothing(self) -> None:
        for terminal in (st.COMMITTED, st.ABANDONED):
            for target in st.STATES:
                with self.assertRaises(st.TransitionError):
                    st.check_transition("t", terminal, target)

    def test_unknown_state_name_is_refused(self) -> None:
        with self.assertRaises(st.TransitionError) as ctx:
            st.check_transition("t", st.PENDING, "done")
        self.assertIn("unknown state", str(ctx.exception))

    def test_unknown_current_state_cannot_jump_forward(self) -> None:
        """'I do not know where it is' must never become 'move it to committed'."""
        with self.assertRaises(st.TransitionError):
            st.check_transition("t", None, st.COMMITTED)
        st.check_transition("t", None, st.PENDING)  # establishing pending is fine

    def test_illegal_shortcut_is_named(self) -> None:
        with self.assertRaises(st.TransitionError) as ctx:
            st.check_transition("proj/impl/1", st.PENDING, st.COMMITTED)
        self.assertIn("proj/impl/1", str(ctx.exception))


class RebuildTests(Base):
    def test_state_is_derived_from_events(self) -> None:
        journal.append(self.project, "r1", {"kind": "run_started", "graph": "g", "policy": {"max_attempts": 3}})
        journal.append(self.project, "r1", {"kind": "task_state", "task": "a", "state": st.PENDING})
        journal.append(self.project, "r1", {"kind": "task_produced", "task": "a", "evidence": []})
        view = vw.rebuild(self.project, "r1")
        self.assertEqual(view.graph, "g")
        self.assertEqual(view.tasks["a"].state, st.PRODUCED)

    def test_snapshot_deletion_does_not_change_the_truth(self) -> None:
        journal.append(self.project, "r1", {"kind": "task_state", "task": "a", "state": st.PENDING})
        journal.append(self.project, "r1", {"kind": "task_committed", "task": "a"})
        first = vw.rebuild(self.project, "r1")
        snap = vw.write_snapshot(first)
        before = {t.task: t.state for t in first.tasks.values()}
        snap.unlink()
        after = {t.task: t.state for t in vw.rebuild(self.project, "r1").tasks.values()}
        self.assertEqual(before, after)

    def test_task_with_no_events_is_unknown_not_guessed(self) -> None:
        journal.append(self.project, "r1", {"kind": "run_started"})
        view = vw.rebuild(self.project, "r1")
        self.assertEqual(view.tasks, {})


class EvidenceTests(Base):
    def test_file_evidence_records_digest(self) -> None:
        path = self.file("src/a.js", "export const a = 1;\n")
        item = ev.make_file_evidence(path, base=self.project)
        self.assertEqual(item["path"], "src/a.js")
        self.assertEqual(item["bytes"], len(path.read_bytes()))
        self.assertEqual(len(item["sha256"]), 64)

    def test_empty_file_is_rejected(self) -> None:
        path = self.file("src/empty.js", "")
        with self.assertRaises(ev.EvidenceError):
            ev.make_file_evidence(path, base=self.project)

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaises(ev.EvidenceError):
            ev.make_file_evidence(self.project / "src" / "ghost.js", base=self.project)

    def test_i1_refuses_empty_evidence_list(self) -> None:
        with self.assertRaises(ev.EvidenceError) as ctx:
            ev.i1_gate("proj/impl/1", [])
        self.assertIn("not evidence", str(ctx.exception))

    def test_i1_refuses_when_file_vanished(self) -> None:
        path = self.file("src/a.js")
        item = ev.make_file_evidence(path, base=self.project)
        path.unlink()
        with self.assertRaises(ev.EvidenceError):
            ev.i1_gate("t", [item], base=self.project)

    def test_i1_refuses_when_content_changed(self) -> None:
        path = self.file("src/a.js", "one\n")
        item = ev.make_file_evidence(path, base=self.project)
        path.write_text("two\n", encoding="utf-8")
        with self.assertRaises(ev.EvidenceError) as ctx:
            ev.i1_gate("t", [item], base=self.project)
        self.assertIn("content changed", str(ctx.exception))

    def test_shape_validation_names_the_problem(self) -> None:
        with self.assertRaises(ev.EvidenceError) as ctx:
            ev.validate_shape({"path": "a"})
        self.assertIn("sha256", str(ctx.exception))


class PredicateTests(Base):
    def test_write_predicate_requires_non_empty_files(self) -> None:
        self.file("src/a.js")
        ok = pred.evaluate({"kind": "write", "files": ["src/a.js"]}, project=self.project)
        self.assertTrue(ok["ok"])
        bad = pred.evaluate({"kind": "write", "files": ["src/absent.js"]}, project=self.project)
        self.assertFalse(bad["ok"])
        self.assertTrue(any("absent" in c["name"] for c in bad["checks"]))

    def test_zero_byte_file_fails_even_though_it_exists(self) -> None:
        self.file("src/a.js", "")
        result = pred.evaluate({"kind": "write", "files": ["src/a.js"]}, project=self.project)
        self.assertFalse(result["ok"])
        self.assertTrue(any("non-empty" in c["name"] and not c["ok"] for c in result["checks"]))

    def test_verify_predicate_catches_zero_tests(self) -> None:
        """The exact false green from the real machine: rc 0 with nothing run."""
        evidence = [ev.make_command_evidence("node --test", 0, 0, 0.1)]
        result = pred.evaluate(
            {"kind": "verify", "cmd": "node --test", "minLines": 1},
            project=self.project, evidence=evidence,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("output lines" in c["name"] for c in result["checks"]))

    def test_verify_predicate_passes_with_real_output(self) -> None:
        evidence = [ev.make_command_evidence("node --test", 0, 8, 0.41)]
        result = pred.evaluate(
            {"kind": "verify", "cmd": "node --test", "minLines": 1, "maxSeconds": 5},
            project=self.project, evidence=evidence,
        )
        self.assertTrue(result["ok"], json.dumps(result, ensure_ascii=False))

    def test_verify_predicate_without_evidence_fails(self) -> None:
        result = pred.evaluate({"kind": "verify", "cmd": "node --test"}, project=self.project, evidence=[])
        self.assertFalse(result["ok"])

    def test_explore_needs_at_least_one_item(self) -> None:
        self.assertFalse(pred.evaluate({"kind": "explore"}, project=self.project, evidence=[])["ok"])
        item = ev.make_command_evidence("grep -rn x", 0, 3, 0.01)
        self.assertTrue(pred.evaluate({"kind": "explore"}, project=self.project, evidence=[item])["ok"])

    def test_join_requires_every_upstream_committed(self) -> None:
        result = pred.evaluate(
            {"kind": "join", "upstreams": ["a", "b"]},
            project=self.project,
            upstream_states={"a": st.COMMITTED, "b": st.WORKING},
        )
        self.assertFalse(result["ok"])
        bad = [c for c in result["checks"] if not c["ok"]]
        self.assertEqual(len(bad), 1)
        self.assertIn("b", bad[0]["name"])

    def test_join_checks_contract_coverage(self) -> None:
        (self.project / "src" / "index.js").write_text('export { a } from "./a.js";\n', encoding="utf-8")
        (self.project / ".crew" / "contract.json").write_text(
            json.dumps({"exports": {"a": "a() -> 1", "b": "b() -> 2"}}), encoding="utf-8"
        )
        result = pred.evaluate(
            {"kind": "join", "files": [], "contract": True},
            project=self.project, upstream_states={},
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("contract" in c["name"] and not c["ok"] for c in result["checks"]))

    def test_unknown_kind_is_an_error_not_a_pass(self) -> None:
        with self.assertRaises(pred.PredicateError):
            pred.evaluate({"kind": "vibes"}, project=self.project)


class InvariantEndToEndTests(Base):
    def test_i3_committed_survives_resume(self) -> None:
        """The checkpoint property: committed work is never re-queued."""
        journal.append(self.project, "r1", {"kind": "run_started"})
        journal.append(self.project, "r1", {"kind": "task_state", "task": "a", "state": st.PENDING})
        journal.append(self.project, "r1", {"kind": "task_committed", "task": "a"})
        journal.append(self.project, "r1", {"kind": "task_state", "task": "b", "state": st.WORKING})

        view = vw.rebuild(self.project, "r1")
        requeue = [t.task for t in view.tasks.values() if t.state in (st.LEASED, st.WORKING)]
        self.assertEqual(requeue, ["b"])
        self.assertEqual([t.task for t in view.committed], ["a"])


class WritePathTests(Base):
    """The wiring tests.

    A state machine, an idempotency rule or a watermark that the write path never
    consults is a comment. Every test here fails if the corresponding wiring is
    removed — that is the only thing that makes it a gate. Each test states which
    line it is holding down.
    """

    def setUp(self) -> None:
        super().setUp()
        self.file("src/a.txt", "hello\n")

    # -- helpers ---------------------------------------------------------

    def _evidence_file(self) -> str:
        import hashlib

        path = self.project / "src/a.txt"
        data = path.read_bytes()
        item = {
            "path": "src/a.txt",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        out = self.tmp / "ev.json"
        out.write_text(json.dumps(item), encoding="utf-8")
        return str(out)

    def _main(self, argv: list[str]) -> tuple[int, str, str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _start(self) -> None:
        code, _, err = self._main([
            "start", "--dir", str(self.project), "--run", "r1",
            "--task", "impl-a/1",
            "--predicate", json.dumps(
                {"task": "impl-a/1", "kind": "write", "files": ["src/a.txt"]}
            ),
        ])
        self.assertEqual(code, 0, err)

    def _produce(self) -> None:
        code, _, err = self._main([
            "record", "--dir", str(self.project), "--run", "r1",
            "--task", "impl-a/1", "--state", st.PRODUCED,
            "--evidence", self._evidence_file(),
        ])
        self.assertEqual(code, 0, err)

    def _committed_events(self) -> list[dict]:
        return [
            e for e in journal.read(self.project, "r1")
            if e.get("kind") == "task_committed" and e.get("task") == "impl-a/1"
        ]

    # -- R6: the state machine is only closed where writes happen --------

    def test_illegal_transition_is_refused_and_writes_nothing(self) -> None:
        """Holds down `_wired_transition` in cmd_record.

        Delete that call and this test goes red: the event lands, the rebuild
        reports a committed task as working, and nothing anywhere complains.
        """
        self._start()
        self._produce()
        self.assertEqual(
            self._main([
                "commit", "--dir", str(self.project), "--run", "r1",
                "--task", "impl-a/1",
            ])[0],
            0,
        )
        before = len(journal.read(self.project, "r1"))

        code, _, err = self._main([
            "record", "--dir", str(self.project), "--run", "r1",
            "--task", "impl-a/1", "--state", st.WORKING,
        ])

        self.assertEqual(code, 1, "an illegal transition must not exit 0")
        self.assertIn("refused:", err)
        self.assertIn("impl-a/1", err, "the refusal must name the task address")
        self.assertEqual(
            len(journal.read(self.project, "r1")), before,
            "a refused transition must not append an event",
        )
        self.assertEqual(
            vw.rebuild(self.project, "r1").tasks["impl-a/1"].state, st.COMMITTED,
            "the task must still read as committed",
        )

    def test_illegal_commit_is_refused(self) -> None:
        """Holds down the same call in cmd_commit (pending -> committed skips work)."""
        self._start()
        before = len(journal.read(self.project, "r1"))
        code, _, err = self._main([
            "commit", "--dir", str(self.project), "--run", "r1", "--task", "impl-a/1",
        ])
        self.assertEqual(code, 1)
        self.assertIn("refused:", err)
        self.assertEqual(len(journal.read(self.project, "r1")), before)

    # -- R4: same task address, same effect ------------------------------

    def test_second_commit_replays_instead_of_refusing(self) -> None:
        """Holds down the COMMITTED branch in cmd_commit.

        The alternative (refuse) passes every "no double write" test while
        telling automation that a finished task failed.
        """
        self._start()
        self._produce()
        argv = [
            "commit", "--dir", str(self.project), "--run", "r1", "--task", "impl-a/1", "--json",
        ]
        first_code, first_out, _ = self._main(argv)
        self.assertEqual(first_code, 0)
        self.assertEqual(len(self._committed_events()), 1)

        second_code, second_out, _ = self._main(argv)

        self.assertEqual(second_code, 0, "a replay must not read as failure")
        self.assertEqual(
            len(self._committed_events()), 1,
            "a replay must not append a second task_committed",
        )
        first = json.loads(first_out)
        second = json.loads(second_out)
        self.assertTrue(second.get("replayed"), "the output must mark itself a replay")
        self.assertTrue(second["ok"])
        # The first outcome is reproduced, not invented: same checks, same values.
        self.assertEqual(second["checks"], first["checks"])
        self.assertTrue(first["checks"], "the first commit must have judged something")

    def test_replay_does_not_rewrite_artifacts(self) -> None:
        """Holds down the early return: replay must stop before the append path."""
        self._start()
        self._produce()
        argv = [
            "commit", "--dir", str(self.project), "--run", "r1", "--task", "impl-a/1",
        ]
        self._main(argv)
        mtime = (self.project / "src/a.txt").stat().st_mtime_ns

        self._main(argv)

        self.assertEqual((self.project / "src/a.txt").stat().st_mtime_ns, mtime)


class SnapshotWatermarkTests(Base):
    """R3: a snapshot must say how much of the journal it absorbed."""

    def _start(self) -> None:
        journal.append(self.project, "r1", {"kind": "run_started"})
        journal.append(
            self.project, "r1",
            {"kind": "task_state", "task": "a", "node": "a", "attempt": 1, "state": st.PENDING},
        )
        vw.write_snapshot(vw.rebuild(self.project, "r1"))

    def test_snapshot_records_watermark(self) -> None:
        """Holds down the `absorbed_through` field in write_snapshot."""
        self._start()
        payload = json.loads(
            (journal.run_root(self.project, "r1") / "state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["absorbed_through"], len(journal.read(self.project, "r1")))
        self.assertEqual(vw.snapshot_watermark(self.project, "r1")["status"], "current")

    def test_watermark_detects_stale_snapshot(self) -> None:
        """Holds down the `raw < events` branch."""
        self._start()
        journal.append(self.project, "r1", {"kind": "task_state", "task": "b", "state": st.PENDING})
        wm = vw.snapshot_watermark(self.project, "r1")
        self.assertEqual(wm["status"], "stale")
        self.assertLess(wm["absorbed_through"], wm["events"])

    def test_watermark_beyond_journal_is_invalid_not_fatal(self) -> None:
        """Holds down the `raw > events` branch: a lying snapshot degrades, never raises."""
        self._start()
        path = journal.run_root(self.project, "r1") / "state.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["absorbed_through"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(vw.snapshot_watermark(self.project, "r1")["status"], "invalid")
        # Reading still works: every read is a full rebuild, so an invalid
        # snapshot costs a warning, not an outage.
        self.assertEqual(vw.rebuild(self.project, "r1").tasks["a"].state, st.PENDING)

    def test_missing_snapshot_is_reported_not_invented(self) -> None:
        """Holds down the `missing` branch."""
        journal.append(self.project, "r1", {"kind": "run_started"})
        self.assertEqual(vw.snapshot_watermark(self.project, "r1")["status"], "missing")


if __name__ == "__main__":
    unittest.main()


class GraphStartTests(unittest.TestCase):
    """`start --graph` asks fd-agent-graph what should run.

    Before this, the two packages were connected only by me typing node names into
    --task. That meant a graph could say seven nodes should run and the ledger could
    hold five, with nothing noticing.
    """

    REPO = Path(__file__).resolve().parents[2]
    GRAPH = REPO / "fd-agent-graph/examples/team-v3.yaml"
    ROSTER = REPO / "fd-agent-graph/roster/team-v3.yaml"

    def test_tasks_come_from_the_expanded_graph(self) -> None:
        from fd_agent_run import cli as runcli

        nodes = runcli._nodes_from_graph(str(self.GRAPH), str(self.ROSTER))
        self.assertIsNotNone(nodes, "the graph package must be readable")
        self.assertEqual(len(nodes), 8, nodes)
        self.assertIn("impl#1", nodes, "roster parallel expansion must show up")
        self.assertIn("merge", nodes)

    def test_unreadable_graph_returns_none_not_a_guess(self) -> None:
        from fd_agent_run import cli as runcli

        self.assertIsNone(runcli._nodes_from_graph("/nonexistent/graph.yaml", ""))
        self.assertIsNone(runcli._nodes_from_graph(str(self.GRAPH), "/nonexistent/roster.yaml"))

    def test_relative_paths_work(self) -> None:
        """The subprocess runs with a different cwd; relative paths must be resolved."""
        import os

        from fd_agent_run import cli as runcli

        old = os.getcwd()
        try:
            os.chdir(self.REPO)
            nodes = runcli._nodes_from_graph(
                "fd-agent-graph/examples/team-v3.yaml",
                "fd-agent-graph/roster/team-v3.yaml",
            )
        finally:
            os.chdir(old)
        self.assertIsNotNone(nodes)
        self.assertEqual(len(nodes), 8)
