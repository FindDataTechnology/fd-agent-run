"""Project run ledgers into the shared event store.

Why this lives beside the journal and not in a separate service: the ledger is the
truth (see `journal.py`), and the store is a disposable projection of it. Keeping
the project function here means the two can never drift in their reading of the
same file — there is one reader, `journal.read_lines`, and one writer of rows.

The three rules this module enforces, in the order they matter:

1. **Same address, same bytes → skip.** Re-running a projection must not duplicate.
2. **Same address, different bytes → refuse.** A run's history is append-only; a
   projector that overwrote would be a silent rewrite channel into that history.
   The caller gets an exception naming the run and the sequence number.
3. **A watermark beyond the ledger means the projection is invalid, not the ledger.**
   The journal may have been truncated or the watermark tampered with; either way a
   full replay is the honest response. It is not an error, and it never returns a
   partial state (same rule `agent-run-ledger` gives for snapshots).

Both drivers (`crew`, `platform`) go through the same `absorb`, so the rules above
cannot be true for one substrate and aspirational for the other.

`psycopg` is imported inside the functions on purpose: `fd_agent_run`'s core is
stdlib-only and must stay importable on a machine with no database driver. The
platform path needs only `sqlite3`/`json`, which are stdlib.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import journal


class ProjectionError(RuntimeError):
    """The projection cannot proceed. Never swallowed."""


class ProjectionConflict(ProjectionError):
    """An already-absorbed address arrived with different content."""

    def __init__(self, run_key: str, seq: int, existing: str, incoming: str) -> None:
        super().__init__(
            f"{run_key}: seq {seq} already absorbed with fingerprint {existing[:12]}, "
            f"incoming is {incoming[:12]} — the ledger was rewritten; refusing to "
            f"overwrite history"
        )
        self.run_key = run_key
        self.seq = seq


#: One event ready to absorb: (seq, kind, at, payload, raw text, fingerprint).
Row = tuple[int, str, "dt.datetime | None", dict[str, Any], str, str]


def _parse_ts(value: Any) -> dt.datetime | None:
    """Best-effort timestamp. Unparseable is None (unknown), never "now"."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # The platform stores millis; seconds are ~1e9, millis ~1e12.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    # `%z` accepts +0800; fromisoformat wants +08:00.
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = text[:-2] + ":" + text[-2:]
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def default_source(project: Path) -> str:
    """`project:<repo>@<host>` — the host is part of the identity on purpose.

    Two machines can hold the same repo name; without the host their runs would
    collapse into one row, which is exactly the failure the store's `source`
    column exists to prevent.
    """
    repo = Path(project).resolve().name or "unknown-project"
    return f"project:{repo}@{socket.gethostname()}"


def cell_source(cell: str) -> str:
    """`cell:<16-hex tenant id>`.

    Not the pod name: one platform pod carries sixteen tenants, so a pod-named
    source would collapse all sixteen into one row — the same trap as two cells
    with the same fleet name.
    """
    return f"cell:{cell}"


def run_key_for(driver: str, source: str, run_id: str) -> str:
    return f"{driver}|{source}|{run_id}"


#: 承载者形状契约。**不是风格问题**：`agent_source` 的左边是「应该有什么」的声明，
#: 一条形状不合法的行会在覆盖面板上变成一个永远没有数据的幽灵租户，而读者无从判断
#: 它是「空租户」还是「打错字」。2026-11-13 生产库里就出现了一条 `('p','p','p')`
#: ——任何代码路径都产生不出它（`cell:<hex>` / `project:<name>@<host>` 两种之外无他），
#: 是手工插入的，而此前没有任何东西会拒绝它。
_DRIVERS = ("platform", "crew")
_CELL_RE = re.compile(r"^cell:[0-9a-f]{8,}$")
_PROJECT_RE = re.compile(r"^project:.+@.+$")


