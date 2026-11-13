"""Task states and the transitions that are allowed between them.

The states are a closed set on purpose. Every incident that motivated this package
was a state that did not exist: a task that "finished" by assertion, a branch that
was neither running nor finished, work that was complete but not committed. Giving
those conditions names is most of the fix.

    pending --lease--> leased --work--> working --produce--> produced
                                            ^          |
                                            +----------+
                                       (or straight from pending/produced below)

    In *this* tool the dispatch step is a query (`next` prints who should act); no
    command writes a lease event, so `leased` is reachable only by an executor that
    chooses to record it. `pending -> produced` is therefore legal: the first write
    a real run makes is the produced report. Relaxing that transition is what the
    first wiring of `check_transition` surfaced — before it, nothing consulted the
    machine at all.

Two transitions carry an invariant:

* reaching ``produced`` requires evidence (I1, enforced in evidence.py)
* reaching ``committed`` requires a passing predicate (I2, enforced in predicate.py)

And one is the whole point:

* only ``committed`` is a checkpoint (I3, honoured by resume)
"""

from __future__ import annotations

from typing import Final

PENDING: Final = "pending"
LEASED: Final = "leased"
WORKING: Final = "working"
PRODUCED: Final = "produced"
COMMITTED: Final = "committed"
PREDICATE_FAILED: Final = "predicate_failed"
ABANDONED: Final = "abandoned"

STATES: Final = (
    PENDING,
    LEASED,
    WORKING,
    PRODUCED,
    COMMITTED,
    PREDICATE_FAILED,
    ABANDONED,
)

#: Terminal states accept no further transition except the explicit re-queue that
#: ``resume`` performs on a *new attempt* (which is a different task address, so
#: the old task stays terminal forever — that is the point of addressing attempts).
TERMINAL: Final = (COMMITTED, ABANDONED)

ALLOWED: Final[dict[str, frozenset[str]]] = {
    # `produced` straight from `pending`: this CLI's dispatch (`next`) is a query,
    # so a run's first recorded event is the produced report. `leased`/`working`
    # remain available for executors that want to record progress.
    PENDING: frozenset({LEASED, WORKING, PRODUCED, ABANDONED}),
    LEASED: frozenset({WORKING, PENDING, ABANDONED}),
    WORKING: frozenset({PRODUCED, PREDICATE_FAILED, PENDING, ABANDONED}),
    PRODUCED: frozenset({COMMITTED, PREDICATE_FAILED}),
    PREDICATE_FAILED: frozenset({PRODUCED, PENDING, ABANDONED}),
    COMMITTED: frozenset(),
    ABANDONED: frozenset(),
}


class TransitionError(ValueError):
    """An illegal state transition. Carries the task address so a log line is enough."""


def check_transition(task: str, current: str | None, target: str) -> None:
    """Raise unless ``current -> target`` is legal. ``current is None`` means unknown.

    An unknown current state is treated as "cannot transition", never as "anything
    goes": if the ledger cannot say where a task is, moving it forward would be
    inventing history.
    """
    if target not in STATES:
        raise TransitionError(
            f"{task}: unknown state {target!r}; legal states are {', '.join(STATES)}"
        )
    if current is None:
        if target != PENDING:
            raise TransitionError(
                f"{task}: cannot enter {target!r} from an unknown state "
                f"(only {PENDING!r} may be established without prior history)"
            )
        return
    if current not in STATES:
        raise TransitionError(
            f"{task}: recorded state {current!r} is not one of {', '.join(STATES)}"
        )
    allowed = ALLOWED[current]
    if target not in allowed:
        raise TransitionError(
            f"{task}: illegal transition {current} -> {target}"
            + (f" (from {current} only {', '.join(sorted(allowed)) or 'nothing'} is allowed)" if allowed else f" ({current} is terminal)")
        )


def is_terminal(state: str) -> bool:
    return state in TERMINAL
