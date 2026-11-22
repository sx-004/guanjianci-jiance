#!/usr/bin/env bash
# 把本轮去重状态与报告提交到独立的 state 分支。
# 不用 actions/cache：缓存被逐出会导致每轮重发。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 先把本轮产物存到临时目录：下面的 checkout 会覆盖工作区里的 state 文件。
if [ -f state/state.json ]; then cp state/state.json "$TMP/state.json"; fi
if [ -f report.json ]; then cp report.json "$TMP/report.json"; fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

apply_files() {
  mkdir -p state
  if [ -f "$TMP/state.json" ]; then cp "$TMP/state.json" state/state.json; fi
  if [ -f "$TMP/report.json" ]; then
    cp "$TMP/report.json" state/last-report.json
  else
    printf '{}\n' > state/last-report.json
  fi
  git add -f state/state.json state/last-report.json
}

# state 分支可能同时被人改过（手动修状态），推不上去就重来一次。
for attempt in 1 2 3; do
  git fetch origin "$STATE_BRANCH"
  git checkout -f -B "$STATE_BRANCH" "origin/$STATE_BRANCH"
  apply_files
  if git diff --cached --quiet; then
    echo "状态无变化，无需提交。"
    exit 0
  fi
  git commit -m "chore: 更新去重状态与运行报告 $STAMP"
  if git push origin "$STATE_BRANCH"; then
    echo "状态已提交到 $STATE_BRANCH 分支。"
    exit 0
  fi
  echo "推 state 分支失败（第 $attempt 次），重新同步后再试。"
done

echo "连续 3 次推不上 state 分支，放弃（去重状态本轮没存上）。"
exit 1
