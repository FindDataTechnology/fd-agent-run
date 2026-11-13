-- agent_runs: 跨 driver 的智能体运行事件库（见 openspec/changes/agent-run-store-and-standard-board）
--
-- 设计要点（与 design.md 的 D1-D10 对应）：
--   * 台账是唯一真源，本 schema 是可丢弃的投影（D1）。删掉整个 schema 重跑投影器即可重建。
--   * (run_key, seq) + content_hash 是幂等键（D2）。同 seq 同 hash 跳过；同 seq 异 hash 必须失败，不覆盖。
--   * status 存来源原值，不折算（D3）。统一裁决在读侧的视图里算。
--   * facts 是视图，不是表（D4）。
--   * retention_class 是列，不是注释（D5）。
--
-- 幂等：本文件可重复执行。所有对象用 IF NOT EXISTS，策略与注册表用 ON CONFLICT 更新。

CREATE SCHEMA IF NOT EXISTS agent_runs;

-- ── 保留分级策略 ─────────────────────────────────────────────────────────────
-- 分级 → 期限与最少保留条数。判据只认这一张表，不认表名（D5）：
-- 下一个新表只要带 retention_class 列就自动受同一套规则约束。
CREATE TABLE IF NOT EXISTS agent_runs.retention_policy (
  retention_class      text PRIMARY KEY,
  period_days          integer,          -- NULL = 永久
  min_keep_per_holder  integer NOT NULL DEFAULT 0,
  note                 text NOT NULL DEFAULT ''
);

INSERT INTO agent_runs.retention_policy (retention_class, period_days, min_keep_per_holder, note) VALUES
  ('permanent', NULL, 0, '运行头与日汇总：不删'),
  ('windowed',  90, 500, '明细：超 90 天 且 超每个承载者最近 500 条才删')
ON CONFLICT (retention_class) DO UPDATE
  SET period_days = EXCLUDED.period_days,
      min_keep_per_holder = EXCLUDED.min_keep_per_holder,
      note = EXCLUDED.note;

-- ── 承载者注册表 ─────────────────────────────────────────────────────────────
-- 为什么需要一张注册表：「从未成功投影」不可能从「只有投影结果」的表里推出来。
-- 覆盖面板的左表必须是这份声明表，否则它恰好漏掉自己存在的那个理由。
CREATE TABLE IF NOT EXISTS agent_runs.agent_source (
  driver                 text NOT NULL,               -- crew | platform
  source                 text NOT NULL,               -- project:<repo>@<host> | cell:<16位租户id>
  label                  text NOT NULL DEFAULT '',
  expected_period_seconds integer,                    -- 超过即判「投影落后」；NULL = 不判
  enabled                boolean NOT NULL DEFAULT true,
  registered_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (driver, source)
);

-- ── 运行头 ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_runs.agent_run (
  run_key         text PRIMARY KEY,
  driver          text NOT NULL,
  source          text NOT NULL,
  run_id          text NOT NULL,
  started_at      timestamptz,
  ended_at        timestamptz,
  status          text,                               -- 来源原值（ok/failed/interrupted/skipped/... 或 crew 的终态）
  stop_reason     text,                               -- 平台侧 interrupted 的分流依据
  retention_class text NOT NULL DEFAULT 'permanent'
                  REFERENCES agent_runs.retention_policy(retention_class),
  projected_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (driver, source, run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_source ON agent_runs.agent_run (driver, source, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_run_status ON agent_runs.agent_run (status, started_at);

-- ── 只追加明细 ───────────────────────────────────────────────────────────────
-- raw 保留原始字节：内容指纹本来就要求它，且「保真」不能靠 jsonb 往返。
CREATE TABLE IF NOT EXISTS agent_runs.agent_run_event (
  run_key         text NOT NULL REFERENCES agent_runs.agent_run(run_key) ON DELETE CASCADE,
  seq             bigint NOT NULL,
  kind            text NOT NULL,
  at              timestamptz,
  payload         jsonb NOT NULL,
  raw             text NOT NULL,
  content_hash    text NOT NULL,
  retention_class text NOT NULL DEFAULT 'windowed'
                  REFERENCES agent_runs.retention_policy(retention_class),
  absorbed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_event_at ON agent_runs.agent_run_event (at);
CREATE INDEX IF NOT EXISTS idx_agent_run_event_kind ON agent_runs.agent_run_event (kind, at);

-- ── 投影水位 ─────────────────────────────────────────────────────────────────
-- 水位超出源实际长度 → 判失效并全量重放，不报错（与快照水位同一语义）。
CREATE TABLE IF NOT EXISTS agent_runs.agent_run_projection (
  driver         text NOT NULL,
  source         text NOT NULL,
  run_id         text NOT NULL,
  last_seq       bigint NOT NULL DEFAULT 0,
  last_hash      text,
  absorbed_count integer NOT NULL DEFAULT 0,
  status         text NOT NULL DEFAULT 'ok',          -- ok | error | invalidated
  last_error     text,
  absorbed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (driver, source, run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_run_projection_src ON agent_runs.agent_run_projection (driver, source, absorbed_at);
