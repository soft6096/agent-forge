#!/usr/bin/env bash
# agent-forge 安装脚本：只从 git 远端安装，不接受本地工作区作为来源。
#
#   ./install.sh                 拷贝形态（默认，兼容性最好）
#   ./install.sh --link          链接形态（一份副本 + 软链，多 Agent 混用推荐）
#   ./install.sh --list          只预览，不安装
#   ./install.sh --ref v0.1.0    指定分支 / 标签
#   ./install.sh --repo github   指定远端别名（gitee / github）或完整 git 地址
#   ./install.sh --tool claude   只装到指定工具（可重复）
#   ./install.sh --force         覆盖已存在副本（先备份）
#   ./install.sh --no-global-rules  不分发全局规则
#
set -euo pipefail

REMOTE_GITEE="https://gitee.com/soft6096/agent-forge.git"
REMOTE_GITHUB="https://github.com/soft6096/agent-forge.git"
DEFAULT_REPO="github"
DEFAULT_REF="main"
REPO_DIR="${AGENT_FORGE_REPO_DIR:-$HOME/.agents/repos/agent-forge}"

MODE="copy"
DRY_RUN=0
FORCE=0
WITH_GLOBAL_RULES=1
REF="$DEFAULT_REF"
REPO="$DEFAULT_REPO"
TOOLS=()

log()  { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die()  { printf '错误：%s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --link)             MODE="link" ;;
    --list)             DRY_RUN=1 ;;
    --force)            FORCE=1 ;;
    --no-global-rules)  WITH_GLOBAL_RULES=0 ;;
    --ref)              shift; REF="${1:-}" ;;
    --repo)             shift; REPO="${1:-}" ;;
    --tool)             shift; TOOLS+=("${1:-}") ;;
    -h|--help)          awk 'NR>1 { if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else exit }' "$0"; exit 0 ;;
    *)                  die "未知参数：$1（--help 查看用法）" ;;
  esac
  shift
done

case "$REPO" in
  gitee)  REMOTE="$REMOTE_GITEE" ;;
  github) REMOTE="$REMOTE_GITHUB" ;;
  http*|git@*) REMOTE="$REPO" ;;
  *)      die "--repo 只接受 gitee / github / 完整 git 地址" ;;
esac

# 工具 → 技能目录（仅安装到已存在的配置目录）
tool_dir() {
  case "$1" in
    agents)    printf '%s' "$HOME/.agents/skills" ;;
    claude)    printf '%s' "$HOME/.claude/skills" ;;
    codebuddy) printf '%s' "$HOME/.codebuddy/skills" ;;
    trae)      printf '%s' "$HOME/.trae/skills" ;;
    hermes)    printf '%s' "$HOME/.hermes/skills" ;;
    qoder)     printf '%s' "$HOME/.qoder/skills" ;;
    *)         printf '' ;;
  esac
}

ALL_TOOLS="agents claude codebuddy trae hermes qoder"

