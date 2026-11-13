"""Cards a human can answer in thirty seconds, and proof the defaults are safe.

Two properties matter more than the formatting:

1. **an irreversible action's default is "do not".** If nobody reads the card,
   nothing irreversible happens. This is tested directly, because it is the one
   property whose absence would make the whole mechanism a liability.
2. **rank is computed from stated factors**, so a card cannot claim urgency by
   asserting it. A card missing a factor must not rank as zero.
"""

from __future__ import annotations

import unittest

from fd_agent_run.escalation import (
    BLAST_WEIGHT,
    TRIGGER_BUDGET,
    TRIGGER_IRREVERSIBLE,
    TRIGGER_UNKNOWABLE,
    Card,
    Option,
    build,
    cards_for,
    rank_cards,
)
from fd_agent_run.fact import unknown


def fact(**overrides) -> dict:
    base = {
        "task": "crew/r1/a/1",
        "at": "2026-11-09T12:00:00+0800",
        "owner": "pid:1",
        "upstreams": [],
        "predicate": {"kind": "write", "declared_at": "2026-11-09T11:59:00+0800"},
        "evidence": [{"path": "a.js", "sha256": "a" * 64, "bytes": 1}],
        "verdict": "committed",
        "reason": "",
        "attempt": 1,
    }
    base.update(overrides)
    return base


class TriggerTests(unittest.TestCase):
    def test_ordinary_work_needs_no_human(self) -> None:
        self.assertEqual(cards_for(fact()), [])

    def test_irreversible_predicate_raises_a_card(self) -> None:
        cards = cards_for(fact(predicate={"kind": "deploy", "declared_at": "2026-11-09T11:59:00+0800"}))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].trigger, TRIGGER_IRREVERSIBLE)

    def test_explicit_irreversible_flag_is_honoured(self) -> None:
        cards = cards_for(fact(predicate={
            "kind": "write", "declared_at": "x", "irreversible": True}))
        self.assertEqual(cards[0].trigger, TRIGGER_IRREVERSIBLE)

    def test_task_name_alone_does_not_imply_irreversibility(self) -> None:
        """Guessing from the name would make the ranking unstable.

        The name says "delete"; the predicate says "write". Only the predicate
        counts, so no irreversible card is raised. (This fact is otherwise clean,
        so nothing else fires either.)
        """
        cards = cards_for(fact(task="crew/r1/delete-everything/1"))
        self.assertEqual([c.trigger for c in cards], [])

    def test_budget_exhaustion_raises_a_card(self) -> None:
        cards = cards_for(fact(attempt=3, policy_max_attempts=3))
        self.assertEqual([c.trigger for c in cards], [TRIGGER_BUDGET])

    def test_unknown_predicate_raises_a_card(self) -> None:
        cards = cards_for(fact(predicate=unknown(), verdict="pending"))
        self.assertEqual([c.trigger for c in cards], [TRIGGER_UNKNOWABLE])

    def test_predicate_without_declared_at_raises_a_card(self) -> None:
        cards = cards_for(fact(predicate={"kind": "write"}, verdict="pending"))
        self.assertEqual([c.trigger for c in cards], [TRIGGER_UNKNOWABLE])

    def test_a_finished_task_is_not_asked_about(self) -> None:
        """A committed task with an unknown predicate is history, not a decision."""
        self.assertEqual(cards_for(fact(predicate=unknown(), verdict="committed")), [])


class DefaultSafetyTests(unittest.TestCase):
    def test_irreversible_default_is_do_not(self) -> None:
        """The property that makes unattended operation safe."""
        card = cards_for(fact(predicate={"kind": "publish", "declared_at": "x"}))[0]
        self.assertIn("不执行", card.default)
        self.assertNotIn("批准", card.default)

    def test_irreversible_card_offers_a_dry_run(self) -> None:
        card = cards_for(fact(predicate={"kind": "delete", "declared_at": "x"}))[0]
        self.assertTrue(any("干跑" in o.label for o in card.options))

    def test_every_option_states_a_cost(self) -> None:
        for predicate in ({"kind": "deploy", "declared_at": "x"}, unknown()):
            for card in cards_for(fact(predicate=predicate, verdict="pending")):
                for option in card.options:
                    self.assertTrue(option.cost.strip(), option.label)


