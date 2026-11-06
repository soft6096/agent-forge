#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_agent_standards.py — Agent 代码与技能规范的机械核对（纯标准库，零依赖）。

设计原则：
1. 只做可机械判定的项，语义项一律标 manual，绝不假装通过；
2. 每条结论附「文件:行号」证据，供人工复核；
3. 不联网、不执行被检代码、不修改任何文件（--output 除外）。

退出码：0 无 fail / 1 存在 fail / 2 环境或参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 扫描时跳过的目录：依赖、构建产物、版本库、IDE、中间产物文档
SKIP_DIRS = {
    ".git", ".svn", ".idea", ".vscode", "node_modules", "vendor",
    "venv", ".venv", "env", "__pycache__", "dist", "build", "target",
    "out", ".gradle", ".mvn", "docs", ".next", "coverage",
}

CODE_EXTS = {".py", ".java", ".kt", ".ts", ".tsx", ".js", ".jsx", ".go", ".cs", ".rb", ".php"}
CONF_EXTS = {".yml", ".yaml", ".json", ".toml", ".ini", ".properties", ".conf", ".env"}
SCAN_EXTS = CODE_EXTS | CONF_EXTS

# 每项：编号与 references/checklist.md 一一对应
# mode: auto = 未命中即 fail；manual = 未命中交人工
ITEMS = [
    (1, "骨架", "存在最大步数配置", r"max_steps|maxSteps|MAX_STEPS|max_iterations|maxIterations|step_limit", "auto"),
    (2, "骨架", "单步超时可配置", r"timeout|time_limit|deadline", "auto"),
    (3, "骨架", "终止条件分支齐全", r"finish_reason|stop_reason|terminat|final_answer|completed", "auto"),
    (4, "骨架", "异常分类处理", r"class\s+\w+(Error|Exception)|retryable|is_retryable", "manual"),
    (5, "工具", "工具带描述与参数结构", r"description&&inputSchema|input_schema|args_schema|parameters|properties", "manual"),
    (6, "工具", "副作用等级已标注", r"read_only|readOnly|destructive|risk_level|side_effect", "manual"),
    (7, "工具", "写类工具有幂等键", r"idempotenc|dedup|dedupe", "auto"),
    (8, "工具", "工具数量与选择策略", r"tool_selector|select_tools|tool_group|tool_routing", "manual"),
    (10, "提示词", "提示词带版本标识", r"prompt_version|PROMPT_VERSION|version\s*[:=]", "manual"),
    (11, "上下文", "声明 token 预算", r"max_tokens|maxTokens|token_budget|context_window|budget_tokens", "auto"),
    (12, "上下文", "有裁剪或压缩实现", r"truncate|summar|compress|prune|trim_history|sliding_window|context_window", "auto"),
    (13, "上下文", "有输出预留", r"max_output_tokens|output_tokens|response_tokens|reserve", "manual"),
    (14, "记忆", "记忆写入有判据", r"should_remember|memory_write|worth_remember|extract_memor", "manual"),
    (15, "记忆", "敏感字段禁写", r"sensitive|redact|mask|pii|sanitize", "manual"),
    (16, "检索", "召回上限与阈值", r"top_k|topK|similarity_threshold|score_threshold|min_score", "auto"),
    (17, "检索", "强制输出引用", r"citation|source_id|provenance", "manual_only"),
    (18, "检索", "未命中分支", r"no_result|not_found|empty_result|no_hit|no_context", "manual"),
    (20, "评测", "门禁阈值配置", r"pass_rate|min_score|threshold\s*[:=]|budget_usd|max_cost", "auto"),
    (22, "运维", "日志含会话 id 与 trace id", r"session_id|trace_id|correlation_id", "auto"),
    (23, "运维", "记录 token 用量", r"prompt_tokens|completion_tokens|total_tokens|\busage\b", "auto"),
    (24, "运维", "成本上限", r"cost_limit|max_cost|budget_usd|cost_cap", "auto"),
    (25, "安全", "外部输入与指令分离", r"<user_input>|external_content|delimiter|untrusted", "manual"),
    (26, "安全", "高危工具有人工确认", r"require_approval|needs_confirmation|human_in_the_loop|confirm_required|approval", "manual"),
    (27, "安全", "输出有过滤", r"moderation|filter_output|blocklist|denylist", "manual"),
]

