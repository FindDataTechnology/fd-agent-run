"""`fd-agent-run` — the run ledger CLI.

Six subcommands, and they are the whole interface because the thing that executes
tasks is a host agent: stateless, restartable, and incapable of holding state
between invocations. So the ledger answers "what next", records what happened, and
decides what may be committed. Everything else is somebody else's job.

    start   create a run and expand its tasks to `pending`
    next    print the next dispatchable task with its predicate
    record  record produced/failed plus evidence (I1 is enforced here)
    commit  evaluate the predicate and commit (I2 is enforced here)
    status  report facts, mark what cannot be determined as unknown
    resume  re-queue pending and expired work, skipping committed (I3)

Exit codes: 0 ok, 1 the operation was refused (invariant or unknown), 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import adapters as ad
from . import escalation as esc
from . import evidence as ev
from . import fact as fct, journal, predicate as pred, state as st, view as vw
from . import projector as proj

#: Where the graph package lives. Overridable so the two can be separated or moved
#: independently — this package must work with the graph package absent.
GRAPH_PKG = Path(
    os.environ.get("FD_AGENT_GRAPH_PKG", Path(__file__).resolve().parents[2] / "fd-agent-graph")
)


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return
    for line in payload.get("lines", []):
        print(line)


def _nodes_from_graph(graph: str, roster: str) -> list[str] | None:
    """Node ids from fd-agent-graph's expansion, or None when it cannot answer.

    Deliberately a subprocess: the graph package owns graph semantics, this one
    owns run state, and neither should import the other. Borrowing over the CLI is
    how `planner` already borrows the code-graph index.
    """
    import subprocess

    # Resolve to absolute paths BEFORE handing them to a subprocess: its cwd is
    # GRAPH_PKG, so a relative path from the caller's directory would resolve
    # against the wrong root. Measured: './examples/team-v3.yaml' looked fine here
    # and failed inside the child.
    graph_arg = str(Path(graph).resolve())
    argv = [sys.executable, "-m", "fd_agent_graph.cli", "metrics", graph_arg, "--nodes", "--json"]
    if roster:
        argv += ["--roster", str(Path(roster).resolve())]
    env = {
        "PYTHONPATH": str(GRAPH_PKG),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    try:
        proc = subprocess.run(
            argv, cwd=str(GRAPH_PKG), capture_output=True, text=True, timeout=120, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return None
    return [str(n["id"]) for n in nodes if isinstance(n, dict) and n.get("id")]


def _lease_seconds() -> int:
    raw = os.environ.get("FD_RUN_LEASE_SECONDS", "900")
    try:
        return max(1, int(raw))
    except ValueError:
        return 900


# --------------------------------------------------------------------- start


def cmd_start(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    if not project.is_dir():
        print(f"error: --dir is not a directory: {project}", file=sys.stderr)
        return 2
    run_id = args.run or time.strftime("r-%Y%m%d-%H%M%S")
    tasks = [t for t in (args.task or [])]

    # `--graph` asks fd-agent-graph what should run, so the two packages share one
    # definition instead of me transcribing node names by hand. Borrowed over the
    # CLI, not imported: either package can be replaced without touching the other.
    if args.graph and not tasks:
        expanded = _nodes_from_graph(args.graph, args.roster)
        if expanded is None:
            print(
                f"error: cannot read nodes from graph {args.graph!r}; "
                f"run `metrics --nodes` in fd-agent-graph to see why",
                file=sys.stderr,
            )
            return 2
        tasks = [f"{node}/1" for node in expanded]
    if not tasks:
        print(
            "error: at least one --task is required. A run with no tasks would be a "
            "ledger that records nothing and reports success.",
            file=sys.stderr,
        )
        return 2

    journal.append(
        project,
        run_id,
        {
            "kind": "run_started",
            "run": run_id,
            "graph": args.graph or "",
            "roster": args.roster or "",
            "policy": {"max_attempts": args.max_attempts},
        },
    )
    # Predicates are declared AT DISPATCH, never invented at completion. A task
    # whose completion condition is decided by the one doing the work is exactly
    # how "done" came to mean "says it is done".
    # Fall back to the project's declared templates so a run does not have to
    # restate them on every invocation. `kind` selects the template; the task may
    # still override it explicitly. No hidden default: if the project declares
    # nothing, the task gets no predicate and commit will refuse it.
    templates: dict[str, dict[str, Any]] = {}
    expect_path = project / ".crew" / "expect.json"
    if expect_path.exists():
        try:
            templates = dict(json.loads(expect_path.read_text(encoding="utf-8")).get("predicate") or {})
        except ValueError:
            templates = {}

    declared: dict[str, dict[str, Any]] = {}
    for raw in args.predicate or []:
        try:
            item = json.loads(raw)
        except ValueError as exc:
            print(f"error: --predicate is not JSON ({exc})", file=sys.stderr)
            return 2
        if not isinstance(item, dict) or "task" not in item:
            print("error: --predicate needs a 'task' key", file=sys.stderr)
            return 2
        declared[str(item.pop("task"))] = item
    unknown = sorted(set(declared) - set(tasks))
    if unknown:
        print(f"error: --predicate names task(s) not in --task: {', '.join(unknown)}", file=sys.stderr)
        return 2

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for task in tasks:
        node = task.rsplit("/", 2)[0] if "/" in task else task
        own = declared.get(task)
        if own is None and args.kind:
            template = templates.get(args.kind)
            if template:
                own = {"kind": args.kind, **template}
        if own is not None:
            # Stamp WHEN the condition was declared. Without this the ledger cannot
            # prove its own predicates were set before the work started — and a
            # post-hoc condition is indistinguishable from a real one. Measured:
            # all six tasks of the fd-fleet run failed fact validation for exactly
            # this reason (docs/FACT-LAYER-TRANSLATION.md).
            own = {**own, "declared_at": own.get("declared_at") or started_at}
        journal.append(
            project,
            run_id,
            {"kind": "task_state", "task": task, "node": node, "attempt": 1,
             "state": st.PENDING, "predicate": own},
        )
    view = vw.rebuild(project, run_id)
    vw.write_snapshot(view)
    _emit(
        {"run": run_id, "tasks": len(view.tasks), "lines": [f"{run_id}: {len(view.tasks)} task(s) pending"]},
        args.json,
    )
    return 0


# ---------------------------------------------------------------------- next


def cmd_next(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    view = vw.rebuild(project, args.run)
    if not view.tasks:
        _emit({"run": args.run, "lines": ["no tasks in this run"]}, args.json)
        return 0

    # `produced` is not "done" — it is "waiting to be judged". The experiment that
    # validated this package surfaced the gap: a task parked in `produced` was
    # invisible to `next`, so a run could sit still forever while every component
    # reported success. Surface it, with what to do about it.
    awaiting = [t for t in view.tasks.values() if t.state == st.PRODUCED]
    if awaiting:
        task = sorted(awaiting, key=lambda t: t.task)[0]
        _emit(
            {
                "run": args.run,
                "task": task.task,
                "action": "commit",
                "predicate": task.predicate,
                "lines": [
                    f"task:      {task.task}",
                    f"action:    commit (already produced, not yet committed)",
                    f"predicate: {json.dumps(task.predicate or {}, ensure_ascii=False)}",
                ],
            },
            args.json,
        )
        return 0

    ready = [t for t in view.tasks.values() if t.state in (None, st.PENDING)]
    if not ready:
        done = len(view.committed)
        _emit(
            {
                "run": args.run,
                "committed": done,
                "lines": [f"nothing dispatchable: {done}/{len(view.tasks)} committed"],
            },
            args.json,
        )
        return 0

    task = sorted(ready, key=lambda t: t.task)[0]
    lines = [
        f"task:      {task.task}",
        f"node:      {task.node}",
        f"attempt:   {task.attempt}",
        f"predicate: {json.dumps(task.predicate or {}, ensure_ascii=False)}",
    ]
    if not task.predicate:
        lines.append("warning:   no predicate recorded — commit will be refused (I2)")
    _emit(
        {
            "run": args.run,
            "task": task.task,
            "node": task.node,
            "predicate": task.predicate,
            "lines": lines,
        },
        args.json,
    )
    return 0


# -------------------------------------------------------------------- record


def _wired_transition(task: str, current: str | None, target: str) -> None:
    """The one door every state write goes through.

    ``state.check_transition`` existed from the start and was called only by its
    own tests — a state machine nobody writes through is a comment, not a gate.
    Callers let ``TransitionError`` escape into ``main``, which turns it into a
    one-line refusal with the task address.
    """
    st.check_transition(task, current, target)


def _committed_outcome(project: Path, run_id: str, task_id: str) -> dict[str, Any] | None:
    """Replay what the first commit of ``task_id`` concluded.

    Idempotency is not "refuse the second call": a caller that retries must be
    able to tell "already done, here is the result" from "this attempt failed".
    A refusal reads as failure to every automation that checks an exit code.
    """
    events = journal.read(project, run_id)
    last: int | None = None
    for i, ev in enumerate(events):
        if ev.get("kind") == "task_committed" and ev.get("task") == task_id:
            last = i
    if last is None:
        return None
    checks: list[dict[str, Any]] = []
    for ev in reversed(events[:last]):
        if ev.get("kind") == "task_predicate" and ev.get("task") == task_id:
            checks = list(ev.get("checks") or [])
            break
    return {"ok": True, "checks": checks, "evidence": events[last].get("evidence") or []}


def cmd_record(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    view = vw.rebuild(project, args.run)
    task = view.tasks.get(args.task)
    if task is None:
        print(f"error: unknown task {args.task!r} in run {args.run}", file=sys.stderr)
        return 1

    items: list[dict[str, Any]] = []
    for raw in args.evidence or []:
        path = Path(raw)
        if not path.exists():
            print(f"error: evidence file not found: {path}", file=sys.stderr)
            return 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"error: evidence file is not JSON ({path}): {exc}", file=sys.stderr)
            return 1
        items.extend(payload if isinstance(payload, list) else [payload])

    # Command evidence that records an exit code must also say the command was
    # actually run. Without this flag a producer can claim `exit_code: 0` having
    # never executed anything, and the reader cannot tell — measured: the fd-fleet
    # run's own evidence failed validation for exactly this reason.
    for item in items:
        if isinstance(item, dict) and "cmd" in item and "cmd_observed" not in item:
            item["cmd_observed"] = True

    if args.state == st.NOT_EXECUTED:
        # Its own door: not_executed must carry a reason, and it is not a
        # completion, so it must not travel through the evidence gate.
        try:
            _wired_transition(args.task, task.state, st.NOT_EXECUTED)
            st.check_reason(args.task, args.reason or "")
        except st.TransitionError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        journal.append(
            project,
            args.run,
            {"kind": "task_not_executed", "task": args.task, "node": task.node,
             "attempt": task.attempt, "reason": args.reason},
        )
        vw.write_snapshot(vw.rebuild(project, args.run))
        _emit(
            {"run": args.run, "task": args.task, "state": st.NOT_EXECUTED,
             "reason": args.reason,
             "lines": [f"{args.task} -> not_executed ({args.reason})"]},
            args.json,
        )
        return 0

    if args.state == st.PRODUCED:
        # R6: a closed state set is only closed where writes happen.
        _wired_transition(args.task, task.state, st.PRODUCED)
        # I1 lives here, at the only door into `produced`.
        try:
            ev.i1_gate(args.task, items, base=project)
        except ev.EvidenceError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            journal.append(
                project,
                args.run,
                {"kind": "task_state", "task": args.task, "node": task.node,
                 "attempt": task.attempt, "state": task.state or st.PENDING,
                 "reason": f"evidence rejected: {exc}"},
            )
            return 1
        journal.append(
            project,
            args.run,
            {"kind": "task_produced", "task": args.task, "node": task.node,
             "attempt": task.attempt, "evidence": items},
        )
    else:
        _wired_transition(args.task, task.state, args.state)
        journal.append(
            project,
            args.run,
            {"kind": "task_state", "task": args.task, "node": task.node,
             "attempt": task.attempt, "state": args.state,
             "reason": args.reason or ""},
        )

    vw.write_snapshot(vw.rebuild(project, args.run))
    _emit(
        {"run": args.run, "task": args.task, "state": args.state,
         "lines": [f"{args.task} -> {args.state} ({len(items)} evidence item(s))"]},
        args.json,
    )
    return 0


# -------------------------------------------------------------------- commit


def cmd_commit(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    view = vw.rebuild(project, args.run)
    task = view.tasks.get(args.task)
    if task is None:
        print(f"error: unknown task {args.task!r} in run {args.run}", file=sys.stderr)
        return 1
    if task.state == st.COMMITTED:
        # R4: same task address, same effect. Replay the first outcome instead of
        # refusing — an automation that retries must not read "already done" as
        # "this attempt failed".
        first = _committed_outcome(project, args.run, args.task) or {"ok": True, "checks": []}
        lines = [f"{args.task}: already committed (replayed, no new event)"]
        for c in first["checks"]:
            mark = "ok" if c.get("ok") else "MISS"
            lines.append(f"  - {c.get('name')}: {mark} got {c.get('actual')!r}, "
                         f"wanted {c.get('expected')!r}")
        _emit({"run": args.run, "task": args.task, "ok": True,
               "checks": first["checks"], "replayed": True, "lines": lines}, args.json)
        return 0
    _wired_transition(args.task, task.state, st.COMMITTED)
    if task.state != st.PRODUCED:
        print(
            f"refused: {args.task} is {task.state or 'unknown'}, not produced; "
            f"commit only follows produced (I2)",
            file=sys.stderr,
        )
        return 1
    if not task.predicate:
        print(
            f"refused: {args.task} has no predicate; a task with no completion "
            f"condition cannot be completed (declare one at dispatch)",
            file=sys.stderr,
        )
        return 1

    upstream_states = {t.task: t.state for t in view.tasks.values()}
    try:
        result = pred.evaluate(
            task.predicate, project=project, evidence=task.evidence,
            upstream_states=upstream_states,
        )
    except pred.PredicateError as exc:
        print(f"refused: predicate is unusable ({exc})", file=sys.stderr)
        return 1

    journal.append(
        project,
        args.run,
        {"kind": "task_predicate", "task": args.task, "node": task.node,
         "attempt": task.attempt, "ok": result["ok"], "checks": result["checks"]},
    )
    if result["ok"]:
        journal.append(
            project,
            args.run,
            {"kind": "task_committed", "task": args.task, "node": task.node,
             "attempt": task.attempt, "evidence": task.evidence},
        )
    vw.write_snapshot(vw.rebuild(project, args.run))

    failed = [c for c in result["checks"] if not c["ok"]]
    lines = [f"{args.task}: {'committed' if result['ok'] else 'predicate failed'}"]
    for c in failed:
        lines.append(f"  - {c['name']}: got {c['actual']!r}, wanted {c['expected']!r}")
    _emit({"run": args.run, "task": args.task, "ok": result["ok"],
           "checks": result["checks"], "lines": lines}, args.json)
    return 0 if result["ok"] else 1


# -------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    view = vw.rebuild(project, args.run)
    now = time.time()
    lines: list[str] = [f"run {view.run}: {view.events} event(s), {len(view.tasks)} task(s)"]
    payload_tasks: list[dict[str, Any]] = []
    for task in sorted(view.tasks.values(), key=lambda t: t.task):
        age = ""
        if task.last_change:
            age = f"{task.last_change}"
        label = task.state if task.state is not None else "unknown"
        # `unknown` is a first-class answer: if the journal cannot say, the status
        # must say that, not guess the most plausible state.
        lines.append(f"  {task.task:<28} {label:<16} {age} {task.reason}".rstrip())
        payload_tasks.append(
            {"task": task.task, "state": task.state, "unknown": task.unknown,
             "last_change": task.last_change, "reason": task.reason}
        )
    counts: dict[str, int] = {}
    for t in view.tasks.values():
        counts[t.state or "unknown"] = counts.get(t.state or "unknown", 0) + 1
    lines.append("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    _emit({"run": view.run, "events": view.events, "tasks": payload_tasks,
           "counts": counts, "lines": lines}, args.json)
    return 0


# -------------------------------------------------------------------- resume


def cmd_resume(args: argparse.Namespace) -> int:
    project = Path(args.dir).resolve()
    before = vw.rebuild(project, args.run)
    requeued: list[str] = []
    skipped: list[str] = []
    for task in sorted(before.tasks.values(), key=lambda t: t.task):
        if task.state in (st.COMMITTED, st.NOT_EXECUTED):
            # I3: committed work is the checkpoint. Re-running it would both waste
            # the work and risk double-writing what is already a fact.
            skipped.append(task.task)
            continue
        if task.state in (None, st.PENDING, st.LEASE_EXPIRED if hasattr(st, "LEASE_EXPIRED") else st.PENDING):
            continue
        if task.state in (st.LEASED, st.WORKING):
            journal.append(
                project,
                args.run,
                {"kind": "task_state", "task": task.task, "node": task.node,
                 "attempt": task.attempt, "state": st.PENDING,
                 "reason": "lease reclaimed by resume"},
            )
            requeued.append(task.task)

    after = vw.rebuild(project, args.run)
    vw.write_snapshot(after)
    lines = [
        f"skipped {len(skipped)} committed task(s) (they are the checkpoint)",
        f"requeued {len(requeued)} unfinished task(s)",
    ]
    for t in requeued:
        lines.append(f"  + {t}")
    _emit({"run": args.run, "skipped": skipped, "requeued": requeued, "lines": lines}, args.json)
    return 0


# ------------------------------------------------------------ dispatch records


def cmd_apply_record(args: argparse.Namespace) -> int:
    """Fold a graph dispatch record into this run's journal."""
    from . import dispatch_adapter as da

    project = Path(args.dir).resolve()
    try:
        result = da.apply_record(project, args.run, Path(args.record))
    except da.DispatchAdapterError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    vw.write_snapshot(vw.rebuild(project, args.run))
    lines = [
        f"applied {len(result['applied'])} not-executed outcome(s) from {result['graph'] or 'record'}",
    ]
    for item in result["skipped"]:
        lines.append(f"  - skipped {item.get('node')}: {item.get('reason')}")
    _emit({**result, "lines": lines}, args.json)
    return 0


