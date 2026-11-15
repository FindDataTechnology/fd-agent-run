"""保留策略执行器：钉住「拒绝」与「删了 0 行」不能混成一个 🆗。

**为什么要这一组**：这条链此前的形态是「策略声明在库里、全仓没有 deleter，而任何
输出里都不存在 red」——删 0 行与「本地确实没有超期行」逐字相同。所以这里比的不是
删除本身（那需要真库），而是**三条会让 0 变成谎话的路径**：
有期限的表没登记 pruner、可删 > 0 却一行没删、时间列为 NULL。

真库侧另有两条自证跑在生产线数据上（`--what-if-period-days 0` 与
`--what-if-keep 100` 能把可删数从 0 抬到 8,975 / 9,850，并与独立 SQL 逐值吻合）；
这里只用假连接测编排，所以**SQL 的正确性不由本文件证明**——两者分工写在这里，
免得下次有人拿一组绿的假连接测试去推断「判据在生产上也对」。
"""
from __future__ import annotations

import pytest

from fd_agent_run import retention as ret


class FakeCursor:
    def __init__(self, script):
        self._script = script
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._rows = self._script(sql, params)
        if "delete from" in sql.lower():
            # 删除语句的行数由脚本给出（脚本返回 [(n,)]），这里是唯一读它的地方
            self.rowcount = self._rows[0][0] if self._rows else 0

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, script):
        self.script = script

    def cursor(self):
        return FakeCursor(self.script)


# --------------------------------------------------------------- 发现与覆盖


def _schema_conn(tables_with_classes: dict[str, list[str]],
                 policy: dict[str, dict] | None = None):
    """按「表 → 它实际用到的分级」回答，模拟一次真实 schema。"""
    policy = policy or {
        "permanent": {"period_days": None, "min_keep_per_holder": 0, "note": ""},
        "windowed": {"period_days": 90, "min_keep_per_holder": 500, "note": ""},
    }

    def script(sql: str, params):
        s = sql.lower()
        if "information_schema" in s:
            return [(t.split(".")[1],) for t in tables_with_classes]
        if "from agent_runs.retention_policy" in s:
            return [(k, v["period_days"], v["min_keep_per_holder"], "") for k, v in policy.items()]
        for table, classes in tables_with_classes.items():
            if f"from {table}" in s and "distinct retention_class" in s:
                return [(c,) for c in classes]
        raise AssertionError(f"未知 SQL：{sql[:120]}")

    return FakeConn(script)


def test_windowed_table_without_a_pruner_is_refused_not_silently_ignored():
    """新表带 windowed 行、没人登记 pruner ⇒ 必须点名，并且拒绝运行。

    这条是整份实现的要点：那种表在删除侧与 permanent 表长得一模一样（都没东西可删），
    所以「发现集里有、登记表里没有」是唯一的时机。
    """
    conn = _schema_conn({
        "agent_runs.agent_run_event": ["windowed"],      # 已登记
        "agent_runs.agent_sidecar": ["windowed"],        # 未登记
        "agent_runs.agent_run": ["permanent"],           # 永久的表不该出现
    })
    blocked = ret.assert_coverage(conn)
    assert [b["table"] for b in blocked] == ["agent_runs.agent_sidecar"]
    assert "GRANT" not in blocked[0]["fix"]  # 这条的修法是登记 pruner，不是授权


def test_permanent_only_tables_are_silent():
    """只有永久行的表不该要求登记——否则每张永久表都要写一遍，闸就变成噪音。"""
    conn = _schema_conn({
        "agent_runs.agent_run_event": ["windowed"],
        "agent_runs.agent_run": ["permanent"],
        "agent_runs.agent_turn_daily": ["permanent"],
    })
    assert ret.assert_coverage(conn) == []


def test_policy_table_does_not_flag_its_own_rulebook():
    """`retention_policy` 自己有 retention_class 列（主键）⇒ 不得被判成缺口。

    2026-11-15 首跑真实发生：那条假红出现在输出最上面，并且把「缺 DELETE 授权」
    那条真红挤到了后面。发现式判据必须先把判据自己排除掉。
    """
    conn = _schema_conn({
        "agent_runs.retention_policy": ["permanent", "windowed"],
        "agent_runs.agent_run_event": ["windowed"],
    })
    assert ret.assert_coverage(conn) == []


# ------------------------------------------------------------- 执行的三条路


