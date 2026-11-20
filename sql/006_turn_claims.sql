-- agent_turn_claim: "this attempt existed", projected from the cell's turn_claims
-- (harden-agent-runtime-spine, agent-turn-claim + agent-run-store MODIFIED).
--
-- WHY THIS IS ITS OWN TABLE, NOT COLUMNS ON agent_run_event:
--   a claim is written at START and MUTATED while it lives (heartbeat, terminal
--   close). agent_run_event is append-only and its idempotency rule is
--   "(run_key, seq) + content_hash, different hash ⇒ refuse". Folding a mutable
--   row into it would either replay as a rewrite rejection or lose the beat —
--   the same two-sided trap the cell side solves by keeping the tables apart.
--
-- ONE ROW PER ATTEMPT, upserted from the cell's current picture. Heartbeats do
-- NOT produce rows: the reducer keeps the latest beat, so a 10-minute turn is
-- still one row (pinned by scripts/test-turn-claim-projection.py-style checks
-- in the operator's suite).
--
-- Retention: PERMANENT. This is the only record that an attempt ever existed;
-- pruning it would silently recreate the "no row = never happened" ambiguity
-- the whole capability exists to remove.

CREATE TABLE IF NOT EXISTS agent_runs.agent_turn_claim (
  turn_ref             text PRIMARY KEY,
  driver               text NOT NULL,
  source               text NOT NULL,
  run_id               text NOT NULL,
  turn_id              text NOT NULL,
  fleet_id             text,
  member_id            text,
  session_id           text,
  carrier              text,
  carrier_instance     text,
  started_at           timestamptz,
  heartbeat_at         timestamptz,
  lease_expires_at     timestamptz,
  closed_at            timestamptz,
  close_reason         text,
  cancelled_by         text,
  runtime_fingerprint  text,
  projected_at         timestamptz NOT NULL DEFAULT now(),
  retention_class      text NOT NULL DEFAULT 'permanent'
                       REFERENCES agent_runs.retention_policy(retention_class)
);
CREATE INDEX IF NOT EXISTS idx_turn_claim_run ON agent_runs.agent_turn_claim (driver, source, run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_turn_claim_open ON agent_runs.agent_turn_claim (closed_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_turn_claim_fingerprint ON agent_runs.agent_turn_claim (runtime_fingerprint);

-- Projection watermarks, one row per SOURCE and per LANE. The lane column is
-- what keeps "results are current" from implying "claims are current": the two
-- ride different cursors (append-only id vs updated_at), and sharing a
-- watermark would make one side's advance read as the other's gap.
CREATE TABLE IF NOT EXISTS agent_runs.agent_claim_projection (
  driver        text NOT NULL,
  source        text NOT NULL,
  lane          text NOT NULL,          -- 'claim' | 'turn' | 'daily'
  last_cursor   bigint,                 -- claim: max(updated_at); turn: max(id)
  absorbed_count integer NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'ok',
  last_error    text,
  absorbed_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (driver, source, lane)
);
