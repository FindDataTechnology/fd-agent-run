# fd-agent-run

一次运行的任务台账。与 `fd-agent-graph` 互不依赖：那边判断**图是否自洽**，这边记录
**运行走到哪了**。

```
python3 -m fd_agent_run.cli start  --graph <yaml> --roster <yaml> --dir <project>
python3 -m fd_agent_run.cli next   --run <id>
python3 -m fd_agent_run.cli record --run <id> --task <id> --state produced --evidence <file>
python3 -m fd_agent_run.cli commit --run <id> --task <id>
python3 -m fd_agent_run.cli status --run <id>
python3 -m fd_agent_run.cli resume --run <id>
```

三条不变量：

- **I1** 没有证据 = 没有 `produced`
- **I2** 判据不过 = 不能 `committed`
- **I3** 只有 `committed` 才是断点

台账落在被检查项目的 `.run/<run_id>/` 下（随项目拷走，自包含）。

## 未执行：一个任务可能被"决定不跑"

图上的条件分支意味着有些任务**不会被尝试**。这不是失败，也不是完成——折进任何一边都会
让台账撒谎：算完成等于认领了没做的工作，算失败等于把设计的绕行报成缺陷。所以它是自己的
状态，且只接受三个原因：

```bash
# 写入必须带原因；集合外的状态与原因都会被拒
python3 -m fd_agent_run.cli record --run <id> --task <id> \
    --state not_executed --reason when_false
```

| 原因 | 含义 |
|---|---|
| `when_false` | 守卫求值为假：这个分支这次不走 |
| `when_unknown` | 守卫引用的决策没有结算，真假不可知（**不是**假） |
| `upstream_not_executed` | 它的图内依赖全部未执行 |

`not_executed` 是终态：没有后续迁移，`resume` 也不会重新排队（连同 `committed` 一起跳过）。
`status` 的计数把它单列，它既不在完成数里，也不在失败数里。

### 从图运行落账：`apply-record`

`fd-agent-graph run` 会写一份 `*.dispatch.json`。这个命令把它折进台账：

```bash
python3 -m fd_agent_run.cli apply-record --dir <project> --run <id> \
    --record <graph>.dispatch.json
```

两条纪律：**节点没有对应任务时只报告、不编造地址**（编出来的地址无法续跑，下一个进程会去
找不存在的任务）；**已经是终态的任务不重写**（一个任务不能有两个结局）。记录里没有
`outcomes` 时直接拒绝——那只说明它记的是裁决，不是一次运行。

## 投影到事件库（可选的读面）

台账是**唯一真源**；下面的库只是它的投影，删了能重放出来。投影器不参与台账写入。

```bash
# 连接串只从环境变量读，源码与默认值里都没有口令
export FD_AGENT_RUN_DSN='postgresql://…'          # schema agent_runs

# 单个 run / 整个项目的全部 run，可先空跑
python3 -m fd_agent_run.cli project --dir <project> --run <id> --dry-run
python3 -m fd_agent_run.cli project --dir <project> --all
```

投影器的三条规矩：

1. **同地址同内容 → 跳过**。重投不会重复计数（第二次 `absorbed=0`）。
2. **同地址异内容 → 拒绝并退出非零**，错误里带 `run_key` 与 `seq`。这不是洁癖：
   台账契约要求「追加不修改历史」，投影器若覆盖，就是给历史开了一个静默改写口。
3. **水位超出账本实际长度 → 判投影失效并全量重放**，不报错（与快照水位同一语义）。

### 因此：`.run/*/journal.jsonl` 不可压缩、不可重排

`seq` 就是行号，是投影的幂等键。压缩或重排会让后续每一行的地址漂移，投影器会以
「同 seq 异 hash」大声停下 —— **这是设计意图，不是故障**。要清理旧台账，删整个
`.run/<run_id>/` 目录（连同该轮全部历史一起消失，语义清晰），不要就地编辑文件。

## 平台侧：从一个 cell 的台账导出（`tools/export_platform.mjs`）

平台侧的数据在 cell 自己的 SQLite 里，不在我们这儿。导出 → 投影分两步，**两端都在仓里**：