def validate_source(driver: str, source: str) -> tuple[str, str]:
    """校验并归一化 (driver, source)；不合法就抛，不静默登记。"""
    d = (driver or "").strip()
    s = (source or "").strip()
    if d not in _DRIVERS:
        raise ValueError(
            f"未知 driver {driver!r}；只允许 {_DRIVERS}（见 _DRIVERS）")
    if d == "platform" and not _CELL_RE.match(s):
        raise ValueError(
            f"platform 的 source 必须是 'cell:<16-hex 租户 id>'，收到 {source!r}"
            f"——可能是把 pod 名/租户短名当成了 source")
    if d == "crew" and not _PROJECT_RE.match(s):
        raise ValueError(
            f"crew 的 source 必须是 'project:<仓名>@<主机>'，收到 {source!r}")
    return d, s


def register_source(conn: Any, *, driver: str, source: str, label: str = "",
                    expected_period_seconds: int | None = None) -> None:
    """Declare a holder before it has ever projected.

    Required because "never projected" cannot be derived from a table that only
    contains projections — the coverage panel's left side has to be a declaration,
    or it silently omits the case it exists for.
    """
    driver, source = validate_source(driver, source)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_runs.agent_source
                (driver, source, label, expected_period_seconds)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (driver, source) DO UPDATE
              SET label = COALESCE(NULLIF(EXCLUDED.label, ''), agent_runs.agent_source.label),
                  expected_period_seconds = COALESCE(EXCLUDED.expected_period_seconds,
                                                     agent_runs.agent_source.expected_period_seconds)
            """,
            (driver, source, label, expected_period_seconds),
        )


def absorb(conn: Any, *, driver: str, source: str, run_id: str,
           header: dict[str, Any], rows: Sequence[Row]) -> dict[str, Any]:
    """Write one run's header and events. Idempotent; refuses rewritten history."""
    from psycopg.types.json import Jsonb

    run_key = run_key_for(driver, source, run_id)
    stats: dict[str, Any] = {"run_key": run_key, "events": len(rows),
                             "absorbed": 0, "skipped": 0, "invalidated": False}

    with conn.cursor() as cur:
        existing: dict[int, str] = {}
        if rows:
            cur.execute(
                "SELECT seq, content_hash FROM agent_runs.agent_run_event WHERE run_key = %s",
                (run_key,),
            )
            existing = {int(s): h for s, h in cur.fetchall()}

        cur.execute(
            "SELECT last_seq FROM agent_runs.agent_run_projection "
            "WHERE driver = %s AND source = %s AND run_id = %s",
            (driver, source, run_id),
        )
        row = cur.fetchone()
        watermark = int(row[0]) if row and row[0] is not None else 0
        if watermark > len(rows):
            # Recorded position beyond the source: the projection is stale or the
            # source shrank. Replay everything; this is not an error.
            stats["invalidated"] = True

        cur.execute(
            """
            INSERT INTO agent_runs.agent_run
                (run_key, driver, source, run_id, started_at, ended_at, status,
                 stop_reason, retention_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'permanent')
            ON CONFLICT (run_key) DO UPDATE
              SET started_at = COALESCE(agent_runs.agent_run.started_at, EXCLUDED.started_at),
                  ended_at   = COALESCE(EXCLUDED.ended_at, agent_runs.agent_run.ended_at),
                  status     = COALESCE(EXCLUDED.status, agent_runs.agent_run.status),
                  stop_reason = COALESCE(EXCLUDED.stop_reason, agent_runs.agent_run.stop_reason),
                  projected_at = now()
            """,
            (run_key, driver, source, run_id, header.get("started_at"),
             header.get("ended_at"), header.get("status"), header.get("stop_reason")),
        )

        last_seq, last_hash = 0, None
        for seq, kind, at, payload, raw, digest in rows:
            known = existing.get(seq)
            if known is not None:
                if known != digest:
                    raise ProjectionConflict(run_key, seq, known, digest)
                stats["skipped"] += 1
                last_seq, last_hash = seq, digest
                continue
            cur.execute(
                """
                INSERT INTO agent_runs.agent_run_event
                    (run_key, seq, kind, at, payload, raw, content_hash, retention_class)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'windowed')
                ON CONFLICT (run_key, seq) DO NOTHING
                """,
                (run_key, seq, kind, at, Jsonb(payload), raw, digest),
            )
            stats["absorbed"] += 1
            last_seq, last_hash = seq, digest

        cur.execute(
            """
            INSERT INTO agent_runs.agent_run_projection
                (driver, source, run_id, last_seq, last_hash, absorbed_count, status, absorbed_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'ok', now())
            ON CONFLICT (driver, source, run_id) DO UPDATE
              SET last_seq = EXCLUDED.last_seq,
                  last_hash = EXCLUDED.last_hash,
                  absorbed_count = EXCLUDED.absorbed_count,
                  status = 'ok',
                  last_error = NULL,
                  absorbed_at = now()
            """,
            (driver, source, run_id, last_seq, last_hash, len(rows)),
        )
    return stats


