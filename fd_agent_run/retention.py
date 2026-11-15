"""保留策略执行器——`agent_runs.retention_policy` 的唯一执行者。

**为什么这个模块存在**：`sql/001_agent_runs.sql` 从第一版起就把保留策略声明成一张
表（D5：`retention_class` 是列，不是注释），`windowed` 写着「超 90 天 **且** 超每个
承载者最近 500 条才删」。但到 2026-11-15 为止，全仓 `delete from agent_runs` 零命中
——**声明了却没有任何执行者**。它不是缺陷的当下形态（彼时明细 22 天、可删 0 行），
而是「两个月后该删了，而没人知道它没动」的形态。

判据只认那张表，不认表名（与 schema 注释同一句话）：

* 哪些表带 `retention_class` 列 —— 从 `information_schema` 发现，不写死清单；
* 每张表的期限与最少保留 —— 从 `retention_policy` 读，不写死 90/500；
* **表在发现集里、行落在有期限的 class 上、却没有登记 pruner ⇒ 直接失败**。
  这一条是整份实现的要点：静默的盲区只会来自「新表没人想起来加 pruner」，
  而那种表在删除侧看起来与 `permanent` 表一模一样（都没东西可删）。宁可拒绝运行
  并点名该补哪条登记，也不要报一个「本次删除 0 行」的绿。

⚠ **权限事实（2026-11-15 实测）**：唯一可写角色 `fd_agent_run_projector` 只有
schema USAGE + INSERT/UPDATE，**没有 DELETE，也没有 CREATE**。所以 `--apply` 今天
会被拒绝，并打出需要的那一条 `GRANT`。这不是本模块的缺陷，是本模块要报告的事实；
`--dry-run`（默认）只读，能给出真实的可删行数。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

#: 一次 `--apply` 最多删多少行。**不是性能旋钮，是安全阀**：判据写错时（比如把
#: `rn > keep` 写成 `rn >= keep`、或把 holder 分区表达错）行数会从几百跳到全表，
#: 而「已删除」这种动作没有 undo。超阀就只删到阀值并把剩余量报成 backlog。
DEFAULT_MAX_ROWS = 5000

#: 每次 DELETE 的行数上限。短事务：长事务会持有全表锁并让 autovacuum 排队。
DEFAULT_BATCH = 500


class RetentionRefused(RuntimeError):
    """没有执行，且**拒绝的理由必须原样传给操作者**（缺授权/缺登记）。"""


class RetentionError(RuntimeError):
    """执行失败（执行了但结果不可信）。"""


@dataclass(frozen=True)
class Pruner:
    """一张明细表的「怎么判行属于哪个承载者」。"""

    table: str
    #: 承载者表达式（指定表别名 ``t``）。必须与另外两列一起给出真实的分组键：
    #: 分区错了，`min_keep_per_holder` 就变成「全表留 500 条」，一次删掉几乎所有历史。
    holder_expr: str
    #: 时间表达式（指定表别名 ``t``）。NULL 合法：`at` 列本身可空，NULL 行
    #: **永远不够格被删**（`NULL < now() - interval` 是 NULL），所以必须单列计数，
    #: 否则它们会在「可删 0」里静默永久驻留。
    time_expr: str
    #: 主键列（删除时按它对回表，避免 ctid 并发失效）。
    key_cols: tuple[str, ...]
    #: 额外的 FROM 子句（需要 join 父表拿承载者时）。
    from_sql: str = ""
    #: 排序里的确定性 tiebreak：同一时刻多行时，两次运行必须删同一批，否则
    #: 「本次删 N」会随运行抖动，而且 idempotency 无法断言。
    tiebreak: str = "t.seq DESC"

    @property
    def alias_from(self) -> str:
        return f"{self.table} t {self.from_sql}".strip()


#: 明细 → pruner 的登记表。**加新的明细表时必须在这里登记**（见 `assert_coverage`）。
PRUNERS: dict[str, Pruner] = {
    "agent_runs.agent_run_event": Pruner(
        table="agent_runs.agent_run_event",
        # 承载者取自父表，不拆 `run_key` 字符串：分隔符是写侧约定，不是约束。
        holder_expr="r.driver || '|' || r.source",
        time_expr="t.at",
        key_cols=("run_key", "seq"),
        from_sql="JOIN agent_runs.agent_run r ON r.run_key = t.run_key",
        tiebreak="t.seq DESC",
    ),
}


#: 发现集的显式豁免。**必须带理由**，理由写不出来就说明它是真缺口。
#:
#: 首跑（2026-11-15）实测：`retention_policy` 自己也有 `retention_class` 列——它是
#: **主键**，不是行的分级——于是「带该列的表都必须有 pruner」这条规则把规则本本身
#: 判成了缺口。同族于「闸会命中自己的规则本」：发现式判据必须先把判据自己排除掉，
#: 否则第一条假红就出现在最显眼的位置，而它会把后面那条真红（缺 DELETE 授权）挤下去。
EXEMPT_TABLES: dict[str, str] = {
    "agent_runs.retention_policy": "策略表本身；它的 retention_class 是主键，不是行的分级",
}


@dataclass
class TablePlan:
    table: str
    retention_class: str
    period_days: int
    min_keep: int
    rows: int = 0
    null_time: int = 0
    eligible: int = 0
    ever_eligible: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "retention_class": self.retention_class,
            "period_days": self.period_days,
            "min_keep_per_holder": self.min_keep,
            "rows": self.rows,
            "null_time": self.null_time,
            "eligible": self.eligible,
        }


@dataclass
class Plan:
    tables: list[TablePlan] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    generated_at: str = ""

    @property
    def eligible(self) -> int:
        return sum(t.eligible for t in self.tables)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "eligible": self.eligible,
            "tables": [t.as_dict() for t in self.tables],
            "blocked": self.blocked,
        }


# ---------------------------------------------------------------- 发现与断言


def discovered_tables(conn: Any) -> list[str]:
    """带 `retention_class` 列的表——**发现**，不查清单。

    判据只认这一张策略表（D5）：下一个新表只要带这一列就自动受同一套规则约束。
    反过来，靠手写表名清单会让新表出生在判据之外，而它在删除侧看起来完全正常。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select table_name
              from information_schema.columns
             where table_schema = 'agent_runs' and column_name = 'retention_class'
             order by table_name
            """
        )
        return [f"agent_runs.{r[0]}" for r in cur.fetchall()]


def classes_in_use(conn: Any, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"select distinct retention_class from {table} order by 1"  # noqa: S608 - 表名来自 information_schema
        )
        return [r[0] for r in cur.fetchall()]


def policies(conn: Any) -> dict[str, dict[str, Any]]:
    """读策略表本身。**期限从库里读**，不写死——写死就等于第二份真相。"""
    with conn.cursor() as cur:
        cur.execute(
            "select retention_class, period_days, min_keep_per_holder, note"
            "  from agent_runs.retention_policy"
        )
        return {
            r[0]: {"period_days": r[1], "min_keep_per_holder": r[2], "note": r[3]}
            for r in cur.fetchall()
        }


def assert_coverage(conn: Any) -> list[dict[str, Any]]:
    """发现集里有、而登记表里没有的表——只要它真的落在有期限的 class 上就失败。

    分三种情形，混起来报会把人送去错的地方：

    * 有 windowed 行、无 pruner ⇒ **拒绝**（这是要补的登记，不是可选项）；
    * 有 windowed 行、有 pruner ⇒ 正常；
    * 只有 permanent 行 ⇒ 不删是对的，静默通过（否则每张永久表都要登记一遍）。
    """
    pol = policies(conn)
    blocked: list[dict[str, Any]] = []
    for table in discovered_tables(conn):
        if table in EXEMPT_TABLES:
            continue
        classes = classes_in_use(conn, table)
        timed = [c for c in classes
                 if (pol.get(c) or {}).get("period_days") is not None]
        if timed and table not in PRUNERS:
            blocked.append({
                "table": table,
                "classes": timed,
                "why": ("该表存在有期限的保留分级行，但没有登记 pruner —— 这些行永远不会被删，"
                        "而删除侧的每次运行都会报「0 行可删」"),
                "fix": (f"在 fd_agent_run/retention.py 的 PRUNERS 里登记 {table}："
                        "holder_expr（承载者）、time_expr（时间列）、key_cols（主键）"),
            })
    return blocked


def plan(conn: Any, *, now: dt.datetime | None = None,
         what_if_period_days: int | None = None,
         what_if_keep: int | None = None) -> Plan:
    """只读地算出「按声明的策略，现在有多少行该删」。不删任何东西。

    `what_if_*` 是**空跑专用**的旋钮，用来回答「这条判据到底会不会点火」：把期限
    调小或把保留名次调小，可删行数必须显著上升；若仍是 0，说明谓词恒假（写成了
    `>=`/列名错/分区错），而 0 与「确实没有超期行」在输出上长得一样。
    `apply_plan` 不接受它们——判据与执行必须用同一份参数，否则「删了什么」无从复算。
    """
    pol = policies(conn)
    result = Plan(blocked=assert_coverage(conn),
                  generated_at=(now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"))
    for table, pruner in PRUNERS.items():
        for cls, row in sorted(pol.items()):
            period = row["period_days"]
            if period is None:  # permanent：不删
                continue
            keep = int(row["min_keep_per_holder"] or 0)
            if what_if_period_days is not None:
                period = what_if_period_days
            if what_if_keep is not None:
                keep = what_if_keep
            sql = f"""
                with ranked as (
                  select {pruner.time_expr} as at,
                         row_number() over (
                           partition by {pruner.holder_expr}
                           order by {pruner.time_expr} desc nulls last, {pruner.tiebreak}
                         ) as rn
                    from {pruner.alias_from}
                   where t.retention_class = %(cls)s
                )
                select count(*)::int,
                       count(*) filter (where at is null)::int,
                       count(*) filter (
                         where at is not null
                           and at < now() - make_interval(days => %(period)s)
                           and rn > %(keep)s)::int
                  from ranked
            """
            with conn.cursor() as cur:
                cur.execute(sql, {"cls": cls, "period": int(period), "keep": keep})
                rows, null_time, eligible = cur.fetchone()
            result.tables.append(TablePlan(
                table=table, retention_class=cls, period_days=int(period), min_keep=keep,
                rows=int(rows or 0), null_time=int(null_time or 0), eligible=int(eligible or 0),
            ))
    return result


# ------------------------------------------------------------------ 执行


def _delete_batch(conn: Any, pruner: Pruner, *, cls: str, period: int,
                  keep: int, limit: int) -> int:
    # 时间列在 CTE 里改名成 `at`，下游只认这个名字——对 `t.at` 做字符串替换
    # （`ranked.at`）能跑，但它把「表达式里必须带 t.」变成隐式前提，改一行就静默错。
    keys = ", ".join(pruner.key_cols)
    keys_t = ", ".join("t." + c for c in pruner.key_cols)
    sql = f"""
        with ranked as (
          select {keys_t},
                 {pruner.time_expr} as at,
                 row_number() over (
                   partition by {pruner.holder_expr}
                   order by {pruner.time_expr} desc nulls last, {pruner.tiebreak}
                 ) as rn
            from {pruner.alias_from}
           where t.retention_class = %(cls)s
        ), dead as (
          select {keys}
            from ranked
           where rn > %(keep)s
             and at is not null
             and at < now() - make_interval(days => %(period)s)
           order by at
           limit %(limit)s
        )
        delete from {pruner.table} t
         using dead
         where {" and ".join(f"t.{c} = dead.{c}" for c in pruner.key_cols)}
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"cls": cls, "period": period, "keep": keep, "limit": limit})
        return int(cur.rowcount or 0)