# ---------------------------------------------------------------------- facts


def cmd_facts(args: argparse.Namespace) -> int:
    """Read substrates into facts and validate them. Read-only, no run needed.

    This is the whole point of the layer: two different substrates, one shape, one
    check. Exit 1 means at least one fact was inadmissible — i.e. some producer
    claimed to know something it did not.
    """
    facts: list[dict[str, Any]] = []
    sources: list[str] = []
    for spec in args.source or []:
        kind, _, raw = spec.partition(":")
        reader = ad.ADAPTERS.get(kind)
        if reader is None:
            print(
                f"error: unknown source kind {kind!r}; known: {', '.join(sorted(ad.ADAPTERS))}",
                file=sys.stderr,
            )
            return 2
        read = reader(Path(raw))
        sources.append(f"{kind}:{raw} -> {len(read)} fact(s)")
        facts.extend(read)

    if not facts and not (args.source or []):
        print("error: at least one --source <kind:path> is required", file=sys.stderr)
        return 2

    results = [(f, fct.validate(f)) for f in facts]
    bad = [(f, p) for f, p in results if p]

    if args.json:
        print(json.dumps(
            {
                "sources": sources,
                "facts": len(facts),
                "admissible": len(facts) - len(bad),
                "problems": [{"task": f.get("task"), "problems": p} for f, p in bad],
            },
            ensure_ascii=False,
            indent=1,
        ))
    else:
        for line in sources:
            print(line)
        print(f"facts: {len(facts)}, admissible: {len(facts) - len(bad)}")
        for fact, problems in bad:
            for problem in problems:
                print(f"  FAIL {problem}")
    return 0 if not bad else 1


