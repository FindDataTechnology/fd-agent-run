# 事实层翻译实验（2026-11-09）

## 目的

检验一件事：同一个 9 字段事实层，能不能同时装下**爬虫舰队**和**编码班底**这两种
完全不同的 substrate。翻不过去的地方，就是底座该长什么样的依据。

只读：两份已落盘的真实产物，不连数据库、不做写入。

- 爬虫：`fd-industry-data/reports/agriculture-alerts/health-2026-11-08.json`（10 个有 telemetry 的源）
- 编码：`fd-fleet/.run/r-fleet2/journal.jsonl`（6 个任务，今天 5 路实验的真数据）

## 结论先说

**格式上翻译成功了（9 个字段都能填满），但成功是假的**——爬虫侧有四处必须编造才能填。

## 四处编造（逐个判定）

| 字段 | 我填的 | 真实情况 | 判定 |
|---|---|---|---|
| `owner` | `"cron"` | 真实持有者是 k8s CronJob；巡检产物里**没有**持有者概念 | **编造** |
| `upstreams` | `[]` | 源的上游是外部站点（moa.gov.cn 等），产物里没有 | **编造为空** |
| `evidence.exit_code` | `0` | 巡检只说 `health.status = healthy`，从不记退出码 | **编造** |
| `evidence.seconds` | `0` | 无耗时 | **编造** |

**"编造为空"和"编造为 0"比缺少字段更危险**：下游会把 `[]` 读成"没有上游"、
把 `0` 读成"成功退出"，而事实是"我们不知道"。这正是今天反复出现的那个病。

## 一个根本性差异：爬虫侧没有 predicate

```
编码班底：predicate 在派发前声明（write 这些文件 / verify 这条命令）
爬虫：    health.status 是**事后判定**（rule1:healthy last_success_age=46h<=840h tail_failed=0<3）
```

爬虫有判定规则，但规则**写在巡检器里、跑完才算**；编码班底是把条件**先声明、后求值**。
这两者不是同一个东西：

- 先声明后求值 → 判据能否决提交（I2）
- 事后判定 → 只能记录结论，无法阻止"错误的东西被当成完成"

**这是本次实验最有价值的发现**：事实层的核心不是"有个 verdict"，而是
**verdict 与 predicate 在时间上的先后**。爬虫要接入，必须把巡检规则从"事后算"
改成"派发时声明"——否则它永远只能记录，不能约束。

## 另外两处

1. **`cancel_requested` 在爬虫有、在编码台账没有**（今天的台账里 `reason` 是自由文本，
   分不清"失败"与"主动取消"）。核心 9 字段里确实缺一个显式的 cancel 语义。
2. **`pending_run_id`（排队/接续）在爬虫有、在编码台账没有**——编码班底没有队列概念，
   因为它每次只有一轮。若要接有排队的 substrate，核心要加一个 `queue` 语义。

## 对核心 schema 的修订建议

原 9 字段：

```
task / at / owner / upstreams / predicate / evidence / verdict / reason / attempt
```

按这次实测，需要三处调整：

1. **`unknown` 必须是一等值**：`owner`/`upstreams`/`evidence` 允许显式 `unknown`，
   且下游必须区分 `unknown` 与 `empty`/`0`。否则只会逼人编造。
2. **`verdict` 增加 `cancelled`**：与 `failed` 分开（主动中止不是故障）。
3. **`predicate` 增加"声明时刻"**：`declared_at` 必须早于任务开始。
   没有它，事后判定可以伪装成先声明——这是唯一能机械检查"判据是否真的前置"的办法。

## 下一步

按这三处修订后重翻一次。如果爬虫侧不再需要编造（`owner` 填 `unknown` 而不是 `"cron"`、
`evidence` 只填真实观测到的 rows/age），就说明 9+3 字段够用，可以写 proposal。

如果还需要编造，那就是 schema 仍不够，继续加字段——但每次加都要过"三家至少两家现在需要"这一关。

---

# 重翻结果（按三处修订后）

## 零编造了，代价是 unknown

