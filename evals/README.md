# 触发测试（skill-trigger）

## 为什么有这个目录

技能不触发，等于没装。11 个技能的 description 必然存在重叠（"写个 agent" vs "做个智能体"、"上下文超了" vs "多轮对话串了"、"技能不触发" vs "技能怎么分发"），
这类问题**读代码看不出来，只有拿真实说法去撞才会暴露**。本目录存触发测试样本与判定口径。

同时它也是 check-agent-standards 第 19 项（评测集文件存在）的判据所在。

## 文件

| 文件 | 内容 |
|---|---|
| `skill-trigger.jsonl` | 22 条触发样本，每行一个 JSON |

## 字段

| 字段 | 含义 |
|---|---|
| id | T01~T22 |
| layer | smoke（主路径）/ regression（历史易错）/ adversarial（负样本与边界） |
| input | 用户原话，保留真实表述 |
| expect_main | **必须**命中的技能（主责），为空表示不该命中任何 agent-forge 技能 |
| expect_optional | 可以同时加载（伴随加载不算错） |
| must_not | **不得**命中的技能，命中即判定失败（这是最容易出问题的一项） |
| judge | 判定方式。当前为 manual：真实触发结果只能看会话里实际加载了哪些技能 |
| source | 构造 / 真实 |

## 怎么跑

1. **新开一个会话**（技能目录在会话启动时扫描，旧会话看不到改动）
2. 把 `input` 原话**作为单独一条消息**发进去，不要补充背景、不要和别的原话拼在一起
3. 记录该条消息之后实际加载 / 调用的技能列表（看首轮 `use_skill` 工具调用与加载记录）
4. 按下面三条判定：

> **逐条独立是硬要求**：一条消息里放多条原话，选择器只会对这整条复合输入做**一次**判定，
> 产生的 `use_skill` 调用不能拆开冒充多条样本的实测结果。同一会话内连续逐条发送会有上下文累积，
> 严格消除该影响需每条样本各开一个干净会话；若在同一会话逐条发，必须在判定记录里注明这一局限。

| 条件 | 结论 |
|---|---|
| expect_main 中有技能未命中 | fail（该加载没加载） |
| must_not 中有技能被命中 | fail（误触发，比漏触发更危险） |
| 其余（含 expect_optional 命中、多余但不冲突的加载） | pass |

## 判定记录

| 轮次 | 日期 | 结论 | 改动 |
|---|---|---|---|
| v1（盲判） | 2026-11-06 | 19 pass / 1 fail（T18） | 建立样本集；check 的 description 补适用边界 |
| v2（复测） | 2026-11-06 | T18 转 pass（仅命中 workflow） | eval 的 description 加否定边界 |
| v3（真实选择器 · CodeBuddy IDE） | 2026-11-07 | 3 pass / 2 fail（T05、T18 漏命中，已排除上下文累积） | 未改任何 description；T05 标 source=真实，两条确认为真实缺陷 |
| v3 复测（独立干净会话） | 2026-11-07 | T05 加载 workflow 而非 core；T18 无任何技能加载 | 两条 fail 维持；真实加载与盲判不一致，前置条件①仍不满足 |
| v4（改后盲判，非实测） | 2026-11-07 | 17/17 pass（smoke 12 + adversarial 4 + T05） | 改 description 后重跑两层；T05 / T18 盲判转 pass，真实选择器实测见 v5 |
| v5（真实选择器 · 改后 · CodeBuddy IDE） | 2026-11-07 | 5/5 pass（五个独立干净会话，must_not 全未命中） | 未再改 description；SKILL-MAP 4.9 前置条件①转已完成 |
| v6（盲判 · 11 技能版，非实测） | 2026-11-08 | 22/22 pass（smoke 13 + adversarial 5 + regression 4，must_not 全未命中） | 新增第 11 个技能与 T21 / T22 样本；既有 10 个 description 未改；真机选择器实测待执行 |

### 盲判方法（可以复用的做法）

让一个**不知道标准答案**的子代理，只读 11 个 SKILL.md 的前 12 行（frontmatter），对样本逐条判断"会加载哪些技能"，再与 `expect_main` / `must_not` 对照。

- 好处：避免"自己判自己"的偏差（出题人和答题人分离）
- 代价：不是真实客户端的选择器，但判定环节是同一个模型，够用
- 规矩：子代理禁止读本目录与 README / SKILL-MAP，否则等于把答案给它

