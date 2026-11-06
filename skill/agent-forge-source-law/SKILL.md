---
name: agent-forge-source-law
description: "来源铁律：区分 agent-forge 的开发源仓库与安装产物，安装只认 git 远端，禁止把本地工作区当安装源。触发：安装技能、更新技能、重装、技能不生效、技能没加载、装到哪个目录、改技能没效果、install.sh、软链失效、版本对不对、装的是哪一版。凡涉及 agent-forge 的安装、更新、修改后是否生效的问题，先加载本 skill 判断源与产物，再动手。"
---

# 来源铁律

## 事实

- **开发源**：agent-forge 的 git 仓库工作副本。有 `.git`、`skill/`、`install.sh`，**没有** `.agent-forge-origin`
- **安装产物**：Agent 实际加载的技能目录。技能目录是指向安装副本的软链（链接形态），或实体副本（拷贝形态）；安装副本根有 `.agent-forge-origin`（记录 git 源、ref、commit、提交时间、安装时间）
- **安装来源**：git 远端（码云 / GitHub / 指定的 git 地址）。**本地工作区一律不作为安装来源**

## 判断当前是什么

| 现象 | 结论 |
|---|---|
| 目录有 `.git` + `skill/` + `install.sh`，无 `.agent-forge-origin` | 开发源 |
| 目录根有 `.agent-forge-origin` | 安装产物，改它无效 |
| 技能目录是指向安装副本 `skill/<name>` 的软链 | 链接形态，改动会随安装副本更新 |
| 技能目录是实体目录且无软链 | 拷贝形态，需重装才更新 |

## 禁止

1. 禁止把开发源目录直接复制或软链给任何 Agent 的技能目录
2. 禁止修改安装产物后声称"已修改 agent-forge"
3. 禁止在开发源未提交的情况下声称"已发布"——本地改动不推送，安装端永远拿不到

## 正确路径

```
1. 在开发源改 skill/<name> 或仓根 global-rules.md
2. commit + push 到远端
3. 运行 ./install.sh --link（或拷贝形态 ./install.sh）
4. 核对安装日志打印的远端提交（形如：远端快照：<hash> <日期> <标题>）
```

## 排查顺序（技能不生效时）

1. 确认技能目录里是否存在该技能（`ls <技能目录>`）
2. 确认是软链还是实体副本，软链是否断（`ls -l`，断链会显示目标不存在）
3. 确认安装副本的 `.agent-forge-origin` 里 commit 与远端最新是否一致
4. 确认 SKILL.md 的 frontmatter 是否合法（name / description 缺失会导致不加载）
5. 确认本会话是否需要新开会话才能重新扫描技能目录

## 常见误判

| 说法 | 实际 |
|---|---|
| "我改了源目录，怎么没生效" | 未推送或未重跑 install.sh |
| "改安装副本更快" | 下次重装即被覆盖，且版本不可追溯 |
| "装了两份，改一份就够" | 拷贝形态下每个 Agent 各一份，必须重装统一 |
| "SKILL.md 写了就会加载" | frontmatter 不合规、description 无触发语都会导致不加载 |