```
爬虫侧（10 源）                    编码侧（6 任务）
task        0/10 unknown          task        0/6
at          7/10 present (3 缺)   at          0/6
owner      10/10 UNKNOWN          owner       6/6  present
upstreams  10/10 UNKNOWN          upstreams   6/6  UNKNOWN
predicate   0/10 present          predicate   0/6  present
evidence    0/10 present          evidence    0/6  present
verdict     0/10 present          verdict     0/6  present
```

**没有一处再需要编造。** 代价是：爬虫侧的 `owner`/`upstreams` 全为 unknown，
编码侧的 `upstreams` 全为 unknown。

## 这才是真正的结论

**未知是常态，不是缺陷。** 两侧各有一半字段本来就无从得知：
- 爬虫不知道"谁持有"(k8s CronJob 不落库)、不知道"上游是谁"(外部站点)
- 编码不知道"上游是谁"(我的 5 路实验里任务间确实没有数据依赖，是文件不重叠)

如果核心 schema 要求这些字段必须有值，**唯一的出路就是让实现者编造**——
而这正是今天反复出现的那个病的根：**不是人不想说实话，是格式逼人说假话。**

## 三条硬约束（写进未来 proposal）

1. **`unknown` 必须是一等值，且必须与 empty / 0 可区分。**
   `owner: unknown` ≠ `owner: none`；`upstreams: unknown` ≠ `upstreams: []`。
   聚合查询里，unknown 不得计入任何分母（否则覆盖率会被系统性地算错）。

2. **`verdict` 的四态**：`committed` / `failed` / `cancelled` / `pending`。
   主动取消不是故障——今天爬虫的 `cancel_requested` 与错误中止在 `reason` 文本里分不开。

3. **`predicate.declared_at` 必须早于 `at`（任务开始）。**
   这是唯一能机械检查"判据是否真的前置"的办法。爬虫侧的巡检规则写在代码里、
   跑完才算——所以它的 `declared_at` 是 unknown，**这本身就诚实地暴露了
   "它只能记录，不能约束"**。

## 由此得到的一件事：爬虫要接入，得改的是规则的位置，不是格式

爬虫不缺数据，缺的是**把判定条件从"巡检器里"搬到"派发时"**。
这件事的收益和今天的 `predicate` 一样：现在的巡检跑完才知道"跑绿了但 0 行"，
前置声明之后可以在提交时就拒绝。

这不是格式问题，是**规则归属问题**——而它比任何 schema 设计都难，因为它要动 160 个源的
既有运维习惯。

## 所以下一步不是写 proposal，是先问一个问题

事实层已经证明能装下两种 substrate（零编造）。**但爬虫侧有一半字段是 unknown，
这意味着它接入后能提供的价值只有一半**：能回答"这个源最近跑过没有、写了多少行"，
回答不了"谁在跑、依赖谁"。

要让那两半也变成事实，得先在爬虫侧补两件事：
1. 记录**谁持有**（CronJob 名 / pod 名）—— 采集侧加一行日志的事
2. 记录**上游**（源站点 URL / 依赖的数据集）—— manifest 里加一个字段

这两件事都不难，但它们得先做，否则事实层再漂亮，爬虫那半边也是空的。

---

# 落地：爬虫侧补归因（2026-11-09）

## 改了什么

`runner_cli.py` 的 metrics 里加了两个事实，**不动表结构、不加列**（`metrics jsonb` 早就存在）：

```python
attribution = _attribution(src, os.environ)
if attribution:
    metrics = {**(metrics or {}), **attribution}
```

- `owner`：`HOSTNAME` → `FD_RUN_OWNER` → `COMPUTERNAME` → `"unknown"`
- `upstreams`：从 `spiders/<src>/manifest.yaml` 的 `source_url` 取；取不到 = `"unknown"`

**为什么是 `"unknown"` 而不是 `""` / `[]`**：空字符串和空数组是断言（"没有持有者"、
"不读任何上游"），与"没读到"是两件事。下游分不开，就会像我第一轮翻译那样把 `[]` 当事实。

## 一个必须记下来的缺陷

第一版实现用 `yaml.safe_load` 读 manifest，外面包了 `except Exception: pass`。实测：
**本机没装 yaml**，于是 `ModuleNotFoundError` 被那个 `pass` 吞掉，`upstreams` 静默变成
`"unknown"`——测试打印出来才发现。