def _plan(*, eligible: int, table: str = "agent_runs.agent_run_event",
          cls: str = "windowed", keep: int = 500) -> ret.Plan:
    return ret.Plan(tables=[ret.TablePlan(
        table=table, retention_class=cls, period_days=90, min_keep=keep,
        rows=eligible + 10, eligible=eligible)])


def test_blocked_tables_refuse_before_any_delete():
    p = _plan(eligible=5)
    p.blocked = [{"table": "agent_runs.agent_sidecar", "classes": ["windowed"],
                  "why": "x", "fix": "y"}]

    def script(sql, params):  # 任何删除都必须先撞上拒绝
        raise AssertionError("有 blocked 表时不应执行任何删除")

    with pytest.raises(ret.RetentionRefused) as exc:
        ret.apply_plan(FakeConn(script), p)
    assert "pruner" in str(exc.value)


def test_eligible_but_deleted_zero_is_an_error_not_a_quiet_success():
    """可删 > 0 却删了 0 行 —— 这是「判据与执行脱节」的唯一指纹。

    权限不足、WHERE 恒假、事务被回滚三种成因在这里同形，而它们写出来的日志与
    「确实没有超期行」一字不差，所以必须抛错，不能返回 0。
    """
    p = _plan(eligible=3)
    conn = FakeConn(lambda sql, params: [(0,)] if "delete from" in sql.lower() else [])
    with pytest.raises(ret.RetentionError) as exc:
        ret.apply_plan(conn, p)
    assert "一行都没删掉" in str(exc.value)


def test_deletes_in_batches_and_reports_backlog_when_capped():
    """多批删除要停在上限上，并且把剩余量报出来（不是静默截断）。"""
    calls = {"n": 0}

    def script(sql, params):
        if "delete from" in sql.lower():
            calls["n"] += 1
            return [(params["limit"],)]
        return []

    p = _plan(eligible=7)
    result = ret.apply_plan(FakeConn(script), p, batch=2, max_rows=4)
    assert result["deleted"] == 4
    assert result["tables"][0]["backlog"] == 3
    assert result["capped"] is True
    assert calls["n"] == 2  # 2 + 2，第三批不该发出去


def test_nothing_eligible_is_a_plain_success():
    p = _plan(eligible=0)
    conn = FakeConn(lambda sql, params: [(0,)] if "delete from" in sql.lower() else [])
    result = ret.apply_plan(conn, p)
    assert result["deleted"] == 0
    assert result["capped"] is False


# ------------------------------------------------------------------- 渲染


def test_render_names_the_null_time_rows():
    """`at` 为 NULL 的行永远不够格被删（`NULL < now() - interval` 是 NULL）。

    不单列计数，它们就会永远静默驻留，而每次运行都报「可删 0」。
    """
    p = ret.Plan(tables=[ret.TablePlan(
        table="agent_runs.agent_run_event", retention_class="windowed",
        period_days=90, min_keep=500, rows=3, null_time=2, eligible=0)])
    text = "\n".join(ret.render(p))
    assert "NULL 的 2 行" in text


def test_render_prints_the_exact_grant_when_delete_is_denied():
    p = ret.Plan(tables=[])
    text = "\n".join(ret.render(p, privileges=[{
        "table": "agent_runs.agent_run_event", "can_delete": False,
        "role": "fd_agent_run_projector",
        "fix": "GRANT DELETE ON agent_runs.agent_run_event TO fd_agent_run_projector;",
    }]))
    assert "没有 DELETE 权限" in text
    assert "GRANT DELETE" in text


# ------------------------------------------------------------------ 心跳


def test_heartbeat_is_written_atomically_and_read_back(tmp_path):
    path = tmp_path / "retention.json"
    assert ret.read_heartbeat(path) is None
    ret.write_heartbeat(path, {"deleted": 4, "eligible": 4})
    assert ret.read_heartbeat(path) == {"deleted": 4, "eligible": 4}
    assert not path.with_suffix(".json.tmp").exists()


def test_broken_heartbeat_reads_as_absent_not_as_a_crash(tmp_path):
    """心跳文件半截/非法 ⇒ 当「没有」处理。

    读侧在这里抛异常会让整块看板变红，而真正的事实只是「这一轮没写成功」——
    后者该由年龄判据说，不该由一个 JSON 解析错误说。
    """
    path = tmp_path / "retention.json"
    path.write_text("{not json", encoding="utf-8")
    assert ret.read_heartbeat(path) is None