def unmapped_states(lines: Iterable[tuple[int, str, dict[str, Any], str]]) -> dict[str, int]:
    """State values the crosswalk does not cover, with counts.

    Reported rather than dropped: a new state value in a newer writer is exactly
    the kind of thing that would otherwise be absorbed silently and then show up
    as an unexplained "open" run on a board months later.
    """
    from . import crosswalk

    counts: dict[str, int] = {}
    for _seq, _raw, payload, _digest in lines:
        if payload.get("kind") != "task_state":
            continue
        state = payload.get("state")
        if not isinstance(state, str):
            continue
        if state in crosswalk.CREW_STATE_TO_VERDICT:
            continue
        counts[state] = counts.get(state, 0) + 1
    return counts


# ------------------------------------------------------------------ crew


def project_journal(project: Path, run_id: str, dsn: str, *,
                    source: str | None = None, driver: str = "crew",
                    dry_run: bool = False) -> dict[str, Any]:
    """Fold one journal into the store. Returns counts; raises on conflict."""
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ProjectionError(
            "projection needs psycopg (pip install 'psycopg[binary]'); "
            "the ledger itself does not"
        ) from exc

    project = Path(project)
    source = source or default_source(project)
    lines = journal.read_lines(project, run_id)
    stats: dict[str, Any] = {
        "run_key": run_key_for(driver, source, run_id), "events": len(lines),
        "absorbed": 0, "skipped": 0, "invalidated": False, "dry_run": dry_run,
        "unmapped_states": unmapped_states(lines),
    }
    if not lines:
        stats["note"] = "empty ledger (a missing journal is an empty run, not an error)"
        if dry_run:
            return stats
    if dry_run:
        stats["absorbed"] = len(lines)
        return stats

    rows: list[Row] = []
    for seq, raw, payload, digest in lines:
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ProjectionError(f"{stats['run_key']}: seq {seq} has no kind")
        rows.append((seq, kind, _parse_ts(payload.get("t")), payload, raw, digest))

    header = {"started_at": rows[0][2] if rows else None,
              # crew has no run-level status: the ledger does not record one.
              # Storing NULL is the honest value; the fact view derives it.
              "status": None, "ended_at": None, "stop_reason": None}

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        register_source(conn, driver=driver, source=source)
        stats.update(absorb(conn, driver=driver, source=source, run_id=run_id,
                            header=header, rows=rows))
        conn.commit()
    return stats


# -------------------------------------------------------------- platform


