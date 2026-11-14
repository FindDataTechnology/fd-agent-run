-- 归档完整性：口径、分诊、以及为什么必须分诊
--
-- 分母是「cell 的永久日汇总」，分子是「库里该日的回合事件数」。两者不等就是缺口。
-- 但**不能把所有不等都叫缺口**——实测（2026-11-13，16 个租户 cell）分出四类，
-- 其中三都不是缺陷，混在一起报就会变成一条常年红的曲线，然后被无视：
--
--   not_enabled        该日早于**日汇总功能上线日**（全体 cell 最早的日汇总日）
--                      → 例：10-23..10-30 共 738 个回合，一行汇总都没有
--   partial_enable_day 恰好等于上线那天：汇总从当天中途开始写
--                      → 实测残差全部落在 10-31，且方向一律 daily < events
--   late_start         上线日之后、但早于**本 cell** 自己第一行日汇总
--                      → 这一类比 not_enabled 严重：功能已经在跑，这个 cell 却没记
--   daily_absent       有回合事件，但该 cell 从来没有过任何日汇总
--                      → 最坏的一类（整表缺席），必须单独可见，不能被过滤掉
--   archive_gap        上线之后仍不等 —— 这才是真正的归档缺口
--
-- ── 为什么「上线日」不能按 cell 自己算（2026-11-14 修）────────────────────
-- 原判据是 `enabled_from = min(day) WHERE source = 本 cell`，**自指**：一个 cell 若从
-- 第一天就在漏记、到第 N 天才恢复，它前面那些天会被永久标成 not_enabled——「一直漏」
-- 长得和「还没上线」一模一样，而闸是绿的。实测 cell 6db2b1b4b490223b 的日汇总从
-- 11-04 才有，而全体 cell 的上线日是 10-31（当前暴露量仅 10-31 那 1 个回合，很小，
-- 但这是一条会随时间撒谎的判据）。现在上线日取**全体最早**，本 cell 晚于它的部分
-- 单独记 late_start 并计入缺陷。
--
-- 另有一类方向相反的 daily_exceeds_events：汇总比明细多，通常意味着明细被剪枝过，
-- 或汇总被重复累加。它是 completeness 的镜像缺陷，同样不该静默。
--
-- ── 归日口径（2026-11-14 修）──────────────────────────────────────────────
-- 事件的日必须与**日汇总的日**同一口径，否则量到的是口径差而不是缺口。
-- cell 侧的日汇总按 `startedAt` 的 UTC 日归口（paas/server/routes/fleets-db.js:1694
--   `const day = new Date(event.startedAt).toISOString().slice(0, 10)`），
-- 而本库 `agent_run_event.at` 存的是**回合结束时刻**（projector.py 取 `ended_at`）。
-- 一个 23:59 开始、00:01 结束的回合，在两边落到相邻两天 —— 表现为「一天 -1、
-- 次日 +1」成对出现、净为零。
--
-- 实测（2026-11-14，34 个缺口格）：按 `started_at` 归日后 **23 格与日汇总完全相等**，
-- 即那 23 格从来不是丢数。所以这里改按 payload 的 `started_at` 归日，缺失时回退到 `at`。
-- 剩下的 11 格集中在 2026-11-03，按两种口径都短 1–2，属于另一件事（疑为导出侧的
-- 每成员行数上限截断），不在本视图处理。

DROP VIEW IF EXISTS agent_runs.agent_run_gap_summary;
DROP VIEW IF EXISTS agent_runs.agent_run_gap;
DROP VIEW IF EXISTS agent_runs.agent_run_ship_day;