def apply_plan(conn: Any, p: Plan, *, batch: int = DEFAULT_BATCH,
               max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """按计划删除，返回每张表删了多少、还剩多少没删（backlog）。

    **「可删 > 0 却一行没删」必须是错误，不是 🆗**：那是判据与执行脱节的唯一形态
    （权限不足、`WHERE` 恒假、事务被回滚），而它写出来的日志与「本次确实没有超期行」
    一字不差。`eligible>0 & deleted==0` 在这里抛错，绝不返回 0。
    """
    if p.blocked:
        names = "、".join(b["table"] for b in p.blocked)
        raise RetentionRefused(
            f"{len(p.blocked)} 张表落在有期限的保留分级上却没有 pruner（{names}）——"
            "先补 PRUNERS 登记再执行删除"
        )
    done: list[dict[str, Any]] = []
    total_deleted = 0
    for t in p.tables:
        pruner = PRUNERS[t.table]
        before = t.eligible
        deleted = 0
        budget = min(before, max_rows - total_deleted) if max_rows else before
        # 每批重新算 rn：删完之后名次会前移，用旧的名次删第二批会多删。
        while deleted < budget:
            n = _delete_batch(conn, pruner, cls=t.retention_class, period=t.period_days,
                              keep=t.min_keep, limit=min(batch, budget - deleted))
            if n == 0:
                break
            deleted += n
        if before > 0 and deleted == 0:
            raise RetentionError(
                f"{t.table} 按策略有 {before} 行该删，但一行都没删掉——"
                "这不是「没有超期行」，是判据或授权与执行脱节（看删除语句的 WHERE、"
                "以及角色是否有 DELETE）"
            )
        total_deleted += deleted
        done.append({
            "table": t.table,
            "retention_class": t.retention_class,
            "eligible_before": before,
            "deleted": deleted,
            "backlog": before - deleted,
        })
    return {"deleted": total_deleted, "tables": done,
            "capped": bool(max_rows and total_deleted >= max_rows)}


# --------------------------------------------------------------- 授权体检


def delete_privileges(conn: Any) -> list[dict[str, Any]]:
    """每张明细表上「这个角色能不能 DELETE」。执行前就问清楚，别等 `--apply` 撞墙。

    `has_table_privilege` 返回 false 时给出可直接粘贴的 `GRANT` 语句：报告「没有权限」
    而不说「要什么样的权限、由谁给」，就等于把问题留在原地再问一次。
    """
    out: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute("select current_user")
        role = cur.fetchone()[0]
        for table in PRUNERS:
            cur.execute("select has_table_privilege(%s, 'DELETE')", (table,))
            ok = bool(cur.fetchone()[0])
            out.append({
                "table": table,
                "can_delete": ok,
                "role": role,
                "fix": "" if ok else (
                    f"由有授权能力的角色执行：GRANT DELETE ON {table} TO {role};"
                    "（本模块只删 `retention_policy` 声明为有期限的分级，不删 permanent）"),
            })
    return out


# --------------------------------------------------------------- 心跳产物


def default_heartbeat_path() -> Path:
    return Path(os.environ.get(
        "FD_RETENTION_HEARTBEAT",
        str(Path.home() / ".local" / "state" / "fd-agent-run" / "retention.json"),
    ))


def write_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先写临时文件再 `os.replace`，读侧不会读到半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_heartbeat(path: Path | None = None) -> dict[str, Any] | None:
    p = path or default_heartbeat_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ 渲染


def render(p: Plan, *, privileges: Iterable[dict[str, Any]] = (),
           applied: dict[str, Any] | None = None) -> list[str]:
    lines = [f"保留策略（读自 agent_runs.retention_policy）· 生成于 {p.generated_at}"]
    if not p.tables:
        lines.append("  · 没有任何「有期限」的分级有明细行——本次没有可判定的对象")
    for t in p.tables:
        keep = f"，且每承载者保留最近 {t.min_keep} 条" if t.min_keep else ""
        lines.append(
            f"  {t.table} [{t.retention_class}] 共 {t.rows} 行"
            f"（期限 {t.period_days} 天{keep}）"
        )
        lines.append(
            f"    可删 {t.eligible} 行"
            + (f"；时间列为 NULL 的 {t.null_time} 行（永远不够格被删，需人工看）"
               if t.null_time else "")
        )
    for b in p.blocked:
        lines.append(f"  ✗ {b['table']}：{b['why']}")
        lines.append(f"    修法：{b['fix']}")
    for row in privileges:
        if not row["can_delete"]:
            lines.append(f"  ✗ 角色 {row['role']} 对 {row['table']} 没有 DELETE 权限")
            lines.append(f"    修法：{row['fix']}")
    if applied is not None:
        lines.append(f"  已删除 {applied['deleted']} 行"
                     + ("（触发单次上限，剩余留到下一轮）" if applied.get("capped") else ""))
        for t in applied["tables"]:
            lines.append(f"    {t['table']}：删 {t['deleted']} / 可删 {t['eligible_before']}"
                         f"，backlog {t['backlog']}")
    return lines
