# 目录划分、渐进披露、体积预算与分发

> 本文件是 `SKILL.md` 硬规则 4 / 5 / 6 / 7 的判据细则。
> 数值口径以 `scripts/audit-size.py` 为准；分发口径以 `agent-forge-source-law` 与安装脚本为准。

## 1. 目录划分

| 目录 | 放什么 | 是否随任务加载 |
|---|---|---|
| `SKILL.md` | 何时用、怎么判、加载矩阵、硬规则目录 | 一定加载 |
| `references/` | 长判据、示例、字典、专项清单 | 按需，一次通常读一份 |
| `standards/` | 领域规范的分章节正文 | 按需 |
| `commands/`、`templates/`、`assets/` | 斜杠命令、中间产物模板、静态资源 | 否，独立产物 |
| `scripts/` | 可机械执行的核对/审计脚本 | 否，但计入随件文件数 |

`SKILL.md` 不写教程、不堆判据。判断标准很简单：**这段内容是不是每次触发都必须看**？不是就下沉。

## 2. 渐进披露三层

1. 常驻元数据：frontmatter，客户端扫描技能目录时全部载入
2. 触发时正文：`SKILL.md`
3. 按需资源：`references/` / `standards/` / `scripts/`

`SKILL.md` 必须给加载矩阵（「你在写什么 → 读哪份」），并要求**精确读子文件、禁止整目录读入**。没有加载矩阵，第三层就退化成「要么全读、要么不读」，预算控制失效。

## 3. 体积预算

来自 `scripts/audit-size.py`：

| 项 | 上限 |
|---|---|
| `SKILL.md` | 300 行 且 24 KB |
| `SKILL.md` + 会被加载的 `references/` / `standards/` | 900 行 且 72 KB |
| 随件（`templates/` `commands/` `docs/` `scripts/` `assets/`） | 40 个文件 |

两条容易算错的地方：

- **按加载路径算，不是整个目录**：模板与命令不会在一次任务里全读，只限文件数不限行数
- **脚本与配置也计入随件文件数**：避免把代码藏在 `scripts/` 里绕开预算

命令：

```bash
python3 scripts/audit-size.py --root .                      # 改技能前先看，超预算先瘦身
python3 scripts/audit-size.py --root . --write-baseline    # 改完刷新 .size-baseline.json
```

基线按技能名记录 `load_lines` / `load_bytes`；新增技能或改了加载路径上的文件，必须刷新基线，否则下一轮审计会把正常增长判成超阈值。脚本的相对增量阈值是 warn 1.05 / fail 1.20。

## 4. 分发与来源回执

唯一合法路径：

```
开发源改 skill/<name> → commit + push 到 git 远端 → ./install.sh --link --force → 核对安装日志
```

- **开发源**：有 `.git`、`skill/`、`install.sh`，没有 `.agent-forge-origin`
- **安装产物**：技能目录是指向安装副本的软链（链接形态）或实体副本（拷贝形态）；安装副本根有 `.agent-forge-origin`，记录 source / ref / commit / installed_at / mode
- 安装脚本用 `skill/*/` 通配分发，不硬编码技能名；新增技能会自动进入各工具目录
- **禁止**把开发源目录直接软链给 Agent，禁止改安装副本后声称改了技能，禁止本地未推送就声称已发布

安装后的核对清单：

1. 日志里的「远端快照 `<hash>`」等于刚推送的提交
2. `.agent-forge-origin` 的 `commit` 与远端一致
3. 开发源 `skill/` 与安装副本逐字节一致（新增技能的 `references/` 尤其要查）
4. 各工具技能目录里技能实例数 = 技能目录数 × 工具数
5. 软链无断链，断链会显示目标不存在
6. `git status` 干净，没有临时脚本或运行期产物混入

链接形态下改远端后重装即可；拷贝形态下每个 Agent 各一份实体，必须重装统一。新装的技能要**新开会话**才扫得到。

## 5. 变更纪律：改一处扫全部引用方

| 引用方 | 要同步什么 |
|---|---|
| `SKILL-MAP.md` | 状态行、职责卡（职责/边界/触发说法/生成物）、技能清单总表落地状态、分批计划落地情况、目录树 |
| `README.md` | 已落地数量、技能家族表、单独使用技能表、仓库结构树 |
| `skill/agent-dev-workflow/SKILL.md` | 与其它技能的分工、加载矩阵说明里的「规划中 / 未发布」措辞 |
| `skill/check-agent-standards/references/checklist.md` | 硬规则对应的判据；保持核对项总数与说明一致 |
| `evals/README.md` / `evals/skill-trigger.jsonl` | 技能数量、样本数量、`must_not`、判定记录 |
| `.size-baseline.json` | 跑审计后刷新 |
| `install.sh` / `global-rules.md` | 仅当分发机制或全局规则本身变化时 |

同步四要素：**计数、术语、编号、目录树**。任何一处留着旧数字，下次改技能的人就会按错的基准动手。

## 6. 定稿验收清单

- [ ] `SKILL.md` 有定位、加载矩阵、可核对硬规则、边界、交付前
- [ ] 硬规则每条都能落到机械判据或明确的人工判据，附「文件:行号」证据
- [ ] 引用的 `references/*.md` 全部真实存在，无悬空引用
- [ ] `python3 scripts/audit-size.py --root .` 通过，基线已刷新
- [ ] smoke + adversarial 两层盲判回归通过，`must_not` 全未命中
- [ ] 全部引用方同步完毕，全仓搜不到「规划中 / 尚无目录 / 旧数量」
- [ ] 提交并推送，重装后 11 个技能在客户端可见
- [ ] 真机选择器实测已回填判定记录；未实测前不得称「验收通过」

## 7. 反例

- `SKILL.md` 写了加载矩阵但指向不存在的文件：触发后按矩阵去读会直接失败
- 只刷新基线不跑审计：把超预算的状态写成了新基线
- 改完本地不推送就跑安装：装到的仍是远端旧版本，且版本不可追溯
- 新增技能后不补触发样本：只能验证「不误触发」，无法验证「该加载时能加载」
