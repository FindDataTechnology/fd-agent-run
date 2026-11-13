"""One vocabulary table, so two substrates cannot drift apart silently.

The problem this exists for: the platform records a turn as `ok` / `failed` /
`interrupted` / `skipped`, while the coding ledger records a task as one of seven
states plus a verdict of `committed` / `failed` / `cancelled` / `pending`. Passing
one straight into the other is how an aggregate silently becomes zero — the two
vocabularies match on nothing and nobody notices, because "0 failures" reads like
good news.

So the mapping is here, once, and it is a **total function**: every input value
maps to something, and "somewhere with no counterpart" maps to `unknown` rather
than to the nearest-looking verdict. Two calls deserve their reasoning inline:

* `interrupted` is split, not mapped. A turn the owner stopped and a turn that
  crashed both arrive as `interrupted`; calling both `failed` would blame the
  operator's own cancellation, and calling both `cancelled` would hide a crash.
  The split needs a fact from outside the turn (the run's stop reason), so it is
  a function of both.
* `skipped` produces **no fact at all**. A skipped turn is the absence of an
  attempt, not a failed attempt. Mapping it to `failed` inflates the failure rate
  with rounds that never ran; mapping it to `cancelled` claims somebody stopped
  something that was never started.

`unknown` is `fact.unknown()` — a dict, not the string `"unknown"`, so it cannot
be confused with a real verdict or slip past a truthiness check.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from .fact import unknown

COMMITTED: Final = "committed"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"
PENDING: Final = "pending"

#: The coding ledger's seven states. `predicate_failed` is `failed` here because
#: the condition was checked and not met — that is a failure, not an unknown.
CREW_STATE_TO_VERDICT: Final[dict[str, str]] = {
    "committed": COMMITTED,
    "abandoned": CANCELLED,
    "produced": PENDING,
    "pending": PENDING,
    "leased": PENDING,
    "working": PENDING,
    "predicate_failed": FAILED,
}

#: The platform's four turn statuses. Two are provisional: `interrupted` and
#: `skipped` need context that a status string does not carry — see below.
PLATFORM_STATUS_TO_VERDICT: Final[dict[str, str]] = {
    "ok": COMMITTED,
    "failed": FAILED,
    "pending": PENDING,
    # resolved by platform_verdict(); listed so the table stays exhaustive to read
    "interrupted": "?interrupted",
    # produces no fact
    "skipped": "?skipped",
}

#: Sentinel for "this input yields no fact" (an attempt that never happened).
NO_FACT: Final = object()


def crew_verdict(state: Any) -> Any:
    """Map a coding-ledger state. Unmapped states are unknown, never `pending`."""
    if not isinstance(state, str):
        return unknown()
    return CREW_STATE_TO_VERDICT.get(state, unknown())


def platform_verdict(status: Any, *, stop_reason: Any = None,
                     stopped_at: Any = None, ended_at: Any = None) -> Any:
    """Map a platform turn. Returns a verdict, `NO_FACT`, or the unknown marker.

    `interrupted` splits on evidence from the run header: a non-empty stop reason
    whose stop time is not after the turn's end means somebody stopped it on
    purpose (`cancelled`); otherwise it is a failure. When the header carries no
    stop reason at all — which is the common case for historical rows — the split
    cannot be made, and the honest answer is the conservative one: `failed`.
    A crash counted as success is worse than a cancellation counted as failure,
    and the board can show both buckets separately so the bias stays visible.
    """
    if not isinstance(status, str) or not status:
        return unknown()
    if status == "skipped":
        return NO_FACT
    if status == "interrupted":
        if not stop_reason:
            return FAILED
        stop = _as_dt(stopped_at)
        end = _as_dt(ended_at)
        if stop is not None and end is not None and stop > end:
            return FAILED
        return CANCELLED
    return PLATFORM_STATUS_TO_VERDICT.get(status, unknown())


def _as_dt(value: Any) -> dt.datetime | None:
    """Accept epoch millis, epoch seconds, or ISO text; anything else is unknown."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # The platform stores millis; seconds are ~1e9, millis ~1e12.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None