# ---------------------------------------------------------------- escalations


def cmd_escalations(args: argparse.Namespace) -> int:
    """Facts in, ranked decision cards out. Read-only.

    Exit 0 when nothing needs a human, 1 when something does. That inversion is
    deliberate: a non-zero exit here means "attention required", and it is the
    only place in this tool where non-zero is good news for the operator.
    """
    facts: list[dict[str, Any]] = []
    for spec in args.source or []:
        kind, _, raw = spec.partition(":")
        reader = ad.ADAPTERS.get(kind)
        if reader is None:
            print(
                f"error: unknown source kind {kind!r}; known: {', '.join(sorted(ad.ADAPTERS))}",
                file=sys.stderr,
            )
            return 2
        facts.extend(reader(Path(raw)))
    if not (args.source or []):
        print("error: at least one --source <kind:path> is required", file=sys.stderr)
        return 2

    result = esc.build(facts)
    if args.json:
        print(json.dumps(
            {
                "headline": result["headline"],
                "byTrigger": result["byTrigger"],
                "cards": [
                    {
                        "task": c.task, "question": c.question, "trigger": c.trigger,
                        "rank": c.rank, "default": c.default,
                        "reversibility": c.reversibility, "blast": c.blast,
                        "waiting": c.waiting,
                        "options": [
                            {"label": o.label, "effect": o.effect,
                             "cost": o.cost, "reversible": o.reversible}
                            for o in c.options
                        ],
                    }
                    for c in result["cards"]
                ],
            },
            ensure_ascii=False,
            indent=1,
        ))
    else:
        print(result["headline"])
        for card in result["cards"]:
            for line in card.as_lines():
                print(line)
            print()
    return 0 if not result["cards"] else 1


