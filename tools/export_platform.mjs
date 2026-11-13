#!/usr/bin/env node
/**
 * 把一个租户 cell 的运行台账导出成 JSONL，交给 `fd_agent_run.cli project` 投影。
 *
 * 为什么要进仓：这段脚本此前只存在于某个 pod 的 /tmp 里（一次性 in-pod Node 脚本），
 * 是整条链上**唯一没有出处**的环节。它的行为直接决定库里的数，却没人能复核、
 * 也没人能在换机后重放。2026-11-14 把它的契约固定在这里。
 *
 * 用法（在 platform pod 内跑；DB 是 hostPath /opt/platform，只读打开）：
 *
 *   node export_platform.mjs --cell 1da519d4c1c0bf89 > /tmp/cell.jsonl
 *   node export_platform.mjs --cell <hex> --db /path/fleets.db --out /tmp/x.jsonl
 *
 * 输出契约（与 fd_agent_run/projector.py::read_export 对齐）：
 *   {"k":"meta","cell":"<hex>","exported_at":...,"counts":{...}}   ← 一行，最先
 *   {"k":"run","run_id":...,"started_at":ms,"ended_at":ms,"status":...,"stop_reason":...}
 *   {"k":"turn","id":...,"run_id":..., "started_at":ms, ...整行}
 *   {"k":"daily","fleet_id":...,"member_id":...,"carrier":...,"provider":...,
 *    "model":...,"day":"YYYY-MM-DD","turns":...,"ok_count":...,...}
 *
 * 两条硬规矩：
 *
 * 1. **不截断**。按成员或按天设行数上限看起来无害，但投影端是 upsert：少导一行
 *    就会把「永久镜像」覆盖成一个更小的数，而这张永久表正是归档完整性的基准。
 *    2026-11-14 实测 2026-11-03 有 16 个回合在 cell 侧丢了（日汇总少于明细），
 *    当时第一怀疑对象就是导出截断——所以这里明确不设上限，宁可导慢。
 * 2. **只读 + 流式**。`readonly: true` 保证不改源库；逐行 `iterate()` 写出，
 *    6,693 个 run / 7,512 个 turn 也不把整表读进内存。
 */
import { createWriteStream } from "node:fs";
import { createRequire } from "node:module";

// ESM 不认 NODE_PATH，`import "better-sqlite3"` 在镜像里解析不到；
// 用 createRequire 从应用根解析，pod 内 /app 就是 app 根。
const requireFromApp = createRequire("/app/");
const Database = requireFromApp("better-sqlite3");

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

const cell = arg("cell");
if (!cell) {
  console.error("FATAL: --cell <hex> 必填（tenant 标识，投影端用它算 source）");
  process.exit(2);
}
const dbPath = arg("db", `/data/cells/${cell}/data/data/fleets.db`);
const outPath = arg("out");
const since = arg("since"); // 可选 YYYY-MM-DD，仅按 started_at 过滤 turn

const db = new Database(dbPath, { readonly: true, fileMustExist: true });
db.pragma("query_only = true");

const out = outPath ? createWriteStream(outPath) : process.stdout;
const emit = (obj) => out.write(JSON.stringify(obj) + "\n");

const counts = {
  runs: db.prepare("select count(*) n from fleet_runs").get().n,
  turns: db.prepare("select count(*) n from member_turn_events").get().n,
  daily: db.prepare("select count(*) n from member_turn_daily").get().n,
};

emit({
  k: "meta",
  cell,
  exported_at: new Date().toISOString(),
  source_db: dbPath,
  since: since || null,
  counts,
  // 明确写下「没有截断」，让读导出的人不必猜
  truncated: false,
});

for (const row of db.prepare(
  `select run_id, started_at, ended_at, status, stop_reason from fleet_runs`
).iterate()) {
  emit({ k: "run", ...row });
}

const turnSql = since
  ? `select * from member_turn_events
     where date(started_at / 1000, 'unixepoch') >= ?
     order by id`
  : `select * from member_turn_events order by id`;
for (const row of db.prepare(turnSql).iterate(...(since ? [since] : []))) {
  emit({ k: "turn", ...row });
}

for (const row of db.prepare(`select * from member_turn_daily order by day, member_id`).iterate()) {
  emit({ k: "daily", ...row });
}

if (outPath) {
  out.end(() => {
    console.error(
      `exported cell=${cell} runs=${counts.runs} turns=${counts.turns} daily=${counts.daily} -> ${outPath}`
    );
  });
} else {
  console.error(
    `exported cell=${cell} runs=${counts.runs} turns=${counts.turns} daily=${counts.daily} -> stdout`
  );
}