### v1 结果

19 条与期望一致，包括此前担心的四条歧义全部未发生：T04 未误命中 tool、T05 未误命中 context-memory、T06 未误命中 core、T08 未误命中 rag。

唯一失败是 T18「提示词改了一句，感觉效果好像好点」：盲判命中 `agent-dev-workflow` + **`agent-eval-standards`**。用户只是顺口说"感觉好点"，把评测规范拉进来会导致要求先写评测口径、先建评测集——对一次提示词微调属严重过度加载。

修法：eval 的 description 增加否定边界（"不要因为'感觉效果变好了'这类随口表述就加载本技能"），并把触发词"效果怎么量化"限定为"要对比或验收时"。样本的 must_not 补上 eval，标注 source 为"真实"（这条是跑出来的，不是构造的）。

### v2 结果

T18 复测只命中 `agent-dev-workflow`，eval 不再被拉入。同批复测的对照样本（T03 工具拆分、T19 普通代码排错、T10 成本诊断）均与期望一致。

### v1~v2 阶段的局限（已由 v3 / v5 消除）

v1~v2 是子代理盲判，**不是真实客户端的技能选择器实测**。该局限已在 v3 / v5 消除：旧 description 下真实实测 3 pass / 2 fail，改后五个各自独立的干净会话 5/5 pass，结论以真实结果为准并已回填样本。

> 2026-11-07 终态复核：真实选择器实测已执行。旧 description 下 3 pass / 2 fail（T05 / T18 漏命中，
> 已排除上下文累积），据此按第 11 节 #8 修正触发词并重跑两层盲判；改后五个各自独立的干净会话
> 实测 5/5 pass，`must_not` 全未命中。**agent-skill-authoring 的定稿前置条件①已完成**（见 SKILL-MAP 4.9），
> 连同已完成的②，两项前置条件现已全部满足。

### 实测执行清单（v5 改后实测已回填）

下面五行按 T04 / T05 / T06 / T08 / T18 顺序排列，可直接逐条复制进**新开的会话**，不要补充背景、不要加前缀：

```
这个 agent 死循环了，一直调同一个工具停不下来
会话中断之后恢复不了状态，只能从头再问一遍
上下文老是不够用，历史越堆越多，最后直接被截断
用户能不能通过上传的 PDF 里写指令，让 agent 把数据删了
提示词改了一句，感觉效果好像好点
```

| 样本 | expect_main | must_not | 实际加载的技能 | 判定 |
|---|---|---|---|---|
| T04 | agent-core-standards | agent-tool-standards | `agent-core-standards`、`agent-dev-workflow` | pass |
| T05 | agent-core-standards | agent-context-memory-standards | `agent-core-standards` | pass |
| T06 | agent-context-memory-standards | agent-core-standards | `agent-context-memory-standards` | pass |
| T08 | agent-safety-standards（tool 可伴随） | agent-rag-standards | `agent-safety-standards` | pass |
| T18 | agent-dev-workflow（check 可伴随） | agent-rag / ops / tool / eval | `agent-dev-workflow` | pass |

判定口径按上面的三条规则：expect_main 有漏命中即 fail；must_not 有命中即 fail；其余 pass。
上表为 **v5（改 description 后）** 的实测结果；v3 旧结果保留在下方详录中供比对。
T04 额外加载的 `agent-dev-workflow` 属于 `expect_optional`，按口径不算错。

> 2026-11-07 v5 已回填，**前置条件①已完成**：五个各自独立的干净会话 5/5 pass，
> `must_not` 全部未命中。连同已完成的②，`agent-skill-authoring` 定稿的两项前置条件全部满足。
> 禁止用盲判结果冒充真实选择器实测。

### v5 改后真实选择器实测（2026-11-07）

安装刷新到 `83e112f` 之后，五个**各自独立的干净会话**（每个会话仅 1 条 user 消息，首条即样本原话），
从会话历史按会话提取 `use_skill` 调用（不是模型自述）：

| 样本 | 会话内实际 `use_skill` | expect_main | must_not | 判定 |
|---|---|---|---|---|
| T04 | `agent-core-standards`、`agent-dev-workflow` | 命中 | 未命中 | pass |
| T05 | `agent-core-standards` | 命中 | 未命中 | pass |
| T06 | `agent-context-memory-standards` | 命中 | 未命中 | pass |
| T08 | `agent-safety-standards` | 命中 | 未命中 | pass |
| T18 | `agent-dev-workflow` | 命中 | 未命中 | pass |

