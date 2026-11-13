"""Validate a fact against the three hard rules the translation experiment produced.

Why rules and not a schema
--------------------------
The experiment (`docs/FACT-LAYER-TRANSLATION.md`) translated real crawler telemetry
and a real coding journal into one shape. The shape fit — but only after four fields
had to be *invented* to fill it: `owner: "cron"`, `upstreams: []`,
`exit_code: 0`, `seconds: 0`. None of those were known; all of them looked like
facts downstream.

So the value here is not "these fields exist". It is that three specific confusions
are refused:

1. **unknown is not empty.** `owner: "unknown"` means we did not look; `owner: ""`
   means nobody. `upstreams: "unknown"` means we did not look; `upstreams: []`
   means it reads nothing. Collapsing the two is how a fabrication becomes
   indistinguishable from a measurement.
2. **cancelled is not failed.** A run a human stopped is not a broken run. Today the
   crawler stores both in a free-text `error_head`/`reason`, so the two cannot be
   told apart afterwards.
3. **a predicate declared after the task started is not a predicate.** It is a
   post-hoc rationalisation. Only `declared_at <= at` proves the completion
   condition predates the work.

This module has no dependency on any substrate, a database, or a clock: it takes a
fact and returns problems. That is deliberate — the rules are worth checking even
when no data exists yet.
"""

from __future__ import annotations

from typing import Any, Iterable

CORE_FIELDS = (
    "task",
    "at",
    "owner",
    "upstreams",
    "predicate",
    "evidence",
    "verdict",
    "reason",
    "attempt",
)

#: `pending` only matters for a task that has not finished; the other three are
#: terminal. `cancelled` exists because the crawler already records
#: `cancel_requested` and had nowhere to put it except free text.
VERDICTS = frozenset({"committed", "failed", "cancelled", "pending"})

#: The literal marker for "we did not look". A dict, so it can never be confused
#: with a value that happens to be the string "unknown".
UNKNOWN_MARKER = "$unknown"

#: Words that name a *class* of runner rather than a specific one.
_ROLE_WORDS = frozenset({"cron", "scheduler", "crawler", "worker", "system", "agent", "bot"})


def is_unknown(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get(UNKNOWN_MARKER))


def unknown() -> dict:
    return {UNKNOWN_MARKER: True}


def validate(fact: dict) -> list[str]:
    """Return problems (empty means the fact is admissible). Never raises."""
    if not isinstance(fact, dict):
        return [f"fact must be an object, got {type(fact).__name__}"]

    problems: list[str] = []
    task = fact.get("task")
    label = task if isinstance(task, str) and task else "<no task>"

    missing = [f for f in CORE_FIELDS if f not in fact]
    if missing:
        # A missing field is NOT the same as an unknown one. Missing means the
        # producer never considered the question; unknown means it looked and
        # could not tell. Only the second is honest.
        problems.append(
            f"{label}: missing core field(s) {', '.join(missing)} — "
            f"record {UNKNOWN_MARKER} explicitly instead of omitting"
        )

    verdict = fact.get("verdict")
    if is_unknown(verdict):
        # The unknown marker is a dict, so it is unhashable: testing membership
        # before this check raised TypeError, i.e. the validator crashed on the
        # very marker it defines. An unknown verdict is still inadmissible — the
        # four verdicts are a closed set — but it must be *reported*, not thrown.
        problems.append(
            f"{label}: verdict is {UNKNOWN_MARKER} — a producer that cannot say "
            f"which of {', '.join(sorted(VERDICTS))} applies has not established "
            f"one; say so here rather than in the ledger"
        )
    elif verdict not in VERDICTS:
        problems.append(
            f"{label}: verdict {verdict!r} is not one of {', '.join(sorted(VERDICTS))}"
        )

    problems.extend(_check_owner(label, fact.get("owner")))
    problems.extend(_check_upstreams(label, fact.get("upstreams")))
    problems.extend(_check_predicate(label, fact))
    problems.extend(_check_evidence(label, fact.get("evidence")))
    return problems


def _check_owner(label: str, owner: Any) -> list[str]:
    if is_unknown(owner):
        return []
    if owner is None or owner == "":
        return [f"{label}: owner is empty — empty reads as 'nobody'; use {UNKNOWN_MARKER} for 'not looked'"]
    # A role name is not an identity. "cron" says which *kind* of thing runs it, not
    # which run this was — and the first translation filled exactly that in. If the
    # producer only knows the kind, the honest value is the unknown marker plus the
    # kind recorded somewhere it cannot be mistaken for an owner.
    if isinstance(owner, str) and owner.strip().lower() in _ROLE_WORDS:
        return [
            f"{label}: owner is the role word {owner!r}, not a holder — a role names a "
            f"kind of runner, not which run this was; use {UNKNOWN_MARKER} plus the kind"
        ]
    if isinstance(owner, str) and owner.strip().lower() == "unknown":
        # A bare string "unknown" is too easy to confuse with a real holder named
        # "unknown", and too easy to slip past a naive `if not owner` check.
        return [
            f"{label}: owner is the bare string 'unknown' — "
            f"use {UNKNOWN_MARKER}, which cannot be mistaken for a value"
        ]
    return []