**这正是这一整天在防的形态**：`except: pass` 把"读不了"伪装成"没有"。
修法不是加日志，而是**不引入依赖**——`source_url` 是顶层标量，一个正则就够，不需要解析器。

## 验证到什么程度（诚实版）

| 项 | 结果 |
|---|---|
| 新测试 `tests/test_attribution.py` | **7/7 通过** |
| `runner_cli.py` 编译 | 通过 |
| metrics 合并真的调用 | 断言源码含 `_attribution(src, os.environ)` 与合并语句 |
| 真实源 `moa-hog-price` | `upstreams = https://www.moa.gov.cn/...` ✓ |
| 未知源 | `unknown` / `unknown`，不抛 ✓ |
| **本包全量回归** | **跑不了** |

**全量回归跑不了，原因是环境**：`tests/` 没有 `__init__.py`，且本机没装 pytest，
而既有测试全部 `import pytest`。`unittest discover` 直接报 `Start directory is not importable`。

**所以"没有打破 160 个源的既有行为"这句话我没有证据。** 我只能说：改动是纯增量的
（新增函数 + 一处字典合并），不触碰既有分支，编译通过，新测试全绿。

要真正验证，得在装了 pytest 的环境（容器里）跑一次全量。**这一步没做。**

---

# 落地：三条硬约束变成可执行校验器（2026-11-09）

`fd_agent_run/fact.py` + `tests/test_fact.py`（27 测试）。不需要任何数据、不连库、不读时钟。

## 它当场抓出的两个问题

### 1. 我原来的规则会把真实答案判成违规

第一版规则拒绝 `upstreams: []`。但跑真实数据时发现：**编码班底那 5 路确实是文件不重叠、
没有上游**，`[]` 就是诚实答案。规则写错了——它会让实现者把真的 `[]` 改成 `unknown`，
或者反过来。**规则本身在制造新的谎言。**

改法：`[]` 合法，"`[]` 因为没看而发" 只能在产出侧防（爬虫的 `_attribution` 就是发 marker
而不是空数组）。**校验器只能拒绝它看得出的错，看不出的一律不判。**

### 2. 台账自己不满足"判据前置"

用校验器跑 `fd-fleet` 那 6 个真实任务，**6/6 全被拒**：

```
r1/1: predicate has no declared_at — without it a condition written after the
      fact is indistinguishable from one declared before
```

`fd-agent-run` 记了事件时刻 `t`，却从没把 `declared_at` 写进 predicate。
**我今天建的台账无法证明自己的判据是前置的**——它只是"恰好"先写后跑。

已修：`start` 现在给每个 predicate 打上 `declared_at`。旧 journal 不改（历史是历史），
所以那 6 条仍然不合格——**这是正确的结果，不是遗留问题。**

## 规则清单（每条都有"必须被拒"的用例）

| 规则 | 拒绝什么 | 为什么 |
|---|---|---|
| unknown ≠ empty | `owner: ""` / `owner: null` | 空值读成"没有持有者"，是另一个断言 |
| 角色词不是持有者 | `owner: "cron"` / `"worker"` / `"system"` | 角色说的是"哪类跑"，不是"哪一次跑" |
| 裸串 "unknown" 也不行 | `owner: "unknown"` | 会被误当真实持有者，也躲得过 `if not owner` |
| 未观测的成功不算成功 | `exit_code: 0` 而无 `cmd_observed` | 最危险的一种编造：下游读成"命令成功" |
| cancelled ≠ failed | 只有 4 个 verdict | 人主动中止不是故障；爬虫现在两者混在自由文本里 |
| 判据必须前置 | `declared_at > at`，或缺失 | 事后写的条件不是条件，是事后合理化 |
| 0 字节产物不算证据 | `bytes: 0` | 空文件不是工作的证据 |

## 分辨力验证（这是它唯一该被信任的理由）

```
第一轮翻译（我编了 3 处）  -> 被拒 3 条：角色词 owner / 无 declared_at / 未观测的 exit_code
重翻（零编造）             -> 零问题，可采纳
```

## 局限（必须写清）

