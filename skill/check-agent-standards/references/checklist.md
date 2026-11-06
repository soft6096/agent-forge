# 30 项核对判据与手工指令

> 脚本不可用时按本文件手工执行。每项的「判定」写明什么算通过，「手工指令」给出可直接跑的 grep。
> 通用目录：先排除 node_modules / venv / .git / target / dist / build。

---

## 骨架

### 1. 存在最大步数配置

- 判定：存在可配置的步数上限（常量、配置项、环境变量均可），且被主循环引用
- 自动模式：`max_steps|maxSteps|MAX_STEPS|max_iterations|maxIterations|step_limit`
- 手工：`grep -rnE "max_steps|maxSteps|max_iterations|step_limit" . --include=*.py --include=*.java --include=*.ts`
- fail 典型：`while (true)` 且无上限

### 2. 单步超时可配置

- 判定：模型调用与工具调用各有超时设置
- 自动模式：`timeout|time_limit|deadline`
- 手工：`grep -rnE "timeout" . --include=*.py --include=*.java --include=*.yml`
- 注意：只在 HTTP 客户端设超时、主循环无总时长 → 判 fail

### 3. 终止条件分支齐全

- 判定：五类终止（正常完成、步数耗尽、预算耗尽、用户中断、安全拦截）中至少存在显式的完成与耗尽两类分支
- 自动模式：`finish_reason|stop_reason|terminate|final_answer|completed`
- 手工：`grep -rnE "finish_reason|stop_reason|terminat|final_answer" . --include=*.py`
- manual 情形：只靠模型输出某字符串退出

### 4. 异常分类处理

- 判定：存在 ≥2 个自定义异常或错误码段，且重试逻辑区分可重试
- 自动模式：`class \w+(Error|Exception)|retryable|is_retryable`
- 手工：`grep -rnE "class \w+(Error|Exception)|retryable" . --include=*.py --include=*.java`
- fail 典型：单个 `catch (Exception)` 包全部

---

## 工具

### 5. 每个工具有描述与参数类型

- 判定：工具定义处同时出现 description 与参数结构（parameters / args schema）
- 自动模式：`description` 与 `parameters|inputSchema|args_schema`
- manual：需确认描述里写了"何时该用/不该用"

### 6. 副作用等级已标注

- 判定：工具带只读/写/高危标记
- 自动模式：`read_only|readOnly|destructive|risk_level|side_effect`
- manual：分级是否合理

### 7. 写类工具有幂等键

- 判定：写类工具出现幂等键或去重逻辑
- 自动模式：`idempotenc|idempotency_key|dedup|dedupe`
- fail 典型：`INSERT` 前仅 SELECT 判重且无唯一索引

### 8. 工具数量与选择策略

- 判定：工具数 > 15 时必须存在分组或检索式选择
- 自动模式：`tool_selector|select_tools|tool_group|tool_routing`
- manual：统计工具总数

---

## 提示词

### 9. 提示词外置

- 判定：系统提示词不在业务逻辑中以长字符串字面量出现；应有 prompts/ 目录或独立配置文件
- 自动模式：存在 `prompts/` 目录，或 `system_prompt` 从文件读取（`read_text|open(...).read`）
- fail 典型：代码里出现 500 字以上的三引号字符串作为系统提示词
- 手工：`grep -rnE "system_prompt\s*=\s*[fr]?[\"']{3}" .`

### 10. 提示词带版本标识

- 判定：提示词文件或配置里有 version / 版本号
- 自动模式：`prompt_version|PROMPT_VERSION|version:`
- manual：版本是否随内容变更递增

---

## 上下文

### 11. 声明 token 预算

- 判定：存在上下文预算配置（各区段配额或总预算）
- 自动模式：`max_tokens|maxTokens|token_budget|context_window|budget_tokens`

### 12. 有裁剪或压缩实现

- 判定：存在历史裁剪、摘要压缩或滑窗逻辑
- 自动模式：`truncate|summar|compress|prune|window|trim_history`
- fail 典型：历史数组只 append 不裁剪

### 13. 有输出预留

- 判定：为模型输出预留 token，未把窗口占满
- 自动模式：`max_output_tokens|reserve|output_tokens|response_tokens`

---

## 记忆

### 14. 写入有判据函数

- 判定：记忆写入前有判定（值得记/不值得记），不是把所有交互都写入
- 自动模式：`should_remember|memory_write|worth_remember|extract_memor`

### 15. 敏感字段禁写

- 判定：写入前有敏感字段过滤
- 自动模式：`sensitive|redact|mask|pii|sanitize`

---

## 检索

### 16. 召回上限与阈值

- 判定：检索调用带条数上限与相似度阈值
- 自动模式：`top_k|topK|similarity_threshold|score_threshold|min_score`

### 17. 强制输出引用

- 判定：回答需带出处，检索结果带 source 字段
- 自动模式：`citation|source_id|provenance|references`

### 18. 未命中分支

- 判定：检索无结果时有明确分支（拒答/告知/降级），不是空数组直接进模型
- 自动模式：`no_result|not_found|empty_result|no_hit|no_context`

---

## 评测

### 19. 评测集文件存在

- 判定：存在评测数据文件（jsonl/json/yaml/csv），或 evals/ tests/eval 目录
- 自动模式：目录 `evals/`、`eval/`、`tests/eval`，文件含 case/sample/expected 字段

### 20. 门禁阈值配置

- 判定：存在通过率/成本/时延的阈值配置
- 自动模式：`pass_rate|min_score|threshold|gate|budget_usd|max_cost`

### 21. 报告含失败样本

- manual：检查最近一次评测报告是否有失败样本的真实输入输出与归因

---

## 运维

### 22. 日志含会话 id 与 trace id

- 判定：结构化日志字段包含两者
- 自动模式：`session_id|trace_id|correlation_id`

### 23. 记录 token 用量

- 判定：每次模型调用后记录 usage
- 自动模式：`prompt_tokens|completion_tokens|total_tokens|usage`

### 24. 成本上限

- 判定：单任务成本或 token 上限存在，且超限行为明确
- 自动模式：`cost_limit|max_cost|budget_usd|cost_cap`

---

## 安全

### 25. 外部输入与指令分离

- 判定：外部内容用明确分隔或标记包裹，系统指令不被外部内容覆盖
- 自动模式：`<user_input>|<external_content>|delimiter|untrusted`
- manual：分隔是否真的有效

### 26. 高危工具有人工确认开关

- 判定：高危工具执行前有确认闸门
- 自动模式：`require_approval|needs_confirmation|human_in_the_loop|confirm_required|approval`

### 27. 输出有过滤

- 判定：输出经过内容过滤或敏感信息拦截
- 自动模式：`moderation|filter_output|blocklist|denylist|redact`

---

## 技能

### 28. frontmatter 合规

- 判定：每个 skill/*/SKILL.md 有 YAML frontmatter，含 name 与 description，name 与目录名一致
- 手工：`head -5 skill/*/SKILL.md`

### 29. description 含触发语

- 判定：description 里出现"触发""使用时机"或具体场景词，长度足够 Agent 判断
- manual：判断能否区分相邻技能

### 30. 未超体积预算

- 判定：SKILL.md 不超过预算（默认 300 行 / 24KB），超出的判据应下沉到 references/
- 手工：`wc -l skill/*/SKILL.md`
