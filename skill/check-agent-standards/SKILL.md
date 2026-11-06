---
name: check-agent-standards
description: "Agent 代码交付前的规范兜底核对（收敛闸门）。**任何 agent 相关代码写完或改完后必须加载本 skill——包括不走完整流程的零散写码**：骨架（步数上限/超时/终止条件/异常分类）、工具（描述/参数/副作用等级/幂等）、提示词外置与版本、上下文预算与裁剪、记忆写入判据、检索阈值与引用、评测集与门禁、日志字段与 token 计量、成本上限、高危确认与输出过滤、技能 frontmatter 与体积预算。优先跑仓内脚本 scripts/check_agent_standards.py（纯标准库、本地零 token），无 Python 环境回退 grep 手工执行，每项附「文件:行号」证据。触发：检查 agent 规范、兜底核对、写完 agent 代码检查、agent code review、规范自检、交付前检查、对某目录跑 check-agent-standards。**禁止凭记忆答通过**；语义项一律标 manual 交人工判定。"
---

# Agent 规范兜底核对

## 定位

agent 代码的收敛闸门。规范分散在多个技能（骨架、工具、上下文、安全、评测），编码时容易漏；本技能在交付前统一扫一遍，只做**可判定**的项，语义项明确交人工。

## 执行方式（脚本优先）

```bash
# 默认：只扫 git 改动的文件（刚写完代码的兜底自检主场景）
python3 scripts/check_agent_standards.py --project <项目根>

# 全项目扫描（全量体检）
python3 scripts/check_agent_standards.py --project <项目根> --all

# 只扫指定目录
python3 scripts/check_agent_standards.py --project <项目根> --path src/agent

# 机读 JSON（推荐，省上下文）
python3 scripts/check_agent_standards.py --project <项目根> --format json --output .ca-report.json

# 只跑部分项
python3 scripts/check_agent_standards.py --project <项目根> --only 1,2,9,22
```

无 Python 环境时，按 `references/checklist.md` 的 grep 指令手工执行（降级路径，结论等价）。

## 输出约定

每项三种状态，不允许第四种：

| 状态 | 含义 | 后续动作 |
|---|---|---|
| pass | 命中证据 | 附文件行号，无需人工 |
| fail | 未命中且属自动判定项 | 必须修或申请豁免 |
| manual | 无法机械判定 | 人工判定后填 pass/fail |

**禁止**把 manual 直接当 pass。**禁止**无证据的"已符合"。

## 30 项核对表

完整判据与手工指令见 `references/checklist.md`。分组：

| 组 | 项 | 关注点 |
|---|---|---|
| 骨架 | 1~4 | 步数上限、超时、终止条件、异常分类 |
| 工具 | 5~8 | 描述与参数、副作用等级、幂等、工具数量策略 |
| 提示词 | 9~10 | 外置、版本标识 |
| 上下文 | 11~13 | token 预算、裁剪压缩、输出预留 |
| 记忆 | 14~15 | 写入判据、敏感字段禁写 |
| 检索 | 16~18 | 召回阈值、强制引用、未命中分支 |
| 评测 | 19~21 | 评测集存在、门禁阈值、失败样本 |
| 运维 | 22~24 | 日志字段、token 计量、成本上限 |
| 安全 | 25~27 | 输入隔离、高危确认、输出过滤 |
| 技能 | 28~30 | frontmatter、触发语、体积预算 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 无 fail（可能有 manual 待人工） |
| 1 | 存在 fail 项 |
| 2 | 环境或参数错误（路径不存在、git 不可用等） |

## 报告落盘

走完整流程时产出 `6.1-规范核对报告.md`（用 agent-dev-workflow 的模板）；独立触发在对话内输出表格 + 待确认清单即可，不强制落盘。
