# kill-and-resume 实验（2026-11-09）

## 目的

证明台账不是装饰品：**中断之后继续，已提交的活不会被重做。**

如果这个证明不成立，阶段 1 就没有价值，后续四个阶段不该写。

## 做法

用 `fd-portmap` 的真实产物（`src/index.js`、`src/history.js`，不是夹具），两个任务：

```
start --task impl-a/1 --task impl-b/1 --kind write      # 判据取自 .crew/expect.json 的模板
record impl-a/1 --state produced --evidence <真实 sha256>
commit impl-a/1                                          # A 完成并提交
record impl-b/1 --state produced --evidence <真实 sha256>
                                                         # ← 在此"中断"：B 已产出未提交
resume                                                   # 恢复
next                                                     # 应指向 B 的下一步，不指向 A
```

## 结果

```
skipped 1 committed task(s) (they are the checkpoint)
requeued 0 unfinished task(s)

next ->
task:      impl-b/1
action:    commit (already produced, not yet committed)
predicate: {"kind":"write","files":["src/index.js"],"contract":true}
```

| 判据 | 结果 |
|---|---|
| A 没被重做（产物 mtime 未变） | **True** |
| A 产物内容未变（sha256 一致） | **True** |
| `next` 不指向 A | **True** |
| `next` 指向 B 的下一步 | **True**（`action: commit`） |
| 最终状态 | `committed=2` |

## 实验发现的一个真缺口（已修）

第一版 `next` 只认 `pending`/未知状态，于是**停在 `produced` 的任务既不可派发也不被提示**——
运行会静默卡住，而每个组件都报"成功"。这正是这套东西要防的形态，却出现在它自己身上。

修法：`produced` 被单列为"已产出待提交"，`next` 返回 `action: commit` 并附判据。

## 诚实的局限（这条比结论重要）

**墙钟时间判据我没有测。** 任务书 5.1 写的是"恢复后总耗时 < 从头做的 50%"，但这次实验是
**重放一个已经完成的产物集**，没有真实的模型推理，所以量出来的秒数不代表任何事——
拿它当收益证据就是自欺。

我实际测的是**任务级跳过率**：2 个任务里 1 个已提交 → `next` 只排出剩下 1 个，跳过率 50%。
这是一个更弱但诚实的代理指标。

要量真实的墙钟收益，需要在一次**真实的 agent 运行**中途 kill，再 resume 对比。那需要一次
完整的 crew 派发（约 10-20 分钟），且受上游稳定性影响。**这件事没做，记在这里，不算通过。**

## 结论

- I1/I2/I3 三条不变量在真实产物上成立
- 断点确实省掉了对已提交任务的重做（任务级证据）
- **墙钟收益未测**，不能声称"恢复变便宜了"


---

# 第二次实验：真实 agent 运行（2026-11-09）

第一次实验的局限是"重放已完成产物集，墙钟无意义"。这次用**真实的两路 agent 工作**：

## 任务（真需求，不是填充）

`fd-portmap` 每小时巡检把日志写成一天约 1000 行（42 个监听各占一行）。加 `--quiet`
只输出摘要行。两路：`quiet/cli`（改 cli.js + 补 6 个测试）、`quiet/test`（新建独立边界测试）。

## 过程与实测

| 步骤 | 事实 |
|---|---|
| `start` | 2 个任务 pending，判据取自 `.crew/expect.json` 模板 |
| 派第一路 agent | 131 → **137** 测试，耗时 98 秒（agent 自报 wallclockSeconds） |
| `record quiet/cli --state produced` | 3 条真实证据：`src/cli.js` sha256 / `test/portmap.test.js` sha256 / `node --test` 的 exit_code+行数+秒数 |
| `commit quiet/cli` | 判据全过 → committed |
| **中断** | 不派第二路，会话停在这里 |
| 中断时指纹 | `cli.js efaa3365…`、`portmap.test.js fc39dfbf…` |
| `resume` | `skipped 1 committed task(s)`；`next` → `quiet/test` |
| 派第二路 agent | 137 → **145** 测试，耗时 214 秒 |
| **关键判据** | 两文件 sha256 与中断前**逐字节一致** —— A 未被重做，B 也未改动它们 |
| 收尾 | `committed=2`，`next` 报 "nothing dispatchable" |

## 这次真正量到了什么

**派发数**：中断后只派 1 路，而不是 2 路 → **省掉 1 次模型派发**（约 98 秒的 agent 时间）。
这是成本的主要构成。

