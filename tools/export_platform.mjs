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
// 所以：给了 `--after-id` 就只用它；没给（首次/回填）就全量导。
const turnFilter = () => {
  if (afterId) return { sql: "id > ?", params: [Number(afterId)], by: "after-id" };
  return { sql: null, params: [], by: "full" };
};
const turn = turnFilter();

// 日汇总**不再按日历收窄**。原先的理由是「upsert 按 day 覆盖，所以只导最近几天是安全的」
// ——幂等成立，但日历窗口意味着「镜像能覆盖哪些天」取决于**每次跑批时它有多新**，
// 而不是取决于台账里有什么。今天是齐的（实测 `cell:1da5` 镜像最早日 = cell 最早日 =
// 2026-10-31），一旦遇到重建镜像、接入历史很久的新 cell、或跑批停过一段，
// 窗口外的天就再也不会补上，而那张表正是归档缺口分析的基数——缺的行会被读成
// 「cell 侧没有日汇总」，一个由导出窗口造出来的假缺陷。日汇总很小（最大 cell 4.6k 行），
// 全量导的代价可忽略，所以宁可不留这个前提。
//
// 注意**行数不会相等，也不该相等**：这张表按 (fleet, member, carrier, provider, model, day)
// 聚合，cell 侧 NULL 维度在那边是不同的行、在这里合并成一行。要验完整性就用
// `sum(turns)` 与 `min/max(day)`，不要数行数（我第一版就是数行数，误报过一次）。
const dailySql = `select * from member_turn_daily order by day, member_id`;
const turnSql = `select * from member_turn_events
                 ${turn.sql ? `where ${turn.sql}` : ""}
                 order by id`;

if (since) {
  console.error(
    `note: --since ${since} 已不再收窄任何表（明细按水位、日汇总全量）。` +
    `日历窗口造出过两种静默缺陷：连挂两天丢第一天的新行、永久镜像缺历史日汇总。`
  );
}

// `--explain`：只报「这次会按哪个判据导什么」，不碰库、不导数据。
// 存在的理由是可测：本机没有 better-sqlite3，没有这个出口就只能靠读代码相信判据。
if (process.argv.includes("--explain")) {
  console.log(JSON.stringify({
    cell, db_path: dbPath, since: since || null, after_id: afterId ? Number(afterId) : null,
    turn_filter_by: turn.by, turn_sql: turnSql, turn_params: turn.params,
    daily_sql: dailySql, daily_params: [], daily_filter_by: "full",
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
  partial: Boolean(afterId),
  // `--since` 曾经收窄过明细与日汇总，两种形态都造出过静默缺失；现在它只是一个
  // 兼容参数，记下来是为了让复核的人知道「这份导出的过滤与应用无关」。
  since_ignored: Boolean(since),
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
for (const row of db.prepare(dailySql).iterate()) {
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
