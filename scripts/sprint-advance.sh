#!/bin/bash
# sprint-advance.sh — Automated sprint advancement
#
# Checks if all issues in the current sprint are closed in the monorepo.
# If so, marks it complete, creates issues for the next sprint from YAML templates.
# Sends Signal notification via pgm-signal-gate.sh on advancement.
#
# Usage:
#   bash scripts/sprint-advance.sh            # Normal run
#   bash scripts/sprint-advance.sh --dry-run  # Preview what would happen
#
# Called from agent-loop.sh every 5 minutes.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/Documents/claude/nuc-vector-orchestrator}"
STATE_DIR="$REPO_DIR/.claude/state"
DEFINITIONS="$REPO_DIR/config/sprint-definitions.yaml"
STATE_FILE="$STATE_DIR/sprint-state.json"

NUC_REPO="ShesekBean/nuc-vector-orchestrator"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SPRINT: $*"
}

# ── Parse YAML via inline Python ───────────────────────────────────────────────
# Returns JSON array of sprint objects from the YAML definitions
parse_definitions() {
    python3 - "$DEFINITIONS" <<'PARSE_PY'
import sys, re, json

with open(sys.argv[1]) as f:
    content = f.read()

sprints = []
# Split on "  - id:" to get each sprint block
blocks = re.split(r'\n  - id:', content)
for i, block in enumerate(blocks):
    if i == 0:
        continue  # skip preamble before first sprint
    block = "id:" + block

    id_m = re.search(r'^id:\s*(.+)', block, re.MULTILINE)
    name_m = re.search(r'name:\s*"([^"]*)"', block)
    status_m = re.search(r'status:\s*(\S+)', block)
    label_m = re.search(r'label:\s*"([^"]*)"', block)

    if not id_m:
        continue

    sprint = {
        "id": id_m.group(1).strip().strip('"'),
        "name": name_m.group(1) if name_m else "",
        "status": status_m.group(1) if status_m else "pending",
        "label": label_m.group(1) if label_m else "",
        "issues": []
    }

    # Parse issues if present
    issue_blocks = re.split(r'\n      - title:', block)
    for j, ib in enumerate(issue_blocks):
        if j == 0:
            continue
        ib = "title:" + ib

        title_m = re.search(r'^title:\s*"([^"]*)"', ib, re.MULTILINE)
        repo_m = re.search(r'repo:\s*(\S+)', ib)
        body_m = re.search(r'body:\s*\|\n((?:          .+\n?)*)', ib)
        labels_m = re.search(r'labels:\s*\[([^\]]*)\]', ib)

        if not title_m:
            continue

        labels = []
        if labels_m:
            labels = [l.strip().strip('"') for l in labels_m.group(1).split(',')]

        body = ""
        if body_m:
            body = re.sub(r'^          ', '', body_m.group(1), flags=re.MULTILINE).strip()

        sprint["issues"].append({
            "title": title_m.group(1),
            "repo": repo_m.group(1) if repo_m else "nuc",
            "labels": labels,
            "body": body
        })

    sprints.append(sprint)

print(json.dumps(sprints))
PARSE_PY
}

# ── Load/save state ───────────────────────────────────────────────────────────
load_state() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo '{}'
    fi
}

save_state() {
    local new_state="$1"
    if [[ "$DRY_RUN" == "true" ]]; then
        log "[DRY RUN] Would save state: $new_state"
    else
        echo "$new_state" > "$STATE_FILE"
    fi
}

# ── Check if sprint label has all issues closed ──────────────────────────────
# Returns 0 if all closed (or no issues exist), 1 if some open
check_sprint_complete() {
    local label="$1"

    # Check monorepo (all issues — NUC and Vector — are on nuc-vector-orchestrator)
    local total_open
    total_open=$(gh issue list -R "$NUC_REPO" -l "$label" --state open --json number -q 'length' 2>/dev/null || echo "0")

    # Also check that issues EXIST (don't auto-advance if label has never been used)
    local total_issues
    total_issues=$(gh issue list -R "$NUC_REPO" -l "$label" --state all --json number -q 'length' 2>/dev/null || echo "0")

    if (( total_issues == 0 )); then
        # No issues with this label at all — sprint hasn't started
        return 2
    fi

    if (( total_open == 0 )); then
        log "Sprint '$label': all $total_issues issues closed"
        return 0
    else
        log "Sprint '$label': $total_open of $total_issues issues still open"
        return 1
    fi
}

