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
# ① 在 platform pod 内导出（DB 是 hostPath，只读打开）
kubectl -n fd-prod cp tools/export_platform.mjs <pod>:/tmp/ -c platform
kubectl -n fd-prod exec <pod> -c platform -- \
  node /tmp/export_platform.mjs --cell <hex> --out /tmp/cell.jsonl

# ② 在这台机器上投影（需要写权限的 DSN）
export FD_AGENT_RUN_DSN='postgresql://…'
python3 -m fd_agent_run.cli project --export /tmp/cell.jsonl
```

**真库路径**：`/data/cells/<hex>/data/data/fleets.db`（`/data` 是节点上的 hostPath
`/opt/platform`，不是 PVC，所以**换节点拿不到**；`/data/data/fleets.db` 是共享的那份，
几乎没有回合数据——两者别搞混，2026-11-14 就查错过一次）。`started_at`/`ended_at`
是**整数毫秒**，不是文本。

三条规矩：

1. **不截断**。按成员或按天设行数上限看着无害，但投影端是 upsert：少导一行就把
   「永久镜像」覆盖成一个更小的数，而这张永久表正是归档完整性的基准。脚本里显式
   写 `truncated: false` 并导出全会话，宁可慢。
2. **导出契约只有一种形状**：JSONL，`{"k":"meta"|"run"|"turn"|"daily", …}`，
   与 `projector.py::read_export` 对齐。字段名逐字沿用 SQLite 列名（投影端把整行
   当 `payload`，改名字就是改事实）。
3. **ESM 不认 `NODE_PATH`**：镜像里 `import "better-sqlite3"` 会 ERR_MODULE_NOT_FOUND，
   脚本用 `createRequire("/app/")` 从应用根解析。

实测（2026-11-14，cell `1da519d4c1c0bf89`）：导出 18,373 行，`run/turn/daily`
= 6,693 / 7,512 / 4,167，与 cell 库计数逐一相等、零坏行；184 条回合没有 `run_id`
（进 `__unassigned__`，不编造身份）。

## 跨 substrate 的事实层（只读）

```bash
python3 -m fd_agent_run.cli facts --source crew:.run/<id>/journal.jsonl
python3 -m fd_agent_run.cli facts --source crawl:../fd-industry-data/reports/<report>.json
```

9 字段（`task/at/owner/upstreams/predicate/evidence/verdict/reason/attempt`）装两种
substrate；`unknown` 是一等值（`$unknown`），**不是** `""`、`[]` 或 `0`。
判据的 `declared_at` 必须早于任务开始 —— 事后补写的判据会被判为事后合理化。