- **它不判断真假，只判断"有没有把不知道说成知道"。** 一个正确填写的谎言它拦不住。
- `cmd_observed` 是产出方的自律声明，校验器无法核实——**这仍是自证，只是自证被摆到了明处。**
- 爬虫侧的 `owner` 现在取 `HOSTNAME`，在 k8s 里是 pod 名（好）；在裸机上可能是机器名（一般）。
  这条是采集侧的取舍，不是校验器的。

---

# 爬虫包全量回归：补上了（2026-11-09）

之前写的是"跑不了"。用独立 venv（`/tmp/fdtest`，只装 pytest + pyyaml，不碰系统 Python）跑通了。

## 结果

```
1138 passed, 54 failed, 2 skipped, 7 errors
```

## 失败全部归类（逐条查过根因）

| 原因 | 条数 | 与本次改动有关？ |
|---|---|---|
| `No module named 'lxml'` | 44 | 否（酒店线 42 + 2） |
| `No module named 'scrapling'` | 8 | 否 |
| `No module named 'fastmcp'` | 3 | 否 |
| `No module named 'cryptography'` | 2 | 否 |
| 镜像 tag 漂移（`sha-750da3f` ≠ `sha-ea21592`） | 1 | 否（发布线） |
| **metrics 精确相等断言，被我新增的归因键打破** | **3** | **是，已修** |

## 我造成的那 3 条

`tests/test_first_crawl_window.py` 里有三处断言把 `metrics` 写成**精确相等**：

```python
assert reports[0]["metrics"] is None                                    # ①
assert reports[0]["metrics"] == {"window_start": ..., "window_end": ...} # ②
assert reports[0]["metrics"] == {"dropped_params": ..., "window...": ...} # ③
```

我加了 `owner`/`upstreams` 之后三条全红。**这是真回归，不是测试写太死**——`metrics` 是
运维和告警读的公共字段。

修法是更新断言，理由写进了测试里：**`metrics` 的语义从"窗口，或者没有"变成
"窗口（可缺）+ 归因（恒有）"**。归因是每次运行都有的事实，不该因为没窗口就消失；
而这三条测试真正关心的是**窗口部分的精确性**，那部分我保留了精确断言，并额外断言
"多出来的键恰好是 owner 与 upstreams"。

修后 `test_first_crawl_window.py` **45/45 通过**。

## 结论

**与本次改动相关的失败数：0。** 其余 57 个问题都是这台机器缺重依赖（lxml/scrapling/fastmcp/cryptography）
和一条发布线镜像 tag 漂移，属于既有状态，不在本 change 范围内。

**仍未做**：在装了全部依赖的容器里跑一次，让那 57 个也变绿——那是环境问题，不是代码问题，
但"环境缺依赖"和"代码没问题"是两件事，我不把前者当后者的证明。

---

# 端到端验证：7/7 单元测试全绿，功能其实是坏的（2026-11-09）

我说"等下一轮定时任务才有真实数据"，然后意识到不必干等——既有测试里有一个能跑通
`runner_cli.main()` 并捕获 `finish_run` kwargs 的夹具。用同一路径跑一遍即可。

## 结果：两个断言全失败

```
metrics: {"owner": "unknown", "upstreams": "unknown"}
✗ owner 缺失或 unknown
✗ upstreams 未从 manifest 读到
```

而 `tests/test_attribution.py` 的 7 条单元测试**全绿**。

## 两个 bug，都只能靠端到端发现

### ① `HOSTNAME` 在 macOS 上不是导出的环境变量

```python
holder = env.get("HOSTNAME") or env.get("FD_RUN_OWNER") or env.get("COMPUTERNAME") or "unknown"
```

`HOSTNAME` 在 zsh 里是 shell 变量、不导出给子进程。实测本机三个变量**全是 None**，
于是每次运行都记 `"unknown"`。单元测试里我传的是 `{"HOSTNAME": "crawler-abc123"}`——
**假对象顺从了我的假设**，所以测得再绿也没有意义。

修：加 `socket.gethostname()` 兜底。

### ② manifest 路径写死，忽略了 `FD_CONTENT_DIR`

```python
here = Path(__file__).resolve().parent.parent        # 包目录
text = (here / "spiders" / source / "manifest.yaml").read_text()
```

但 runner 是按 `FD_CONTENT_DIR` 找源的。分派器跑时它指向一个 checkout，
测试夹具在临时目录——**我却在包目录里找，于是永远 unknown**。