# ── Create an issue with title-based dedup ────────────────────────────────────
create_issue() {
    local repo_slug="$1" title="$2" body="$3"
    shift 3
    local labels=("$@")

    # Check if issue with this title already exists (open or closed)
    local existing
    existing=$(gh issue list -R "$repo_slug" --state all --search "\"$title\" in:title" --json number,title \
        -q "[.[] | select(.title == \"$title\")] | length" 2>/dev/null || echo "0")

    if (( existing > 0 )); then
        log "  SKIP (exists): $title"
        return 0
    fi

    # Build label args
    local label_args=""
    for l in "${labels[@]}"; do
        label_args+=" -l \"$l\""
    done

    if [[ "$DRY_RUN" == "true" ]]; then
        log "  [DRY RUN] Would create on $repo_slug: $title (labels: ${labels[*]})"
    else
        # Ensure labels exist
        for l in "${labels[@]}"; do
            gh label create "$l" -R "$repo_slug" --color "0e8a16" 2>/dev/null || true
        done

        # Create issue
        local body_file
        body_file=$(mktemp /tmp/sprint-issue-XXXXXX.md)
        echo "$body" > "$body_file"

        local issue_url label_args
        label_args=()
        for lbl in "${labels[@]}"; do
            label_args+=(-l "$lbl")
        done
        issue_url=$(gh issue create -R "$repo_slug" \
            --title "$title" \
            --body-file "$body_file" \
            "${label_args[@]}" \
            2>&1) || {
            log "  FAIL: could not create '$title' on $repo_slug"
            rm -f "$body_file"
            return 1
        }
        rm -f "$body_file"
        log "  CREATED: $issue_url"
    fi
}

