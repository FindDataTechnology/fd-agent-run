"""Read real substrates into facts. Readers, not producers.

Why an adapter layer exists at all: the fact layer is only worth anything if two
*different* substrates can be expressed in it. The translation experiment
(`docs/FACT-LAYER-TRANSLATION.md`) proved that on paper; this module proves it in
code, so the claim can be re-run instead of trusted.

The rule every adapter follows:

    **Absent is `unknown()`, never a plausible value.**

A journal that predates `declared_at` recording does not have one — the honest
fact says so. It would be easy to stamp "now" or drop the predicate, and both
would produce a fact that validates while being false. An adapter that invents is
worse than no adapter: the validator downstream cannot tell the difference.

Adapters also never raise for missing input. A missing report is `[]`, not an
exception, because a base layer that crashes on absence cannot express absence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import crosswalk
from .fact import CORE_FIELDS, unknown

#: The mapping lives in `crosswalk` so the coding ledger and the platform's turn
#: statuses cannot drift into two vocabularies that match on nothing. The old
#: local table here defaulted an unrecognised state to `pending`; that was a
#: guess wearing a verdict's clothes, and `crosswalk` answers `unknown` instead.
_STATE_TO_VERDICT = crosswalk.CREW_STATE_TO_VERDICT


def _blank_fact(task: str) -> dict[str, Any]:
    """A fact with every core field present, most of them unknown."""
    fact: dict[str, Any] = {field: unknown() for field in CORE_FIELDS}
    fact["task"] = task
    fact["owner"] = unknown()
    fact["upstreams"] = unknown()
    fact["verdict"] = "pending"
    fact["reason"] = ""
    fact["attempt"] = 1
    fact["evidence"] = []
    return fact


def from_crew_journal(journal_path: Path) -> list[dict[str, Any]]:
    """Fold an `fd-agent-run` journal into facts, one per task.

    The journal is a log of transitions; a fact is the current position plus the
    evidence that got it there. This folds the former into the latter.
    """
    path = Path(journal_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    facts: dict[str, dict[str, Any]] = {}
    produced_evidence: dict[str, list] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # One corrupt line must not make a whole run unreadable; the fact for
            # that task will simply be less complete, and it says so.
            continue
        task = event.get("task")
        if not isinstance(task, str) or not task:
            continue

        fact = facts.setdefault(task, _blank_fact(task))
        if event.get("t"):
            fact["at"] = event["t"]
        if isinstance(event.get("attempt"), int):
            fact["attempt"] = event["attempt"]
        if isinstance(event.get("holder"), str):
            # Pass the recorded holder through **verbatim, including the empty
            # string**. `"holder": ""` is the writer saying "nobody", and
            # `fact.validate` refuses it precisely because empty reads as
            # "nobody" while `$unknown` reads as "not looked". Truthiness here
            # (`if event.get("holder")`) dropped the empty string, so the fact
            # came out `$unknown` — admissible — and that rule could never fire
            # from this substrate. That is an adapter improving its input, the
            # one thing this module must not do.
            # A missing key (or an explicit null) still means "not looked".
            fact["owner"] = event["holder"]
        if isinstance(event.get("predicate"), dict):
            # Keep whatever the ledger recorded. If it has no `declared_at`, the
            # fact says unknown rather than inventing a timestamp — the fix
            # belongs in the writer, not here.
            predicate = dict(event["predicate"])
            predicate.setdefault("declared_at", unknown())
            fact["predicate"] = predicate
            # The predicate names this task's data dependencies. Reporting
            # `unknown` while the ledger states them would be the mirror of
            # inventing a value: "not looked" and "looked, nothing there" have
            # to stay different.
            if isinstance(predicate.get("upstreams"), list):
                fact["upstreams"] = predicate["upstreams"]
        if isinstance(event.get("evidence"), list) and event["evidence"]:
            produced_evidence[task] = event["evidence"]

        kind = event.get("kind")
        if kind == "task_state" and isinstance(event.get("state"), str):
            fact["verdict"] = _STATE_TO_VERDICT.get(event["state"], unknown())
            if event.get("reason"):
                fact["reason"] = str(event["reason"])
        elif kind == "task_predicate":
            fact["verdict"] = "committed" if event.get("ok") else "failed"
        elif kind == "task_committed":
            fact["verdict"] = "committed"
        elif kind == "task_abandoned":
            fact["verdict"] = "cancelled"
            fact["reason"] = str(event.get("reason") or "")

    for task, evidence in produced_evidence.items():
        facts[task]["evidence"] = evidence
    return [facts[k] for k in sorted(facts)]


def from_crawl_report(report_path: Path) -> list[dict[str, Any]]:
    """One fact per source from an `fd-industry-data` health report.

    The shape here is `{"at": ..., "units": [{source, schedule, telemetry,
    observations, health}]}`. Nothing is derived that the report does not carry:
    no exit code, no duration, no holder.
    """
    path = Path(report_path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(report, dict):
        return []

    at = report.get("at")
    facts: list[dict[str, Any]] = []
    for unit in report.get("units") or []:
        if not isinstance(unit, dict):
            continue
        source = unit.get("source")
        if not isinstance(source, str) or not source:
            continue

        telemetry = unit.get("telemetry") if isinstance(unit.get("telemetry"), dict) else {}
        observations = unit.get("observations") if isinstance(unit.get("observations"), dict) else {}
        health = unit.get("health") if isinstance(unit.get("health"), dict) else {}

        fact = _blank_fact(f"crawl/{source}/latest")
        fact["at"] = telemetry.get("last_run") or at or unknown()
        # A crawler run has a holder (the CronJob pod) but the health report does
        # not record it. That is unknown, not "cron".
        fact["owner"] = unknown()
        fact["upstreams"] = unknown()
        fact["predicate"] = {
            "kind": "crawlFresh",
            # The freshness rule lives in the inspector and is evaluated after the
            # fact, so it has no declaration time. Saying so is the point: this
            # substrate can record a verdict but cannot commit to one in advance.
            "declared_at": unknown(),
            "scheduled": unit.get("schedule") or unknown(),
            "dataPeriod": unit.get("data_period") or unknown(),
        }
        # Observation-shaped evidence: only what was actually observed.
        fact["evidence"] = [{
            "observed": {
                "rows": observations.get("rows"),
                "ageDays": observations.get("age_days"),
                "latest": observations.get("latest"),
            }
        }]
        status = health.get("status")
        fact["verdict"] = {"healthy": "committed"}.get(status, "pending")
        fact["reason"] = str(health.get("suggested_action") or unit.get("unlit_reason") or "")
        runs = telemetry.get("runs")
        fact["attempt"] = runs if isinstance(runs, int) else unknown()
        facts.append(fact)
    return facts


#: Which reader handles which substrate. A new substrate is one entry here, not a
#: new schema.
ADAPTERS = {
    "crew": from_crew_journal,
    "crawl": from_crawl_report,
}
