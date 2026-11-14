#!/usr/bin/env python3
"""线上视图是否带上了仓里那几处**关键性质**（不是文本比对）。

**为什么不是文本比对**：第一版拿 `pg_get_viewdef()` 的正文与仓里 `sql/*.sql` 逐字比，
5 个视图全报漂移——可其中 3 个根本没改过。实测 `pg_get_viewdef()` 会做这些事：

  * 去掉不必要的表别名：仓 `g.source` → 线上 `source`
  * 重排括号：仓 `a || '/' || b` → 线上 `(a || '/') || b`
  * 给字面量补类型转换：仓 `'committed'` → 线上 `'committed'::text`、`null::text`
  * 改写 `IN (a,b)` → `= ANY (ARRAY[a,b])`
  * 吃掉 `'x' as y` 里的空格 → `'x'as y`

这些都是**排版**。想靠逐步归一化把它们抹平，等于拿正则去模拟一个 SQL 解析器；
抹得越多，越可能把「真变了」也一起抹掉——一个既会假红又会假绿的闸。

**换判据**：只断言「哪些性质必须在场」。每条都对应一次真实事故，失败时直接说
后果是什么。性质比文本稳：加列、换别名、调排版都不会让它红；而真正丢掉那条语义
（比如归日从 `started_at` 退回 `ended_at`）一定红。

用法：
    python3 tools/check_view_drift.py [--dsn-env FD_AGENT_RUN_DSN] [--json]
退出码：0 = 全部性质在场；1 = 有缺失（逐条点名后果）；2 = 连不上/取不到定义。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

#: (视图, 性质说明, 必须在定义里出现的正则, 缺失的后果)
#: 只登记**有过事故**的性质；不为「好看」加检查。
CHECKS = [
    (
        "agent_run_gap",
        "事件按 started_at 归日（与 cell 的 member_turn_daily 同口径）",
        r"started_at",
        "归日退回 ended_at ⇒ 跨日界的回合被算成缺口，一半假缺陷、真缺陷被淹没"
        "（2026-11-14：34 格缺陷里 23 格是口径差）",
    ),
    (
        "agent_run_fact_platform",
        "skipped 不产生事实行",
        r"distinct\s+from\s+'skipped'|status\s*<>\s*'skipped'",
        "skipped 被当成一次尝试 ⇒ 裁决统计虚高，失败率算错",
    ),
    (
        "agent_run_fact",
        "对不上的裁决记为 $unknown 而不是猜一个",
        r"\$unknown",
        "词表以外的取值被折成某个已知裁决 ⇒ 板面把「不知道」显示成「正常」",
    ),
    (
        "agent_run_gap_summary",
        "缺陷分诊在场（is_defect）",
        r"is_defect",
        "五类归因不再分诊 ⇒ not_enabled 与真缺口混在一起，闸变噪声",
    ),
]

#: 行为判据：(名字, 说明, 返回「违反行数」的 SQL, 违反时的后果)
#: 有些性质正则**表达不了**——它们约束的是视图**算出来的东西**，不是定义文本，
#: 只能真查一遍。每条 SQL 必须返回单个数，0 = 通过。
BEHAVIOUR_CHECKS = [
    (
        "上线日不是自指的",
        "not_enabled 里不许出现 ≥ 上线日（agent_run_ship_day）的行",
        """
        select count(*)
        from agent_runs.agent_run_gap
        where attribution = 'not_enabled'
          and day >= (select ship_day from agent_runs.agent_run_ship_day)
        """,
        "某 cell 自己第一行日汇总被当成上线日 ⇒ 它若从第一天就漏记、第 N 天才恢复，"
        "前面那些天会永久读成 not_enabled：一直漏看起来像还没上线，而闸是绿的"
        "（2026-11-14 实测 cell 6db2b1b4b490223b 的日汇总从 11-04 才有、上线日是 10-31）",
    ),
    (
        "上线日没被空壳 cell 拽跑",
        "上线日必须等于「有 >30 天历史」的 cell 里最早那天，而不是全体最早",
        """
        select count(*)
        from agent_runs.agent_run_ship_day
        where ship_day is distinct from (select min(first_day) from (
                select source, min("day") as first_day
                from agent_runs.agent_turn_daily
                group by 1 having count(*) > 30) s)
        """,
        "取全体最早会被补建的空壳 cell 拽回 10-24，把活跃 cell 功能上线前的 1274 个"
        "回合翻成缺陷 —— 假红比漏报更快摧毁这道闸",
    ),
    (
        "分诊不默认豁免",
        "is_defect 为假的只能有 not_enabled / partial_enable_day 两类",
        """
        select count(*)
        from agent_runs.agent_run_gap_summary
        where not is_defect and attribution not in ('not_enabled', 'partial_enable_day')
        """,
        "新加的分诊类默认落进「不是缺陷」⇒ 真缺陷被静默豁免（late_start 就是这么加的）",
    ),
]


def _live_definitions(dsn: str) -> dict[str, str]:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute("""
            select c.relname, pg_get_viewdef(c.oid, true)
            from pg_class c join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'agent_runs' and c.relkind = 'v'
        """)
        return {name.lower(): (defn or "") for name, defn in cur.fetchall()}


def _behaviour_results(dsn: str) -> list[dict]:
    """真查一遍行为判据。查不动就抛，让上层按「判不出」处理（不是通过）。"""
    import psycopg

    out = []
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        for name, prop, sql, consequence in BEHAVIOUR_CHECKS:
            cur.execute(sql)
            violations = int(cur.fetchone()[0])
            out.append({
                "view": "(行为)",
                "property": f"{name}：{prop}",
                "state": "ok" if violations == 0 else "stale",
                "detail": "" if violations == 0 else f"{violations} 行违反",
                "consequence": consequence,
            })
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn-env", default="FD_AGENT_RUN_DSN")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print(f"FATAL 缺少 {args.dsn_env}（DSN 只从环境变量读）", file=sys.stderr)
        return 2
    try:
        live = _live_definitions(dsn)
    except Exception as exc:            # noqa: BLE001
        print(f"FATAL 读不到视图定义：{exc}", file=sys.stderr)
        return 2

    rows = []
    for view, prop, pattern, consequence in CHECKS:
        defn = live.get(view)
        if defn is None:
            rows.append({"view": view, "property": prop, "state": "missing-view",
                         "detail": "线上没有这个视图", "consequence": consequence})
        elif re.search(pattern, defn, re.IGNORECASE):
            rows.append({"view": view, "property": prop, "state": "ok", "detail": ""})
        else:
            rows.append({"view": view, "property": prop, "state": "stale",
                         "detail": "线上定义里找不到这条性质", "consequence": consequence})

    # 行为判据：查不动 = 判不出（退 2），不能当成通过。正则抓不住自指类的问题，
    # 所以这一类必须真跑。
    try:
        rows.extend(_behaviour_results(dsn))
    except Exception as exc:            # noqa: BLE001
        print(f"FATAL 行为判据查不动：{exc}（判不出 ≠ 通过）", file=sys.stderr)
        return 2

    bad = [r for r in rows if r["state"] != "ok"]
    if args.json:
        print(json.dumps({"checks": rows, "stale": len(bad)}, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            mark = "✓" if r["state"] == "ok" else "✗"
            print(f"  {mark} {r['view']:26s} {r['property']}")
            if r["state"] != "ok":
                print(f"      后果：{r['consequence']}")
                if r["detail"]:
                    print(f"      实测：{r['detail']}")
                print(f"      修法：以有 DDL 权限的角色重建该视图（psql -f sql/00X…），再跑一次本体检")
        if bad:
            print(f"\nFAIL {len(bad)} 条性质不成立 —— **线上跑的是旧视图或旧口径**，"
                  f"板面读数按旧口径算。「仓里改了」不等于「线上变了」。")
        else:
            print("\nOK 全部关键性质都在线上成立（定义性质 + 行为性质）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