修：先查 `FD_CONTENT_DIR`，再退回包目录。

## 修后

```
metrics: {"owner": "Bruces-Mac-55.local", "upstreams": ["https://example.gov.cn/stats/"]}
```

## 变异验证（证明端到端那条是必需的）

| 变异 | 单元测试 | 端到端 |
|---|---|---|
| 去掉 `socket.gethostname()` 兜底 | 7/7 全绿 | **失败** |
| 路径只找包目录、忽略 `FD_CONTENT_DIR` | 7/7 全绿 | **失败** |

**两个变异都只让端到端那条红。** 这就是为什么必须有它：单元测试喂的是我构造的世界，
端到端跑的是真实解析路径。

## 这条教训比功能本身重要

今天一整天我都在说"声明 ≠ 实际"。这一轮证明同一句话对**测试**也成立：
**测试绿 ≠ 功能对**，如果测试的假对象是我自己写的、且顺从了我的假设。

判断依据只能是：端到端跑真实代码路径。7 条单元测试全部通过而功能完全失效，
这是最干净的反例。

---

# 事实层从文档变成可执行（2026-11-09）

之前两轮翻译都是 `/tmp` 里的一次性脚本。现在是包里的东西：

```
fd_agent_run/adapters.py    from_crew_journal / from_crawl_report
fd_agent_run/cli.py         新增 facts 子命令
```

```
python3 -m fd_agent_run.cli facts \
  --source crew:<journal.jsonl> \
  --source crawl:<health-report.json>
```

七个命令：`start / next / record / commit / status / facts / resume`。

## 真实数据跑出来的结果

```
crew:fd-fleet/.run/r-fleet2/journal.jsonl  -> 6 fact(s)
crawl:agriculture-alerts/health-2026-11-08.json -> 10 fact(s)
facts: 16, admissible: 13          EXIT=1
  FAIL merge/1: evidence[2] claims exit_code 0 without cmd_observed
  FAIL r4/1:    evidence[1] claims exit_code 0 without cmd_observed
  FAIL r5/1:    evidence[2] claims exit_code 0 without cmd_observed
```

**13/16 可采纳，三条失败——而这个结果是校验器对、我错。**

那三条是我在 5 路实验里提交的命令证据：`{"cmd":"node --test","exit_code":0,...}`。
我**确实跑了** `node --test`，但记录里没标"这条命令是被观测到的"。校验器无法区分
"我跑了"和"我填了个 0"，于是拒绝。

## 由此修了一处（不是改判据，是补记录）

`record` 现在会给带 `cmd` 的证据自动加上 `cmd_observed: true`——因为**进到这个函数就说明
命令已经跑过了**。修后新记录校验通过：

```
facts: 1, admissible: 1    EXIT=0
```

**旧 journal 不改**：那是历史记录，改了就等于伪造。它继续不合格，这是正确的结果。

## 一个没改的地方（有意）

`from_crew_journal` 遇到没有 `declared_at` 的 predicate 时，写 `unknown`，**不写任务时刻**。
理由：写任务时刻会让它校验通过，而事实是"这个 ledger 当时没记"。**适配器不能改善输入。**

## 变异验证

| 变异 | 结果 |
|---|---|
| 适配器把 `declared_at` 补成任务时刻 | `test_absent_declared_at_does_not_become_the_task_time` 红 |
| crawl 适配器把 owner 填成 `"cron"` | `test_owner_is_unknown_not_a_role_word` 红 |
| crawl 适配器给 evidence 补 `exit_code: 0` | `test_evidence_carries_only_observations` 红 |

## 现在这条链是完整的

```
substrate 数据  ->  adapter  ->  fact(9 字段)  ->  validate(7 条规则)  ->  exit code
     (真)          (不改输入)      (unknown 一等)      (只判"有没有把不知道说成知道")
```

**两个 substrate、一个形状、一次校验、一个退出码。** 这就是"底座"能给出的最小东西：
不是编排能力，是**可采信性**。

---

# 治理层第一步：决策卡片（2026-11-09）

你说“都很重要”，我连着三次把决定推回给你。那是偷懒。这一轮我自己定了三条治理语义，
并把最关键的那条（**什么时候该叫人**）做成了东西。