PROMPT_DIR_NAMES = ("prompts", "prompt", "system_prompts")
EVAL_DIR_NAMES = ("evals", "eval", "evaluation")

# 脚本自身不参与扫描：规则定义行本身就是关键词，自扫会产生假阳性证据。
# 按文件名排除，不只按当前运行路径——同一脚本可能存在多份副本（工作区 + 安装副本 + 拷进项目），
# 只排除运行中的那一份，其余副本仍会被当成"实现"而把 27 项判成 pass。
SELF_PATH = Path(__file__).resolve()
SELF_NAMES = {"check_agent_standards.py"}


def collect(scope: Path):
    """收集待扫描的代码与配置文件（跳过依赖与产物目录）。"""
    files = []
    for dirpath, dirnames, filenames in os.walk(scope):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in SCAN_EXTS:
                continue
            if name in SELF_NAMES:
                continue
            try:
                if p.resolve() == SELF_PATH:
                    continue
            except OSError:
                pass
            files.append(p)
    return files


def rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


COMMENT_MARKS = ("#", "//", "/*", "*", "<!--", "--")


def strip_comment(line: str) -> str:
    """去掉整行注释与行尾注释。

    为什么：关键词出现在注释或文档字符串里不代表有实现。不剥离会让"注释里写了 trace_id"
    被判成 pass，比 fail 更危险——它让人相信已经合规。
    """
    s = line.strip()
    if not s or s.startswith(COMMENT_MARKS):
        return ""
    body = line
    for mark in (" #", "\t#", " //"):
        idx = body.find(mark)
        if idx > 0:
            body = body[:idx]
    return body.strip()


def search(files, root: Path, pattern: str, limit: int = 3):
    """逐文件匹配，每个文件只取首个命中，最多 limit 条证据。

    pattern 含 && 时按"同一文件内所有子模式都命中"判定（用于两段式判据，
    避免单个宽泛词就命中，例如工具描述必须同时有 description 与参数结构）。
    注释行一律不参与匹配。
    """
    parts = [x.strip() for x in pattern.split("&&") if x.strip()]
    try:
        rxs = [re.compile(x, re.IGNORECASE) for x in parts]
    except re.error:
        return []
    hits = []
    for p in files:
        lines = read_text(p).splitlines()
        first = None
        for rx in rxs:
            found = None
            for lineno, line in enumerate(lines, 1):
                body = strip_comment(line)
                if body and rx.search(body):
                    found = (lineno, body)
                    break
            if found is None:
                first = None
                break
            if first is None:
                first = found
        if first:
            hits.append("%s:%d  %s" % (rel(root, p), first[0], first[1][:120]))
            if len(hits) >= limit:
                break
    return hits


def find_dir(root: Path, names):
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for d in list(dirnames):
            if d.lower() in names:
                return Path(dirpath) / d
    return None


