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
