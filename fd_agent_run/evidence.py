"""Evidence: the only thing that may move a task into `produced`.

A role's report is prose. Prose is not evidence — on the real machine a role
reported `completed` with zero files on disk, another shipped an empty `test/`
directory that made the test runner exit 0, and one invented a statement about
what the orchestrator had done. None of those could have passed a check that asked
"show me the bytes".

Two shapes cover everything this workspace produces:

    {"path": "src/history.js", "sha256": "...", "bytes": 11183}
    {"cmd": "node --test", "exit_code": 0, "stdout_lines": 8, "seconds": 0.41}

Deliberately shallow: no recursive directory hashing. Task-level granularity is
enough for every failure observed so far, and a cheap check that runs every time
beats a thorough one that gets skipped.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class EvidenceError(ValueError):
    """Evidence is missing, malformed, or does not match what is on disk."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_file_evidence(path: Path, *, base: Path | None = None) -> dict[str, Any]:
    """Describe a file that must exist and be non-empty."""
    if not path.exists():
        raise EvidenceError(f"file evidence: {path} does not exist")
    size = path.stat().st_size
    if size == 0:
        raise EvidenceError(f"file evidence: {path} is empty (0 bytes)")
    rel = str(path.relative_to(base)) if base else str(path)
    return {"path": rel, "sha256": sha256_of(path), "bytes": size}


def make_command_evidence(
    cmd: str, exit_code: int, stdout_lines: int, seconds: float
) -> dict[str, Any]:
    """Describe an observed command outcome. Nothing is executed here."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise EvidenceError("command evidence: cmd must be a non-empty string")
    if not isinstance(exit_code, int):
        raise EvidenceError("command evidence: exit_code must be an int")
    if not isinstance(stdout_lines, int) or stdout_lines < 0:
        raise EvidenceError("command evidence: stdout_lines must be a non-negative int")
    if not isinstance(seconds, (int, float)) or seconds < 0:
        raise EvidenceError("command evidence: seconds must be a non-negative number")
    return {
        "cmd": cmd,
        "exit_code": exit_code,
        "stdout_lines": stdout_lines,
        "seconds": round(float(seconds), 3),
    }


def validate_shape(item: Any) -> None:
    """Raise unless `item` is one of the two legal evidence shapes."""
    if not isinstance(item, dict):
        raise EvidenceError(f"evidence item must be an object, got {type(item).__name__}")
    if "path" in item:
        for key in ("path", "sha256", "bytes"):
            if key not in item:
                raise EvidenceError(f"file evidence missing {key!r}: {item!r}")
        if not isinstance(item["bytes"], int) or item["bytes"] <= 0:
            raise EvidenceError(f"file evidence bytes must be a positive int: {item!r}")
        return
    if "cmd" in item:
        for key in ("cmd", "exit_code", "stdout_lines", "seconds"):
            if key not in item:
                raise EvidenceError(f"command evidence missing {key!r}: {item!r}")
        return
    raise EvidenceError(
        "evidence item is neither a file ({path,sha256,bytes}) nor a command "
        f"({cmd,exit_code,stdout_lines,seconds}): {item!r}"
    )


def verify(item: dict[str, Any], *, base: Path | None = None) -> list[str]:
    """Re-check a recorded item against the world. Returns problems (empty = ok)."""
    validate_shape(item)
    if "path" not in item:
        # A command's outcome is a historical fact; re-running it is the task's
        # business, not the ledger's. Shape validation is all we can do here.
        return []
    path = Path(item["path"]) if base is None else Path(base) / item["path"]
    if not path.exists():
        return [f"{item['path']}: no longer exists"]
    if path.stat().st_size == 0:
        return [f"{item['path']}: now empty"]
    if sha256_of(path) != item["sha256"]:
        return [f"{item['path']}: content changed since it was recorded"]
    return []


def i1_gate(task: str, evidence: list[dict[str, Any]] | None, *, base: Path | None = None) -> None:
    """Invariant I1: no evidence, no `produced`.

    Called immediately before a task is moved into `produced`. Raises
    `EvidenceError` — a task that cannot show the bytes stays where it is.
    """
    if not evidence:
        raise EvidenceError(
            f"{task}: cannot mark produced without evidence "
            f"(a role saying it finished is not evidence)"
        )
    problems: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            problems.append(f"evidence item is not an object: {item!r}")
            continue
        try:
            problems.extend(verify(item, base=base))
        except EvidenceError as exc:
            problems.append(str(exc))
    if problems:
        raise EvidenceError(f"{task}: evidence does not hold up: " + "; ".join(problems))
