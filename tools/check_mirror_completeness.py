#!/usr/bin/env python3
"""核对「永久镜像」有没有丢数：cell 自报的总数 vs 我们在 `agent_runs` 里的行数。

**为什么需要它**：这条链上每一段都各自有闸，却没有一处能回答「库里到底全不全」。
导出成功、投影 `absorbed=0`、日志全绿——这三种状态在「少导了一段」时**完全一样**。
2026-11-14 就是靠拿 cell 自己的总数比镜像，才看清哪些「差」是丢数、哪些不是。

**量词必须对，否则本工具会自己造假红**（这三条都是当天踩出来的）：

* `agent_turn_daily` 是**聚合表**（按 fleet/member/carrier/provider/model/day 合并，
  cell 侧的 NULL 维度在那边是多行、在这里是一行）⇒ **行数永远不相等**。
  日汇总只比 `sum(turns)` 与 `min/max(day)`，不比行数。
* `run_id` 为空的回合会并进 `__unassigned__`（不编造身份）⇒ 镜像的 run 数**可能多 1**。
* cell 一直在写：比 `watermark` 更新的行本来就没进镜像。所以判据不是
  「cell == 镜像」，而是 **`镜像 + 待吸收 == cell`**——把「落后」与「丢了」分开。
  待吸收 = 该 cell 里 `id > max(seq)` 的回合数（导出端正是按这个水位取的）。

用法::

    python3 tools/check_mirror_completeness.py                # 全部 cell
    python3 tools/check_mirror_completeness.py --cell 1da5    # 子串过滤
    python3 tools/check_mirror_completeness.py --json

退出码：0 = 每个 cell 都能对上；1 = 有 cell 对不上（镜像缺行／多行）；2 = 判不出
（pod 取不到、某 cell 读不出来）。**「什么都没查」不许读成「一个缺口都没有」。**
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

CTX_DEFAULT = "cheap-ts"
NS_DEFAULT = "fd-prod"
POD_LABEL = "app.kubernetes.io/name=platform"
#: 心跳行的保留 run_id（不是一次 run）。统计 run 数时必须排除它。
HEARTBEAT_RUN_ID = "__cell__"

CELL_STATS_JS = r"""
const Database=require("/app/node_modules/better-sqlite3");
const db=new Database("/data/cells/"+process.argv[1]+"/data/data/fleets.db",{readonly:true});
const after=Number(process.argv[2]||0);
const one=(s)=>db.prepare(s).get();
console.log(JSON.stringify({
  runs: one("select count(*) n from fleet_runs").n,
  runs_null_id: db.prepare("select count(*) n from fleet_runs where run_id is null").get().n,
  orphan_turns: db.prepare("select count(*) n from member_turn_events t "
    + "where t.run_id is null or not exists "
    + "(select 1 from fleet_runs r where r.run_id = t.run_id)").get().n,
  turns: one("select count(*) n from member_turn_events").n,
  turns_pending: db.prepare("select count(*) n from member_turn_events where id > ?").get(after).n,
  daily_rows: one("select count(*) n from member_turn_daily").n,
  daily_turns: one("select coalesce(sum(turns),0) n from member_turn_daily").n,
  day_min: one("select min(day) d from member_turn_daily").d,
  day_max: one("select max(day) d from member_turn_daily").d,
}));
"""


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


class CannotDetermine(Exception):
    pass


def resolve_pod(ctx: str, ns: str) -> str:
    out = sh(["kubectl", "--context", ctx, "-n", ns, "get", "pod", "-l", POD_LABEL,
              "--field-selector=status.phase=Running",
              "-o", "jsonpath={.items[0].metadata.name}"])
    name = (out.stdout or "").strip()
    if out.returncode != 0 or not name:
        raise CannotDetermine(f"取不到 Running 的 platform pod：{(out.stderr or '').strip()[:160]}")
    return name


def list_cells(ctx: str, ns: str, pod: str) -> list[str]:
    out = sh(["kubectl", "--context", ctx, "-n", ns, "exec", pod, "-c", "platform", "--",
              "sh", "-lc", "ls -d /data/cells/*/data/data/fleets.db 2>/dev/null | cut -d/ -f4"])
    if out.returncode != 0:
        raise CannotDetermine(f"列不出 cell：{(out.stderr or '').strip()[:160]}")
    cells = (out.stdout or "").split()
    if not cells:
        raise CannotDetermine("一个 cell 都没有——判不出，不是「全都齐」")
    return cells


def watermark(dsn: str, cell: str) -> int:
    import psycopg

    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "select coalesce(max(seq),0) from agent_runs.agent_run_event "
            "where run_key like %s", (f"platform|cell:{cell}|%",),
        )
        return int(cur.fetchone()[0])


def mirror_stats(dsn: str, cell: str) -> dict:
    import psycopg

    source = f"cell:{cell}"
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from agent_runs.agent_run "
            "where source=%s and run_id <> %s", (source, HEARTBEAT_RUN_ID))
        runs = cur.fetchone()[0]
        cur.execute(
            "select count(*) from agent_runs.agent_run_event e "
            "join agent_runs.agent_run r using (run_key) where r.source=%s", (source,))
        turns = cur.fetchone()[0]
        cur.execute(
            "select count(*), coalesce(sum(turns),0), min(day)::text, max(day)::text "
            "from agent_runs.agent_turn_daily where source=%s", (source,))
        daily_rows, daily_turns, day_min, day_max = cur.fetchone()
    return {"runs": runs, "turns": turns, "daily_rows": daily_rows,
            "daily_turns": daily_turns, "day_min": day_min, "day_max": day_max}


def cell_stats(ctx: str, ns: str, pod: str, cell: str, after: int) -> dict:
    out = sh(["kubectl", "--context", ctx, "-n", ns, "exec", pod, "-c", "platform", "--",
              "node", "-e", CELL_STATS_JS, cell, str(after)])
    if out.returncode != 0:
        raise CannotDetermine(f"{cell} 读不出统计：{(out.stderr or '').strip()[:160]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise CannotDetermine(f"{cell} 的统计不是 JSON：{exc}") from exc


def verdict(cell: dict, mirror: dict) -> list[str]:
    """返回不合格项（空 = 对得上）。判据只写在这里，打印不重算。

    三条量词，每条都对应一次实测误判（2026-11-14）：

    ① **回合**：`镜像 + 待吸收 == cell`。「落后」与「丢了」必须分开——比水位更新的
       行本来就没进镜像，把它算成缺就是每天假红。
    ② **日汇总**：聚合表，行数不比；`sum(turns)` 允许落后「待吸收」那么多（那些回合
       还没进日汇总），但**多出来一定不对**。
    ③ **run 数**：`run_id` 为空或不在 `fleet_runs` 里的回合会并进 `__unassigned__`
       （不编造身份）⇒ 镜像**可能多 1 行**，且只在存在这种孤儿回合时。没有孤儿却多，
       说明有一行不是从台账来的。
    日期覆盖：最早日必须相同；最晚日允许落后，但只有在还有待吸收回合时。
    """
    problems: list[str] = []
    pending = cell["turns_pending"]
    orphans = cell["orphan_turns"]

    if mirror["turns"] + pending != cell["turns"]:
        problems.append(f"回合对不上：cell={cell['turns']}，"
                        f"镜像={mirror['turns']} + 待吸收={pending} "
                        f"= {mirror['turns'] + pending}（差 {cell['turns'] - mirror['turns'] - pending}）")

    if mirror["daily_turns"] > cell["daily_turns"]:
        problems.append(f"日汇总合计比 cell 多：cell={cell['daily_turns']}，"
                        f"镜像={mirror['daily_turns']}")
    elif mirror["daily_turns"] < cell["daily_turns"] - pending:
        problems.append(f"日汇总合计对不上：cell={cell['daily_turns']}，"
                        f"镜像={mirror['daily_turns']}，待吸收={pending}"
                        f"（差 {cell['daily_turns'] - mirror['daily_turns'] - pending} 超出来不及）")

    if mirror["day_min"] != cell["day_min"]:
        problems.append(f"日汇总最早日对不上：cell={cell['day_min']}，镜像={mirror['day_min']}")
    if (mirror["day_max"] or "") < (cell["day_max"] or "") and pending == 0:
        problems.append(f"日汇总最晚日落后但没有待吸收：cell={cell['day_max']}，"
                        f"镜像={mirror['day_max']}")

    allowed_extra = 1 if orphans else 0
    if mirror["runs"] > cell["runs"] + allowed_extra:
        problems.append(f"run 数比 cell 多：cell={cell['runs']}，镜像={mirror['runs']}"
                        f"（孤儿回合 {orphans} ⇒ 最多允许 +{allowed_extra}）")
    return problems


def _case(**kw):
    base_cell = {"runs": 100, "runs_null_id": 0, "orphan_turns": 0, "turns": 500,
                 "turns_pending": 0, "daily_rows": 80, "daily_turns": 500,
                 "day_min": "2026-10-01", "day_max": "2026-11-14"}
    base_mirror = {"runs": 100, "turns": 500, "daily_rows": 40, "daily_turns": 500,
                   "day_min": "2026-10-01", "day_max": "2026-11-14"}
    base_cell.update(kw.get("cell", {}))
    base_mirror.update(kw.get("mirror", {}))
    return base_cell, base_mirror


def self_test() -> int:
    """五红三绿。绿路必须存在——「全都判红」与「判据写反」长得一模一样。"""
    # 每个 case 给的是**内层覆盖**（直接喂 `_case(cell=…, mirror=…)`）。
    # 第一版多包了一层 `{"mirror": {...}}`，于是覆盖值落到了无关键上、五条红样本
    # 全都「一致」——自证本身没有报错，是红样本救的。写红样本就是在验自证自己。
    cases = [
        ("绿·完全一致（行数不同但合计相同）", {}, {}, False),
        ("绿·落后=待吸收（不是丢数）", {"turns": 507, "turns_pending": 7}, {}, False),
        ("绿·有孤儿回合 ⇒ 镜像多 1 行 run", {"orphan_turns": 3}, {"runs": 101}, False),
        ("红·回合少了且无待吸收解释", {}, {"turns": 480}, True),
        ("红·日汇总合计多出来", {}, {"daily_turns": 520}, True),
        ("红·日汇总最早日晚了", {}, {"day_min": "2026-10-05"}, True),
        ("红·没有孤儿却多出 run", {}, {"runs": 102}, True),
        ("红·最晚日落后且没有待吸收", {}, {"day_max": "2026-11-10"}, True),
    ]
    bad = 0
    for name, cell_kw, mirror_kw, want_red in cases:
        cell, mirror = _case(cell=cell_kw, mirror=mirror_kw)
        problems = verdict(cell, mirror)
        got_red = bool(problems)
        ok = got_red == want_red
        bad += not ok
        print(f"{'✓' if ok else '✗'} {name}"
              + (f"  → {problems}" if problems and not want_red else "")
              + ("" if ok else f"  ← 期望{'红' if want_red else '绿'}，实际{'红' if got_red else '绿'}"))
    print(f"---- 自证 {len(cases) - bad}/{len(cases)}")
    return 1 if bad else 0

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对永久镜像与各 cell 台账是否一致")
    ap.add_argument("--context", default=CTX_DEFAULT)
    ap.add_argument("--namespace", default=NS_DEFAULT)
    ap.add_argument("--cell", default=None, help="按子串过滤 cell")
    ap.add_argument("--dsn-env", default="FD_AGENT_RUN_DSN")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="不连任何东西，直接跑判据的红/绿自证")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"判不出：没有 {args.dsn_env}（只读角色就够，本工具不写任何东西）",
              file=sys.stderr)
        return 2
    try:
        pod = resolve_pod(args.context, args.namespace)
        cells = list_cells(args.context, args.namespace, pod)
        if args.cell:
            cells = [c for c in cells if args.cell in c]
            if not cells:
                raise CannotDetermine(f"没有 cell 匹配 {args.cell!r}")

        rows, bad = [], 0
        for cell in cells:
            after = watermark(dsn, cell)
            cell_s = cell_stats(args.context, args.namespace, pod, cell, after)
            mir = mirror_stats(dsn, cell)
            problems = verdict(cell_s, mir)
            bad += bool(problems)
            rows.append({"cell": cell, "after": after, "cell": cell_s, "mirror": mir,
                         "problems": problems})
            if not args.json:
                state = "一致" if not problems else "⚠ " + "；".join(problems)
                print(f"{cell}  cell: runs={cell_s['runs']} turns={cell_s['turns']} "
                      f"daily={cell_s['daily_turns']}({cell_s['daily_rows']} 行)  "
                      f"镜像: runs={mir['runs']} turns={mir['turns']} "
                      f"daily={mir['daily_turns']}({mir['daily_rows']} 行)  待吸收={cell_s['turns_pending']}"
                      f"  {state}")
        if args.json:
            print(json.dumps({"pod": pod, "cells": rows}, ensure_ascii=False, indent=1))
        print(f"---- {len(rows)} 个 cell，{bad} 个对不上", file=sys.stderr)
        return 1 if bad else 0
    except CannotDetermine as exc:
        print(f"判不出：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


# ── 自证：每条判据都要能红 ───────────────────────────────────────────────────
