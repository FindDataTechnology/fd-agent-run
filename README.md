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

## 跨 substrate 的事实层（只读）

```bash
python3 -m fd_agent_run.cli facts --source crew:.run/<id>/journal.jsonl
python3 -m fd_agent_run.cli facts --source crawl:../fd-industry-data/reports/<report>.json
```

9 字段（`task/at/owner/upstreams/predicate/evidence/verdict/reason/attempt`）装两种
substrate；`unknown` 是一等值（`$unknown`），**不是** `""`、`[]` 或 `0`。
判据的 `declared_at` 必须早于任务开始 —— 事后补写的判据会被判为事后合理化。

