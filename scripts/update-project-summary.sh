#!/bin/bash
# update-project-summary.sh — Refresh project-shon-summary from live repos
#
# Regenerates ~/Documents/claude/project-shon-summary/ with current state
# of the monorepo. This folder is for onboarding new LLM sessions —
# a self-contained snapshot of everything about the project.
#
# Called by sprint-advance.sh after sprint completion, or manually.
#
# Usage: bash scripts/update-project-summary.sh

set -euo pipefail

NUC_REPO="/home/ophirsw/Documents/claude/nuc-vector-orchestrator"
SUMMARY_DIR="/home/ophirsw/Documents/claude/project-shon-summary"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SUMMARY: $*"
}

log "Refreshing project summary at $SUMMARY_DIR..."

# ── Clean and recreate ──
rm -rf "$SUMMARY_DIR"
mkdir -p "$SUMMARY_DIR"

# ── NUC snapshot ──
NUC_OUT="$SUMMARY_DIR/nuc-orchestrator"
mkdir -p "$NUC_OUT"/{agents,docs,.github/workflows,apps/control_plane,apps/test_harness,apps/openclaw,scripts,config,deploy,memory,tests}

# Core config
cp "$NUC_REPO/.claude/CLAUDE.md"                  "$NUC_OUT/CLAUDE.md"
cp "$NUC_REPO/.claude/settings.json"               "$NUC_OUT/settings.json"
cp "$NUC_REPO/README.md"                           "$NUC_OUT/README.md"

# Agent definitions
for f in "$NUC_REPO"/.claude/agents/*.md; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/agents/"
done

# Docs
for f in "$NUC_REPO"/docs/*; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/docs/"
done

# Key scripts
for f in "$NUC_REPO"/scripts/*.sh "$NUC_REPO"/scripts/*.py; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/scripts/"
done

# Config
for f in "$NUC_REPO"/config/*.yaml; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/config/"
done

# CI workflows
for f in "$NUC_REPO"/.github/workflows/*.yml; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/.github/workflows/"
done

# Test harness
for f in "$NUC_REPO"/apps/test_harness/*.py "$NUC_REPO"/apps/test_harness/*.yaml; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/apps/test_harness/"
done

# OpenClaw mods
for f in "$NUC_REPO"/apps/openclaw/*; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/apps/openclaw/"
done

# Tests
for f in "$NUC_REPO"/tests/*.py; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/tests/"
done

# Memory files (useful for context)
for f in "$NUC_REPO"/.claude/projects/*/memory/*.md; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/memory/" 2>/dev/null || true
done
# Also grab from the direct memory path
for f in /home/ophirsw/.claude/projects/-home-ophirsw-Documents-claude-nuc-vector-orchestrator/memory/*.md; do
    [[ -f "$f" ]] && cp "$f" "$NUC_OUT/memory/" 2>/dev/null || true
done

# ── Vector snapshot ──
# TODO: Add Vector-specific files when available (gRPC protos, vector SDK code, etc.)
# Vector communicates via gRPC — no SSH/Docker/Jetson infrastructure to snapshot.

# ── Generate project overview ──
COMPLETED_SPRINTS=$(cat "$NUC_REPO/.claude/state/sprint-state.json" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    state = json.load(sys.stdin)
    sprints = state.get('sprints', {})
    completed = [f'Sprint {k} (completed {v.get(\"completed_at\",\"?\")[:10]})' for k,v in sorted(sprints.items()) if v.get('status') == 'complete']
    print('\n'.join(completed) if completed else 'None recorded')
except: print('Unable to parse')
" 2>/dev/null || echo "Unable to parse")

NUC_OPEN=$(gh issue list -R ShesekBean/nuc-vector-orchestrator --state open --json number,title,labels \
    --jq '.[] | "- #\(.number): \(.title) [\([.labels[].name] | join(", "))]"' 2>/dev/null || echo "  (unable to fetch)")
VECTOR_OPEN=$(gh issue list -R ShesekBean/nuc-vector-orchestrator -l "component:vector" --state open --json number,title,labels \
    --jq '.[] | "- #\(.number): \(.title) [\([.labels[].name] | join(", "))]"' 2>/dev/null || echo "  (unable to fetch)")

cat > "$SUMMARY_DIR/00-PROJECT-OVERVIEW.md" << OVERVIEW
# Project Shon — Summary

**Generated:** $(date '+%Y-%m-%d %H:%M')
**Purpose:** Self-contained snapshot for onboarding new LLM sessions.

## What Is This?

Project Shon is a distributed multi-agent robotics system where a robot (Anki/DDL Vector)
codes itself, tests itself, and improves itself — with minimal human intervention.

The NUC orchestrates via GitHub Issues; Vector communicates via gRPC SDK.
- **NUC "desk"** (Intel x86_64, Ubuntu) — orchestrator, Signal gateway, NUC-side agents
- **Vector** — robot hardware (gRPC SDK)

**Repo:** \`ShesekBean/nuc-vector-orchestrator\` (monorepo)
**Human:** Ophir (communicates via Signal messenger)

## Completed Sprints

$COMPLETED_SPRINTS

## Open Issues — NUC

$NUC_OPEN

## Open Issues — Vector (component:vector)

$VECTOR_OPEN

## Folder Structure

\`\`\`
project-shon-summary/
├── 00-PROJECT-OVERVIEW.md     ← this file
├── nuc-vector-orchestrator/
│   ├── CLAUDE.md              ← full NUC project config + sprint roadmap
│   ├── agents/                ← agent definitions (coach, worker, pgm, etc.)
│   ├── docs/                  ← operational lessons, workflow docs, changelogs
│   ├── apps/                  ← control plane, test harness, openclaw mods, vector SDK
│   ├── scripts/               ← signal gate, sprint scripts, utilities
│   ├── config/                ← LLM provider, sprint definitions
│   ├── memory/                ← persistent LLM memory files
│   └── tests/
\`\`\`

## Key Files to Read First

1. **\`CLAUDE.md\`** — architecture, sprint roadmap, agent definitions, safety rules
2. **\`agents/issue-worker.md\`** — how Workers handle issues end-to-end
3. **\`docs/operational-lessons.md\`** — institutional memory (what went wrong and how we fixed it)
4. **\`docs/agent-workflow.md\`** — issue lifecycle diagram
5. **\`apps/control_plane/agent_loop/\`** — the daemon that runs everything
OVERVIEW

log "Generated 00-PROJECT-OVERVIEW.md"

# ── Copy standalone reference docs (not in repos) ──
# Standalone reference docs
# (No Jetson flash doc needed for Vector)

# ── Summary stats ──
nuc_files=$(find "$NUC_OUT" -type f | wc -l)
total_size=$(du -sh "$SUMMARY_DIR" | cut -f1)

log "Done: $nuc_files NUC files, $total_size total"