```bash
# 一条命令刷全部 cell（默认空跑；--apply 才写库）
export FD_AGENT_RUN_DSN='postgresql://…'      # 必须是有写权限的角色
tools/refresh_platform.sh                      # 昨天起增量，逐 cell 空跑
tools/refresh_platform.sh --apply               # 真写
tools/refresh_platform.sh --cells 1da5 3fdc --since 2026-11-10

# 手工分步（调试用）
kubectl -n fd-prod cp tools/export_platform.mjs <pod>:/tmp/ -c platform
kubectl -n fd-prod exec <pod> -c platform -- \
  node /tmp/export_platform.mjs --cell <hex> --since 2026-11-12 | gzip -c > /tmp/cell.jsonl.gz
gunzip -c /tmp/cell.jsonl.gz > /tmp/cell.jsonl
python3 -m fd_agent_run.cli project --export /tmp/cell.jsonl --dry-run
```

调度器为什么在 pod 外：导出要读 cell 的 SQLite（在 pod 里），投影要写 Postgres
（要写权限的 DSN）。两端都不在同一个网络域里，所以驱动脚本在本机跑、用管道把
gzip 后的 JSONL 取回来（全量导出 ~11MB，两天增量 gzip 后 ~240KB，稳在 tailnet 单次
传输的上限内）。

**真库路径**：`/data/cells/<hex>/data/data/fleets.db`（`/data` 是节点上的 hostPath
`/opt/platform`，不是 PVC，所以**换节点拿不到**；`/data/data/fleets.db` 是共享的那份，
几乎没有回合数据——两者别搞混，2026-11-14 就查错过一次）。`started_at`/`ended_at`
是**整数毫秒**，不是文本。

四条规矩：

1. **不截断**。按成员或按天设行数上限看着无害，但投影端是 upsert：少导一行就把
   「永久镜像」覆盖成一个更小的数，而这张永久表正是归档完整性的基准。脚本里显式
   写 `truncated: false` 并导出全会话，宁可慢。
2. **导出契约只有一种形状**：JSONL，`{"k":"meta"|"run"|"turn"|"daily", …}`，
   与 `projector.py::read_export` 对齐。字段名逐字沿用 SQLite 列名（投影端把整行
   当 `payload`，改名字就是改事实）。
3. **ESM 不认 `NODE_PATH`**：镜像里 `import "better-sqlite3"` 会 ERR_MODULE_NOT_FOUND，
   脚本用 `createRequire("/app/")` 从应用根解析。
4. **不看日历，只看「还缺什么」**。明细按 `id > after`（水位）导，日汇总**全量**导；
   `--since` 现在不缩小任何表，只作为兼容参数被记进 `meta.since_ignored`。
   日历窗口造出过两种静默缺陷，都实测过：
   * **丢新行**：驱动器每次传 `--since 昨天`，某个 cell 连挂两天时，第一天的新行就落在
     第三天的窗口外、**永久不再导出**——而导出成功、投影 `absorbed=0`、日志一行红都没有；
   * **镜像覆盖哪些天取决于「跑批当时多新」**：日汇总按 `--since` 收窄时是幂等的（按
     day upsert），但一旦遇到重建镜像、接入一个历史很久的新 cell、或跑批停过一段，
     窗口外的天就再也不会补上。而那张表正是归档缺口分析的基数 ⇒ 缺的行会被读成
     「cell 侧没有日汇总」，一个由导出窗口造出来的假缺陷。
     （**今天没有缺**：实测 `cell:1da5` 镜像最早日 = cell 最早日 = 2026-10-31。
     这条是「移除一个潜伏前提」，不是「修一个正在发生的缺陷」——我第一版把它
     当成正在发生的缺陷写进了注释，还拿行数当证据，那是错的，见下。）
   `id > after` 本身就是「哪些行还没吸收」的精确答案；日汇总很小（最大 cell 4.6k 行），
   全量导的代价可忽略。判据在 `export_platform.mjs::turnFilter()`，`--explain` 可直接看
   （在开库之前退出，本机无 npm 依赖也能跑），并写进 `meta.turn_filter_by` / `daily_filter_by`。

**怎么验「镜像齐不齐」**：拿 cell 自己的总数比镜像，不要比两张自家表。
**量词要对**——`agent_turn_daily` 是**聚合表**（按 fleet/member/carrier/provider/model/day
合并，cell 侧 NULL 维度在那边是多行、在这里是一行），所以**行数本来就不相等**：
`cell:1da5` 是 4,663 行 / 837 行。要验的是 `sum(turns)` 与 `min/max(day)`。
（我第一版数行数，据此报了一个「镜像缺 3,822 行」的假缺陷——这是同一类量词错误。）

