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


def test_backfill_without_a_watermark_is_full_not_windowed():
    """没有水位（首次/回填）就全量导——日历窗口不是正确性边界。

    这里曾经允许按 `--since` 收窄，代价是：连着两天没导成功的 cell，第一天的行
    落在窗外、永久不导出。日汇总更直接：日历窗口让**永久镜像缺历史**，
    2026-11-14 实测 `cell:1da5` cell 侧 4,659 行、镜像只有 837 行。
    """
    got = explain("--since", "2026-10-01")
    assert got["turn_filter_by"] == "full"
    assert "where" not in got["turn_sql"]
    assert got["turn_params"] == []


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


def test_daily_is_always_full():
    """日汇总永远全量：upsert 保证幂等，但**不保证完整**。

    按 `--since` 收窄时，从没导过的历史 day 永远不出现，永久镜像与 cell 总数对不上；
    而归档缺口分析正是拿这张表比事件表，缺的行会被读成「cell 侧没有日汇总」——
    一个由导出窗口造出来的假缺陷。表很小（最大 cell 4.6k 行），全量导的代价可忽略。
    """
    got = explain("--since", "2026-11-13", "--after-id", "7734")
    assert "where" not in got["daily_sql"], "日汇总又被窗口收窄了，镜像会对不上 cell 总数"
    assert got["daily_params"] == []
    assert got["daily_filter_by"] == "full"


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


# ── 刷新心跳：把「读通了」与「有数据」分成两件事 ──────────────────────────


class _FakeCur:
    """只记录 SQL 与参数：心跳是**决策**，判据落在「写了什么」上。"""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))


def test_heartbeat_marks_idle_when_nothing_new():
    """零新增也必须留痕，且状态是 `idle`——否则一个安静的 cell 会被判成坏掉。"""
    from fd_agent_run import projector

    cur = _FakeCur()
    projector._heartbeat(cur, "cell:abc", absorbed=0)
    sql, params = cur.calls[0]
    assert "agent_run_projection" in sql
    assert projector.HEARTBEAT_RUN_ID == "__cell__"
    assert params[0] == "cell:abc"
    assert params[1] == "__cell__"
    assert params[3] == "idle", "absorbed=0 时状态必须是 idle，不能冒充 ok"
    assert "absorbed_at = now()" in sql, "冲突时要刷新时刻，否则心跳不会前进"


def test_heartbeat_marks_ok_when_rows_arrived():
    from fd_agent_run import projector

    cur = _FakeCur()
    projector._heartbeat(cur, "cell:abc", absorbed=7)
    _sql, params = cur.calls[0]
    assert params[2] == 7
    assert params[3] == "ok"


def test_heartbeat_id_is_not_a_run_id_shape():
    """保留名必须不可能与真实 run_id 撞上。"""
    from fd_agent_run import projector

    assert projector.HEARTBEAT_RUN_ID.startswith("__")
    assert projector.HEARTBEAT_RUN_ID.endswith("__")


def test_project_platform_export_actually_calls_the_heartbeat():
    """光有 `_heartbeat()` 不够——必须证明**每次真写都调用它**。

    2026-11-14 实测过这个缺口：把调用那行删掉，全部测试仍然绿，因为其他用例只测
    函数自身的行为。而少写一条心跳的后果是「安静的 cell 2 个周期后无故转红」，
    正好是这条判据要消灭的东西。用 AST 查调用点，比 grep 文本稳（改格式不会假红）。
    """
    import ast

    tree = ast.parse((ROOT / "fd_agent_run" / "projector.py").read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "project_platform_export"),
              None)
    assert fn is not None, "projector.py 里没有 project_platform_export"
    called = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_heartbeat"]
    assert called, "project_platform_export 没有调用 _heartbeat：安静承载者会无故转红"


# ── 承载者形状契约：`agent_source` 只收两种 source ─────────────────────────────
# 2026-11-13 生产库里出现了一条 ('p','p','p') 的登记行，任何代码路径都产生不出它
# （`cell:<16-hex>` / `project:<名>@<主机>` 之外无他）——手工插入的，而此前没有
# 任何东西会拒绝。它在覆盖面板上变成一个永远没有数据的幽灵租户，读者分不出它是
# 「空租户」还是「打错字」。


def test_validate_source_accepts_the_two_contract_shapes():
    from fd_agent_run.projector import validate_source
    assert validate_source("platform", "cell:1da519d4c1c0bf89") == (
        "platform", "cell:1da519d4c1c0bf89")
    assert validate_source("crew", "project:fd-fleet@Bruces-Mac-62.local") == (
        "crew", "project:fd-fleet@Bruces-Mac-62.local")


@pytest.mark.parametrize("driver,source", [
    ("p", "p"),                                  # 生产里那条真实垃圾行
    ("platform", "1da519d4c1c0bf89"),            # 把租户 id 当成了一个来源
    ("platform", "cell:zzzz"),                   # 非 hex
    ("crew", "cell:1da519d4c1c0bf89"),           # driver 与 source 前缀不匹配
    ("platform", "project:fd-fleet@host"),       # 同上
])
def test_validate_source_refuses_anything_else(driver, source):
    from fd_agent_run.projector import validate_source
    with pytest.raises(ValueError):
        validate_source(driver, source)


def test_register_source_refuses_before_touching_the_database():
    """校验必须在 SQL **之前**：先写进去再校验，库里就已经脏了。"""
    from fd_agent_run import projector

    class _Conn:
        def cursor(self):  # pragma: no cover - 不该被调用
            raise AssertionError("形状不合法时不许发出 SQL")

    with pytest.raises(ValueError):
        projector.register_source(_Conn(), driver="p", source="p")
