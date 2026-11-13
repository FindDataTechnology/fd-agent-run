"""Apply a graph dispatch record to the run ledger.

The dispatch record says what the runner did; the journal says what the run knows.
This module is the join, and it is deliberately one-directional: the graph package
never imports this one, so the ledger can be read by a tool that knows nothing
about graphs.

Two rules, both consequences of "an address must be stable and must not be
invented":

* a node with no task in the run is reported, never given a made-up address. A
  ledger that invents addresses cannot be resumed, because the next process would
  look for tasks that never existed.
* a not-executed outcome is written with its reason and no evidence. It is not a
  completion, so it must not look like one anywhere downstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import journal
from . import state as st
from .view import rebuild


class DispatchAdapterError(RuntimeError):
    """The record or the run cannot be joined. Loud, like every other ledger error."""


def read_record(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise DispatchAdapterError(f"cannot read dispatch record {path}: {exc}") from exc
    except ValueError as exc:
        raise DispatchAdapterError(f"dispatch record {path} is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DispatchAdapterError(f"dispatch record {path} is not a JSON object")
    if not isinstance(raw.get("outcomes"), list):
        raise DispatchAdapterError(
            f"dispatch record {path} has no 'outcomes': it records a verdict, not a run"
        )
    return raw


def apply_record(
    project: Path,
    run_id: str,
    record_path: Path,
    *,
    node_to_task: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write one journal event per not-executed outcome that has a task.

    Returns a report naming what was applied and what was skipped, because a
    silent skip here would look exactly like a task that ran.
    """
    record = read_record(record_path)
    view = rebuild(project, run_id)
    by_node: dict[str, list[str]] = {}
    for task in view.tasks.values():
        by_node.setdefault(task.node, []).append(task.task)

    applied: list[str] = []
    skipped: list[dict[str, str]] = []

    for outcome in record["outcomes"]:
        if not isinstance(outcome, dict):
            continue
        if outcome.get("status") != "not_executed":
            continue
        node = str(outcome.get("node") or "")
        reason = str(outcome.get("reason") or "")
        if reason not in st.REASONS:
            raise DispatchAdapterError(
                f"record {record_path} reports {node!r} not executed with reason "
                f"{reason!r}; legal reasons are {', '.join(st.REASONS)}"
            )

        explicit = (node_to_task or {}).get(node)
        addresses = [explicit] if explicit else by_node.get(node, [])
        if not addresses:
            skipped.append({"node": node, "reason": "no task in this run addresses it"})
            continue

        for task_id in sorted(addresses):
            task = view.tasks.get(task_id)
            if task is None:
                skipped.append({"node": node, "task": task_id, "reason": "unknown task"})
                continue
            if task.state is not None and st.is_terminal(task.state):
                # Already settled; rewriting a terminal state would be inventing a
                # second ending for one task.
                skipped.append(
                    {"node": node, "task": task_id, "reason": f"already {task.state}"}
                )
                continue
            journal.append(
                project,
                run_id,
                {
                    "kind": "task_not_executed",
                    "task": task_id,
                    "node": task.node,
                    "attempt": task.attempt,
                    "reason": reason,
                    "source": str(record_path),
                    "fingerprint": record.get("fingerprint", ""),
                },
            )
            applied.append(task_id)

    return {
        "run": run_id,
        "record": str(record_path),
        "graph": record.get("graph", ""),
        "fingerprint": record.get("fingerprint", ""),
        "applied": applied,
        "skipped": skipped,
    }
