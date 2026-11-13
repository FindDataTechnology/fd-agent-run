"""Turn "this needs a human" into a decision a human can actually make.

The problem this solves is not a shortage of asking. Today, over one working
session, I asked the operator six questions and got "继续" five times. That is not
disengagement — it is bad question design:

    "which 45 ports should be allowed?"        needs context they do not have
    "should we schedule this?"                 reversible and trivial; not theirs to spend
    "hard or soft predicate?"                  asked in my vocabulary, not in costs
    "experiment first or archive?"             both fine; what was missing was my ranking

A question is only fair if the person can answer it in thirty seconds from the card
alone. So a card carries: the one-line question, two or three options with their
costs, the default if nobody answers, and whether it is reversible.

Two rules that make the default trustworthy:

1. **the default for an irreversible action is "do not".** If nobody reads the card,
   nothing irreversible happens. A default that spends money or publishes something
   is not a default, it is an unread approval.
2. **ranking is computed, not declared.** `不可逆 × 影响面 × 等的代价` — three
   factors, each with a stated value, so a card cannot quietly claim urgency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fact import UNKNOWN_MARKER, is_unknown

#: The three triggers that must reach a human. Everything else gets a safe default
#: and is recorded instead of asked — the whole point of the ranking below.
TRIGGER_IRREVERSIBLE = "irreversible"
TRIGGER_BUDGET = "budget-exhausted"
TRIGGER_UNKNOWABLE = "predicate-unknowable"

#: Weight for "can it be undone". An irreversible action outranks any pile of
#: reversible ones, because a reversible mistake can be re-run and this cannot.
REVERSIBILITY_WEIGHT = {"reversible": 1, "partly": 4, "irreversible": 12}

#: Weight for "how much does it touch". Kept coarse on purpose: a precise blast
#: radius is unavailable more often than not, and a fabricated one is worse than
#: a coarse honest one.
BLAST_WEIGHT = {"local": 1, "module": 2, "repo": 4, "fleet": 8, "unknown": 3}

#: Weight for "what does waiting cost". `blocking` means the rest of the work
#: cannot proceed; `none` means the work simply stops here, which is fine.
WAITING_WEIGHT = {"none": 1, "low": 2, "high": 4, "blocking": 6}


def _weight(table: dict[str, int], value: Any) -> int:
    if isinstance(value, str) and value in table:
        return table[value]
    # An unstated factor is not zero: treating it as zero would let a card hide
    # urgency by omitting a field.
    return table["unknown"] if "unknown" in table else 2


@dataclass(frozen=True)
class Option:
    label: str
    effect: str
    cost: str
    reversible: str = "reversible"

    def as_line(self) -> str:
        return f"{self.label}: {self.effect} (代价: {self.cost}; {self.reversible})"


@dataclass(frozen=True)
class Card:
    task: str
    question: str
    trigger: str
    options: tuple[Option, ...]
    default: str
    reversibility: str = "irreversible"
    blast: str = "unknown"
    waiting: str = "none"
    why_now: str = ""
    #: Tasks a merged card speaks for. One entry means the card is about that task.
    members: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return (
            _weight(REVERSIBILITY_WEIGHT, self.reversibility)
            * _weight(BLAST_WEIGHT, self.blast)
            * _weight(WAITING_WEIGHT, self.waiting)
        )

    def as_lines(self) -> list[str]:
        lines = [f"[{self.rank}] {self.question}"]
        if self.why_now:
            lines.append(f"    为什么现在: {self.why_now}")
        lines.append(f"    任务: {self.task}  ({self.trigger})")
        if self.members:
            shown = ", ".join(self.members[:6])
            more = f" … 另 {len(self.members) - 6} 个" if len(self.members) > 6 else ""
            lines.append(f"    涉及: {shown}{more}")
        for option in self.options:
            lines.append("    - " + option.as_line())
        lines.append(f"    没人回答时: {self.default}")
        return lines


def _evidence_irreversible(fact: dict) -> bool:
    """An irreversible effect is one that cannot be re-run to fix.

    Only the fact's own declaration counts: `predicate.kind` of `publish`, `delete`
    or `deploy`, or an explicit `irreversible: true`. Guessing from the task name
    would make the ranking unstable.
    """
    predicate = fact.get("predicate")
    if is_unknown(predicate) or not isinstance(predicate, dict):
        return False
    if predicate.get("irreversible") is True:
        return True
    kind = predicate.get("kind")
    return isinstance(kind, str) and kind.lower() in {"publish", "delete", "deploy", "deploy!", "migrate"}


def _blast_of(fact: dict) -> str:
    declared = fact.get("blast")
    if isinstance(declared, str) and declared in BLAST_WEIGHT:
        return declared
    upstreams = fact.get("upstreams")
    if isinstance(upstreams, list) and len(upstreams) > 4:
        return "fleet"
    return "unknown"


def cards_for(fact: dict) -> list[Card]:
    """Cards a human must answer for this fact. Empty means "no human needed"."""
    task = str(fact.get("task") or "<no task>")
    verdict = fact.get("verdict")
    predicate = fact.get("predicate")
    cards: list[Card] = []

    if _evidence_irreversible(fact):
        cards.append(Card(
            task=task,
            question=f"是否允许对 {task} 执行不可逆动作？",
            trigger=TRIGGER_IRREVERSIBLE,
            why_now="该任务的判据声明了不可逆副作用（发布/删除/部署/迁移）",
            options=(
                Option("批准", "按声明执行", "不可撤销", "irreversible"),
                Option("先做一次干跑", "在同一输入上跑但不落副作用", "多一轮时间", "reversible"),
                Option("否决", "放弃该任务", "该任务标记 cancelled", "reversible"),
            ),
            # The default for irreversible work is always "do not".
            default="不执行（任务停在 pending，记录的 reason 是 awaiting-approval）",
            reversibility="irreversible",
            blast=_blast_of(fact),
            waiting="blocking",
        ))

    attempts = fact.get("attempt")
    if isinstance(attempts, int) and fact.get("policy_max_attempts") is not None:
        if attempts >= int(fact["policy_max_attempts"]):
            cards.append(Card(
                task=task,
                question=f"{task} 已用尽重试预算（{attempts} 次），是停还是加预算？",
                trigger=TRIGGER_BUDGET,
                why_now="尝试次数达到声明上限",
                options=(
                    Option("停", "标记 abandoned 并记录原因", "该路作废", "reversible"),
                    Option("加预算", "上限乘 2 再试", "多一轮时间与 token", "reversible"),
                ),
                default="停（标记 abandoned）",
                reversibility="reversible",
                blast=_blast_of(fact),
                waiting="low",
            ))

    # A MISSING `declared_at` is as unknowable as an explicit unknown marker — the
    # condition has no provenance either way. Checking only for the marker let the
    # commonest case (an older journal that simply lacks the key) slip through
    # silently, which is exactly the omission this card exists to surface.
    unknown_predicate = is_unknown(predicate) or (
        isinstance(predicate, dict)
        and (is_unknown(predicate.get("declared_at")) or "declared_at" not in predicate)
    )
    if unknown_predicate and verdict == "pending":
        cards.append(Card(
            task=task,
            question=f"{task} 没有可判定的完成条件，要不要先补一个？",
            trigger=TRIGGER_UNKNOWABLE,
            why_now="判据缺失或没有声明时刻，无法判断是否完成",
            options=(
                Option("补判据", "为该任务声明一个可求值条件", "一次编辑", "reversible"),
                Option("降级为 explore", "只要求有证据，不要求结论", "结论变弱", "reversible"),
                Option("取消该任务", "从本轮移除", "少一路产出", "reversible"),
            ),
            default="不派发（任务停在 pending，原因记录为 no-predicate）",
            reversibility="reversible",
            blast=_blast_of(fact),
            waiting="blocking",
        ))
    return cards


def rank_cards(cards: list[Card]) -> list[Card]:
    """Highest first. Ties broken by task id so the order is reproducible."""
    return sorted(cards, key=lambda c: (-c.rank, c.task))


def merge(cards: list[Card]) -> list[Card]:
    """Collapse cards that ask the SAME question about many tasks into one.

    Measured on real data: eight crawler sources produced eight identical
    "no decidable completion condition" cards. That is not eight decisions, it is
    one decision repeated — and eight repeats is how a human learns to ignore the
    queue. The merged card keeps the highest rank of its group and lists the tasks,
    so answering once settles all of them.
    """
    groups: dict[tuple[str, str], list[Card]] = {}
    for card in cards:
        groups.setdefault((card.trigger, card.question.replace(card.task, "<task>")), []).append(card)

    merged: list[Card] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        head = max(group, key=lambda c: c.rank)
        tasks = [c.task for c in group]
        merged.append(Card(
            task=f"{len(tasks)} 个任务",
            question=f"{len(tasks)} 个任务都没有可判定的完成条件，要不要一起补？",
            trigger=head.trigger,
            why_now=head.why_now,
            options=head.options,
            default=head.default,
            reversibility=head.reversibility,
            blast=head.blast if len(tasks) < 4 else "fleet",
            waiting=head.waiting,
            members=tuple(tasks),
        ))
    return merged


def build(facts: list[dict]) -> dict:
    """All cards for a set of facts, merged and ranked, with a headline."""
    cards: list[Card] = []
    for fact in facts:
        cards.extend(cards_for(fact))
    ranked = rank_cards(merge(cards))
    by_trigger: dict[str, int] = {}
    for card in ranked:
        by_trigger[card.trigger] = by_trigger.get(card.trigger, 0) + 1
    return {
        "cards": ranked,
        "byTrigger": by_trigger,
        "headline": (
            "没有需要你决定的事"
            if not ranked
            else (
                f"{len(ranked)} 件需要你决定（涉及 {len(facts)} 个任务），"
                f"其中 {by_trigger.get(TRIGGER_IRREVERSIBLE, 0)} 件不可逆"
            )
        ),
    }