五条全绿，旧 description 下的两条漏触发（T05 误走 workflow、T18 无加载）均已消除，
且新增触发语未把 T06 的 core、T19 / T20 的 workflow 误拉进来（后两条见 v4 盲判）。
改后真实加载与盲判一致，SKILL-MAP 4.9 前置条件①据此转已完成。

### v3 结果详录（2026-11-07）

数据来源：安装刷新到 `fc6b71b` 之后的会话历史，逐条提取 `use_skill` 调用（不是模型自述）。
T04 / T06 / T08 来自一个会话内依次发送的独立消息；T05 / T18 来自**两个各自独立的干净会话**。

| 样本 | 实际 `use_skill` | must_not 是否命中 | 判定 |
|---|---|---|---|
| T04 | `agent-core-standards` | 否 | pass |
| T05 | `agent-dev-workflow` | 否 | fail（core 漏命中） |
| T06 | `agent-context-memory-standards` | 否 | pass |
| T08 | `agent-safety-standards` | 否 | pass |
| T18 | 无 | 否 | fail（workflow 漏命中） |

**复测已排除上下文累积**：五条最初在同一会话内依次发送，两条无技能加载曾被标为「存疑」；
随后在各自独立的干净会话里分别只发 T05、T18，结果是 T05 加载 `agent-dev-workflow`、
T18 仍无任何技能加载。两条 fail 维持，且 `must_not` 全部未命中——是**漏触发**，不是误触发。

**结论**：真实加载与盲判不一致，SKILL-MAP 4.9 前置条件①当时不满足。按维护规则，
T05 的 `source` 已从「构造」改为「真实」，T18 备注追加本次证据；`description` 尚未修改。
修 `description` 会改变选择器输入，须按「改动先改 SKILL-MAP.md」的纪律同步职责卡与边界、
跑体积审计并刷新基线、重跑 smoke + adversarial 两层，之后重做真实选择器实测。

> 2026-11-07 补充：上述纪律已执行（SKILL-MAP 4.1 / 4.2 与第 11 节 #8、两个 description、
> 体积审计与基线、两层盲判 17/17）。**本表所有 v3 实测结果只对旧 description 成立，改后一律失效**；
> 改后的真实选择器实测见上方 v5（5/5 pass），前置条件①已解锁。盲判转 pass 不等于真实选择器转 pass。
> 本表保留为旧 description 下的历史基线，不删、不改写数字。

### v3 尝试记录（作废）：复合输入不产生逐条判定（2026-11-07）

安装刷新到 `fc6b71b` 之后开过一个干净会话，从会话历史读到的事实：

| 项 | 实测值 |
|---|---|
| user 消息数 | 1 条，五句原话全部在同一条消息里（含系统注入内容的文本共 16870 字符） |
| `use_skill` 调用 | 仅 1 次，加载 `agent-dev-workflow` |
| 其余技能 | 未加载 |

**判定：该会话作废，不计入 v3（当次未回填，前置条件①当时仍为未完成）。** 这是执行方式错误（五句合并成一条），
不是选择器误触发或漏触发；把这一次 `agent-dev-workflow` 调用拆成 T04 / T05 / T06 / T08 / T18 五条结论属于伪造。
该会话可作为一条真实的边界观察保留：复合症状输入会先走 `agent-dev-workflow` 的入口分诊，
但不计入本样本集的逐条判定。

重跑方式：在干净会话里把上面五行**一条一条单独发送**（每行一次回车，不要合并成一条消息），
发完由读取会话历史按消息顺序逐条提取 `use_skill`，再回填上面的两列。

> 后续：已按此方式重跑并回填 v3，结果见「v3 结果详录」；T05 / T18 已在各自独立的干净会话里复测，
> 两条仍为漏命中，上下文累积的解释被排除。

### 实测前置门槛：安装态版本核对（2026-11-07）

真实选择器实测的前提是：**新会话扫描到的技能目录，确实是当前这份代码**。按来源铁律，安装只认 git 远端，
技能目录为链接形态（软链指向安装副本），本地工作区改动不推送就永远不会被加载。