# ── Main logic ────────────────────────────────────────────────────────────────
main() {
    if [[ ! -f "$DEFINITIONS" ]]; then
        log "ERROR: $DEFINITIONS not found"
        exit 1
    fi

    local definitions state
    definitions=$(parse_definitions)
    state=$(load_state)

    # Find the current active sprint (first non-complete in state)

    # Walk sprints in order, find first that isn't complete in state
    local sprint_count
    sprint_count=$(echo "$definitions" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

    local advanced=false

    for idx in $(seq 0 $((sprint_count - 1))); do
        local sid sname sstatus slabel
        sid=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$idx]['id'])")
        sname=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$idx]['name'])")
        sstatus=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$idx]['status'])")
        slabel=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$idx].get('label',''))")

        # Check if already complete in state
        local state_status
        state_status=$(echo "$state" | python3 -c "
import sys, json
s = json.load(sys.stdin)
print(s.get('sprints', {}).get('$sid', {}).get('status', 'unknown'))
" 2>/dev/null || echo "unknown")

        if [[ "$state_status" == "complete" || "$sstatus" == "complete" ]]; then
            continue
        fi

        # This is the current active sprint


        if [[ -z "$slabel" ]]; then
            log "Sprint $sid has no label defined — cannot check GitHub issues"
            break
        fi

        log "Checking sprint $sid ($sname) with label '$slabel'..."

        local check_result=0
        check_sprint_complete "$slabel" || check_result=$?

        if (( check_result == 0 )); then
            # Sprint complete! Mark it and advance
            log "Sprint $sid ($sname) is COMPLETE!"
            state=$(echo "$state" | python3 -c "
import sys, json
s = json.load(sys.stdin)
if 'sprints' not in s: s['sprints'] = {}
s['sprints']['$sid'] = {'status': 'complete', 'completed_at': '$(date -Iseconds)'}
print(json.dumps(s, indent=2))
")
            save_state "$state"
            advanced=true

            # Generate sprint changelog
            if [[ "$DRY_RUN" == "true" ]]; then
                log "[DRY RUN] Would generate changelog for Sprint $sid ($sname) label=$slabel"
            else
                bash "$REPO_DIR/scripts/sprint-changelog.sh" "$sid" "$sname" "$slabel" || \
                    log "WARN: changelog generation failed for Sprint $sid"
            fi

            # Refresh project summary snapshot
            if [[ "$DRY_RUN" == "true" ]]; then
                log "[DRY RUN] Would refresh project-shon-summary"
            else
                bash "$REPO_DIR/scripts/update-project-summary.sh" || \
                    log "WARN: project summary refresh failed"
            fi

            # Find and create next sprint's issues
            local next_idx=$((idx + 1))
            if (( next_idx < sprint_count )); then
                local next_sid next_sname next_sstatus
                next_sid=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$next_idx]['id'])")
                next_sname=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$next_idx]['name'])")
                next_sstatus=$(echo "$definitions" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d[$next_idx]['status'])")

                if [[ "$next_sstatus" != "complete" ]]; then
                    log "Creating issues for Sprint $next_sid ($next_sname)..."
                    create_sprint_issues "$definitions" "$next_idx"

                    # Send Signal notification
                    local msg="📊 PGM: 🎉 Sprint $sid ($sname) complete! Auto-advancing to Sprint $next_sid ($next_sname). Issues created."
                    if [[ "$DRY_RUN" == "true" ]]; then
                        log "[DRY RUN] Would send Signal: $msg"
                    else
                        bash "$REPO_DIR/scripts/pgm-signal-gate.sh" "general" "sprint-$sid" "$msg" || true
                    fi
                fi
            else
                log "No more sprints defined after $sid"
                local msg="📊 PGM: 🎉 Sprint $sid ($sname) complete! All defined sprints are done."
                if [[ "$DRY_RUN" == "true" ]]; then
                    log "[DRY RUN] Would send Signal: $msg"
                else
                    bash "$REPO_DIR/scripts/pgm-signal-gate.sh" "general" "sprint-$sid" "$msg" || true
                fi
            fi
        elif (( check_result == 2 )); then
            # No issues exist yet — create them from template
            log "Sprint $sid ($sname) has no issues yet — creating from template..."
            create_sprint_issues "$definitions" "$idx"
        fi
        # Only process one sprint at a time
        break
    done

    if [[ "$advanced" == "false" ]]; then
        log "No sprint advancement needed"
    fi
}

# ── Create issues for a sprint from definitions ──────────────────────────────
create_sprint_issues() {
    local definitions="$1" sprint_idx="$2"

    local issue_count
    issue_count=$(echo "$definitions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(len(d[$sprint_idx].get('issues', [])))
")

    if (( issue_count == 0 )); then
        log "  No issue templates for this sprint"
        return 0
    fi

    for iidx in $(seq 0 $((issue_count - 1))); do
        local ititle irepo ibody
        ititle=$(echo "$definitions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d[$sprint_idx]['issues'][$iidx]['title'])
")
        irepo=$(echo "$definitions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d[$sprint_idx]['issues'][$iidx]['repo'])
")
        ibody=$(echo "$definitions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d[$sprint_idx]['issues'][$iidx]['body'])
")
        local ilabels_json
        ilabels_json=$(echo "$definitions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for l in d[$sprint_idx]['issues'][$iidx]['labels']:
    print(l)
")

        # All issues go to the monorepo; vector issues get component:vector label
        local repo_slug="$NUC_REPO"
        if [[ "$irepo" == "vector" ]]; then
            # Ensure component:vector label is included
            ilabels_json="$ilabels_json
component:vector"
        fi

        # Collect labels into array
        local labels=()
        while IFS= read -r label; do
            [[ -n "$label" ]] && labels+=("$label")
        done <<< "$ilabels_json"

        create_issue "$repo_slug" "$ititle" "$ibody" "${labels[@]}"
    done
}

main "$@"
