"""导出器的两把过滤器谁说了算 —— 钉住「水位优先，日历退位」。

**为什么值得单测**：这台机器上没有 `better-sqlite3`，导出器平时只能在 pod 里跑，
于是它的判据长期只能靠读代码相信。而它决定的是**库里有没有数**，判错一次的形态是
「导出成功、投影 absorbed=0、日志全绿」——没有任何通道会红。

具体那条丢数路径：驱动器每次都传 `--since 昨天`。若某 cell 连挂两天，第一天的
新行在第三天的窗口外 ⇒ **永久跳过**。而 `id > after` 本身就是「哪些行还没吸收」
的精确答案，日历帮不上忙只会遮蔽。

所以判据抽成 `turnFilter()`，并用 `--explain` 出口（在开库之前退出）把它暴露出来，
本机 node 即可验证——不需要 npm 依赖、不需要数据库。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "tools" / "export_platform.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="没有 node，无法验证导出器判据"
)


def explain(*args: str) -> dict:
    proc = subprocess.run(
        ["node", str(EXPORTER), "--cell", "testcell", "--explain", *args],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"explain 失败：{proc.stderr[:300]}"
    return json.loads(proc.stdout)


def test_watermark_wins_over_the_calendar_window():
    """给了 `--after-id` 就只能按水位导，日历窗口不许同时生效。

    两把过滤器用 and 串起来就是那条丢数路径：连挂两天 ⇒ 第一天的行落在窗外。
    这里断言 SQL 里**没有**日期条件——不是断言「日期条件较弱」，是不存在。
    """
    got = explain("--since", "2026-11-13", "--after-id", "7734")
    assert got["turn_filter_by"] == "after-id"
    assert "id > ?" in got["turn_sql"]
    assert "date(" not in got["turn_sql"], (
        "明细查询里仍带日历条件：连挂两天时第一天的新行会被永久跳过"
    )
    assert got["turn_params"] == [7734]


def test_backfill_without_a_watermark_uses_the_window():
    """没有水位（首次/回填）时才由 `--since` 收窄，这是流量控制而非正确性。"""
    got = explain("--since", "2026-10-01")
    assert got["turn_filter_by"] == "since"
    assert "date(started_at / 1000, 'unixepoch') >= ?" in got["turn_sql"]
    assert got["turn_params"] == ["2026-10-01"]


def test_no_filters_means_a_full_export():
    """两者都不给 = 全量导出，且必须标成非 partial。

    `partial` 决定投影端是否跳过「水位 > 台账长度 ⇒ 失效」这条判据；
    全量导出被标成 partial 会让那条判据永远不生效。
    """
    got = explain()
    assert got["turn_filter_by"] == "full"
    assert "where" not in got["turn_sql"]
    assert got["turn_params"] == []
    assert got["partial"] is False


def test_daily_still_narrowed_by_the_window_when_incremental():
    """日汇总是 upsert（按 day 覆盖），日历窗口在那里是安全的省流量，保留。"""
    got = explain("--since", "2026-11-13", "--after-id", "7734")
    assert "member_turn_daily where day >= ?" in got["daily_sql"]
    assert got["daily_params"] == ["2026-11-13"]


def test_explain_never_touches_the_database():
    """`--explain` 必须在开库之前退出。

    否则本机（无 better-sqlite3、无 /data/cells）根本跑不了这条判据，
    「可测」就退化成「读代码相信」。
    """
    got = explain("--after-id", "1")
    assert got["db_path"].startswith("/data/cells/testcell/")
    assert "turn_sql" in got


def test_meta_records_which_filter_won():
    """判据要写进导出产物本身，不能只写在 stderr 上。

    stderr 的提示只在失败时被人看见（本仓的驱动把每个 cell 的 stderr 收进
    `$WORK/$cell.err`，成功就丢掉）。而「这份导出被过滤过、按哪个判据滤的」
    是事后复核必须能读到的事实——所以它是 meta 行的一部分。
    """
    import pathlib
    import re

    src = (ROOT / "tools" / "export_platform.mjs").read_text(encoding="utf-8")
    assert re.search(r"^\s*turn_filter_by: turn\.by,", src, re.M), (
        "meta 行里没有 turn_filter_by：导出被过滤了，产物里却看不出来"
    )