**墙钟**：仍然要诚实——resume 之后的第二路本身要 214 秒，这个时间省不掉。真正省的是
**第一路的 98 秒 + 它的 token 成本**。所以"恢复变便宜"的准确说法是
**省掉了已提交部分的重复派发**，而不是"恢复很快"。

## 结论

- I1/I2/I3 在真实 agent 产出上成立（证据是 sha256 与命令结果，不是自述）
- resume 精确指向未完成的那一路，已提交部分的产物逐字节未变
- 收益 = 1 次派发（约 98 秒 / 一份 agent 成本），在 2 路里占一半

**这不足以宣称"恢复变便宜"**：2 路里省 1 路是显然的算术，真正的问题是**在 5-10 路、
多次中断的实际场景里它是否仍然成立**。这需要真实的多路运行，本次没做。


---

# 第三次实验：5 路（2026-11-09）

## 任务

`fd-fleet`——一条命令看整机（把 fd-win-health / fd-portmap / fd-backup-age 三个日志压成一页）。
真需求：现在看机器状态要翻三个日志、三种格式。

五路 + 一个合并点，六个任务：

| 任务 | 文件 | 结果 |
|---|---|---|
| r1/1 | src/sources-health.js | 5,086 B |
| r2/1 | src/sources-ports.js | 8,108 B |
| r3/1 | src/sources-backup.js | 7,387 B |
| r4/1 | src/render.js | 10,211 B（253 行，纯函数） |
| r5/1 | src/cli.js + test/fleet-cli.test.js | 13,527 B + 14 测试 |
| merge/1 | src/index.js + src/rollup.js | 7 名导出 + 判据层 |

## 中断与恢复

```
start 6 tasks -> 提交 r1/r2/r3（3 committed, 3 pending）   ← 中断点
resume -> "skipped 3 committed task(s) (they are the checkpoint)"
next   -> merge/1（见下面的缺口）
然后派 r4、r5，写合并点，提交 r4/r5/merge -> 6/6 committed
```

**核心判据**：中断点记录的三个 sources 文件 sha256，在全部工作完成后**逐字节一致** ——
已提交的 3 路没有被重做。

## 省了多少

中断后只需要做剩下的 3 个任务，而不是重做 6 个 → **省 3 次派发（50%）**。
实际派发：r1/r2/r3 各 1 次（中断前）+ r4/r5 各 1 次（恢复后）+ 合并点由主 agent 做。
若从头重来，需要再派 3 次。

## 这一轮抓到两个真缺陷

### 1. 判据作用域错了（第一轮 5 路全被拒）

`.crew/expect.json` 把 `write` 判据（含 `contract: true`）套给每个任务，而"契约 7 个名字全覆盖"
是**合并点**的属性。于是 r1/r2/r3 产出齐全、证据真实，`commit` 却全部被拒：

```
- every contract name is exported: got ['main','renderHuman','renderJson','rollup'], wanted []
```

**状态机是对的，判据是错的。** I2 正确地拒绝了条件未满足的提交。修法：route 级只验自己的文件，
契约覆盖只放在 join 上。**这验证了"判据必须与任务粒度匹配"，而不是判据越严越好。**

另外确认了一件好事：**判据不能在运行中修改**（记在 `task_state` 事件里）——
所以正确做法是重开一轮，而不是改历史。

### 2. rollup 里两个我自己写的 bug

- `if (effective === null) return;` 提前返回，把后面所有严重度规则短路掉了 →
  `ports: 1 unexpected` 被算成 `ok`
- `reduce(fn, null)` 的种子是 null，`RANK[null]` 是 undefined，比较恒假 → 结果永远是 null

两条都是"看起来对的代码"，靠测试才抓出来（`rollup: 有白名单外监听 -> degraded` 与
`rollup: 磁盘 level=degraded 能拉低整体`）。

还有一处语义修正：**显式 status 与逐项 severity 要取最坏，不是显式优先**——
否则一个自称 `status: ok` 但某磁盘 `level: degraded` 的报告会被整体判 ok。

## 诚实边界

- **中断是模拟的**：我没有真杀进程，而是完成 3 路后停止、再 resume。真实 kill 的场景
  （进程持有租约时被 SIGKILL）**没有测**。
- 省下的 3 次派发是**算术**，不是压力测试。9-10 路、多次中断、并发租约冲突都没测。
- 合并点由主 agent 做，不是 agent 角色做的；这与真实派发形状不同。

## 结论

三条不变量在 5 路规模上成立，已提交部分确实不被重做。但"台账在大规模、真中断下仍省成本"
**仍然没有被证明**——这条我没做到，就不写它成立。