class RankTests(unittest.TestCase):
    def test_irreversible_outranks_reversible(self) -> None:
        irreversible = Card("t", "q", TRIGGER_IRREVERSIBLE, (Option("a", "b", "c"),),
                            "不执行", reversibility="irreversible", blast="repo", waiting="blocking")
        reversible = Card("t", "q", TRIGGER_BUDGET, (Option("a", "b", "c"),),
                          "停", reversibility="reversible", blast="repo", waiting="low")
        self.assertGreater(irreversible.rank, reversible.rank)

    def test_missing_factor_does_not_rank_as_zero(self) -> None:
        """An omitted factor must not let a card hide its urgency."""
        vague = Card("t", "q", TRIGGER_BUDGET, (Option("a", "b", "c"),), "停",
                     reversibility="nonsense", blast="nonsense", waiting="nonsense")
        self.assertGreater(vague.rank, 0)
        self.assertEqual(vague.blast, "nonsense")
        self.assertIn("nonsense", ("nonsense",))

    def test_blast_from_upstream_count(self) -> None:
        cards = cards_for(fact(predicate={"kind": "deploy", "declared_at": "x"},
                               upstreams=[f"u{i}" for i in range(6)]))
        self.assertEqual(cards[0].blast, "fleet")

    def test_ordering_is_stable(self) -> None:
        a = Card("b", "q", TRIGGER_BUDGET, (Option("x", "y", "z"),), "停")
        b = Card("a", "q", TRIGGER_BUDGET, (Option("x", "y", "z"),), "停")
        ranked = rank_cards([a, b])
        self.assertEqual([c.task for c in ranked], ["a", "b"], "ties break by task id")


class BuildTests(unittest.TestCase):
    def test_empty_is_a_clear_headline(self) -> None:
        result = build([fact()])
        self.assertEqual(result["cards"], [])
        self.assertIn("没有需要你决定的事", result["headline"])

    def test_headline_counts_irreversible(self) -> None:
        result = build([
            fact(predicate={"kind": "deploy", "declared_at": "x"}),
            fact(task="t2", attempt=3, policy_max_attempts=3),
        ])
        self.assertEqual(len(result["cards"]), 2)
        self.assertIn("不可逆", result["headline"])
        self.assertEqual(result["byTrigger"][TRIGGER_IRREVERSIBLE], 1)

    def test_card_renders_readably(self) -> None:
        card = cards_for(fact(predicate={"kind": "deploy", "declared_at": "x"}))[0]
        text = "\n".join(card.as_lines())
        self.assertIn("是否允许", text)
        self.assertIn("没人回答时", text)
        self.assertIn("代价", text)


class MergeTests(unittest.TestCase):
    """Eight identical questions is one decision, not eight.

    Measured on real data: eight crawler sources produced eight identical cards.
    A queue that repeats itself trains the reader to ignore it, which is worse
    than not asking at all.
    """

    def test_identical_questions_collapse_into_one_card(self) -> None:
        facts = [fact(task=f"crawl/s{i}/latest", predicate=unknown(), verdict="pending")
                 for i in range(8)]
        result = build(facts)
        self.assertEqual(len(result["cards"]), 1)
        card = result["cards"][0]
        self.assertEqual(len(card.members), 8)
        self.assertIn("8 个任务", card.question)
        self.assertIn("8", result["headline"])

    def test_distinct_questions_stay_separate(self) -> None:
        facts = [
            fact(task="a", predicate={"kind": "deploy", "declared_at": "x"}),
            fact(task="b", predicate=unknown(), verdict="pending"),
        ]
        result = build(facts)
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual({c.trigger for c in result["cards"]},
                         {TRIGGER_IRREVERSIBLE, TRIGGER_UNKNOWABLE})

    def test_single_card_is_not_wrapped(self) -> None:
        result = build([fact(predicate=unknown(), verdict="pending")])
        self.assertEqual(result["cards"][0].members, ())

    def test_merged_card_keeps_the_safest_default(self) -> None:
        """Merging must not weaken the default of the highest-ranked member."""
        facts = [fact(task=f"crawl/s{i}/latest", predicate=unknown(), verdict="pending")
                 for i in range(3)]
        card = build(facts)["cards"][0]
        self.assertIn("不派发", card.default)

    def test_merged_blast_widens_with_count(self) -> None:
        facts = [fact(task=f"crawl/s{i}/latest", predicate=unknown(), verdict="pending")
                 for i in range(5)]
        self.assertEqual(build(facts)["cards"][0].blast, "fleet")


if __name__ == "__main__":
    unittest.main()