## 三条治理决定（我的判断，你不同意再改）

| 决定 | 定什么 | 依据 |
|---|---|---|
| 所有权 | 同一时刻一个持有者，显式领取 + 租约过期 | 今天我两个编排器抢同一目录，代价实打实 |
| 预算 | 尝试数 + 墙钟截止；超了降级不停机，花钱类硬停 | 停机比降级更贵；钱不可逆 |
| 升级排序 | 只有三类必须叫人：不可逆 / 预算耗尽 / 判据不可知，其余给安全默认值并记录 | 人的注意力只该花在不可逆上 |

## 卡片长什么样

```
1 件需要你决定（涉及 16 个任务），其中 0 件不可逆
[48] 8 个任务都没有可判定的完成条件，要不要一起补？
    为什么现在: 判据缺失或没有声明时刻，无法判断是否完成
    任务: 8 个任务  (predicate-unknowable)
    涉及: crawl/agri-casde/latest, crawl/cncotton/latest, … 另 2 个
    - 补判据: 为该任务声明一个可求值条件 (代价: 一次编辑; reversible)
    - 降级为 explore: 只要求有证据，不要求结论 (代价: 结论变弱; reversible)
    - 取消该任务: 从本轮移除 (代价: 少一路产出; reversible)
    没人回答时: 不派发（任务停在 pending，原因记录为 no-predicate）
```

一句话问题 + 三个选项各带代价 + 没人回答时的默认 + 可逆性。**三十秒能答完。**

## 两条硬规则

1. **不可逆动作的默认值是“不执行”。** 没人看的卡片不能让任何不可逆的事发生。
   （有专门测试：`test_irreversible_default_is_do_not`）
2. **排序是算出来的，不是声明的**：`不可逆 × 影响面 × 等的代价`，三个因子各有权重，
   缺一个不能当零（否则卡片可以靠省略字段伪造紧急度）。

## 这一轮抓到的两个真问题

### ① 8 张一模一样的卡片

真实数据第一版输出 **8 张完全相同的卡**（8 个爬虫源都没有可判定判据）。
那不是 8 个决定，是 1 个决定重复了 8 次——**正是我今天批评过的“问得太多”**，
而且重复的队列会训练人忽略它。

已加 `merge()`：同触发 + 同问题的卡片合并，列出涉及的任务，答一次解决全部。
真实数据从 8 张降到 **1 张**。

### ② 缺字段被当成了“没问题”

判据没有 `declared_at` 时，我原来的检查只认显式 unknown 标记，**字段不存在时取到 None，直接放行**。
而“字段不存在”恰恰是最常见的情况（旧 journal 根本没有这个键）。

已修：缺失与显式 unknown 同等对待。这是这张卡片存在的意义——**省略不该等于合规**。

## 命令

```
python3 -m fd_agent_run.cli escalations --source crew:<journal> --source crawl:<report>
```

退出码：0 = 没有需要你决定的事；**1 = 有事需要你**。这是整个工具里唯一一处
“非零是好消息”的地方。

现在八个命令：`start / next / record / commit / status / facts / escalations / resume`。
102 测试。


---

# 归档时发现的并发规格（2026-11-09）

归档后发现 `openspec/specs/fact-ledger-kernel/` 已存在（13:40 创建，比本 change 的规格早 24 分钟），
**来自另一个并发会话**。不是冲突，但必须记下来，否则以后两边会各自演进到互相矛盾。

## 两边的分工

| 主题 | `fact-ledger-kernel`（另一会话） | `fact-ledger`（本 change） |
|---|---|---|
| 事实只追加 | ✅ | — |
| 更正以冲正表达 | ✅ | — |
| 派生可重算 + 快照水位 | ✅ | — |
| 写入幂等键去重 | ✅ | — |
| 生命周期状态封闭 | ✅ | ✅（verdict 四态） |
| 未过确认门的不算事实 | ✅ | ✅（判据前置、未观测不算） |
| unknown 一等值 | — | ✅ |
| 证据可复核 / 适配器不改输入 | — | ✅ |
| 决策卡片 / 排序 / 合并 | — | ✅ |