def read_export(path: Path) -> list[dict[str, Any]]:
    """Read one cell export (JSONL: `{"k": "run"|"turn"|"daily", ...}`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectionError(f"cannot read {path}: {exc}") from exc
    out: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ProjectionError(f"{path}:{lineno}: not JSON ({exc})") from exc
        if isinstance(obj, dict):
            out.append(obj)
    return out


def project_platform_export(path: Path, dsn: str, *, cell: str | None = None,
                            dry_run: bool = False) -> dict[str, Any]:
    """Project one tenant cell's export into the store.

    Batched per cell on purpose: a tenant can hold thousands of runs, and one
    round trip per run over a tailnet link turns a two-second job into a
    half-hour one. The rules are unchanged (same addresses, same fingerprints,
    same refusal) — only the number of trips differs.

    `__unassigned__` exists because 491 of 9,297 turns carry no `run_id`: those
    rows predate the run ledger. Inventing a run id would fabricate an identity;
    one explicitly named bucket keeps them queryable and visibly unassigned.
    """
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover
        raise ProjectionError(
            "projection needs psycopg (pip install 'psycopg[binary]')"
        ) from exc

    objects = read_export(path)
    if not objects:
        return {"cell": cell, "runs": 0, "turns": 0, "daily": 0, "note": "empty export"}

    meta = next((o for o in objects if o.get("k") == "meta"), {})
    if cell is None:
        cell = meta.get("cell") or Path(path).stem
    # 部分导出（--since / --after-id）：只含**新增**行。它对「水位」有两个后果，
    # 都必须挡住，否则增量刷新会把簿记弄坏：
    #   ① 某个 run 这次没有新行 → 不能把它的水位写成 0（那是「从没见过」的意思）；
    #   ② 「水位 > 台账长度 ⇒ 投影失效」这条判据在这里量纲都不对——水位是事件 id，
    #      台账长度是条数；部分导出里 len(rows) 恒远小于 id，于是每次都误报失效
    #      （2026-11-14 实测：16 个 cell 全报 [watermark invalid -> full replay]）。
    # 全量导出（无过滤）仍然照旧判失效 —— 那条判据在一份完整台账上是有意义的。
    partial = bool(meta.get("partial"))
    source = cell_source(cell)

    from . import crosswalk

    runs: dict[str, dict[str, Any]] = {}
    turns: dict[str, list[Row]] = {}
    unmapped_status: dict[str, int] = {}
    daily: list[dict[str, Any]] = []
    claim_objects: list[dict[str, Any]] = []
    for obj in objects:
        kind = obj.get("k")
        if kind == "run":
            run_id = obj.get("run_id") or "__unassigned__"
            runs[run_id] = {
                "started_at": _parse_ts(obj.get("started_at")),
                "ended_at": _parse_ts(obj.get("ended_at")),
                "status": obj.get("status"),
                "stop_reason": obj.get("stop_reason"),
            }
        elif kind == "turn":
            run_id = obj.get("run_id") or "__unassigned__"
            # Fingerprint over the canonical serialisation of the row: the export
            # is regenerated each run, so byte-level fidelity to the SQLite file is
            # not available — content is. Sort keys so column order cannot fake a
            # rewrite.
            raw = json.dumps({k: v for k, v in obj.items() if k != "k"},
                             ensure_ascii=False, sort_keys=True, default=str)
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            payload = {k: v for k, v in obj.items() if k != "k"}
            turns.setdefault(run_id, []).append(
                (int(obj["id"]), "turn", _parse_ts(obj.get("ended_at")), payload, raw, digest)
            )
            status = obj.get("status")
            if not (isinstance(status, str) and status in crosswalk.PLATFORM_STATUS_TO_VERDICT):
                unmapped_status[str(status)] = unmapped_status.get(str(status), 0) + 1
        elif kind == "daily":
            daily.append(obj)
        elif kind == "claim":
            claim_objects.append(obj)

    # One row per attempt, reduced to its LATEST picture: the export may carry
    # the same turn_ref twice (a heartbeat between two passes) and the later
    # observation is the same fact, further along.
    claims = reduce_claims(claim_objects)
    stats: dict[str, Any] = {
        "cell": cell, "source": source, "runs": len(runs),
        "turns": sum(len(v) for v in turns.values()), "daily": len(daily),
        "claims": len(claims),
        "absorbed": 0, "skipped": 0, "invalidated": [], "unmapped_status": unmapped_status,
        "dry_run": dry_run,
    }
    if dry_run:
        return stats

    all_run_ids = sorted(set(runs) | set(turns))
    # Only a holder that has ever produced something is expected to keep
    # producing. Declaring an expectation for an empty tenant leaves the coverage
    # panel permanently red with tenants that are simply unused — and a panel that
    # is always red is a panel nobody reads.
    has_data = bool(stats["turns"] or stats["runs"])
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        register_source(conn, driver="platform", source=source,
                        label=f"tenant {cell}",
                        expected_period_seconds=86_400 if has_data else None)
        with conn.cursor() as cur:
            # register_source COALESCEs so an existing expectation is never erased
            # by accident; this is the one place that legitimately clears it again
            # when a tenant turns out to hold nothing.
            cur.execute(
                "UPDATE agent_runs.agent_source SET expected_period_seconds = %s "
                "WHERE driver = 'platform' AND source = %s",
                (86_400 if has_data else None, source),
            )
            cur.execute(
                """
                SELECT e.run_key, e.seq, e.content_hash
                FROM agent_runs.agent_run_event e
                JOIN agent_runs.agent_run r USING (run_key)
                WHERE r.driver = 'platform' AND r.source = %s
                """,
                (source,),
            )
            existing = {(rk, int(seq)): h for rk, seq, h in cur.fetchall()}
            cur.execute(
                "SELECT run_id, last_seq FROM agent_runs.agent_run_projection "
                "WHERE driver = 'platform' AND source = %s",
                (source,),
            )
            watermarks = {rid: int(ls or 0) for rid, ls in cur.fetchall()}

            headers: list[tuple[Any, ...]] = []
            events: list[tuple[Any, ...]] = []
            projections: list[tuple[Any, ...]] = []
            for run_id in all_run_ids:
                rk = run_key_for("platform", source, run_id)
                h = runs.get(run_id) or {"started_at": None, "ended_at": None,
                                        "status": None, "stop_reason": None}
                rows = sorted(turns.get(run_id, []), key=lambda r: r[0])
                if not rows and h.get("status") is None:
                    continue
                headers.append((rk, "platform", source, run_id, h.get("started_at"),
                                h.get("ended_at"), h.get("status"), h.get("stop_reason")))
                if partial:
                    # 部分导出：水位与「台账长度」无从比较，且没有新行的 run 不该被写
                    if not rows:
                        continue
                elif watermarks.get(run_id, 0) > len(rows):
                    stats["invalidated"].append(run_id)

                last_seq, last_hash = 0, None
                for seq, kind, at, payload, raw, digest in rows:
                    known = existing.get((rk, seq))
                    if known is not None:
                        if known != digest:
                            raise ProjectionConflict(rk, seq, known, digest)
                        stats["skipped"] += 1
                        last_seq, last_hash = seq, digest
                        continue
                    events.append((rk, seq, kind, at, Jsonb(payload), raw, digest))
                    stats["absorbed"] += 1
                    last_seq, last_hash = seq, digest
                projections.append(("platform", source, run_id, last_seq,
                                    last_hash, len(rows)))

            cur.executemany(
                """
                INSERT INTO agent_runs.agent_run
                    (run_key, driver, source, run_id, started_at, ended_at, status,
                     stop_reason, retention_class)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'permanent')
                ON CONFLICT (run_key) DO UPDATE
                  SET started_at = COALESCE(agent_runs.agent_run.started_at, EXCLUDED.started_at),
                      ended_at   = COALESCE(EXCLUDED.ended_at, agent_runs.agent_run.ended_at),
                      status     = COALESCE(EXCLUDED.status, agent_runs.agent_run.status),
                      stop_reason = COALESCE(EXCLUDED.stop_reason, agent_runs.agent_run.stop_reason),
                      projected_at = now()
                """,
                headers,
            )
            if events:
                cur.executemany(
                    """
                    INSERT INTO agent_runs.agent_run_event
                        (run_key, seq, kind, at, payload, raw, content_hash, retention_class)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'windowed')
                    ON CONFLICT (run_key, seq) DO NOTHING
                    """,
                    events,
                )
            cur.executemany(
                """
                INSERT INTO agent_runs.agent_run_projection
                    (driver, source, run_id, last_seq, last_hash, absorbed_count,
                     status, absorbed_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'ok', now())
                ON CONFLICT (driver, source, run_id) DO UPDATE
                  SET last_seq = EXCLUDED.last_seq,
                      last_hash = EXCLUDED.last_hash,
                      absorbed_count = EXCLUDED.absorbed_count,
                      status = 'ok', last_error = NULL, absorbed_at = now()
                """,
                projections,
            )
            _heartbeat(cur, source, absorbed=stats["absorbed"])
            if daily:
                _upsert_daily(cur, source, daily)
            claim_stats = _upsert_claims(cur, source, claims)
        conn.commit()
    stats.update(claim_stats)
    return stats


#: 心跳行的保留 `run_id`。它不是一次 run（`__unassigned__` 是先例：保留名字显式
#: 表达「这里没有身份」），而是「这条链路在某时刻确实跑通了」这个事实。
HEARTBEAT_RUN_ID = "__cell__"


def _heartbeat(cur: Any, source: str, *, absorbed: int) -> None:
    """记一条「本次刷新跑通了」的心跳，**哪怕一行新数据都没有**。

    为什么必须有它：覆盖面板用 `max(absorbed_at)` 判新鲜度，而水位行只在**有新增
    行**时才更新。于是一个「确实在被读、但这两天没有新回合」的 cell 会在 2 个周期后
    被判 `stale`——红的是一个没毛病的东西（2026-11-14 实测：`cell:de7ec3a69617c6e0`
    历史上只有 1 次 run、`cell:6db2b1b4b490223b` 两天无新回合，两个都会在
    11-15 18:18Z / 11-16 02:00Z 同时翻红）。而那正是「闸常年红就会被无视」的起点。

    两个事实分开记，因为它们是两种故障：
      * `status='ok'` + `absorbed_count>0` → 有数据进来（正常）；
      * `status='idle'` + `absorbed_count=0` → 读通了、只是没有新数据；
      * 心跳本身变旧（> 2 个声明周期）→ **流水线断了**，这才是该红的。
    面板据此把 `idle` 与 `stale` 分开，不再把「安静」当成「坏了」。
    """
    cur.execute(
        """
        INSERT INTO agent_runs.agent_run_projection
            (driver, source, run_id, last_seq, last_hash, absorbed_count, status, absorbed_at)
        VALUES ('platform', %s, %s, 0, NULL, %s, %s, now())
        ON CONFLICT (driver, source, run_id) DO UPDATE
          SET absorbed_count = EXCLUDED.absorbed_count,
              status = EXCLUDED.status,
              last_error = NULL,
              absorbed_at = now()
        """,
        (source, HEARTBEAT_RUN_ID, absorbed, "ok" if absorbed else "idle"),
    )


def reduce_claims(objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The claim lane's reducer, PURE (no DB): one row per turn_ref, latest wins.

    Testable without a database on purpose — the rule this function encodes is
    the one the spec pins ("heartbeats do not grow the row count"), and a rule
    only checkable by connecting to Postgres is a rule nobody re-checks.
    """
    out: dict[str, dict[str, Any]] = {}
    for obj in objects:
        if obj.get("k") != "claim":
            continue
        ref = obj.get("turn_ref")
        if ref:
            out[str(ref)] = obj
    return out