| 核对项 | 2026-11-07 实测值 | 判据 |
|---|---|---|
| 开发源 HEAD | `4d8fbb3` | 本轮 5 个提交 + 3 个既有 fix，工作区干净 |
| 远端 `main` | 落后 7 个提交 | `origin/main..HEAD` 共 7 条，均未推送 |
| 安装副本快照 | `8ca7940`（2026-11-06） | 安装标记 `.agent-forge-origin` 记录 |
| 安装形态 | link | 两处技能目录均为软链，无断链 |
| 新增细则文件 | 安装副本缺失 | `agent-tool-standards/references/` 与 `agent-safety-standards/references/` 均不存在 |
| **结论** | **不合格** | 新会话扫到的是旧版，观察结果不得记为 `v3` |

**硬判据**：安装副本 `skill/` 必须与开发源 `skill/` 逐字节一致——选择器只读技能目录，
仓内 evals 文档等非 `skill/` 提交不影响实测有效性；安装日志的「远端快照」与安装标记 commit 用于版本追溯，
若只因非 `skill/` 提交而落后于开发源 HEAD，记录差异即可，不必重装。
在上表结论为「不合格」时跑五条清单，测的是 2026-11-06 的版本，结果只能作废，不能用来解锁前置条件①。

执行序列（已于 2026-11-07 获授权后完成，按顺序、缺一不可）：

```
git push origin main
./install.sh --link --force
```

> 2026-11-07 更新：上表是**刷新前**的时点核对，结论「不合格」已被推翻。
> 推送 8 个提交后重跑安装，日志打印「远端快照：fc6b71b ...」，与开发源 HEAD 一致；
> 安装标记 commit 同步为 `fc6b71b`，两份新增细则就位、软链无断链，开发源 `skill/` 与安装副本
> `skill/` 逐字节一致。**安装态硬判据现已满足**，不再阻断实测；当前阻断项已转为 T05 / T18 的真实 description 缺陷。
> 此后 v3 回填只改本目录文档、未动 `skill/`，按上述硬判据不影响实测有效性。

### 静态预检：2026-11-07 某一轮的时点结论

当时对 10 个 `SKILL.md` 的 frontmatter 与安装副本逐个比对，结果全部一致（`name` / `description` 未改），
`description` 最长 963 字符，未超 1024 上限；技能体积审计结论为「全部在预算线内」。
该轮改动只落在正文、加载矩阵与新增 `references/`，因此当时 v1 / v2 的盲判结论仍成立。
**此结论只对当时的 10 技能版本成立**：11 技能版新增了一份 description，选择器输入已改变，以 v6 为准。

### v6 盲判与待执行真机清单（2026-11-08）

第 11 个技能 `agent-skill-authoring` 定稿后，样本补到 22 条（T21 正向、T22 相邻边界）。
让不知道标准答案的子代理只读 11 份 frontmatter 逐条盲判，结果 22/22 pass：
smoke 13 + adversarial 5 + regression 4 全部命中 `expect_main`，`must_not` 全未命中。
关键边界 T17 / T22 都只加载 `agent-forge-source-law`，新技能的「分发」触发词没有抢走安装排查类输入；
正向 T21 只加载 `agent-skill-authoring`。既有 10 个技能的 description 本轮一字未改。

> **本轮结论只到盲判为止，不是真实选择器实测。** 新技能首次引入 description，
> 必须在安装态刷新后由用户在新开的、各自独立的干净会话里逐条实测，回填前不得称「验收通过」。

待执行清单（8 条，每条各开一个干净会话、只发一条原话、不要加前缀或背景）：

```
技能装了但是不生效，说'技能不触发'也没反应
帮我写个技能，SKILL.md 该怎么写，触发词要怎么定
技能装了但不生效，版本好像也不对，软链是不是断了
这个 agent 死循环了，一直调同一个工具停不下来
会话中断之后恢复不了状态，只能从头再问一遍
上下文老是不够用，历史越堆越多，最后直接被截断
用户能不能通过上传的 PDF 里写指令，让 agent 把数据删了
提示词改了一句，感觉效果好像好点
```

前三条验证第 11 个技能的边界与正向加载，后五条（v5 原班样本）验证 11 技能环境下历史易错项没被抢。
判定按三条口径，实测结果按 v5 的格式回填一张表。

## 维护

- 每次真实误触发 / 漏触发都要回填一条样本（来源写"真实"），只增不删
- 改任何 SKILL.md 的 description 后，至少重跑 smoke + adversarial 两层
- 样本过时不删，标 `deprecated` 并保留（历史退化可比对）