**它管一致性纪律**（append-only、冲正、幂等、水位、续跑），**我管可采信性与打扰策略**
（unknown 语义、判据前置、什么时候叫人）。

## 两处重叠，是同义不是冲突

1. "生命周期状态封闭" ↔ "verdict 四态"：kernel 管状态机合法迁移，我管结论取值。
2. "未过确认门的不算事实" ↔ "判据前置 + 未观测的成功不算成功"：kernel 说必须有确认门，
   我说确认门必须早于工作。

**以后若要合并，应让 kernel 承载纪律、让 fact-ledger 承载字段与语义**，而不是二选一。

---

# 两个包接上了：从图直接建运行（2026-11-09）

在这之前，`fd-agent-graph` 说"该跑哪几个节点"，`fd-agent-run` 说"跑到哪了"，
**两者之间只靠我手打 `--task` 衔接**。那意味着图说该跑 7 个节点、台账只登记 5 个，
没有任何东西会发现——这正是这一整天在批的形态，只不过发生在两个包之间。

## 接法

`fd-agent-graph metrics` 新增 `--nodes`（比新增子命令克制），输出展开后的节点列表；
`fd-agent-run start --graph <yaml> [--roster <yaml>]` 通过 **CLI 子进程**借它，
不 import——两个包互不依赖，任何一个可替换。

## 一条命令的效果

```
$ start --graph team-v3.yaml --roster team-v3.yaml
demo: 8 task(s) pending
   contractor/1  impl#1/1  impl#2/1  merge/1
   planner/1     relay/1   sentry/1   verify/1
```

注意 `impl#1` / `impl#2`：**roster 的 `parallel: 2` 展开成了两个任务**。之前手打
`--task` 时我只会写一个 `impl/1`，于是图说 8 个、台账 7 个。

## 建完立刻告诉你还缺什么

```
$ escalations --source crew:.../journal.jsonl
1 件需要你决定（涉及 8 个任务），其中 0 件不可逆
[48] 8 个任务都没有可判定的完成条件，要不要一起补？
```

**不是建完就报成功，是建完就说出缺口。** 这是这套东西该有的样子。

## 过程中踩的两个坑（都是我自己的）

1. **锚点没匹配就静默跳过**：用 `s.replace(anchor, ...)` 插入函数和常量，锚点字符串不存在，
   `replace` 什么都不做、也不报错。于是 `NameError: _nodes_from_graph is not defined`。
   教训：**批量替换必须断言替换真的发生**（这次后面几次都加了 `assert anchor in s`）。
2. **相对路径传给子进程**：子进程的 cwd 是 `GRAPH_PKG`，`./examples/team-v3.yaml` 在父进程里
   成立、在子进程里失效。已改为 `Path(...).resolve()` 再传。
   这与之前 `_attribution` 忽略 `FD_CONTENT_DIR` 是同一类错——**路径的基准点没想清**。


---

# 之前说"不做"的那件事，我判断错了（2026-11-09）

上一轮我把"爬虫规则前置"归为 **运营决策**（改 160 个源的运维习惯），因此不做。

**那个判断是错的。** 如果做成 manifest 里的**可选 `accept:` 块**，就是纯增量的：
不声明 = 行为逐字节不变，160 个源一个都不受影响；谁想用谁声明一行。

## 做法

```yaml
name: moa-hog-price
accept:
  min_rows: 10        # 至少写 10 行
  max_age_days: 45    # 数据不得超过 45 天（记录但不判定，见下）
```

`runner_cli` 在**状态提交之前**求值，不达标则 `success` → `partial`。

## 为什么必须放在提交之前

巡检的 `health.status` 是**事后判定**（`rule1: last_success_age<=840h`），它只能记录结论。
在提交前求值，才能让"0 行不是成功"这件事**成真**而不是"事后记一笔"。

**这是报告与闸门的区别。**

## 三条安全规则

1. **没有 `accept` 块 → 返回 None，不写任何 metrics 键。** 不是写 `{"ok": true}`——
   那会把"没问过"变成"通过了"，正是事实层存在的意义所在。
2. **`max_age_days` 只记录不判定**：runner 不知道数据的年龄，声称通过就是撒谎。
   所以它的 `ok` 是 `None`（未判定），而**未判定不会让运行失败**。