def _check_upstreams(label: str, upstreams: Any) -> list[str]:
    if is_unknown(upstreams):
        return []
    if upstreams is None:
        return [f"{label}: upstreams is null — use {UNKNOWN_MARKER} for 'not looked'"]
    # An empty list IS a legitimate fact: a task with no upstream genuinely reads
    # nothing (the five routes in the fd-fleet run were file-disjoint by design).
    # What must not happen is a producer emitting `[]` because it could not look —
    # that is the confusion rule 1 targets, and it is only preventable at the
    # producer (see the crawler's `_attribution`, which emits the marker instead).
    # Rejecting `[]` outright would make the honest answer unrepresentable.
    if isinstance(upstreams, str) and upstreams.strip().lower() == "unknown":
        return [f"{label}: upstreams is the bare string 'unknown' — use {UNKNOWN_MARKER}"]
    if isinstance(upstreams, list):
        bad = [u for u in upstreams if not (isinstance(u, str) and u.strip())]
        if bad:
            return [f"{label}: upstreams contains empty/non-string entries: {bad!r}"]
    elif not isinstance(upstreams, str):
        return [f"{label}: upstreams must be a list of strings or {UNKNOWN_MARKER}"]
    return []


def _check_predicate(label: str, fact: dict) -> list[str]:
    predicate = fact.get("predicate")
    if is_unknown(predicate):
        return []
    if not isinstance(predicate, dict):
        return [f"{label}: predicate must be an object or {UNKNOWN_MARKER}"]

    problems: list[str] = []
    declared_at = predicate.get("declared_at")
    if declared_at is None:
        problems.append(
            f"{label}: predicate has no declared_at — without it a condition written "
            f"after the fact is indistinguishable from one declared before"
        )
    elif not is_unknown(declared_at):
        at = fact.get("at")
        if isinstance(at, str) and isinstance(declared_at, str):
            if declared_at > at:
                problems.append(
                    f"{label}: predicate declared_at {declared_at} is after task start {at} "
                    f"— that is a post-hoc rationalisation, not a completion condition"
                )
    return problems


def _check_evidence(label: str, evidence: Any) -> list[str]:
    if is_unknown(evidence):
        return []
    if not isinstance(evidence, list):
        return [f"{label}: evidence must be a list or {UNKNOWN_MARKER}"]
    if not evidence:
        return []  # an empty list is legitimate for a task that produced nothing yet
    problems: list[str] = []
    for i, item in enumerate(evidence):
        if not isinstance(item, dict):
            problems.append(f"{label}: evidence[{i}] is not an object")
            continue
        if "path" in item:
            for key in ("path", "sha256", "bytes"):
                if key not in item:
                    problems.append(f"{label}: evidence[{i}] file entry missing {key!r}")
            if isinstance(item.get("bytes"), int) and item["bytes"] <= 0:
                problems.append(
                    f"{label}: evidence[{i}] bytes is {item['bytes']} — a zero-byte "
                    f"artifact is not evidence of work"
                )
        elif "cmd" in item:
            for key in ("cmd", "exit_code", "stdout_lines"):
                if key not in item:
                    problems.append(f"{label}: evidence[{i}] command entry missing {key!r}")
            exit_code = item.get("exit_code")
            if isinstance(exit_code, int) and exit_code == 0 and "cmd_observed" not in item:
                # A success code nobody observed is the most dangerous fabrication:
                # downstream reads it as "the command succeeded". Requiring
                # `cmd_observed` makes the producer say it actually ran the command.
                problems.append(
                    f"{label}: evidence[{i}] claims exit_code 0 without cmd_observed — "
                    f"a success nobody observed is indistinguishable from a real one"
                )
            if item.get("exit_code") is None:
                # Measured on the crawler side: the health report says "healthy" but
                # never records an exit code, and the first translation filled in 0.
                problems.append(
                    f"{label}: evidence[{i}] has no exit_code — record {UNKNOWN_MARKER} "
                    f"rather than implying success"
                )
        elif "observed" in item:
            pass  # observation-shaped evidence, e.g. crawler rows/ageDays
        else:
            problems.append(
                f"{label}: evidence[{i}] is neither file (path), command (cmd) nor observed"
            )
    return problems


def validate_all(facts: Iterable[dict]) -> dict:
    """Validate many facts. Returns a summary plus per-fact problems."""
    items = list(facts)
    per: list[list[str]] = []
    for fact in items:
        per.append(validate(fact))
    return {
        "facts": len(items),
        "admissible": sum(1 for p in per if not p),
        "problems": per,
        "totalProblems": sum(len(p) for p in per),
    }