def check_prompts_external(files, root: Path):
    """第 9 项：提示词外置。命中 prompts 目录，或代码里从文件读取提示词即为通过。"""
    d = find_dir(root, PROMPT_DIR_NAMES)
    if d is not None:
        return "pass", ["提示词目录：%s" % rel(root, d)], ""
    # 要求同一文件里既出现 prompt 语义、又出现读取动作，避免任意 read_text 就被判外置
    hits = search(files, root, r"prompt&&read_text\(|open\(|load_prompt|get_prompt", 2)
    if hits:
        return "pass", hits, ""
    # 反向证据：代码里出现超长字符串字面量作为提示词
    rx = re.compile(r'(system_prompt|SYSTEM_PROMPT|PROMPT)\s*[:=]\s*[fr]?(\"\"\"|\'\'\'|")', re.IGNORECASE)
    long_hits = []
    for p in files:
        text = read_text(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            body = strip_comment(line)
            if body and rx.search(body) and (len(body) > 200 or '"""' in body or "'''" in body):
                long_hits.append("%s:%d  疑似硬编码提示词" % (rel(root, p), lineno))
                break
        if len(long_hits) >= 2:
            break
    if long_hits:
        return "fail", long_hits, "把提示词移到 prompts/ 或配置文件，并加版本号"
    return "manual", [], "未发现提示词目录，也未发现读取逻辑；请人工确认提示词位置"


def check_eval_dataset(root: Path):
    """第 19 项：评测集文件存在。"""
    d = find_dir(root, EVAL_DIR_NAMES)
    if d is not None:
        data = [p for p in d.rglob("*") if p.suffix.lower() in {".jsonl", ".json", ".yaml", ".yml", ".csv"}]
        if data:
            return "pass", ["%s（%d 个数据文件）" % (rel(root, d), len(data))], ""
        return "fail", ["%s 存在但无数据文件" % rel(root, d)], "补评测数据文件（jsonl/json/yaml）"
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() == ".jsonl" and "eval" in p.name.lower():
            return "pass", [rel(root, p)], ""
    return "fail", [], "未找到评测集，建立 evals/ 目录并放入样本文件"


def check_skills(root: Path):
    """第 28~30 项：技能目录合规。"""
    base = root / "skill"
    results = []
    if not base.is_dir():
        for i, name in ((28, "frontmatter 合规"), (29, "description 含触发语"), (30, "未超体积预算")):
            results.append((i, "技能", name, "manual", [], "未发现 skill/ 目录，跳过"))
        return results

    fm_bad, desc_bad, size_bad = [], [], []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        if not skill_md.is_file():
            fm_bad.append("%s  缺 SKILL.md" % rel(root, d))
            continue
        text = read_text(skill_md)
        lines = text.splitlines()
        head = "\n".join(lines[:8])
        if not head.startswith("---"):
            fm_bad.append("%s  无 YAML frontmatter" % rel(root, skill_md))
        else:
            name_m = re.search(r"^name:\s*(\S+)", head, re.MULTILINE)
            desc_m = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
            if not name_m:
                fm_bad.append("%s  缺 name" % rel(root, skill_md))
            elif name_m.group(1).strip().strip('"') != d.name:
                fm_bad.append("%s  name 与目录名不一致（%s）" % (rel(root, skill_md), name_m.group(1)))
            if not desc_m:
                desc_bad.append("%s  缺 description" % rel(root, skill_md))
            else:
                desc = desc_m.group(1)
                if len(desc) < 40:
                    desc_bad.append("%s  description 过短，无法区分触发场景" % rel(root, skill_md))
        line_count = len(lines)
        byte_size = len(text.encode("utf-8"))
        if line_count > 300 or byte_size > 24576:
            size_bad.append("%s  %d 行 / %.1fKB，超预算（300 行 / 24KB）"
                            % (rel(root, skill_md), line_count, byte_size / 1024.0))

    results.append((28, "技能", "frontmatter 合规",
                    "pass" if not fm_bad else "fail",
                    fm_bad[:3] or ["frontmatter 齐全"], "" if not fm_bad else "补 name/description 并保证 name 与目录名一致"))
    results.append((29, "技能", "description 含触发语",
                    "pass" if not desc_bad else "manual",
                    desc_bad[:3] or ["description 长度足够"], "人工确认能否区分相邻技能的触发边界"))
    results.append((30, "技能", "未超体积预算",
                    "pass" if not size_bad else "fail",
                    size_bad[:3] or ["SKILL.md 体积在预算内"], "超预算的判据下沉到 references/"))
    return results


def build_report(root: Path, files, only=None):
    items = []

    for item_id, group, name, pattern, mode in ITEMS:
        if only and item_id not in only:
            continue
        hits = search(files, root, pattern)
        if mode == "manual_only":
            # 命中的关键词只能当线索：这类项本质是语义判定，不能自动判 pass
            status = "manual"
            hint = "命中关键词仅供参考，需人工确认是否真的强制生效"
        elif hits:
            status = "pass"
            hint = ""
        else:
            status = "fail" if mode == "auto" else "manual"
            hint = "未发现相关配置或实现" if mode == "auto" else "需人工判定"
        items.append({
            "id": item_id, "group": group, "name": name,
            "status": status, "evidence": hits or [], "hint": hint,
        })

    if not only or 9 in only:
        status, ev, hint = check_prompts_external(files, root)
        items.append({"id": 9, "group": "提示词", "name": "提示词外置",
                      "status": status, "evidence": ev, "hint": hint})

    if not only or 19 in only:
        status, ev, hint = check_eval_dataset(root)
        items.append({"id": 19, "group": "评测", "name": "评测集文件存在",
                      "status": status, "evidence": ev, "hint": hint})

    if not only or 21 in only:
        items.append({"id": 21, "group": "评测", "name": "报告含失败样本",
                      "status": "manual", "evidence": [],
                      "hint": "人工检查最近一次评测报告是否含失败样本的真实输入输出与归因"})

    if not only or {28, 29, 30} & only:
        for rec in check_skills(root):
            if only and rec[0] not in only:
                continue
            items.append({"id": rec[0], "group": rec[1], "name": rec[2],
                          "status": rec[3], "evidence": rec[4], "hint": rec[5]})

    items.sort(key=lambda x: x["id"])
    summary = {
        "total": len(items),
        "pass": sum(1 for i in items if i["status"] == "pass"),
        "fail": sum(1 for i in items if i["status"] == "fail"),
        "manual": sum(1 for i in items if i["status"] == "manual"),
    }
    return {"project": str(root), "files_scanned": len(files), "summary": summary, "items": items}


def render_md(report):
    s = report["summary"]
    out = []
    out.append("# Agent 规范核对报告\n")
    out.append("- 项目：%s" % report["project"])
    out.append("- 扫描文件：%d" % report["files_scanned"])
    out.append("- 汇总：pass %d / fail %d / manual %d（共 %d 项）\n"
               % (s["pass"], s["fail"], s["manual"], s["total"]))
    out.append("| # | 组 | 核对项 | 状态 | 证据 | 建议 |")
    out.append("|---|---|---|---|---|---|")
    for i in report["items"]:
        ev = "<br>".join(i["evidence"]) if i["evidence"] else "—"
        out.append("| %d | %s | %s | %s | %s | %s |"
                   % (i["id"], i["group"], i["name"], i["status"], ev, i["hint"] or "—"))
    out.append("")
    out.append("> fail 项必须修复或申请豁免；manual 项必须人工判定后填 pass/fail，禁止直接当 pass。")
    return "\n".join(out)


def changed_files(root: Path):
    """取本次 git 改动文件（含未跟踪）；失败返回空集合。"""
    for cmd in (["ls-files", "-m", "-o", "--exclude-standard"], ["diff", "--name-only", "HEAD"]):
        try:
            r = subprocess.run(["git", "-C", str(root)] + cmd,
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return {line.strip() for line in r.stdout.splitlines() if line.strip()}
    return set()


def main():
    ap = argparse.ArgumentParser(description="Agent 规范机械核对（30 项）")
    ap.add_argument("--project", default=".", help="项目根目录，默认当前目录")
    ap.add_argument("--path", default=None, help="只扫指定子目录（相对项目根）")
    ap.add_argument("--all", action="store_true", help="全项目扫描（默认在无 git 改动时自动全扫）")
    ap.add_argument("--changed", action="store_true", help="只扫本次 git 改动文件")
    ap.add_argument("--format", choices=["md", "json"], default="md", help="输出格式")
    ap.add_argument("--output", default=None, help="写入文件，默认打印到标准输出")
    ap.add_argument("--only", default=None, help="只跑指定项，逗号分隔，如 1,2,9")
    args = ap.parse_args()

    root = Path(args.project).expanduser().resolve()
    if not root.is_dir():
        print("错误：项目路径不存在或不是目录：%s" % root, file=sys.stderr)
        return 2

    scope = root
    if args.path:
        scope = (root / args.path).resolve()
        if not scope.exists():
            print("错误：--path 不存在：%s" % scope, file=sys.stderr)
            return 2

    files = collect(scope)
    if not files:
        print("提示：未找到可扫描的代码或配置文件，仅执行技能类核对（28~30）", file=sys.stderr)

    if args.changed and not args.all:
        changed = changed_files(root)
        if changed:
            files = [f for f in files if rel(root, f) in changed]
            if not files:
                print("提示：git 改动文件中无可扫描的代码文件，本次按全量扫描", file=sys.stderr)
                files = collect(scope)

    only = None
    if args.only:
        try:
            only = {int(x.strip()) for x in args.only.split(",") if x.strip()}
        except ValueError:
            print("错误：--only 需为逗号分隔的整数，如 1,2,9", file=sys.stderr)
            return 2

    report = build_report(root, files, only)
    text = render_md(report) if args.format == "md" else json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print("报告已写入：%s" % args.output)
    else:
        print(text)

    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
