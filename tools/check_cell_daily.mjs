#!/usr/bin/env node
/**
 * cell 侧自证：`member_turn_daily`（永久日汇总）与 `member_turn_events`（明细）
 * 按日是否一致。只读、在 platform pod 内跑。
 *
 *   kubectl -n fd-prod exec <platform-pod> -c platform -- \
 *     node /tmp/check_cell_daily.mjs --cell 1da519d4c1c0bf89
 *
 * 为什么需要它：cell 的日汇总写入是
 *   try { INSERT … ON CONFLICT DO UPDATE turns = turns + 1 }
 *   catch (e) { meteringWarnOnce(`daily rollup failed …`) }
 * —— 异常被吞、告警按消息去重只留一行、没有计数也没有重试。事件行照样写成功，
 * 于是「明细有、汇总没有」这种损失**除了事后对账没有任何信号**。
 * 2026-11-14 实测 cell 1da519d4c1c0bf89 的 2026-11-03 少了 16 个回合
 * （11 个成员各 −1/−2，次日无反向补偿 ⇒ 不是归日口径问题，是真丢）。
 *
 * 口径必须与投影端一致：
 *   * 日汇总的 day = `new Date(startedAt).toISOString().slice(0,10)`（UTC 日）
 *     —— paas/server/routes/fleets-db.js 的日汇总写入就是这条式子；
 *   * 明细按 `started_at`（整数毫秒）同一个 UTC 日归口，
 *     **不能**用 ended_at，否则跨日界的回合会被算成缺口（那是口径差不是丢数）。
 *
 * 退出码：0 = 无缺口；1 = 有缺口（逐条打印成员与差额）；2 = 用法/读库错误。
 * 汇总方式对齐 `sql/005_completeness.sql`：日汇总按 (fleet, member, carrier,
 * provider, model, day) 多行，比较前按 (member, day) 求和。
 */
import { createRequire } from "node:module";

const requireFromApp = createRequire("/app/");
const Database = requireFromApp("better-sqlite3");

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

const cell = arg("cell");
if (!cell) {
  console.error("FATAL: --cell <hex> 必填");
  process.exit(2);
}
const dbPath = arg("db", `/data/cells/${cell}/data/data/fleets.db`);
const since = arg("since");
const until = arg("until");
const verbose = process.argv.includes("--verbose");

let db;
try {
  db = new Database(dbPath, { readonly: true, fileMustExist: true });
  db.pragma("query_only = true");
} catch (e) {
  console.error(`FATAL: 打不开 ${dbPath}: ${e.message}`);
  process.exit(2);
}

const range = [];
const params = [];
if (since) {
  range.push("d >= ?");
  params.push(since);
}
if (until) {
  range.push("d <= ?");
  params.push(until);
}
const where = range.length ? `where ${range.join(" and ")}` : "";

const evRows = db.prepare(`
  select member_id m, date(started_at / 1000, 'unixepoch') d, count(*) n
  from member_turn_events
  group by 1, 2
`).all();

const dlRows = db.prepare(`
  select member_id m, day d, sum(turns) n
  from member_turn_daily
  group by 1, 2
`).all();

const enabledFrom = db.prepare(`select min(day) d from member_turn_daily`).get()?.d || null;

const ev = new Map(evRows.map((r) => [`${r.m}|${r.d}`, r.n]));
const dl = new Map(dlRows.map((r) => [`${r.m}|${r.d}`, r.n]));
const keys = new Set([...ev.keys(), ...dl.keys()]);

const perDay = new Map();
const rows = [];
for (const key of keys) {
  const [m, d] = key.split("|");
  if (since && d < since) continue;
  if (until && d > until) continue;
  const e = ev.get(key) || 0;
  const v = dl.get(key) || 0;
  if (e === v) continue;
  rows.push({ member: m, day: d, events: e, daily: v, diff: v - e });
  const acc = perDay.get(d) || { events: 0, daily: 0, members: 0 };
  acc.events += e;
  acc.daily += v;
  acc.members += 1;
  perDay.set(d, acc);
}

console.log(`cell=${cell}  日汇总起始日=${enabledFrom || "（无日汇总）"}  库=${dbPath}`);
console.log(`对账：${keys.size} 个 (member, day) 组合，不等 ${rows.length} 个`);

for (const d of [...perDay.keys()].sort()) {
  const a = perDay.get(d);
  const tag = d === enabledFrom ? "  ← 上线当天（部分启用，不算缺陷）"
    : d < (enabledFrom || "") ? "  ← 早于上线（日汇总本就没有）" : "";
  // 注意：这里是**有差异的成员**的合计，不是当天全量。写清楚，否则会被读成日总量。
  console.log(`  ${d}  不等成员合计 events=${a.events} daily=${a.daily}`
    + ` diff=${a.daily - a.events} (${a.members} 个成员)${tag}`);
  if (verbose) {
    for (const r of rows.filter((x) => x.day === d)) {
      console.log(`      ${r.member}  events=${r.events} daily=${r.daily} diff=${r.diff}`);
    }
  }
}

// 缺陷 = 上线之后、且不是上线当天，日汇总少于明细（真丢）。
// 反方向（daily > events）单独提示：那意味着明细被剪枝过，不是本工具的判据。
const defects = rows.filter((r) => r.day > (enabledFrom || "") && r.diff < 0);
const surplus = rows.filter((r) => r.day > (enabledFrom || "") && r.diff > 0);

if (surplus.length) {
  console.log(`\n提示：${surplus.length} 个组合 daily > events（明细被剪枝，或归日口径仍有差异），不计缺陷`);
}
if (defects.length) {
  console.log(`\nFAIL 上线后仍丢 ${defects.length} 个 (member, day) 组合，共 ${defects.reduce((a, r) => a + -r.diff, 0)} 个回合：`);
  for (const r of defects.sort((a, b) => a.day.localeCompare(b.day))) {
    console.log(`  ✗ ${r.day}  ${r.member}  events=${r.events} daily=${r.daily} 少 ${-r.diff}`);
  }
  console.log("\n修法：日汇总可以从明细重建（同一口径 group by member, UTC 日 of started_at）；");
  console.log("      根因未明前不要自动重算——先看是否还有 daily rollup failed 的告警。");
  process.exit(1);
}
console.log("\nOK 上线之后每 (member, day) 的日汇总与明细一致");
process.exit(0);
