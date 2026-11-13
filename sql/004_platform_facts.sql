-- 平台侧事实视图：一次回合（turn）一行，与 crew 的事实并到同一张统一视图。
--
-- 三条口径（对应 design D3 与 crosswalk）：
--   * 入库存来源原值（`status`），折算只在这里做 —— 映射表改了只需重建视图。
--   * `skipped` 不产生事实（它是尝试的缺席），因此被 WHERE 排除。
--   * 未覆盖的 status 落 NULL（未知），不是 guessed 成 pending。
--
-- 已知偏差：历史数据的 `stop_reason` 多为空，于是 `interrupted` 一律判 failed。
-- 这是保守方向（宁可把主动停止算成失败，也不把崩溃算成成功），板面需按
-- 「有/无 stop_reason」分桶让偏差可见。

DROP VIEW IF EXISTS agent_runs.agent_run_fact_all;
DROP VIEW IF EXISTS agent_runs.agent_run_fact_platform;

CREATE OR REPLACE VIEW agent_runs.agent_run_fact_platform AS
WITH turns AS (
  SELECT
    e.run_key,
    e.seq,
    e.at,
    e.payload,
    r.stop_reason,
    r.started_at  AS run_started_at,
    e.payload->>'fleet_id'  AS fleet_id,
    e.payload->>'member_id' AS member_id,
    e.payload->>'turn_id'   AS turn_id,
    e.payload->>'status'    AS status,
    NULLIF(e.payload->>'error_kind', '') AS error_kind,
    NULLIF(e.payload->>'error', '')      AS error_text,
    NULLIF(e.payload->>'parent_turn_id', '') AS parent_turn_id,
    NULLIF(e.payload->>'edge_kind', '')      AS edge_kind,
    NULLIF(e.payload->>'detail', '')         AS detail,
    row_number() OVER (PARTITION BY e.run_key, e.payload->>'member_id' ORDER BY e.seq) AS attempt
  FROM agent_runs.agent_run_event e
  JOIN agent_runs.agent_run r ON r.run_key = e.run_key
  WHERE e.kind = 'turn' AND r.driver = 'platform'
)
SELECT
  t.run_key,
  coalesce(t.fleet_id, '') || '/' || coalesce(t.member_id, '') || '/' ||
    coalesce(t.turn_id, t.seq::text)                              AS task,
  t.attempt,
  t.at,
  -- owner：member_id 是具体持有者（不是 "agent" 这类角色词）；缺了才是未知。
  CASE WHEN t.member_id IS NULL THEN jsonb_build_object('$unknown', true)
       ELSE to_jsonb(t.member_id) END                             AS owner,
  -- upstreams：边关系给了就有，根回合没有上游这个概念 —— 未知，不是空数组。
  CASE WHEN t.parent_turn_id IS NULL THEN jsonb_build_object('$unknown', true)
       ELSE jsonb_build_array(t.parent_turn_id || coalesce(' [' || t.edge_kind || ']', '')) END
                                                                  AS upstreams,
  -- predicate：回合级没有「派发前声明的完成条件」这一列。对外车道的裁决
  -- (mandateVerdict) 在出口取用、不落在回合行上，所以这里只能记未知。
  jsonb_build_object('$unknown', true)                            AS predicate,
  -- evidence：detail 里是 attribution / presetPath / 预算用量，属于运行元信息，
  -- 不是可独立复核的产物。不拿它冒充证据。
  jsonb_build_object('$unknown', true)                            AS evidence,
  CASE t.status
    WHEN 'ok'          THEN 'committed'
    WHEN 'failed'      THEN 'failed'
    WHEN 'pending'     THEN 'pending'
    WHEN 'interrupted' THEN
      -- 主动中止 ≠ 故障；但只有运行头记了 stop_reason 才分得出来。
      CASE WHEN t.stop_reason IS NULL OR t.stop_reason = '' THEN 'failed'
           ELSE 'cancelled' END
    ELSE NULL                     -- 未覆盖状态：未知
  END                                                             AS verdict,
  CASE
    WHEN t.error_kind IS NOT NULL AND t.error_text IS NOT NULL
      THEN t.error_kind || ': ' || split_part(t.error_text, E'\n', 1)
    WHEN t.error_kind IS NOT NULL THEN t.error_kind
    WHEN t.error_text IS NOT NULL THEN split_part(t.error_text, E'\n', 1)
    WHEN t.detail IS NOT NULL THEN coalesce((t.detail::jsonb)->>'kind', '')
    ELSE ''
  END                                                             AS reason,
  NULL::boolean                                                   AS predicate_declared_before_start,
  t.seq                                                           AS last_seq
FROM turns t
WHERE t.status IS DISTINCT FROM 'skipped';

-- 统一事实面：两种 driver 一张视图，调用方不需要知道自己是哪一种。
CREATE OR REPLACE VIEW agent_runs.agent_run_fact_all AS
SELECT r.driver, r.source, r.run_key, f.task, f.attempt, f.at,
       f.owner, f.upstreams, f.predicate, f.evidence, f.verdict, f.reason,
       f.predicate_declared_before_start
FROM agent_runs.agent_run_fact f
JOIN agent_runs.agent_run r USING (run_key)
UNION ALL
SELECT r.driver, r.source, p.run_key, p.task, p.attempt, p.at,
       p.owner, p.upstreams, p.predicate, p.evidence, p.verdict, p.reason,
       p.predicate_declared_before_start
FROM agent_runs.agent_run_fact_platform p
JOIN agent_runs.agent_run r USING (run_key);
