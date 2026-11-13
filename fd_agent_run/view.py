"""Derive run state from the journal. Nothing here caches; everything is computed.

The point of deriving rather than storing is that a snapshot can be wrong, stale or
half-written, while the log cannot be "half true" — it is either there or it is
not. `state.json` is written as a convenience, never read as authority, and a test
deletes it and asserts the rebuild is identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import journal, state as st


@dataclass
class TaskView:
    """One task's derived state."""

    task: str
    node: str
    attempt: int
    state: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    predicate: dict[str, Any] | None = None
    last_change: str | None = None
    holder: str | None = None
    expires: str | None = None
    reason: str = ""

    @property
    def unknown(self) -> bool:
        return self.state is None


@dataclass
class RunView:
    run: str
    project: Path
    graph: str = ""
    roster: str = ""
    policy: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, TaskView] = field(default_factory=dict)
    events: int = 0

    def by_state(self, want: str) -> list[TaskView]:
        return [t for t in self.tasks.values() if t.state == want]

    @property
    def committed(self) -> list[TaskView]:
        return self.by_state(st.COMMITTED)


def _task_of(event: dict[str, Any]) -> tuple[str, str, int] | None:
    task = event.get("task")
    if not isinstance(task, str) or not task:
        return None
    node = event.get("node") or task.rsplit("/", 2)[0]
    attempt = event.get("attempt")
    if not isinstance(attempt, int):
        attempt = 1
    return task, str(node), attempt


def rebuild(project: Path, run_id: str) -> RunView:
    """Fold the journal into a view. This is the only way state is ever produced."""
    events = journal.read(project, run_id)
    view = RunView(run=run_id, project=Path(project), events=len(events))

    for event in events:
        kind = event.get("kind")
        if kind == "run_started":
            view.graph = str(event.get("graph") or "")
            view.roster = str(event.get("roster") or "")
            view.policy = dict(event.get("policy") or {})
            continue

        target = _task_of(event)
        if target is None:
            # Run-level event we do not model (yet). Counting it is enough; guessing
            # at its meaning would put invented state into the view.
            continue
        task_id, node, attempt = target

        tv = view.tasks.get(task_id)
        if tv is None:
            tv = TaskView(task=task_id, node=node, attempt=attempt)
            view.tasks[task_id] = tv

        if kind == "task_leased":
            tv.state = st.LEASED
            tv.holder = event.get("holder")
            tv.expires = event.get("expires")
        elif kind == "task_state":
            tv.state = str(event.get("state") or "") or None
            if event.get("predicate"):
                tv.predicate = dict(event["predicate"])
            if event.get("reason"):
                tv.reason = str(event["reason"])
        elif kind == "task_produced":
            tv.state = st.PRODUCED
            tv.evidence = list(event.get("evidence") or [])
        elif kind == "task_predicate":
            ok = bool(event.get("ok"))
            tv.predicate = dict(event)
            tv.state = st.COMMITTED if ok else st.PREDICATE_FAILED
        elif kind == "task_committed":
            tv.state = st.COMMITTED
            if event.get("evidence"):
                tv.evidence = list(event["evidence"])
        elif kind == "task_not_executed":
            tv.state = st.NOT_EXECUTED
            tv.reason = str(event.get("reason") or "")
        elif kind == "task_abandoned":
            tv.state = st.ABANDONED
            tv.reason = str(event.get("reason") or "")
        else:
            continue
        tv.last_change = str(event.get("t") or "") or None
    return view


SNAPSHOT = "state.json"


def write_snapshot(view: RunView) -> Path:
    """Write `state.json` as a convenience. Never the source of truth.

    ``absorbed_through`` records how many journal events this snapshot absorbed.
    Without it a reader cannot tell "snapshot is current" from "snapshot is a
    stale copy that looks complete" — and the second is how a wrong answer gets
    served with full confidence.
    """
    import json

    path = journal.run_root(view.project, view.run) / SNAPSHOT
    payload = {
        "run": view.run,
        "graph": view.graph,
        "roster": view.roster,
        "policy": view.policy,
        "events": view.events,
        "absorbed_through": view.events,
        "tasks": {
            t.task: {
                "node": t.node,
                "attempt": t.attempt,
                "state": t.state,
                "last_change": t.last_change,
                "evidence": t.evidence,
                "reason": t.reason,
            }
            for t in view.tasks.values()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def snapshot_watermark(project: Path, run_id: str) -> dict[str, Any]:
    """Compare the convenience snapshot's watermark with the journal.

    Never raises and never decides state: it answers only "would using this
    snapshot be safe, and if not, why". Callers fall back to a full rebuild —
    which is what every read already does, so an invalid snapshot costs a
    warning, not an outage.
    """
    import json

    path = journal.run_root(Path(project), run_id) / SNAPSHOT
    events = len(journal.read(project, run_id))
    if not path.exists():
        return {"status": "missing", "absorbed_through": None, "events": events}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "invalid", "absorbed_through": None, "events": events}
    if not isinstance(payload, dict):
        return {"status": "invalid", "absorbed_through": None, "events": events}
    raw = payload.get("absorbed_through", payload.get("events"))
    if not isinstance(raw, int) or raw < 0:
        return {"status": "invalid", "absorbed_through": None, "events": events}
    if raw > events:
        # Tampered snapshot, or a truncated journal. Either way its contents
        # describe events that are no longer there.
        return {"status": "invalid", "absorbed_through": raw, "events": events}
    if raw < events:
        return {"status": "stale", "absorbed_through": raw, "events": events}
    return {"status": "current", "absorbed_through": raw, "events": events}