三类「差」也不算丢数：
① `run_id` 为空的回合并进 `__unassigned__`（不编造身份），run 数会**多** 1；
② 比上一次导出**更新**的 run 还没进镜像——run 段不受过滤，下一轮就补上
   （实测 27 条差全部落在 13:05–14:15Z，而该 cell 上一次导出是 13:02Z）；
③ 实测 cell 正在写入，`sum(turns)` 差 1–2 是两次读数的时差。

实测（2026-11-14，cell `1da519d4c1c0bf89`）：导出 18,373 行，`run/turn/daily`
= 6,693 / 7,512 / 4,167，与 cell 库计数逐一相等、零坏行；184 条回合没有 `run_id`
（进 `__unassigned__`，不编造身份）。

### 心跳行 `__cell__`：投影表里那个不是 run 的 run_id

`agent_runs.agent_run_projection` 里有一行 `run_id = '__cell__'`，**它不是一次 run**，
而是「这条链路在某时刻确实跑通了、本次带进来 N 行」这个事实（`__unassigned__` 是同款
保留名）。每次成功投影都会 upsert 它，`status` 分 `ok`（有新增行）/ `idle`（零新增）。

为什么必须有它：覆盖面板用 `max(absorbed_at)` 判新鲜度，而水位行只在**有新增行**时
更新。于是「链路每天跑通、只是这两天没有新回合」的承载者会在 2 个期望周期后判 `stale`，
红的是没毛病的东西。有心跳之后三件事分开了：有数据 / 读通了但没新数据 / **流水线断了**
（心跳过期）。板面把中间那档叫 `quiet`，不进 unhealthy。

读者要各自过滤它：水位查询按 `(driver, source, run_id)` 精确匹配，天然不受影响；
统计 run 数时排除它（`count(*) filter (where run_id <> '__cell__')`）。

### 顺带自证：cell 的日汇总自己是否对得上（`tools/check_cell_daily.mjs`）

```bash
kubectl -n fd-prod exec <pod> -c platform -- \
  node /tmp/check_cell_daily.mjs --cell <hex> [--since D] [--until D] [--verbose]
# EXIT=1 即上线后仍有 (member, day) 的日汇总少于明细
```

为什么需要它：cell 的 `recordTurnEvent` 有两段降级叠在一起 —— 扩展插入（38 列）
失败就改用 18 列写事件行，而**那条路径早返回**，下面的 `member_turn_daily` 累加整段被跳过。
事件行在、永久汇总不在，运行期零信号。指纹很好认：走降级路径的事件行**所有计量列同时为
NULL**（实测 2026-11-03 有 16 行，日汇总恰好少 16 个回合）。

2026-11-14 实测 cell `1da519d4c1c0bf89`：**2026-11-03 少 16 个回合**，11 个成员各
−1/−2，次日无反向补偿（所以不是归日口径问题，是真丢）。上线前的日子与上线当天
分别归入 `not_enabled` / 部分启用，不计缺陷。

口径：日汇总的 `day` = `new Date(startedAt)` 的 UTC 日（`fleets-db.js` 就是这么算的），
明细也按 `started_at` 的 UTC 日归口——**不能**用 `ended_at`，否则跨日界回合会被误报。

## 跨 substrate 的事实层（只读）

```bash
python3 -m fd_agent_run.cli facts --source crew:.run/<id>/journal.jsonl
python3 -m fd_agent_run.cli facts --source crawl:../fd-industry-data/reports/<report>.json
```

9 字段（`task/at/owner/upstreams/predicate/evidence/verdict/reason/attempt`）装两种
substrate；`unknown` 是一等值（`$unknown`），**不是** `""`、`[]` 或 `0`。
判据的 `declared_at` 必须早于任务开始 —— 事后补写的判据会被判为事后合理化。


### 修复：从明细重建日汇总（`tools/rebuild_cell_daily.mjs`，默认空跑）

```bash
kubectl -n fd-prod exec <pod> -c platform -- \
  node /tmp/rebuild_cell_daily.mjs --cell <hex>            # 只打印要补什么
… --apply                                                  # 真写：整组重建
… --apply --create-missing                                 # 连「整表缺席」也建（须显式）
```

三条口径/安全约束（每一条都有实历）：

1. **按 `(member, day)` 比对，不能按 `model` 逐键**。同一回合在明细与汇总里的
   `model` 归属可能不同（实测多条明细 `model=NULL` 而汇总里是具体型号）。第一版
   按 model 逐键比，报出 434 处「少记」，真实缺口只有 15 处。