detect_tools() {
  local out=()
  local t
  for t in $ALL_TOOLS; do
    local d; d="$(tool_dir "$t")"
    [ -n "$d" ] || continue
    # 已存在技能目录，或对应工具配置目录存在
    if [ -d "$d" ] || [ -d "$(dirname "$d")" ]; then
      out+=("$t")
    fi
  done
  # 有命中才输出；无命中时函数必须返回 0，否则调用处的赋值会被 set -e 提前终止
  if [ ${#out[@]} -gt 0 ]; then
    printf '%s\n' "${out[@]}"
  fi
  return 0
}

if [ ${#TOOLS[@]} -eq 0 ]; then
  detected="$(detect_tools || true)"
  [ -n "$detected" ] || die "未检测到任何 Agent 技能目录；用 --tool <agents|claude|codebuddy|trae|hermes|qoder> 显式指定"
  # 兼容 bash 3.2（macOS 自带）：不使用 mapfile
  while IFS= read -r t; do
    [ -n "$t" ] && TOOLS+=("$t")
  done <<< "$detected"
fi

log "来源：$REMOTE"
log "引用：$REF"
log "形态：$MODE"
log "安装副本：$REPO_DIR"
log "目标工具：${TOOLS[*]}"

if [ "$DRY_RUN" -eq 1 ]; then
  for t in "${TOOLS[@]}"; do log "  将安装到：$(tool_dir "$t")"; done
  [ "$WITH_GLOBAL_RULES" -eq 1 ] && log "  将分发全局规则受管块到各 Agent 全局指令文件"
  exit 0
fi

# ---------- 1. 更新安装副本（始终来自远端） ----------
if [ -d "$REPO_DIR/.git" ]; then
  log "更新安装副本..."
  # --ref 允许分支 / 标签 / 提交号：先按 refspec 拉，失败退回拉全部分支
  git -C "$REPO_DIR" fetch --quiet origin "$REF" \
    || git -C "$REPO_DIR" fetch --quiet origin \
    || die "fetch 失败：$REMOTE ($REF)"
  git -C "$REPO_DIR" checkout --quiet FETCH_HEAD || die "checkout 失败（ref：${REF}）"
else
  # 目录存在但不是 git 仓库时不再静默清理（下面有 rm -rf），避免误删用户内容
  if [ -e "$REPO_DIR" ] && [ -n "$(ls -A "$REPO_DIR" 2>/dev/null)" ]; then
    die "安装副本路径已存在且不是 git 仓库：${REPO_DIR}（请手动清理，或设 AGENT_FORGE_REPO_DIR 换路径）"
  fi
  log "拉取安装副本..."
  mkdir -p "$(dirname "$REPO_DIR")"
  # 分支 / 标签可浅克隆；提交号不支持 --branch，退回全量克隆再 checkout
  if ! git clone --quiet --depth 1 --branch "$REF" "$REMOTE" "$REPO_DIR" 2>/dev/null; then
    rm -rf "$REPO_DIR"
    git clone --quiet "$REMOTE" "$REPO_DIR" || die "clone 失败：$REMOTE"
    git -C "$REPO_DIR" checkout --quiet "$REF" \
      || die "checkout 失败：$REF 不是 $REMOTE 的分支、标签或提交"
  fi
fi

COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
CDATE="$(git -C "$REPO_DIR" log -1 --format=%cd --date=short)"
CTITLE="$(git -C "$REPO_DIR" log -1 --format=%s)"
log "远端快照：$COMMIT $CDATE $CTITLE"

[ -d "$REPO_DIR/skill" ] || die "安装副本缺少 skill/ 目录，来源可能不是 agent-forge"

cat > "$REPO_DIR/.agent-forge-origin" <<EOF
source=$REMOTE
ref=$REF
commit=$COMMIT
commit_date=$CDATE
installed_at=$(date '+%Y-%m-%d %H:%M:%S')
mode=$MODE
EOF

# ---------- 2. 分发技能 ----------
STAMP="$(date '+%Y%m%d%H%M%S')"
installed=0
skipped=0

for t in "${TOOLS[@]}"; do
  dest="$(tool_dir "$t")"
  [ -n "$dest" ] || die "未知工具：$t"
  mkdir -p "$dest"
  for src in "$REPO_DIR"/skill/*/; do
    name="$(basename "$src")"
    target="$dest/$name"
    if [ "$MODE" = "link" ]; then
      if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$FORCE" -eq 1 ]; then
          # 软链只删链接本身；实体目录先备份，避免 rm -rf 抹掉用户内容
          if [ -L "$target" ]; then
            rm -f "$target"
          else
            mv "$target" "$target.pre-agent-forge-$STAMP"
          fi
        else
          warn "已存在，跳过（未更新）：${target}（要覆盖请加 --force）"
          skipped=$((skipped + 1))
          continue
        fi
      fi
      ln -s "${src%/}" "$target"
    else
      if [ -e "$target" ] || [ -L "$target" ]; then
        if [ "$FORCE" -eq 1 ]; then
          mv "$target" "$target.pre-agent-forge-$STAMP"
        else
          warn "已存在，跳过（未更新）：${target}（要覆盖请加 --force）"
          skipped=$((skipped + 1))
          continue
        fi
      fi
      cp -R "$src" "$target"
    fi
    installed=$((installed + 1))
  done
  log "已写入：$dest"
done

log "技能实例：$installed 个"
if [ "$skipped" -gt 0 ]; then
  warn "有 $skipped 个技能因已存在被跳过，版本未更新；确认要覆盖请重跑并加 --force"
fi

# ---------- 3. 分发全局规则（受管块，幂等） ----------
if [ "$WITH_GLOBAL_RULES" -eq 1 ] && [ -f "$REPO_DIR/global-rules.md" ]; then
  BEGIN_MARK="# BEGIN agent-forge::global-rules"
  END_MARK="# END agent-forge::global-rules"
  targets=()
  for f in "$HOME/.agents/AGENTS.md" "$HOME/.claude/CLAUDE.md" "$HOME/.codebuddy/AGENTS.md" \
           "$HOME/.trae/AGENTS.md" "$HOME/.hermes/AGENTS.md" "$HOME/.qoder/AGENTS.md"; do
    [ -f "$f" ] && targets+=("$f")
  done
  if [ ${#targets[@]} -eq 0 ]; then
    warn "未找到任何 Agent 全局指令文件，跳过全局规则分发"
  else
    for f in "${targets[@]}"; do
      tmp="$(mktemp)"
      # 先删掉旧受管块，再去掉文件尾部空行，保证重跑幂等（不会每次多一个空行）
      awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { skip=1; next }
        $0 == e { skip=0; next }
        skip    { next }
        { lines[++n] = $0 }
        END {
          while (n > 0 && lines[n] == "") n--
          for (i = 1; i <= n; i++) print lines[i]
        }
      ' "$f" > "$tmp"
      {
        printf '\n%s\n' "$BEGIN_MARK"
        cat "$REPO_DIR/global-rules.md"
        printf '%s\n' "$END_MARK"
      } >> "$tmp"
      mv "$tmp" "$f"
      log "全局规则已写入：$f"
    done
  fi
fi

log "完成。新开会话或重载技能后生效。"
