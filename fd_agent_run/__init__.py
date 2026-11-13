"""Run lifecycle for multi-agent work: a durable task ledger.

The static layer (`fd-agent-graph`) proves a plan is self-consistent. It has never
had any concept of *a run*: no task state, no completion predicate, no persisted
evidence, nowhere to resume from. Everything that went wrong on the real machine —
a role reporting done with zero files, an empty test dir passing, a hung walk, an
upstream 502 costing the whole batch — was that absence showing up in a different
costume.

This package supplies exactly one thing: a ledger of tasks. Not an executor, not a
service, not a queue. The thing that actually runs nodes is a host agent, which is
stateless and restartable, so the ledger plus a "what next" query is the whole
interface it needs.
"""

__all__ = ["journal", "state", "evidence", "predicate"]