3. **未知键判失败**：`min_row`（少个 s）这样的拼写错误会静默接受一切，
   与规则的目的正相反。

## 一个我差点造成的生产事故

端到端跑通后发现 rc 从 0 变 3。顺着查 dispatcher：

```python
pending_status = {"success": "done", "cancelled": "cancelled"}.get(run_status, "failed")
_auth.release_identity(conn, ident["id"], ..., success=(run_status == "success"))
```

**`partial` 会被当成 `failed`，并且把登录账号按失败释放。** 一个源声明了 `min_rows: 10`、
这次只写 8 行，就会连带污染它用的账号——后果比"这轮数据不达标"大得多。

已修：`partial` 在两处判定里与 `success` 同等对待（任务完成、账号成功），
因为**采集本身是成功的，不达标的是数据**。同时确认巡检按 `status` 统计时会把 `partial`
自然计入，不会掉进盲区。

## 验证

| 项 | 结果 |
|---|---|
| 新测试 `test_accept_conditions.py` | **11/11** |
| 既有测试未改动即通过（纯增量证据） | `test_first_crawl_window` + `test_attribution` 等 **100/100** |
| 端到端：无 `accept` 块 | `status=success`、metrics 无 `accept` 键 ✓ |
| 端到端：`min_rows: 10` 而 0 行 | `status=partial`、`metrics.accept.checks` 有判定依据 ✓ |
| 爬虫包全量 | **1151 passed**（原 1140），失败数仍 53（均为缺依赖/tag 漂移） |

## 修正后的判断

这件事**不是**运营决策，是给源一个可选能力。真正需要运营决策的是**谁先声明**——
那一步我没做：`accept:` 块目前在所有 manifest 里都不存在，所以线上行为零变化。


---

# 我造了一个重复机制，然后拆了它（2026-11-09）

上一轮我加了 `accept.min_rows`，理由是"给源一个可选的完成条件"。

**做下一轮时才发现：这个字段早就有了。**

## 已有的东西

```python
# fd_industry_data/health/inspector.py:255
raw = doc.get("min_rows_per_run")
```

manifest 字段 `min_rows_per_run`，语义完全一致（>0 整数、未声明即不生效），
巡检读它出工单。已有 1 个源声明（`aiph-stats`）。

我造的 `accept.min_rows` 是**同一件事的第二套说法**——正是这一天反复批的
"两处各写一套断言，最后靠人裁决"。

## 改法：复用字段，只补缺的那半个

真正缺的从来不是"能不能声明"，而是**什么时候判定**：

```
巡检：事后判定 → 只能记录结论、出工单
runner：提交前判定 → 可以拒绝把 success 写进库
```

所以现在：`min_rows_per_run` 被 runner 读，在状态提交前求值；`accept:` 块
只保留巡检无法表达的东西（当前只有 `max_age_days`，且只记录不判定）。

## 加了一条测试专门防这条路

`test_same_value_as_the_inspector` 拿 8 种写法跑两个读取器，断言答案一致：

| manifest 写法 | 我的读取器 | 巡检 |
|---|---|---|
| `min_rows_per_run: 10` | 10 | 10 |
| `"10"`（带引号） | **None** | None |
| `0` / `-5` / `abc` / 注释掉 | None | None |

**第一版这条测试就抓到了真差异**：我把 `"10"` 读成 10，巡检读成 None（YAML 里引号是字符串，
`isinstance(raw, int)` 为假）。已改成与巡检一致——**两处不一致意味着同一份 manifest 会让
runner 和工单得出不同结论**，那个后果比"宽松一点"严重得多。

## 过程中还踩了三个

1. **`block` 可以为 None 而 `min_rows_per_run` 已设置** —— 两个声明互相独立，
   我却在 `_evaluate_accept` 里直接 `in block`，`TypeError`。
2. **测试夹具路径不符两侧约定** —— runner 找 `<FD_CONTENT_DIR>/<source>`，
   inspector 找 `<repo>/spiders/<source>`。夹具少一层 `spiders/`，于是"两处不一致"是假的。
3. **系统 Python 没有 yaml** —— 巡检的读取器在那种环境下直接 `ModuleNotFoundError`。
   测试里改成"能跑才比对"，而不是伪造一个比较结果。