def _upsert_claims(cur: Any, source: str, claims: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Upsert the attempt facts and advance the CLAIM lane's own watermark.

    Two rules, each with the failure it prevents:

    * One row per ``turn_ref`` (upsert), never an append. Heartbeats arrive as
      the same attempt further along; appending them would grow the table with
      liveness instead of with attempts, and the "what is running" panel would
      read a long turn as many.
    * The watermark is the CLAIM lane's max(updated_at), stored under
      lane='claim'. Sharing the turn lane's max(id) would let "results are
      current" masquerade as "claims are current" — the two ride different
      cursors on the cell side and one side's advance is not the other's.
    """
    stats: dict[str, Any] = {"claims_absorbed": 0}
    if not claims:
        return stats

    def _ts(v: Any) -> Any:
        return _parse_ts(v) if v is not None else None

    rows = []
    for ref, o in claims.items():
        rows.append((
            ref, o.get("driver") or "platform", source, o.get("run_id"), o.get("turn_id"),
            o.get("fleet_id"), o.get("member_id"), o.get("session_id"),
            o.get("carrier"), o.get("carrier_instance"),
            _ts(o.get("started_at")), _ts(o.get("heartbeat_at")), _ts(o.get("lease_expires_at")),
            _ts(o.get("closed_at")), o.get("close_reason"), o.get("cancelled_by"),
            o.get("runtime_fingerprint"),
        ))
    cur.executemany(
        """
        INSERT INTO agent_runs.agent_turn_claim
            (turn_ref, driver, source, run_id, turn_id, fleet_id, member_id, session_id,
             carrier, carrier_instance, started_at, heartbeat_at, lease_expires_at,
             closed_at, close_reason, cancelled_by, runtime_fingerprint, retention_class)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'permanent')
        ON CONFLICT (turn_ref) DO UPDATE
          SET heartbeat_at        = COALESCE(EXCLUDED.heartbeat_at, agent_runs.agent_turn_claim.heartbeat_at),
              lease_expires_at    = COALESCE(EXCLUDED.lease_expires_at, agent_runs.agent_turn_claim.lease_expires_at),
              closed_at           = COALESCE(EXCLUDED.closed_at, agent_runs.agent_turn_claim.closed_at),
              close_reason        = COALESCE(EXCLUDED.close_reason, agent_runs.agent_turn_claim.close_reason),
              cancelled_by        = COALESCE(EXCLUDED.cancelled_by, agent_runs.agent_turn_claim.cancelled_by),
              runtime_fingerprint = COALESCE(agent_runs.agent_turn_claim.runtime_fingerprint, EXCLUDED.runtime_fingerprint),
              projected_at        = now()
        """,
        rows,
    )
    stats["claims_absorbed"] = len(rows)
    # The lane watermark: max(updated-ish) observed in THIS batch. We use the
    # latest of the timestamps we carry (heartbeat > started) so a claim that
    # only beat (never closed) still advances it.
    cursor = max(
        (int(v) for o in claims.values() for v in (o.get("heartbeat_at"), o.get("started_at")) if v is not None),
        default=None,
    )
    cur.execute(
        """
        INSERT INTO agent_runs.agent_claim_projection
            (driver, source, lane, last_cursor, absorbed_count, status, absorbed_at)
        VALUES ('platform', %s, 'claim', %s, %s, 'ok', now())
        ON CONFLICT (driver, source, lane) DO UPDATE
          SET last_cursor = GREATEST(COALESCE(EXCLUDED.last_cursor, 0), COALESCE(agent_runs.agent_claim_projection.last_cursor, 0)),
              absorbed_count = agent_runs.agent_claim_projection.absorbed_count + EXCLUDED.absorbed_count,
              status = 'ok', last_error = NULL, absorbed_at = now()
        """,
        (source, cursor, len(rows)),
    )
    return stats


def _upsert_daily(cur: Any, source: str, rows: list[dict[str, Any]]) -> None:
    """Mirror the cell's permanent daily rollup — the archive's integrity baseline.

    Must aggregate before inserting, not upsert row by row. The cell's unique key
    includes nullable `carrier`/`provider`/`model`, and SQLite treats NULLs as
    distinct, so one member can legitimately own several rows for the same day.
    Collapsing those NULLs to '' on our side maps all of them onto one primary key,
    and a last-wins upsert silently discarded the earlier ones — measured before
    the fix: 4,130 source rows totalling 6,637 turns became 744 rows totalling
    3,251. Summing first keeps the totals right and stays idempotent on re-runs.
    """
    merged: dict[tuple, list[int]] = {}
    for r in rows:
        key = (r.get("fleet_id") or "", r.get("member_id") or "", r.get("carrier") or "",
               r.get("provider") or "", r.get("model") or "", r.get("day") or "")
        acc = merged.setdefault(key, [0, 0, 0, 0])
        acc[0] += int(r.get("turns") or 0)
        acc[1] += int(r.get("ok_count") or 0)
        acc[2] += int(r.get("failed_count") or 0)
        acc[3] += int(r.get("skipped_count") or 0)

    params = [(source, *key, acc[0], acc[1], acc[2], acc[3])
              for key, acc in merged.items()]
    cur.executemany(
        """
        INSERT INTO agent_runs.agent_turn_daily
            (driver, source, fleet_id, member_id, carrier, provider, model, day,
             turns, ok_count, failed_count, skipped_count, retention_class, absorbed_at)
        VALUES ('platform', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'permanent', now())
        ON CONFLICT (driver, source, fleet_id, member_id, carrier, provider, model, day)
        DO UPDATE SET turns = EXCLUDED.turns,
                      ok_count = EXCLUDED.ok_count,
                      failed_count = EXCLUDED.failed_count,
                      skipped_count = EXCLUDED.skipped_count,
                      absorbed_at = now()
        """,
        params,
    )


def discover_runs(project: Path) -> list[str]:
    """Run ids present under `<project>/.run/`, newest directory name last."""
    root = Path(project) / ".run"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / journal.JOURNAL).exists())


def dsn_from_env(var: str = "FD_AGENT_RUN_DSN") -> str:
    """Credentials come from the environment only — never a literal in the repo."""
    value = os.environ.get(var, "").strip()
    if not value:
        raise ProjectionError(
            f"missing {var}: the projector takes its connection string from the "
            f"environment (no credentials in source or defaults)"
        )
    return value
