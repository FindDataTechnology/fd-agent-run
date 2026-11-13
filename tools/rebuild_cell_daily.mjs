#!/usr/bin/env node
/**
 * 从明细重建 cell 的永久日汇总（`member_turn_daily`）——**默认只空跑**。
 *
 *   kubectl -n fd-prod exec <pod> -c platform -- node /tmp/rebuild_cell_daily.mjs \
 *     --cell 1da519d4c1c0bf89            # 空跑：只打印要补什么
 *   … --apply                             # 真写（须显式给）
 *
 * 为什么有这个东西：日汇总的写入路径把异常吞了
 * （`try { INSERT … turns = turns + 1 } catch { meteringWarnOnce(...) }`），
 * 事件行写成功、汇总没记上，运行期零信号。2026-11-14 实测 3 个 cell 共
 * 15 个 (member, day) / 22 个回合缺失。
 *
 * ── 口径（第一版在这里错过，记下来）────────────────────────────────────────
 * 比对与重建都必须按 **(member, day)**，**不能**按 (…, model) 逐键：
 * 同一个回合在明细与汇总里的 `model` 归属可能不同（实测 11-10、11-13 有多条
 * 明细 model=NULL 而汇总是具体型号）。按 model 逐键比会凭空造出几十上百个
 * 「少记」——第一版就是这样报出 434 处，而真实缺口只有 15 处。
 * 这与 `tools/check_cell_daily.mjs`、`sql/005_completeness.sql` 的口径一致。
 *
 * ── 重建方式：整组替换，不是逐行加数 ──────────────────────────────────────
 * 对某个 (member, day)：按明细算出该组应有的每行（含 model 等键），
 * 删掉该组旧行、写入新行。这样 model 归属随明细走，不会把补的数量错记到
 * 某个型号头上。
 *
 * 四条安全约束：
 *  1. **只补不减**：仅当该组「按明细算出的 turns ≥ 已有 turns」时才重建；
 *     反方向通常是明细被剪枝，此时**绝不碰**（用被剪枝的明细覆盖永久真相
 *     等于抹掉证据）。
 *  2. **只处理上线之后的日子**：早于日汇总起始日、以及起始日当天（部分启用）
 *     不动，那是口径问题不是缺陷。
 *  3. **一个事务 + 事后自证**：提交后立刻重算，仍不一致就报错退出。
 *  4. **默认空跑**：不加 `--apply` 不写任何东西。
 *
 * 归日口径与写入路径同源：`started_at`（整数毫秒）的 UTC 日。
 */
import { createRequire } from "node:module";

const requireFromApp = createRequire("/app/");
const Database = requireFromApp("better-sqlite3");

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

const cell = arg("cell");
const apply = process.argv.includes("--apply");
const since = arg("since");
const until = arg("until");

if (!cell) {
  console.error("FATAL: --cell <hex> 必填");
  process.exit(2);
}
const dbPath = arg("db", `/data/cells/${cell}/data/data/fleets.db`);

let db;
try {
  db = new Database(dbPath, { readonly: !apply, fileMustExist: true });
} catch (e) {
  console.error(`FATAL: 打不开 ${dbPath}: ${e.message}`);
  process.exit(2);
}

const enabledFrom = db.prepare(`select min(day) d from member_turn_daily`).get()?.d || null;

function eventsByGroup() {
  const where = [];
  const params = [];
  if (since) { where.push("d >= ?"); params.push(since); }
  if (until) { where.push("d <= ?"); params.push(until); }
  // 组内明细：保留 model 等键，供重建时逐行写入
  return db.prepare(`
    select member_id, day, model, carrier, provider, fleet_id,
           count(*) as turns,
           sum(case when status = 'ok' then 1 else 0 end) as ok_count,
           sum(case when status in ('failed','interrupted') then 1 else 0 end) as failed_count,
           sum(case when status = 'skipped' then 1 else 0 end) as skipped_count
    from (
      select fleet_id, member_id, carrier, provider, model,
             date(started_at / 1000, 'unixepoch') as day, status
      from member_turn_events
    )
    ${where.length ? `where ${where.join(" and ")}` : ""}
    group by member_id, day, model, carrier, provider, fleet_id
  `).all(...params);
}

function dailyByGroup() {
  return db.prepare(`
    select member_id, day, sum(turns) as turns
    from member_turn_daily group by 1, 2
  `).all();
}

