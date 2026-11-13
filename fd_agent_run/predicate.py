"""Completion predicates: what makes a task allowed to be `committed`.

The distinction this module exists to enforce is between *doing* and *being done*.
On the real machine, `node --test` exited 0 on an empty test directory, a CLI exited
0 printing nothing, and a walk hung until the host killed it — in all three cases
the work "ran" and nothing was finished.

Four templates cover the task kinds this workspace actually produces. Each one is
cheap enough to run on every commit, because a check that is expensive is a check
that gets skipped.

    write    files exist, are non-empty, and every contract name is exported
    verify   a command's exit code, output size and duration are within bounds
    explore  at least one reviewable piece of evidence was recorded
    join     every upstream is committed and the contract is fully re-exported
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_EXPORT_DECL = re.compile(
    r"^\s*export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
    re.MULTILINE,
)
_EXPORT_LIST = re.compile(r"^\s*export\s*\{([^}]*)\}", re.MULTILINE)


class PredicateError(ValueError):
    """A predicate could not be evaluated. Never treated as a pass."""


def _js_exports(project: Path) -> set[str]:
    names: set[str] = set()
    for path in sorted((project / "src").glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(_EXPORT_DECL.findall(text))
        for group in _EXPORT_LIST.findall(text):
            for piece in group.split(","):
                alias = piece.split(" as ")[-1].strip()
                if IDENTIFIER.match(alias):
                    names.add(alias)
    return names


def _check(name: str, ok: bool, actual: Any, expected: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "actual": actual, "expected": expected}


def evaluate(
    predicate: dict[str, Any],
    *,
    project: Path,
    evidence: list[dict[str, Any]] | None = None,
    upstream_states: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Run a predicate. Returns `{ok, kind, checks}` — never raises for a failure.

    `PredicateError` is reserved for "the predicate itself is unusable", which is a
    configuration bug and must not be reported as a passing task.
    """
    if not isinstance(predicate, dict):
        raise PredicateError("predicate must be an object")
    kind = predicate.get("kind")
    if kind not in ("write", "verify", "explore", "join"):
        raise PredicateError(
            f"unknown predicate kind {kind!r}; expected write / verify / explore / join"
        )

    project = Path(project)
    checks: list[dict[str, Any]] = []

    if kind in ("write", "join"):
        files = predicate.get("files") or []
        if not isinstance(files, list):
            raise PredicateError("write/join predicate: 'files' must be a list")
        for rel in files:
            path = project / str(rel)
            exists = path.exists()
            size = path.stat().st_size if exists else 0
            checks.append(_check(f"file {rel} exists", exists, exists, True))
            checks.append(_check(f"file {rel} non-empty", size > 0, size, ">0"))

        contract = project / ".crew" / "contract.json"
        if predicate.get("contract") and contract.exists():
            import json

            try:
                declared = json.loads(contract.read_text(encoding="utf-8")).get("exports") or {}
            except ValueError as exc:
                raise PredicateError(f"contract is not JSON: {exc}") from exc
            actual = _js_exports(project)
            missing = sorted(n for n in declared if n not in actual)
            checks.append(
                _check("every contract name is exported", not missing, missing, [])
            )

    if kind == "verify":
        cmd = predicate.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise PredicateError("verify predicate: 'cmd' must be a non-empty string")
        command_ev = [
            e for e in (evidence or []) if isinstance(e, dict) and e.get("cmd") == cmd
        ]
        if not command_ev:
            checks.append(_check(f"evidence for {cmd!r}", False, None, "recorded"))
        else:
            ev = command_ev[-1]
            want_rc = predicate.get("exitCode", 0)
            want_lines = predicate.get("minLines", 0)
            max_seconds = predicate.get("maxSeconds")
            checks.append(
                _check(f"{cmd} exit code", ev.get("exit_code") == want_rc, ev.get("exit_code"), want_rc)
            )
            checks.append(
                _check(
                    f"{cmd} output lines",
                    (ev.get("stdout_lines") or 0) >= want_lines,
                    ev.get("stdout_lines"),
                    f">={want_lines}",
                )
            )
            if isinstance(max_seconds, (int, float)):
                checks.append(
                    _check(
                        f"{cmd} duration",
                        float(ev.get("seconds") or 0) <= float(max_seconds),
                        ev.get("seconds"),
                        f"<={max_seconds}s",
                    )
                )

    if kind == "explore":
        checks.append(
            _check("evidence recorded", bool(evidence), len(evidence or []), ">=1")
        )

    if kind == "join":
        upstreams = predicate.get("upstreams") or []
        if not isinstance(upstreams, list):
            raise PredicateError("join predicate: 'upstreams' must be a list")
        states = upstream_states or {}
        for up in upstreams:
            got = states.get(str(up))
            checks.append(
                _check(f"upstream {up} committed", got == "committed", got, "committed")
            )

    ok = all(c["ok"] for c in checks) and bool(checks)
    return {"kind": kind, "ok": ok, "checks": checks}
