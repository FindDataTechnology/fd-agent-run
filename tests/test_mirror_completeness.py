"""`tools/check_mirror_completeness.py` 的判据自证。

工具本身连 pod 与库，CI 里跑不了；但**判据**（`verdict()`）是纯函数，
它的红/绿必须能在本机无依赖地验证——否则「闸能红」只能靠读代码相信。

2026-11-14 写这个文件时，第一版自证把覆盖值多包了一层
（`{"mirror": {"turns": 480}}` 喂给已经按 `mirror=` 传参的 `_case`），
于是**五条红样本全都返回「一致」**，而自证整体不报错。是那五条红样本自己
把这件事暴露出来的——所以下面每条都同时断言「该红的红了」。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_mirror_completeness as mc  # noqa: E402


def _base() -> tuple[dict, dict]:
    return mc._case()


def test_clean_pair_is_green():
    assert mc.verdict(*_base()) == []


def test_lag_by_pending_turns_is_not_loss():
    """`镜像 + 待吸收 == cell` 是判据；落后多少都可以，只要差额就是待吸收。"""
    cell, mirror = _base()
    cell["turns"] += 7          # cell 新增 7 条
    cell["turns_pending"] = 7   # 镜像还没吸收
    assert mc.verdict(cell, mirror) == []


def test_missing_turns_without_pending_is_red():
    """差额超出待吸收 —— 这才是丢数。"""
    cell, mirror = _base()
    mirror["turns"] -= 20
    problems = mc.verdict(cell, mirror)
    assert problems and "回合对不上" in problems[0]


def test_daily_aggregate_row_count_is_not_compared():
    """日汇总是聚合表：行数不同不算问题（实测 cell 4,664 行 / 镜像 837 行）。"""
    cell, mirror = _base()
    mirror["daily_rows"] = 1
    assert mc.verdict(cell, mirror) == []


def test_daily_total_exceeding_the_cell_is_red():
    """合计多出来一定不对——upsert 只会覆盖成 cell 的值。"""
    cell, mirror = _base()
    mirror["daily_turns"] += 20
    assert any("日汇总合计比 cell 多" in p for p in mc.verdict(cell, mirror))


def test_daily_earliest_day_must_match():
    cell, mirror = _base()
    mirror["day_min"] = "2026-10-05"
    assert any("最早日" in p for p in mc.verdict(cell, mirror))


def test_daily_latest_day_may_lag_only_while_something_is_pending():
    cell, mirror = _base()
    mirror["day_max"] = "2026-11-10"
    assert any("最晚日" in p for p in mc.verdict(cell, mirror))
    cell["turns_pending"] = 3
    cell["turns"] = mirror["turns"] + 3
    assert mc.verdict(cell, mirror) == [], "还有待吸收时，最晚日落后是正常的"


def test_unassigned_bucket_allows_exactly_one_extra_run():
    """`run_id` 为空或不在 fleet_runs 的回合会并进 `__unassigned__`（不编造身份）。"""
    cell, mirror = _base()
    assert mc.verdict(cell, mirror) == []          # 没有孤儿却一样多
    cell["orphan_turns"] = 3
    mirror["runs"] = cell["runs"] + 1
    assert mc.verdict(cell, mirror) == [], "有孤儿回合时镜像多 1 行是设计"
    mirror["runs"] = cell["runs"] + 2
    assert mc.verdict(cell, mirror), "多出超过 1 行就不是合并解释了"


def test_self_test_flag_passes():
    """把工具自带的自证也跑一遍——它覆盖的组合比上面这几条更密。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_mirror_completeness.py"), "--self-test"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "自证" in proc.stdout