function compare() {
  const ev = eventsByGroup();
  const evTot = new Map();
  for (const r of ev) {
    const k = `${r.member_id}|${r.day}`;
    evTot.set(k, (evTot.get(k) || 0) + r.turns);
  }
  const dl = new Map(dailyByGroup().map((r) => [`${r.member_id}|${r.day}`, r.turns]));
  const under = [];
  const surplus = [];
  const beforeEnable = [];
  const absent = [];   // 整表缺席：该 cell 从来没有过任何日汇总行（daily_absent）
  for (const k of new Set([...evTot.keys(), ...dl.keys()])) {
    const [member, day] = k.split("|");
    const e = evTot.get(k) || 0;
    const d = dl.get(k) || 0;
    if (e === d) continue;
    const rec = { member, day, events: e, daily: d, diff: d - e };
    if (enabledFrom === null) {
      // 没有起始日 = 这个 cell 一行日汇总都没有。不能当成「上线前的正常状态」：
      // 按 sql/005 的口径这是 daily_absent（整表缺席），是最严重的一类。
      absent.push(rec);
    } else if (day <= enabledFrom) {
      beforeEnable.push(rec);
    } else if (d < e) {
      under.push(rec);
    } else {
      surplus.push(rec);
    }
  }
  return { ev, under, surplus, beforeEnable, absent };
}

const { ev, under, surplus, beforeEnable, absent } = compare();
console.log(`cell=${cell}  日汇总起始日=${enabledFrom || "（从无日汇总）"}  库=${dbPath}`);
console.log(`模式=${apply ? "APPLY（会写）" : "dry-run（不写）"}`);
console.log(`上线后仍少 ${under.length} 组；整表缺席 ${absent.length} 组；`
  + `汇总多于明细 ${surplus.length} 组（不碰）；上线前/当天 ${beforeEnable.length} 组（不碰）`);

for (const r of under.sort((a, b) => a.day.localeCompare(b.day))) {
  console.log(`  ✗ ${r.day}  ${r.member}  events=${r.events} daily=${r.daily}  少 ${-r.diff}`);
}
for (const r of absent.sort((a, b) => a.day.localeCompare(b.day)).slice(0, 12)) {
  console.log(`  ✗ ${r.day}  ${r.member}  events=${r.events} daily=0  整表缺席`);
}
if (absent.length > 12) console.log(`  … 另有 ${absent.length - 12} 组整表缺席`);

if (!under.length && !absent.length) {
  console.log("\nOK 上线之后每 (member, day) 的日汇总与明细一致");
  process.exit(0);
}

if (!apply) {
  console.log("\n（dry-run 结束，未写任何东西。）");
  if (absent.length) {
    console.log("整表缺席的重建会把「存活下来的明细」当成全部历史，"
      + "所以需要显式 --create-missing，避免把被剪枝的历史悄悄写成一个偏小的孤儿汇总。");
  }
  console.log("要真写请加 --apply（整表缺席还需 --create-missing）");
  process.exit(1);
}

const createMissing = process.argv.includes("--create-missing");
const groups = createMissing ? [...under, ...absent] : under;
if (absent.length && !createMissing) {
  console.log(`\n跳过 ${absent.length} 组整表缺席（未给 --create-missing）`);
}

// 整组重建：删该组旧行 → 按明细逐 (model,carrier,provider,fleet) 写回
const del = db.prepare(`delete from member_turn_daily where member_id is ? and day = ?`);
const ins = db.prepare(`
  insert into member_turn_daily
    (fleet_id, member_id, carrier, provider, model, day,
     turns, ok_count, failed_count, skipped_count, updated_at)
  values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`);

let rebuiltGroups = 0;
let rows = 0;
db.exec("begin immediate");
try {
  for (const r of groups) {
    // 只补不减：再确认一次该组明细确实 ≥ 旧汇总
    const rowsForGroup = ev.filter((x) => x.member_id === r.member && x.day === r.day);
    const total = rowsForGroup.reduce((a, x) => a + x.turns, 0);
    if (total < r.daily) continue;
    del.run(r.member, r.day);
    for (const g of rowsForGroup) {
      ins.run(g.fleet_id, g.member_id, g.carrier, g.provider, g.model, g.day,
              g.turns, g.ok_count, g.failed_count, g.skipped_count, Date.now());
      rows += 1;
    }
    rebuiltGroups += 1;
  }
  db.exec("commit");
} catch (e) {
  db.exec("rollback");
  console.error(`FATAL 写入失败已回滚：${e.message}`);
  process.exit(2);
}

console.log(`\n已重建 ${rebuiltGroups} 组 / 写入 ${rows} 行。事后自证：`);
const after = compare();
if (after.under.length) {
  console.error(`FAIL 仍有 ${after.under.length} 组不一致`);
  process.exit(2);
}
console.log("OK 重建后日汇总与明细一致");
