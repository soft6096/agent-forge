# 全局裁决推荐规则（agent-forge 分发）

> 本文件由 `./install.sh` 以受管块（`# BEGIN/END agent-forge::global-rules` 之间）追加到各 Agent 的全局指令文件
> （`~/.agents/AGENTS.md`、`~/.claude/CLAUDE.md`、`~/.codebuddy/AGENTS.md`、`~/.trae/AGENTS.md`、
> `~/.hermes/AGENTS.md`、`~/.qoder/AGENTS.md` 等，仅对已存在的文件写入）。
> 改规则 = 改本文件 → 提交推送 → 重跑 `./install.sh`；重跑整块替换，不重复叠加。
> 适用于所有编程 Agent。
>
> **不重复其它 forge 已分发的规则**：同一规则两处真源必然分叉。「需要用户裁决时给出 AI 推荐项」的真源在
> jee-forge 的 `global-rules.md`，本文件只放指针，不复制正文（见文末「关联规则」）。

## 规则：结论必须有证据

任何"通过 / 符合 / 已修复"的结论必须附证据（文件:行号、命令输出、样本编号）。无证据的结论视为未完成，不得汇报为已完成。

## 规则：不确定就问，不自行推定

判档不清、需求有歧义、边界未定时，先列两条路 + 推荐 + 理由，等用户答复再动手。禁止"先按大的跑反正不会错"，禁止"先改着看"。

## 关联规则（不在本文件维护）

「需要用户裁决时，必须给出 AI 推荐项」由 jee-forge 分发（受管块 `# BEGIN jee-forge::global-rules`），正文只维护在 jee-forge 的 `global-rules.md`。本机已装 jee-forge 时按该块执行；未装时不要在此处另立一份，按需补装 jee-forge 即可。
