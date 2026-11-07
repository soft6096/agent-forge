# agent-forge

> AI Agent 开发的"规范锻造炉"——把 agent 开发中**没有确定答案**的部分（循环怎么转、工具怎么定、上下文怎么分配、效果怎么算），收敛成可核对、可评测、可回归的工程规范。

**English:** agent-forge is a family of Agent Skills for AI agent development. It sits between generic software methodology and framework tutorials: it does not teach you how to call an SDK, it constrains what you build into shapes that can be mechanically checked and statistically evaluated. The flagship skill `agent-dev-workflow` enforces one rule above all others — **define the evaluation before writing the code**.

当前状态：**10 个技能已落地**，可直接安装使用；仅 `agent-skill-authoring` 仍在规划（需前 10 个技能真实运行一轮后再定稿）。

---

## 一、为什么需要

直接让 AI 写 agent，常见结果是：demo 能跑，上线就露馅。原因不是模型不行，而是四件事没人管：

1. **没有边界** —— 只说"要做什么"，没说"不做什么"，越权与幻觉无法拦截
2. **没有评测** —— 改完提示词靠"感觉变好了"，一改修好 A 又弄坏 B
3. **没有骨架纪律** —— `while(true)` 靠模型自己停，无步数上限、无成本上限、无终止条件
4. **没有可核对项** —— 全是"应该""建议"，没有任何一条能在交付前机器扫一遍

agent-forge 就是把这四件事变成流程与规范。核心方法只有一条：**评测口径必须先于编码**。agent 的输出是分布式的、非确定的，没有评测集的"验收"本质是自证。

## 二、技能家族

### 第一批（已落地，5 个）

| 技能 | 类 | 职责 |
|---|---|---|
| [`agent-dev-workflow`](skill/agent-dev-workflow) | 流程 | 0.0~7.1 十一步完整链路，13 个命令、11 份产物模板（另 2 份通用规范）、每步人工闸门 |
| [`agent-core-standards`](skill/agent-core-standards) | 规范 | 骨架：主循环、状态机、五类终止条件、失败分类、重试与熔断、幂等、死循环识别、会话与并发 |
| [`agent-eval-standards`](skill/agent-eval-standards) | 规范 | 评测：四层数据集、六项指标、判定方式与模型裁判的六个坑、回归基线、三层门禁、失败归因 |
| [`check-agent-standards`](skill/check-agent-standards) | 核对 | 30 项核对（17 项机械判定 + 13 项人工判定）+ 扫描脚本，每项附「文件:行号」证据 |
| [`agent-forge-source-law`](skill/agent-forge-source-law) | 元 | 来源铁律：区分开发源与安装产物，安装只认远端 |

### 第二批（已落地，5 个）

| 技能 | 类 | 职责 |
|---|---|---|
| [`agent-tool-standards`](skill/agent-tool-standards) | 规范 | 工具粒度与命名、schema 九字段、副作用分级、幂等与去重窗口、权限边界、错误语义、超时归属、MCP 接入、工具选择策略 |
| [`agent-context-memory-standards`](skill/agent-context-memory-standards) | 规范 | 上下文预算表、组装顺序与不可变段、裁剪与滚动摘要、降级阶梯、三层记忆与写入判据、遗忘与污染防护 |
| [`agent-safety-standards`](skill/agent-safety-standards) | 规范 | 提示词注入与内容隔离、越权防护、高危工具人在环、双向过滤与脱敏、审计留痕、人工接管 |
| [`agent-ops-standards`](skill/agent-ops-standards) | 规范 | token 计量口径、成本核算与三层预算、缓存与模型分级、trace/span 与日志七字段、告警三档、降级与看板 |
| [`agent-rag-standards`](skill/agent-rag-standards) | 规范 | 切分与索引更新、召回阈值与重排、查询改写与多路融合、引用强制、无命中拒答、权限过滤、离线召回评测 |

### 规划中（1 个）

| 技能 | 类 | 职责 | 批次 |
|---|---|---|---|
| `agent-skill-authoring` | 规范 | 技能工程：SKILL.md 写法、触发词、渐进披露、体积预算、分发与版本 | 三 |

## 三、流程一览

```
0.0 项目初始化（11 项基础设施硬检查）
  ↓
0.5 存量扫描（改造既有 agent 时）→ 改造决策闸门
  ↓
1.1 能力清单（正向能力 + 负向边界 + 成功判据）
  ↓
1.2 失败模式（幻觉/串工具/死循环/越权/成本失控/上下文溢出）
  ↓
2.1 评测口径与数据集  ★ 强制前置，未确认禁止编码
  ↓
3.1 架构方案（循环形态/状态机/预算表/记忆/降级）
  ↓
3.2 工具清单与契约（九字段，未确认不得实现工具）
  ↓
4.1 任务拆解（评测集建设单列 Phase 1）
  ↓
5.1 编码（编码完成即停）
  ↓
6.1 规范核对（独立节点，跑脚本 + 人工判定）
  ↓
7.1 评测与覆盖（统计式结论，失败样本归因）
```