2. **只补不减**。反方向（汇总 > 明细）通常是明细被剪枝，此时绝不碰——用被剪枝的
   明细覆盖永久真相等于抹掉证据。
3. **整表缺席要单独显式**。`420a3344…`、`de7ec3a6…` 从来没有过任何日汇总行；
   把「没有起始日」当成「上线前正常」会把最坏的一类（`daily_absent`）藏起来。
   重建这类等于用「存活下来的明细」当全部历史，所以必须显式 `--create-missing`。

实测（2026-11-14，dry-run，未写生产）：三个中招 cell 共 15 组 / 22 个回合，
与 `check_cell_daily.mjs` 和改过口径的 `sql/005` 三处一致。

## 线上视图是否真的换了口径（`tools/check_view_drift.py`）

```bash
FD_AGENT_RUN_DSN=... python3 tools/check_view_drift.py      # EXIT=1 即有性质缺失
```

「仓里改了 SQL」不等于「线上视图变了」。`sql/*.sql` 是 `create or replace view`，
没人用 DDL 角色跑一遍，线上就还是旧定义——板面照旧按旧口径出数，而任何翻文件、
看测试的人都会以为已经修好。2026-11-14 就是这么过的：`005` 改完、单测全绿，
线上 `agent_run_gap` 仍按 `ended_at` 归日。

**判据是性质，不是文本。** 第一版把 `pg_get_viewdef()` 的正文与仓里 SQL 逐字比，
5 个视图全报漂移，其中 3 个根本没改过——`pg_get_viewdef()` 会去别名、重排括号、
补 `::text`、把 `IN (a,b)` 写成 `= ANY (ARRAY[a,b])`、吃掉 `'x'as y` 里的空格。
靠正则逐步归一化等于手写一个 SQL 解析器：抹少了假红，抹多了把真变化一起抹掉。
现在只断言四条**有过事故**的性质（归日字段 / skipped 不进事实 / `$unknown` 在场 /
`is_defect` 在场），加列、换别名、调排版都不会红。

修法：用有 DDL 权限的角色在一个事务里跑 `sql/00X…`（`DROP`+`CREATE` 原子替换，
不留半旧半新的可见窗口），再跑一次本体检。

2026-11-14 已按此法把 `005_completeness.sql` 部署到生产，四处性质全绿；实测读数
与只读复算、cell 侧自证三处吻合：`archive_gap 11/16`、`daily_absent 4/6`
（此前 `daily_exceeds_events` 那 23 对跨日界假缺陷归零）。

### 有些性质正则抓不住，只能真查（`BEHAVIOUR_CHECKS`）

正则断言的是「定义文本里有没有这个词」。但有一类性质约束的是**视图算出来的东西**，
文本里怎么看都对：

- `enabled_from` 曾是 `min(day) WHERE source = 本 cell` —— **自指**。一个 cell 若从
  第一天就漏记、第 N 天才恢复，它前面那些天会永久读成 `not_enabled`（「还没上线」），
  一直漏看起来像还没上线，而闸是绿的。文本判据永远发现不了这件事。
- 现在上线日单列成 `agent_run_ship_day` 视图：取「有 >30 天日汇总历史的 cell」里最早的
  那天（本部署 = 2026-10-31）。为什么不取全体最早：我按明细补建的空壳 cell
  （`420a3344` 3 天、`de7ec3a6` 1 天）会把上线日拽回 10-24，把两个活跃 cell 在
  10-24…10-30「功能确实还没上」的 **1274 个回合**一次性翻成缺陷——假红比漏报更快
  摧毁一道闸。
- 配套新增 `late_start` 分诊类（上线日之后、但早于本 cell 第一行日汇总），**计入缺陷**；
  `is_defect` 的豁免只剩 `not_enabled` / `partial_enable_day` 两类。

三条行为判据（SQL 返回违反行数，0 = 通过）：
`not_enabled` 里不许出现 ≥ `ship_day` 的行／`ship_day` 必须等于「>30 天 cell」里最早那天／
`is_defect` 为假的只能有两类。**查不动就退 2**（判不出 ≠ 通过）——上线前
`agent_run_ship_day` 不存在时，本闸报的是 `FATAL … 判不出`，而不是绿。

实测：上产前后**零噪声**（`not_enabled` 264→263、`partial_enable_day` 28→29，
只是 10-31 那一格挪了类），没有新增缺陷。

