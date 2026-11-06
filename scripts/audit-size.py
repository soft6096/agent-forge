#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit-size.py — 技能体积审计（纯标准库）。

为什么需要：技能是加载进 Agent 上下文的文本，越长每个任务越贵，且膨胀是渐进无声的。
本脚本按"实际加载路径"分三类统计，避免把永远不会一次读入的模板算进预算：

  1. SKILL.md        常驻成本（技能被触发时一定加载）
  2. references/     按需成本（同一任务通常会读其中一份，最坏情况全读）
  3. 其它（templates/ commands/ docs/ scripts/）
                    随件，独立产物，不随任务加载；只限文件数，不限行数
                    脚本（.py/.sh）与配置同样计入文件数，避免"代码藏在 scripts/ 不计预算"


预算：SKILL.md ≤ 300 行且 ≤ 24KB；SKILL.md + references ≤ 900 行且 ≤ 72KB；随件 ≤ 40 个文件。
退出码：0 通过 / 1 超预算或增量超阈值 / 2 环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_LINE_BUDGET = 300
SKILL_BYTE_BUDGET = 24 * 1024
LOAD_LINE_BUDGET = 900
LOAD_BYTE_BUDGET = 72 * 1024
ASSET_FILE_BUDGET = 40

WARN_RATIO = 1.05
FAIL_RATIO = 1.20

BASELINE_NAME = ".size-baseline.json"
# 这些目录是"随件"：独立产物，不随任务一次性加载
ASSET_DIRS = {"templates", "commands", "docs", "scripts", "assets"}
# 按文本统计的后缀：技能目录里的脚本与配置也要计入随件文件数
TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".txt"}


def text_stats(p: Path):
    try:
        raw = p.read_bytes()
    except OSError:
        return 0, 0
    return len(raw.decode("utf-8", errors="ignore").splitlines()), len(raw)


def audit_skill(skill_dir: Path):
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return {"name": skill_dir.name, "error": "缺少 SKILL.md",
                "skill_lines": 0, "skill_bytes": 0,
                "load_lines": 0, "load_bytes": 0,
                "asset_lines": 0, "asset_bytes": 0, "asset_files": 0}

    s_lines, s_bytes = text_stats(skill_md)
    load_lines, load_bytes = s_lines, s_bytes
    asset_lines = asset_bytes = asset_files = 0

    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p == skill_md:
            continue
        if "__pycache__" in p.parts or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        lines, size = text_stats(p)
        if ASSET_DIRS & set(p.parts):
            asset_lines += lines
            asset_bytes += size
            asset_files += 1
        else:
            load_lines += lines
            load_bytes += size

    return {"name": skill_dir.name, "error": None,
            "skill_lines": s_lines, "skill_bytes": s_bytes,
            "load_lines": load_lines, "load_bytes": load_bytes,
            "asset_lines": asset_lines, "asset_bytes": asset_bytes, "asset_files": asset_files}


def load_baseline(root: Path):
    p = root / BASELINE_NAME
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fmt_kb(n: int) -> str:
    return "%.1fKB" % (n / 1024.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="agent-forge 技能体积审计")
    ap.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    ap.add_argument("--write-baseline", action="store_true", help="写入/刷新基线")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    skill_root = root / "skill"
    if not skill_root.is_dir():
        print("错误：未找到 skill/ 目录：%s" % skill_root, file=sys.stderr)
        return 2

    results = [audit_skill(d) for d in sorted(skill_root.iterdir()) if d.is_dir()]
    if not results:
        print("错误：skill/ 下没有任何技能目录", file=sys.stderr)
        return 2

    baseline = load_baseline(root)
    failures, warnings = [], []

    print("# 技能体积审计\n")
    print("| 技能 | SKILL.md | 加载路径(含 references) | 随件 | 与基线比 |")
    print("|---|---|---|---|---|")

    for r in results:
        name = r["name"]
        if r["error"]:
            failures.append("%s：%s" % (name, r["error"]))
            print("| %s | %s | — | — | — |" % (name, r["error"]))
            continue

        if r["skill_lines"] > SKILL_LINE_BUDGET or r["skill_bytes"] > SKILL_BYTE_BUDGET:
            failures.append("%s：SKILL.md 超预算（%d 行 / %s）"
                            % (name, r["skill_lines"], fmt_kb(r["skill_bytes"])))
        if r["load_lines"] > LOAD_LINE_BUDGET or r["load_bytes"] > LOAD_BYTE_BUDGET:
            failures.append("%s：SKILL+references 超预算（%d 行 / %s）"
                            % (name, r["load_lines"], fmt_kb(r["load_bytes"])))
        if r["asset_files"] > ASSET_FILE_BUDGET:
            failures.append("%s：随件文件过多（%d 个，上限 %d）"
                            % (name, r["asset_files"], ASSET_FILE_BUDGET))

        delta = "—"
        prev = baseline.get(name, {}).get("load_bytes", 0)
        if prev:
            ratio = r["load_bytes"] / float(prev)
            pct = (ratio - 1) * 100
            delta = "%+.1f%%" % pct
            if ratio >= FAIL_RATIO:
                failures.append("%s：加载路径较基线增长 %.1f%%（阈值 +20%%）" % (name, pct))
            elif ratio >= WARN_RATIO:
                warnings.append("%s：加载路径较基线增长 %.1f%%（警告线 +5%%）" % (name, pct))

        print("| %s | %d 行 / %s | %d 行 / %s | %d 个 / %s | %s |"
              % (name,
                 r["skill_lines"], fmt_kb(r["skill_bytes"]),
                 r["load_lines"], fmt_kb(r["load_bytes"]),
                 r["asset_files"], fmt_kb(r["asset_bytes"]),
                 delta))

    total_load = sum(r["load_bytes"] for r in results)
    print("\n合计：%d 个技能，加载路径合计 %s" % (len(results), fmt_kb(total_load)))

    if args.write_baseline:
        data = {r["name"]: {"load_lines": r["load_lines"], "load_bytes": r["load_bytes"]}
                for r in results if not r["error"]}
        (root / BASELINE_NAME).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print("基线已写入：%s" % BASELINE_NAME)

    for w in warnings:
        print("警告：%s" % w, file=sys.stderr)
    for f in failures:
        print("失败：%s" % f, file=sys.stderr)

    if failures:
        return 1
    print("\n结论：全部在预算线内。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