# ----------------------------------------------------------------------- cli


def cmd_project(args: argparse.Namespace) -> int:
    """Project ledgers into the shared event store. Read-only on the ledgers.

    Exit codes: 0 = absorbed (or nothing to do), 1 = refused (the ledger was
    rewritten under an address already absorbed), 2 = misconfigured (no DSN, no
    run selected). The refusal is the point: a projection that overwrote would be
    a way to rewrite a run's history without touching the journal.
    """
    project_dir = Path(args.dir) if args.dir else None

    # 平台侧：`--export <jsonl>` 直接把一个 cell 的导出投影进库。
    # 这条入口此前只能从 Python API 调（`project_platform_export`），于是日常刷新
    # 只能靠临时脚本；README 里曾写成 `project --export …` 而 CLI 根本不认这个参数
    # ——文档先行、实现没跟上，属于「写了就以为对」。现在两边对齐。
    if args.export:
        try:
            dsn = proj.dsn_from_env(args.dsn_env)
            stats = proj.project_platform_export(
                Path(args.export), dsn, cell=args.cell, dry_run=args.dry_run)
        except proj.ProjectionConflict as exc:
            print(f"REFUSED {exc}", file=sys.stderr)
            return 1
        except proj.ProjectionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=1))
        else:
            flag = " (dry-run)" if stats.get("dry_run") else ""
            invalid = " [watermark invalid -> full replay]" if stats.get("invalidated") else ""
            print(f"cell {stats.get('cell')}: events={stats.get('turns')} "
                  f"runs={stats.get('runs')} daily={stats.get('daily')} "
                  f"absorbed={stats.get('absorbed')} skipped={stats.get('skipped')}"
                  f"{invalid}{flag}")
            for state, n in sorted((stats.get("unmapped_status") or {}).items()):
                print(f"  WARN unmapped state {state!r} x{n} — recorded as unknown, "
                      f"not guessed into a verdict")
        return 0

    if project_dir is None:
        print("error: pass --dir <project> (crew side) or --export <file> (platform side)",
              file=sys.stderr)
        return 2
    run_ids = proj.discover_runs(project_dir) if args.all else ([args.run] if args.run else [])
    if not run_ids:
        print("error: pass --run <id> or --all", file=sys.stderr)
        return 2

    try:
        dsn = proj.dsn_from_env(args.dsn_env)
    except proj.ProjectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    source = args.source or proj.default_source(project_dir)
    results: list[dict[str, Any]] = []
    refused = 0
    for run_id in run_ids:
        try:
            stats = proj.project_journal(project_dir, run_id, dsn,
                                         source=source, dry_run=args.dry_run)
        except proj.ProjectionConflict as exc:
            refused += 1
            print(f"REFUSED {exc}", file=sys.stderr)
            results.append({"run": run_id, "refused": str(exc)})
            continue
        except proj.ProjectionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        stats["run"] = run_id
        results.append(stats)

    if args.json:
        print(json.dumps({"source": source, "runs": results}, ensure_ascii=False, indent=1))
    else:
        for r in results:
            if "refused" in r:
                continue
            flag = " (dry-run)" if r.get("dry_run") else ""
            invalid = " [watermark invalid -> full replay]" if r.get("invalidated") else ""
            print(f"{r['run']}: events={r['events']} absorbed={r['absorbed']} "
                  f"skipped={r['skipped']}{invalid}{flag}")
            unmapped = r.get("unmapped_states") or {}
            for state, n in sorted(unmapped.items()):
                print(f"  WARN unmapped state {state!r} x{n} — recorded as unknown, "
                      f"not guessed into a verdict")
        print(f"source: {source}")
    return 1 if refused else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fd-agent-run", description="run ledger for multi-agent work")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run", required=True)
        p.add_argument("--dir", required=True, help="project directory holding .run/")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("start", help="create a run and its pending tasks")
    p.add_argument("--graph", default="")
    p.add_argument("--roster", default="",
                   help="with --graph: expand template nodes by roster instance counts")
    p.add_argument("--task", action="append", help="task address, repeatable (e.g. impl-a/1)")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--kind", choices=["write", "verify", "explore", "join"],
                   help="predicate kind to take from the project's .crew/expect.json templates")
    p.add_argument(
        "--predicate",
        action="append",
        help="JSON with a task key: task, kind, and the kind arguments",
    )
    common(p)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("next", help="print the next dispatchable task")
    common(p)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("record", help="record produced/failed with evidence")
    p.add_argument("--task", required=True)
    p.add_argument(
        "--state",
        required=True,
        choices=[st.PRODUCED, st.WORKING, st.PREDICATE_FAILED, st.ABANDONED, st.NOT_EXECUTED],
        help="not_executed requires --reason (when_false / when_unknown / upstream_not_executed)",
    )
    p.add_argument("--evidence", action="append", help="JSON file with evidence (object or list), repeatable")
    p.add_argument("--reason", default="")
    common(p)
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("commit", help="evaluate the predicate and commit")
    p.add_argument("--task", required=True)
    common(p)
    p.set_defaults(func=cmd_commit)

    p = sub.add_parser("status", help="report facts, with unknown marked as unknown")
    common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("facts", help="read substrates into facts and validate them")
    p.add_argument("--source", action="append",
                   help="<kind>:<path>, repeatable; kinds: crew (journal.jsonl), crawl (health report)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_facts)

    p = sub.add_parser("escalations", help="ranked decisions a human must make")
    p.add_argument("--source", action="append",
                   help="<kind>:<path>, repeatable; kinds: crew (journal.jsonl), crawl (health report)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_escalations)

    p = sub.add_parser(
        "apply-record",
        help="fold a graph dispatch record's not-executed outcomes into this run",
    )
    p.add_argument("--record", required=True, help="path to a *.dispatch.json")
    common(p)
    p.set_defaults(func=cmd_apply_record)

    p = sub.add_parser("resume", help="re-queue unfinished work, skip committed")
    common(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "project",
        help="project ledger(s) into the shared event store (idempotent; refuses rewrites)",
    )
    p.add_argument("--dir", help="project directory holding .run/ (crew side)")
    p.add_argument("--run", help="one run id; omit with --all")
    p.add_argument("--all", action="store_true", help="every run under .run/")
    p.add_argument("--export", help="cell export JSONL (platform side) instead of a ledger")
    p.add_argument("--cell", help="tenant id for --export; default: read from the export's meta line")
    p.add_argument("--source", help="holder identity; default project:<repo>@<host>")
    p.add_argument("--dsn-env", default="FD_AGENT_RUN_DSN",
                   help="env var holding the connection string (never a literal)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_project)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except journal.JournalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except st.TransitionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
