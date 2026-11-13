-- 事实层：由事件流派生，**不落表**（design D4）
--
-- `agent-run-ledger` 要求「任何派生快照 MUST 可由事件流重建」。落成表就是第二份
-- 能说谎的真相，所以这里是 VIEW。
--
-- 与 `fd_agent_run/adapters.py` 的折叠规则必须一致（同一套 crosswalk）。已知的一处
-- 有意差异：未覆盖的状态在这里是 NULL（SQL 的未知），在 Python 里是 `$unknown`
-- 标记 —— 两者都不是 `pending`。

-- 视图不持数据，因此「先 DROP 再建」比 CREATE OR REPLACE 更合适：
-- 后者不允许改列名/列序，而这里的列会随口径演进。按依赖顺序删（先下游）。
DROP VIEW IF EXISTS agent_runs.agent_run_state;
DROP VIEW IF EXISTS agent_runs.agent_run_fact;

CREATE OR REPLACE VIEW agent_runs.agent_run_fact AS
WITH ev AS (
  SELECT run_key, seq, kind, at, payload,
         payload->>'task' AS task,
         COALESCE(NULLIF(payload->>'attempt', '')::int, 1) AS attempt
  FROM agent_runs.agent_run_event
  WHERE payload ? 'task'
), steps AS (
  SELECT *,
    CASE kind
      WHEN 'task_state' THEN
        -- 未覆盖状态给一个显式哨兵，而不是 NULL：NULL 会被「取最近一个非空步骤」
        -- 的聚合跳过，于是前一条 pending 顶掉它，未知就被静默洗成了 pending。
        CASE payload->>'state'
          WHEN 'committed'   THEN 'committed'
          WHEN 'abandoned'   THEN 'cancelled'
          WHEN 'produced'    THEN 'pending'
          WHEN 'pending'     THEN 'pending'
          WHEN 'leased'      THEN 'pending'
          WHEN 'working'     THEN 'pending'
          WHEN 'predicate_failed' THEN 'failed'
          ELSE '?unknown'
        END
      WHEN 'task_predicate' THEN
        CASE WHEN (payload->>'ok')::boolean THEN 'committed' ELSE 'failed' END
      WHEN 'task_committed' THEN 'committed'
      WHEN 'task_abandoned' THEN 'cancelled'
      ELSE NULL
    END AS verdict_step
  FROM ev
), folded AS (
  SELECT
    run_key, task, attempt,
    min(at) AS at,
    (array_agg(verdict_step ORDER BY seq DESC)
       FILTER (WHERE verdict_step IS NOT NULL))[1] AS verdict_raw,
    (array_agg(payload->>'reason' ORDER BY seq DESC)
       FILTER (WHERE payload ? 'reason' AND coalesce(payload->>'reason', '') <> ''))[1] AS reason,
    (array_agg(payload->>'holder' ORDER BY seq DESC)
       FILTER (WHERE payload ? 'holder' AND coalesce(payload->>'holder', '') <> ''))[1] AS holder,
    (array_agg(payload->'predicate' ORDER BY seq DESC)
       FILTER (WHERE jsonb_typeof(payload->'predicate') = 'object'))[1] AS predicate,
    (array_agg(payload->'evidence' ORDER BY seq DESC)
       FILTER (WHERE jsonb_typeof(payload->'evidence') = 'array'
                 AND jsonb_array_length(payload->'evidence') > 0))[1] AS evidence,
    (array_agg(payload->'upstreams' ORDER BY seq DESC)
       FILTER (WHERE payload ? 'upstreams'))[1] AS upstreams,
    max(seq) AS last_seq
  FROM steps
  GROUP BY run_key, task, attempt
)
SELECT
  f.run_key,
  f.task,
  f.attempt,
  f.at,
  -- owner：具体持有者，否则未知。角色词（cron/agent）不是持有者。
  coalesce(to_jsonb(f.holder), jsonb_build_object('$unknown', true)) AS owner,
  -- upstreams：事件自带的优先，其次取判据里声明的依赖，都没有才是未知。
  -- 只报未知会让「有数据但没抬上来」和「确实没去查」变成同一个样子。
  coalesce(f.upstreams, f.predicate->'upstreams',
           jsonb_build_object('$unknown', true))                   AS upstreams,
  -- predicate：缺声明时刻时补上未知标记，而不是补一个看起来合理的时刻。
  CASE
    WHEN f.predicate IS NULL THEN jsonb_build_object('$unknown', true)
    WHEN f.predicate ? 'declared_at' THEN f.predicate
    ELSE f.predicate || jsonb_build_object('declared_at', jsonb_build_object('$unknown', true))
  END AS predicate,
  coalesce(f.evidence, jsonb_build_object('$unknown', true))        AS evidence,
  f.verdict_raw AS verdict_raw,
  -- 哨兵还原成 SQL 的未知：NULL 与任何字符串都可区分，也不会被读成 pending。
  CASE WHEN f.verdict_raw = '?unknown' THEN NULL ELSE f.verdict_raw END AS verdict,
  coalesce(f.reason, '')                                            AS reason,
  f.last_seq,
  -- 判据前置校验：true/false/NULL（NULL = 缺声明时刻，无法判定）
  CASE
    WHEN f.predicate ? 'declared_at'
     AND f.at IS NOT NULL
     AND (f.predicate->>'declared_at') ~ '^\d{4}-\d{2}-\d{2}'
    THEN ((f.predicate->>'declared_at')::timestamptz <= f.at)
    ELSE NULL
  END AS predicate_declared_before_start
FROM folded f;

-- 运行级派生状态：crew 的运行头没有 status 原值（来源没这个概念），
-- 所以这里由事实折叠；平台侧保留 source_status 原值供对账。
CREATE OR REPLACE VIEW agent_runs.agent_run_state AS
SELECT
  r.run_key, r.driver, r.source, r.run_id,
  r.started_at, r.ended_at,
  r.status     AS source_status,
  r.stop_reason, r.retention_class, r.projected_at,
  count(f.task)                                                        AS facts,
  count(*) FILTER (WHERE f.verdict = 'committed')                       AS committed,
  count(*) FILTER (WHERE f.verdict = 'failed')                          AS failed,
  count(*) FILTER (WHERE f.verdict = 'cancelled')                       AS cancelled,
  count(*) FILTER (WHERE f.verdict = 'pending')                         AS pending,
  count(*) FILTER (WHERE f.verdict IS NULL)                             AS unknown_verdict,
  CASE
    WHEN count(f.task) = 0                                   THEN 'unknown'
    WHEN count(*) FILTER (WHERE f.verdict IS NULL) > 0        THEN 'unknown'
    WHEN count(*) FILTER (WHERE f.verdict = 'pending') > 0    THEN 'open'
    WHEN count(*) FILTER (WHERE f.verdict IN ('failed','cancelled')) > 0 THEN 'failed'
    ELSE 'committed'
  END AS derived_status
FROM agent_runs.agent_run r
LEFT JOIN agent_runs.agent_run_fact f ON f.run_key = r.run_key
GROUP BY r.run_key, r.driver, r.source, r.run_id, r.started_at, r.ended_at,
         r.status, r.stop_reason, r.retention_class, r.projected_at;
