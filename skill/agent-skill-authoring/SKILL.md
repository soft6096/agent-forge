---
name: agent-skill-authoring
description: "Agent 技能工程质量规范（写技能的技能）：SKILL.md 结构与必备段落、frontmatter 的 name / description 写法、触发词与否定边界、渐进披露三层、体积预算与加载路径估算、目录划分、多客户端分发与来源回执、技能变更纪律、触发测试与误触发回归。触发：写个技能、写个 skill、SKILL.md 该怎么写、skill 目录结构、技能太大、加载路径超预算、触发词怎么修、技能改完怎么分发。**任何新增或修改技能目录、SKILL.md、description、references 前必须加载本 skill**（含只改一句触发词的零散改动）。边界：不规定技能的业务内容；技能装了但不生效、版本不对、软链失效归 agent-forge-source-law；普通业务代码排错与 Java 后端开发不归本技能。"
---

# Agent 技能工程规范

## 定位

技能不触发等于没装，误触发比不触发更危险。本技能约束的是「技能本身怎么写」：让加载边界可机械判定、体积可审计、变更可回归。
经验来源：本仓技能族与 22 条触发样本的真实选择器实测（v1~v5，见 `evals/README.md`）。

## 加载矩阵

| 你在写什么 | 读哪份 |
|---|---|
| frontmatter、description 触发语与否定边界、触发测试口径 | `references/frontmatter-and-triggers.md` |
| 目录划分、渐进披露、体积预算、分发与来源回执、变更纪律 | `references/disclosure-and-budget.md` |

**按需精确加载**，不要整目录读入。

## 八条硬规则（可核对）

1. **frontmatter 只有 name 与 description**：description 的唯一职责是「何时用」，不写教程、不复制正文
2. **触发语与否定边界成对写**：只加触发词不加边界，必然抢走相邻技能；边界要写清「归哪个技能」，不能只写「不归我」
3. **description 受客户端长度上限约束**：默认 1024 字符，超了技能直接不被加载
4. **SKILL.md 只放何时用、怎么判、加载矩阵**：长判据下沉 `references/`，禁止整目录读入
5. **渐进披露三层**：常驻元数据（frontmatter）/ 触发时正文（SKILL.md）/ 按需资源（references、standards、scripts）
6. **体积按加载路径算**：SKILL.md 加会被加载的 references，不是整个目录；改完跑审计并刷新基线
7. **改动先改基准文档**：技能地图类基准文档先于代码；计数、术语、编号、目录树与引用方一次性同步
8. **改 description 后必须重跑触发回归**：至少 smoke + adversarial 两层；盲判不能冒充真实选择器实测

## 三条反例（出现即返工）

- 只给某技能补触发词、不给相邻技能补否定边界，导致原本 pass 的样本被抢走
- 把多行样本原话合并成一条消息做「逐条实测」，一次选择器判定被拆成多条结论
- 改完本地不推送就声称安装端已生效；或改安装副本后声称改了技能

## 与其它技能的边界

| 关注点 | 归属 |
|---|---|
| 技能装了不生效、版本不对、软链断、装到哪个目录 | agent-forge-source-law |
| 评测集分层、指标口径、判定与统计显著性 | agent-eval-standards |
| 交付前机械核对与证据报告 | check-agent-standards |
| 技能的业务内容（工具契约、骨架、检索等） | 对应领域规范技能 |
| 框架 API 教程、具体产品选型 | 不覆盖，见 SKILL-MAP 1.4 |

## 交付前

技能相关改动写完，过 `check-agent-standards` 的「技能」类核对项（第 28~30 项：frontmatter 合规、description 含触发语、未超体积预算），
并按本技能 `references/` 的专项判据人工判定触发边界；每项附文件行号证据，语义项禁止直接标 pass。
