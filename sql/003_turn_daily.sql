-- 日汇总：cell 侧 `member_turn_daily` 的镜像。
--
-- 为什么要在库里再存一份「已经是汇总」的东西：
--   1. 它是**完整性基准**（design D6）。cell 的明细会被剪枝，而这张表在 cell 侧是
--      PERMANENT 的，所以「明细归档漏了多少」只能靠它来对账。
--   2. 长期趋势不能依赖明细：明细按 windowed 分级保留，3 年前的趋势只有这里有。

CREATE TABLE IF NOT EXISTS agent_runs.agent_turn_daily (
  driver          text NOT NULL,
  source          text NOT NULL,
  fleet_id        text NOT NULL,
  member_id       text NOT NULL,
  -- 这三个在来源里可为 NULL；主键不接受 NULL，所以归一成空串并在注释里说明。
  carrier         text NOT NULL DEFAULT '',
  provider        text NOT NULL DEFAULT '',
  model           text NOT NULL DEFAULT '',
  day             text NOT NULL,          -- 来源的 UTC 日期文本，原样
  turns           integer NOT NULL DEFAULT 0,
  ok_count        integer NOT NULL DEFAULT 0,
  failed_count    integer NOT NULL DEFAULT 0,
  skipped_count   integer NOT NULL DEFAULT 0,
  retention_class text NOT NULL DEFAULT 'permanent'
                  REFERENCES agent_runs.retention_policy(retention_class),
  absorbed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (driver, source, fleet_id, member_id, carrier, provider, model, day)
);
CREATE INDEX IF NOT EXISTS idx_turn_daily_day ON agent_runs.agent_turn_daily (day);
CREATE INDEX IF NOT EXISTS idx_turn_daily_member ON agent_runs.agent_turn_daily (source, member_id, day);
