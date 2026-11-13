"""Append-only event journal: the single source of truth for a run.

Why a journal and not a state file
----------------------------------
A state file answers "where are we" only as long as nothing crashes between the
write and the next read. A journal answers "how did we get here", so any state is
derivable and no write can half-destroy history. That property is what makes
resume possible: the boundary of committed work is a fact in the log, not a field
somebody had to remember to update.

Layout: ``<project>/.run/<run_id>/journal.jsonl``, one JSON object per line. The
ledger lives inside the project so it travels with it — this whole toolset is
built to be copied to another machine and still be true.

Failure policy: writes are loud. A ledger that silently fails to record is worse
than no ledger, because the absence looks like "nothing happened".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

JOURNAL = "journal.jsonl"


class JournalError(RuntimeError):
    """Raised when the journal cannot be written or read. Never swallowed."""


def run_root(project: Path, run_id: str) -> Path:
    return Path(project) / ".run" / run_id


def journal_path(project: Path, run_id: str) -> Path:
    return run_root(project, run_id) / JOURNAL


def append(project: Path, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Append one event. Returns the event with its timestamp filled in.

    The timestamp is added here, not by callers: every event needs one, and a
    caller that forgets would produce a log that cannot order itself.
    """
    if not isinstance(event, dict):
        raise JournalError("journal.append: event must be a dict")
    kind = event.get("kind")
    if not isinstance(kind, str) or not kind:
        raise JournalError("journal.append: event needs a non-empty 'kind'")

    path = journal_path(project, run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JournalError(f"cannot create run directory {path.parent}: {exc}") from exc

    record = {"t": event.get("t") or time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise JournalError(f"cannot append to {path}: {exc}") from exc
    return record


def read(project: Path, run_id: str) -> list[dict[str, Any]]:
    """Every event, in write order.

    A missing journal is an empty run, not an error — the same forgiveness the
    Node tools apply to a missing report file. A *corrupt* line, however, raises:
    silently skipping it would let a run look shorter than it was.
    """
    path = journal_path(project, run_id)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JournalError(f"cannot read {path}: {exc}") from exc
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise JournalError(f"{path}:{lineno}: not JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise JournalError(f"{path}:{lineno}: event must be an object")
        events.append(payload)
    return events


def iter_events(project: Path, run_id: str) -> Iterator[dict[str, Any]]:
    yield from read(project, run_id)


def read_lines(project: Path, run_id: str) -> list[tuple[int, str, dict[str, Any], str]]:
    """Every event with its address and fingerprint: ``(seq, raw, payload, sha256)``.

    ``seq`` is the 1-based line number and it is the projection key, so the
    contract that makes it safe has to be stated where it is used: **the journal
    is append-only and is never compacted or reordered.** A writer that rewrites
    the file shifts every later line, and the projector then finds a different
    payload under an address it already absorbed — reported as a conflict, never
    overwritten.

    The fingerprint is over the raw line bytes (minus the newline), not over a
    re-serialised payload: ``json.dumps`` is not canonical — key order, spacing
    and unicode escaping all vary — so hashing a round-trip would report a
    rewrite every time the writer's serialiser changed, and miss a rewrite that
    changed only formatting.
    """
    path = journal_path(project, run_id)
    if not path.exists():
        return []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JournalError(f"cannot read {path}: {exc}") from exc

    out: list[tuple[int, str, dict[str, Any], str]] = []
    for lineno, line in enumerate(raw_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise JournalError(f"{path}:{lineno}: not JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise JournalError(f"{path}:{lineno}: event must be an object")
        out.append((lineno, line, payload, hashlib.sha256(line.encode("utf-8")).hexdigest()))
    return out