每条斜杠命令：`/agent-forge`、`/agent-init`、`/agent-legacy-scan`、`/capability-list`、`/failure-modes`、`/eval-spec`、`/agent-design`、`/tool-contract`、`/task-breakdown`、`/implement`、`/check-agent`、`/eval-run`、`/agent-progress`。

## 四、安装

> 安装来源只认 git 远端。本地工作区一律不作为安装来源——本地改完必须提交并推送才能装到。

```bash
git clone https://github.com/soft6096/agent-forge.git
cd agent-forge

./install.sh --list          # 先看会装到哪
./install.sh --link          # 链接形态：一份副本 + 软链，多 Agent 混用推荐
./install.sh                 # 拷贝形态：兼容性最好，每个 Agent 一份实体
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--link` | 链接形态，安装副本在 `~/.agents/repos/agent-forge` |
| `--tool claude` | 只装到指定工具，可重复 |
| `--ref <ref>` | 指定分支 / 标签 / 提交 |
| `--force` | 覆盖已存在的技能（拷贝形态先备份为 `<技能名>.pre-agent-forge-<时间戳>`，链接形态只重建软链） |
| `--no-global-rules` | 不分发全局规则受管块 |

已有技能目录时默认**不覆盖**，安装日志会打印「跳过（未更新）」，末尾再汇总一次跳过数量。升级到新版必须显式加 `--force`，否则装到的还是旧版。

安装完成后新开会话，说：

```
/agent-forge 一个能查订单状态的客服 agent
```

看不到斜杠命令也没关系，说"按流程开发一个 agent"效果等价。

## 五、单独使用某个技能

不需要全流程时可直接触发对应技能：

| 你想做什么 | 怎么说 |
|---|---|
| 写 agent 主循环 / 修死循环 | "写个 agent 循环" / "agent 跑飞了"（加载 agent-core-standards） |
| 定评测口径 / 建评测集 | "怎么验证这个 agent" / "做评测"（加载 agent-eval-standards） |
| 加工具 / 接 MCP / 工具选不准 | "加个工具" / "接 MCP"（加载 agent-tool-standards） |
| 上下文爆了 / 要加记忆 | "上下文超了" / "加记忆"（加载 agent-context-memory-standards） |
| 防注入 / 高危操作确认 | "提示词注入" / "高危操作要人工确认"（加载 agent-safety-standards） |
| 成本与监控 | "太贵了" / "加 trace"（加载 agent-ops-standards） |
| 召回不准 / 引用出处 | "召回不准" / "做 RAG"（加载 agent-rag-standards） |
| 交付前检查 | "对 src 跑 check-agent-standards" |
| 技能没生效 | "技能不触发"（加载 agent-forge-source-law） |

## 六、仓库结构

```
agent-forge/
├── README.md / AGENTS.md / SKILL-MAP.md / global-rules.md / install.sh
├── .gitignore                    # 含 !**/SKILL.md 反忽略
├── .size-baseline.json           # 体积基线，改技能后跑审计对比
├── evals/                        # 技能触发样本 20 条 + 判定口径（skill-trigger.jsonl）
├── examples/
│   └── three-guarantee-agent/    # 可运行示例：主循环 / 工具契约 / 幂等 / 冒烟与负样本评测
├── scripts/
│   └── audit-size.py             # 技能体积审计（按实际加载路径统计）
└── skill/
    ├── agent-dev-workflow/              # SKILL.md + references/ + commands/ + templates/
    ├── agent-core-standards/            # SKILL.md + standards/
    ├── agent-eval-standards/            # SKILL.md + standards/
    ├── agent-tool-standards/            # SKILL.md + standards/
    ├── agent-context-memory-standards/  # SKILL.md + standards/
    ├── agent-safety-standards/          # SKILL.md + standards/
    ├── agent-ops-standards/             # SKILL.md + standards/
    ├── agent-rag-standards/             # SKILL.md + standards/
    ├── check-agent-standards/           # SKILL.md + references/ + scripts/
    └── agent-forge-source-law/          # SKILL.md
```

## 七、与 jee-forge 的关系

同构不同域。骨架（SKILL.md 约定、安装分发、体积预算、来源铁律、闸门机制）沿用 jee-forge 已验证的做法；内容全部重写——jee-forge 约束的是 Java 后端的生成物，agent-forge 约束的是 agent 的组成件与行为。

## 八、维护纪律

1. **改技能前先跑审计**：`python3 scripts/audit-size.py --root .`，超预算先瘦身
2. **硬规则必须配核对项**：新增规范条目时，同时确认 `check-agent-standards` 里有对应判据
3. **长判据下沉 references/**：SKILL.md 只放"何时用、怎么判、加载矩阵"
4. **改完刷新基线**：`python3 scripts/audit-size.py --root . --write-baseline`
5. **本地改完必须推送再安装**，否则装到的仍是旧版本
