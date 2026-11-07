# three-guarantee-agent（售后三包判定助手示例）

> 这是 agent-forge 的落地示例：把技能里的硬规则写成**能跑、能被评测、能被机械核对**的代码。
> 不是可直接上线的业务系统——VIN 校验、条款库、工单都是本地模拟。

## 一、它验证了什么

| 组 | 落点 |
|---|---|
| 骨架 | 最大步数、三处超时、五类终止、失败分类、死循环识别、中断恢复 |
| 工具 | 九字段契约、副作用分级、写类幂等、结构化错误码、超时归属 |
| 上下文 | 预算表、三级降级（折叠外部内容 → 压历史 → 明确告知不足）、状态不裁剪 |
| 安全 | 入向过滤、出向脱敏、外部内容与指令隔离、高危操作人工确认 |
| 评测 | 12 条冒烟用例，replay 回放，零成本可复现 |
| 运维 | 结构化日志、token 计量、成本三层上限 |

当前机械核对结果：**pass 23 / fail 0 / manual 4 / n/a 3**，可自行复现：

```bash
cd examples/three-guarantee-agent
python3 ../../skill/check-agent-standards/scripts/check_agent_standards.py --project . --all
```

其中 4 项 manual（工具数量与选择策略、记忆写入判据、强制输出引用、输出过滤）需人工判定；3 项 n/a 是技能类核对项，本工程没有 `skill/` 目录，不适用。

## 二、环境

- Python **3.11+**（配置解析用了标准库 `tomllib`）
- **零第三方依赖**，不需要 `pip install` 任何东西
- **没有 API key 也能跑**：`model.provider = "auto"` 且无凭据时自动降级为离线回放

## 三、跑一次任务

```bash
cd examples/three-guarantee-agent
PYTHONPATH=src python3 -m tga.main --input "VIN 为 LSVAA1234R1234567，变速箱异响是否在保"
```

| 参数 | 作用 |
|---|---|
| `--input` | 用户输入原文，必填 |
| `--config` | 配置文件路径，默认 `config/agent.toml` |
| `--no-log` | 不输出结构化日志 |

输出是 JSON（`reason` / `answer` / `steps` / `usage` / `halted_at` / `detail`），退出码 `0` 正常完成、`1` 其他终止、`2` 配置或工具契约不合法。结构化日志走 stderr，JSON 走 stdout，可以直接管道给 `jq`。

> **回放模式按输入原文匹配录制**。换一种说法提问会命中不到录制，模型回一句"未找到该输入的录制响应"——这不是 bug。要验证别的说法，先在 `evals/replay/` 补录制，或切真实模型（第四节）。

四个工具：`get_vehicle_profile` / `query_warranty_period` / `search_clause`（只读）、`submit_appeal_ticket`（高危写，需人工确认）。

## 四、切真实模型

```bash
export OPENAI_API_KEY=...
PYTHONPATH=src python3 -m tga.main --input "VIN 为 LSVAA1234R1234567，变速箱异响是否在保"
```

`provider = "auto"` 下检测到凭据即走真实调用；也可用环境变量显式覆盖：`TGA_PROVIDER`（`auto` / `replay` / `openai`）、`TGA_MODEL_NAME`、`TGA_API_BASE`。

## 五、跑评测

评测固定走 replay，不读 `OPENAI_API_KEY`，因此**可复现且不产生费用**。

```bash
# 只查用例与录制是否配齐：秒级、不调用模型
PYTHONPATH=src python3 -m tga.eval.runner --check-only

# 跑整套冒烟集，输出人读报告
PYTHONPATH=src python3 -m tga.eval.runner

# 只跑一条 / 输出机读 JSON
PYTHONPATH=src python3 -m tga.eval.runner --case 11-context-exhausted
PYTHONPATH=src python3 -m tga.eval.runner --suite smoke --json
```

退出码 `0` 全绿、`1` 有失败样本、`2` 用例或环境错误。失败样本会带真实输入、实际答复、终止说明、调用工具、错误码与未命中项。

### 用例字段（一文件一条，`evals/smoke/*.json`）

| 字段 | 必填 | 含义 |
|---|---|---|
| `id` | 是 | 用例标识，通常与文件名一致 |
| `input` | 是 | 喂给助手的用户输入原文 |
| `note` | 否 | 这条用例覆盖什么，失败时直接进报告 |
| `expect.reason` | 是 | 期望终止原因：`completed` / `step_exhausted` / `budget_exceeded` / `cancelled` / `blocked` |
| `expect.max_steps` | 是 | 允许的最大步数，超出算失控 |
| `expect.tools_used` | 否 | 期望被调用过的工具，按"包含"判定 |
| `expect.answer_contains` | 否 | 答复里必须出现的片段 |
| `expect.answer_not_contains` | 否 | 答复里不允许出现的片段（如编造的条款号） |
| `expect.detail_contains` | 否 | 终止说明里必须出现的片段，用于非 `completed` 分支 |
| `expect.tool_error_codes` | 否 | 期望出现的工具错误码，按集合相等判定 |
| `cancel_before_run` | 否 | 开跑前置中断标志，用于覆盖 `cancelled` 分支 |
| `context_override` | 否 | 用例级上下文预算覆盖，只用于构造极端场景（如把窗口压到 1200 验证降级） |
| `requires_recording` | 否 | 是否需要回放录制，默认 `true`；纯终止分支可不录 |
| `confirm_pending` | 否 | 跑完后模拟人工确认，验证高危操作的开闸通道 |

用例写错（`reason` 不在五类内、`context_override` 键名或类型不对、目录为空）会在加载期直接报错，不会静默跑成"零失败全绿"。

### 负样本集：证明失败归因真的可用

冒烟集永远全绿，只能证明"没坏"，证明不了"坏的时候说得清"。`evals/negative/` 放的是**判定项故意写错**的用例，跑它必然红，退出码必然是 `1`：

```bash
PYTHONPATH=src python3 -m tga.eval.runner --suite negative
```

`evals/reports/negative-20261107.txt` 是这条命令的真实输出（原样保存，未编辑）：7 条未命中逐项列出，并带上真实输入、实际答复、调用工具与错误码。这也是核对项"报告含失败样本"的证据——只写通过率、没有失败明细的报告不算证据。

## 六、高危操作的人工确认

`submit_appeal_ticket` 属高危写操作：主循环只登记待确认项并回 `approval_required`，**模型和主循环都拿不到确认令牌**，必须由人执行：

```bash
PYTHONPATH=src python3 -m tga.confirm --list          # 列出待确认项
PYTHONPATH=src python3 -m tga.confirm --id <令牌>      # 确认并执行
PYTHONPATH=src python3 -m tga.confirm --id <令牌> --reject   # 拒绝，不执行
```

确认项 30 分钟过期，令牌绑定工具名与参数指纹，防止"确认的是 A、执行的是 B"。

## 七、目录与产物

```
config/            配置与提示词（prompt 版本在 frontmatter 里，与配置不一致启动即报错）
data/              本地模拟数据：条款库、车辆档案、三包规则
evals/smoke/       评测用例，一文件一条
evals/negative/    负样本用例，判定项故意写错，跑红是预期
evals/replay/      回放录制，按输入原文匹配
evals/reports/     真实评测报告留档（失败归因证据）
src/tga/           代码：loop / tools / context / safety / eval / ops
runtime/           运行期产物（会话快照、确认项、工单、幂等记录），不进版本库
```

**有意不做**：长期记忆与用户画像、向量库检索、真实工单系统对接。示例聚焦骨架与判定链路，这几项接真实业务时应替换掉本地模拟实现。
