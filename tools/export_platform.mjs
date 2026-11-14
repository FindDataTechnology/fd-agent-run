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
// **延迟到真要开库时再 require**：`--explain` 要在没有任何 npm 依赖的机器上
// （比如本机 Python 测试里）也能跑，判据才可能被真正测到。
const requireFromApp = createRequire("/app/");
function openDb(path) {
  const Database = requireFromApp("better-sqlite3");
  const db = new Database(path, { readonly: true, fileMustExist: true });
  db.pragma("query_only = true");
  return db;
}

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
const since = arg("since");       // 起始日（YYYY-MM-DD），按 started_at 的 UTC 日
const afterId = arg("after-id");  // 只导 id > N 的明细：增量刷新用，见下

// ── 两把过滤器谁说了算（这是本文件唯一的判据，单独抽出来以便复核）──────────
// **水位优先，时钟窗口退位**。原因是一条真实的丢数路径：
// 驱动器每次都传 `--since 昨天`，而 N 个 cell 连挂两天时，第一天的新行在第三天
// 的窗口外 ⇒ 永久跳过，且没有任何判据会红（导出照常成功、投影照常 absorbed=0）。
// 而 `id > after` 本身就是「哪些行还没吸收」的精确答案，不需要日历帮忙。
// 所以：给了 `--after-id` 就只用它；没给（首次/回填）才按 `--since` 收窄。
const turnFilter = () => {
  if (afterId) return { sql: "id > ?", params: [Number(afterId)], by: "after-id" };
  if (since) {
    return {
      sql: "date(started_at / 1000, 'unixepoch') >= ?",
      params: [since],
      by: "since",
    };
  }
  return { sql: null, params: [], by: "full" };
};
const turn = turnFilter();
// 日汇总走 upsert（按 day 覆盖），日历窗口在这里是安全的省流量，保留。
const dailySql = since
  ? `select * from member_turn_daily where day >= ? order by day, member_id`
  : `select * from member_turn_daily order by day, member_id`;
const turnSql = `select * from member_turn_events
                 ${turn.sql ? `where ${turn.sql}` : ""}
                 order by id`;

// `--explain`：只报「这次会按哪个判据导什么」，不碰库、不导数据。
// 存在的理由是可测：本机没有 better-sqlite3，没有这个出口就只能靠读代码相信判据。
if (process.argv.includes("--explain")) {
  console.log(JSON.stringify({
    cell, db_path: dbPath, since: since || null, after_id: afterId ? Number(afterId) : null,
    turn_filter_by: turn.by, turn_sql: turnSql, turn_params: turn.params,
    daily_sql: dailySql, daily_params: since ? [since] : [],
    partial: Boolean(since || afterId),
  }, null, 1));
  process.exit(0);
}

const db = openDb(dbPath);


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
  after_id: afterId ? Number(afterId) : null,
  counts,
  // 明确写下「没有截断」，让读导出的人不必猜
  truncated: false,
  // 过滤过就是「部分」：投影端据此跳过「水位 > 台账长度」的失效判据，
  // 并且不给本次没有新行的 run 写水位（那会被读成「从没见过」）。
  partial: Boolean(since || afterId),
  // 明细到底按哪把判据导的（after-id / since / full）。写进导出本身，
  // 是因为 stderr 上的提示只在失败时才被人看见——而「这份导出跳过了一部分行」
  // 这件事，事后复核的人必须能从产物里读出来，不能靠猜。
  turn_filter_by: turn.by,
});

for (const row of db.prepare(
  `select run_id, started_at, ended_at, status, stop_reason from fleet_runs`
).iterate()) {
  emit({ k: "run", ...row });
}

// ── 为什么必须有 --after-id ───────────────────────────────────────────────
// 投影器对同一 `(run_key, seq)` 的判据是「内容指纹相同则跳过、不同则拒绝」。
// 而指纹算的是**整行**，于是表结构一旦加列（实测 2026-11-14：cell 的行从 38 键
// 变成 40 键，多出 trace_turn_id / usage_input_max），**历史上每一行都变成
// 「与已吸收的不同」**，重发即被拒绝。这不是数据被改写，是契约与 schema 演进相撞。
// 所以增量刷新必须按自增 id 只取**新行**，绝不重发老行：老行在库里是当时那份快照，
// 让它保持原样，比用新形状覆盖它更诚实。
//
// 判据（过滤器怎么选）在文件上半部的 `turnFilter()` 里，`--explain` 可以直接看。
if (turn.by === "after-id" && since) {
  // 说清楚谁生效，免得有人以为日历窗口还在兜底。
  console.error(
    `note: 传了 --after-id ${afterId}，明细按水位导（id > ${afterId}）；` +
    `--since ${since} 只作用于日汇总——时钟窗口不是「哪些行还没吸收」的答案，` +
    `连挂两天时它会静默跳过第一天。`
  );
}
for (const row of db.prepare(turnSql).iterate(...turn.params)) {
  emit({ k: "turn", ...row });
}

// 日汇总也按 --since 收窄：增量刷新时没必要每次搬全部历史。
// 投影端是 upsert（按 (driver, source, fleet, member, carrier, provider, model, day)
// 求聚后覆盖），所以只导最近几天是安全的、幂等的。
for (const row of db.prepare(dailySql).iterate(...(since ? [since] : []))) {
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