-- 日汇总功能的**上线日**，单列一张视图，让 gap 视图与体检闸共用同一份定义
-- （两处各写一遍必然漂移）。
--
-- 取法：在「有实质日汇总历史（> 30 天）的 cell」里取最早的一天。
-- 三种取法各自为什么不行（2026-11-14 实测）：
--   · 本 cell 自己最早一行 —— **自指**：cell 若从第一天就漏记、第 N 天才恢复，
--     前面那些天永久读成 not_enabled，「一直漏」长得像「还没上线」，闸还是绿的。
--   · 全体最早（min(day) 不分 cell）—— 被补建的空壳 cell 拽跑：420a3344 只有 3 天、
--     de7ec3a6 只有 1 天日汇总（都是我按明细补出来的），它们把上线日拉回 10-24，
--     于是两个活跃 cell 在 10-24..10-30「功能确实还没上」的 1274 个回合
--     一次性翻成缺陷 —— 纯假红。
--   · 写成常量 —— 会随下一次上线过期，且没人知道该改。
-- 证据（本部署）：两个活跃 cell 的日汇总都恰好从 2026-10-31 起、之前一行都没有；
-- 空壳 cell 的 10-24/10-25 是事后补建，不代表功能在跑。
CREATE OR REPLACE VIEW agent_runs.agent_run_ship_day AS
SELECT COALESCE(
         (SELECT min(first_day) FROM (
            SELECT source, min("day") AS first_day
            FROM agent_runs.agent_turn_daily
            GROUP BY 1
            HAVING count(*) > 30
          ) substantial),
         (SELECT min("day") FROM agent_runs.agent_turn_daily)
       ) AS ship_day;

CREATE OR REPLACE VIEW agent_runs.agent_run_gap AS
WITH ev AS (
  SELECT r.source,
         e.payload->>'member_id' AS member_id,
         -- 与 cell 日汇总同口径：started_at 的 UTC 日；缺字段时回退到 at。
         to_char((
           COALESCE(
             to_timestamp(
               CASE WHEN (e.payload->>'started_at') ~ '^[0-9]{10,}$'
                    THEN ((e.payload->>'started_at')::bigint) / 1000.0
               END
             ),
             e.at
           ) AT TIME ZONE 'UTC'
         )::date, 'YYYY-MM-DD') AS d,
         count(*) AS n
  FROM agent_runs.agent_run_event e
  JOIN agent_runs.agent_run r USING (run_key)
  WHERE r.driver = 'platform' AND e.kind = 'turn'
    AND ((e.payload->>'started_at') IS NOT NULL OR e.at IS NOT NULL)
  GROUP BY 1, 2, 3
), dd AS (
  SELECT source, member_id, "day" AS d, sum(turns)::int AS turns
  FROM agent_runs.agent_turn_daily
  GROUP BY 1, 2, 3
), en AS (
  SELECT source, min("day") AS enabled_from
  FROM agent_runs.agent_turn_daily
  GROUP BY 1
), pairs AS (
  SELECT coalesce(dd.source, ev.source)       AS source,
         coalesce(dd.member_id, ev.member_id) AS member_id,
         coalesce(dd.d, ev.d)                 AS d,
         coalesce(dd.turns, 0)                AS daily_turns,
         coalesce(ev.n, 0)                    AS events
  FROM dd
  FULL OUTER JOIN ev
    ON dd.source = ev.source AND dd.member_id = ev.member_id AND dd.d = ev.d
)
SELECT
  p.source, p.member_id, p.d AS day,
  p.daily_turns, p.events,
  p.daily_turns - p.events AS gap,
  CASE
    WHEN en.enabled_from IS NULL              THEN 'daily_absent'
    WHEN p.d <  (SELECT ship_day FROM agent_runs.agent_run_ship_day)
                                              THEN 'not_enabled'
    WHEN p.d =  (SELECT ship_day FROM agent_runs.agent_run_ship_day)
                                              THEN 'partial_enable_day'
    WHEN p.d <  en.enabled_from               THEN 'late_start'
    WHEN p.daily_turns < p.events             THEN 'archive_gap'
    ELSE                                           'daily_exceeds_events'
  END AS attribution
FROM pairs p
LEFT JOIN en ON en.source = p.source
WHERE p.daily_turns <> p.events;

-- 板面直接读这张：按 cell 与分诊聚合，只把 archive_gap / daily_absent /
-- daily_exceeds_events / late_start 计为需要处理的量；not_enabled 与
-- partial_enable_day 只作为事实展示（连同起始日），不计入缺陷。
CREATE OR REPLACE VIEW agent_runs.agent_run_gap_summary AS
SELECT
  g.source,
  g.attribution,
  count(*)      AS pairs,
  sum(abs(g.gap)) AS abs_gap,
  min(g.day)    AS from_day,
  max(g.day)    AS to_day,
  (SELECT min("day") FROM agent_runs.agent_turn_daily d WHERE d.source = g.source) AS enabled_from,
  g.attribution IN ('archive_gap', 'daily_absent', 'daily_exceeds_events', 'late_start') AS is_defect
FROM agent_runs.agent_run_gap g
GROUP BY g.source, g.attribution;
