#!/bin/bash
# sprint-changelog.sh — Generate docs/sprint-N-changelog.md on sprint completion
#
# Collects all closed issues + merged PRs for a sprint label from the monorepo,
# then writes a changelog to docs/sprint-<id>-changelog.md.
#
# Usage:
#   bash scripts/sprint-changelog.sh <sprint_id> <sprint_name> <sprint_label>
#
# Example:
#   bash scripts/sprint-changelog.sh 4 "Face Tracking V0.2" sprint-4
#
# Called by sprint-advance.sh when a sprint completes.

set -euo pipefail

SPRINT_ID="${1:?Usage: sprint-changelog.sh <sprint_id> <sprint_name> <sprint_label>}"
SPRINT_NAME="${2:?Missing sprint name}"
SPRINT_LABEL="${3:?Missing sprint label}"

NUC_REPO="ophir-sw/nuc-vector-orchestrator"
NUC_DIR="${REPO_DIR:-$HOME/Documents/claude/nuc-vector-orchestrator}"
# No separate Jetson directory — Vector code lives in apps/vector/

OUTFILE="$NUC_DIR/docs/sprint-${SPRINT_ID}-changelog.md"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] CHANGELOG: $*"
}

log "Generating changelog for Sprint $SPRINT_ID ($SPRINT_NAME)..."

# ── Gather closed issues ──
gather_issues() {
    local repo="$1"
    gh issue list -R "$repo" -l "$SPRINT_LABEL" --state closed \
        --json number,title,closedAt,labels,body \
        --jq '.[] | "- **#\(.number)**: \(.title) _(closed \(.closedAt | split("T")[0]))_"' \
        2>/dev/null || echo "  _(none)_"
}

# ── Gather merged PRs ──
gather_prs() {
    local repo="$1"
    gh pr list -R "$repo" -l "$SPRINT_LABEL" --state merged \
        --json number,title,mergedAt,additions,deletions \
        --jq '.[] | "- **PR #\(.number)**: \(.title) _(merged \(.mergedAt | split("T")[0]), +\(.additions)/-\(.deletions))_"' \
        2>/dev/null || echo "  _(none)_"
}

# ── Gather key commits (PRs may not cover direct-to-main commits) ──
gather_commits() {
    local repo_dir="$1"
    if [[ ! -d "$repo_dir" ]]; then
        echo "  _(repo not available locally)_"
        return
    fi
    # Find commits that mention the sprint in their message
    git -C "$repo_dir" log --oneline --all --grep="$SPRINT_LABEL\|Sprint $SPRINT_ID\|sprint-$SPRINT_ID" \
        --since="6 months ago" --format="- %h %s (%ai)" 2>/dev/null | head -30 || echo "  _(none found)_"
}

NUC_ISSUES=$(gather_issues "$NUC_REPO")
VECTOR_ISSUES=$(gh issue list -R "$NUC_REPO" -l "$SPRINT_LABEL" -l "component:vector" --state closed \
    --json number,title,closedAt,labels,body \
    --jq '.[] | "- **#\(.number)**: \(.title) _(closed \(.closedAt | split("T")[0]))_"' \
    2>/dev/null || echo "  _(none)_")
NUC_PRS=$(gather_prs "$NUC_REPO")
VECTOR_PRS=$(gh pr list -R "$NUC_REPO" -l "$SPRINT_LABEL" -l "component:vector" --state merged \
    --json number,title,mergedAt,additions,deletions \
    --jq '.[] | "- **PR #\(.number)**: \(.title) _(merged \(.mergedAt | split("T")[0]), +\(.additions)/-\(.deletions))_"' \
    2>/dev/null || echo "  _(none)_")
NUC_COMMITS=$(gather_commits "$NUC_DIR")

# ── Count totals ──
nuc_issue_count=$(gh issue list -R "$NUC_REPO" -l "$SPRINT_LABEL" --state closed --json number -q 'length' 2>/dev/null || echo 0)
vector_issue_count=$(gh issue list -R "$NUC_REPO" -l "$SPRINT_LABEL" -l "component:vector" --state closed --json number -q 'length' 2>/dev/null || echo 0)
total_issues=$((nuc_issue_count))

nuc_pr_count=$(gh pr list -R "$NUC_REPO" -l "$SPRINT_LABEL" --state merged --json number -q 'length' 2>/dev/null || echo 0)
vector_pr_count=$(gh pr list -R "$NUC_REPO" -l "$SPRINT_LABEL" -l "component:vector" --state merged --json number -q 'length' 2>/dev/null || echo 0)
total_prs=$((nuc_pr_count))

# ── Write changelog ──
cat > "$OUTFILE" << CHANGELOG
# Sprint $SPRINT_ID: $SPRINT_NAME — Changelog

**Completed:** $(date '+%Y-%m-%d')
**Label:** \`$SPRINT_LABEL\`
**Totals:** $total_issues issues closed, $total_prs PRs merged

---

## NUC Issues ($nuc_issue_count)

$NUC_ISSUES

## Vector Issues ($vector_issue_count)

$VECTOR_ISSUES

## NUC PRs ($nuc_pr_count)

$NUC_PRS

## Vector PRs ($vector_pr_count)

$VECTOR_PRS

## Key Commits

$NUC_COMMITS
CHANGELOG

log "Wrote $OUTFILE"

# No separate directory to copy to — monorepo only

log "Sprint $SPRINT_ID changelog complete."
