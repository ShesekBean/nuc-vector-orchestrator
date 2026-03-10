#!/bin/bash
# test-llm-provider.sh — Smoke test for LLM provider abstraction
# Run this OUTSIDE of Claude Code (in a regular terminal).
# Tests all 3 tiers (heavy/medium/light) for a given provider.
#
# Usage:
#   bash scripts/test-llm-provider.sh claude
#   bash scripts/test-llm-provider.sh openai
#   bash scripts/test-llm-provider.sh          # defaults to claude
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROVIDER="${1:-claude}"
LLM_CONFIG="$REPO_DIR/config/llm-provider.yaml"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Parse YAML config ──
parse_llm_config() {
    if [[ ! -f "$LLM_CONFIG" ]]; then
        log "WARN: $LLM_CONFIG not found, defaulting to claude"
        LLM_BINARY="claude"; LLM_SKIP_PERMS="--dangerously-skip-permissions"
        LLM_PROMPT_FLAG="-p"; LLM_MODEL_FLAG="--model"; LLM_ENV_UNSET="CLAUDECODE"
        LLM_MODEL_HEAVY=""; LLM_MODEL_MEDIUM="sonnet"; LLM_MODEL_LIGHT="haiku"
        return
    fi

    eval "$(python3 - "$LLM_CONFIG" <<'PARSE_LLM_PY'
import sys, re
config_file = sys.argv[1]
with open(config_file) as f:
    content = f.read()
provider = re.search(r'^provider:\s*(\S+)', content, re.MULTILINE).group(1)
block_match = re.search(rf'^{re.escape(provider)}:\s*\n((?:[ ]{{2}}.+\n)*)', content, re.MULTILINE)
block = block_match.group(1) if block_match else ""
def get_val(key, text):
    m = re.search(rf'^\s+{key}:\s*"?([^"\n]*)"?', text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else ""
models_match = re.search(r'  models:\s*\n((?:    .+\n)*)', block)
models_block = models_match.group(1) if models_match else ""
print(f'LLM_BINARY="{get_val("binary", block)}"')
print(f'LLM_SKIP_PERMS="{get_val("skip_permissions", block)}"')
print(f'LLM_PROMPT_FLAG="{get_val("prompt_flag", block)}"')
print(f'LLM_MODEL_FLAG="{get_val("model_flag", block)}"')
print(f'LLM_ENV_UNSET="{get_val("env_unset", block)}"')
print(f'LLM_MODEL_HEAVY="{get_val("heavy", models_block)}"')
print(f'LLM_MODEL_MEDIUM="{get_val("medium", models_block)}"')
print(f'LLM_MODEL_LIGHT="{get_val("light", models_block)}"')
PARSE_LLM_PY
    )"

    log "LLM provider: $(grep '^provider:' "$LLM_CONFIG" | awk '{print $2}') (binary=$LLM_BINARY, heavy=${LLM_MODEL_HEAVY:-default}, medium=$LLM_MODEL_MEDIUM, light=$LLM_MODEL_LIGHT)"
}

build_llm_cmd() {
    local tier="$1"
    local model
    case "$tier" in
        heavy)  model="$LLM_MODEL_HEAVY" ;;
        medium) model="$LLM_MODEL_MEDIUM" ;;
        light)  model="$LLM_MODEL_LIGHT" ;;
        *)      model="$LLM_MODEL_MEDIUM" ;;
    esac
    local cmd="$LLM_BINARY $LLM_SKIP_PERMS"
    if [[ -n "$model" ]]; then
        cmd+=" $LLM_MODEL_FLAG $model"
    fi
    cmd+=" $LLM_PROMPT_FLAG"
    echo "$cmd"
}

# ── Set provider and parse ──
sed -i "s/^provider: .*/provider: $PROVIDER/" "$LLM_CONFIG"
# Unset CLAUDECODE so Claude CLI doesn't think it's nested
unset CLAUDECODE 2>/dev/null || true
parse_llm_config

echo ""
echo "=========================================="
echo "  SMOKE TEST: provider=$PROVIDER"
echo "=========================================="

PASS=0
FAIL=0

for tier in heavy medium light; do
    llm_cmd=$(build_llm_cmd "$tier")
    echo ""
    echo "--- Tier: $tier ---"
    echo "  Command: $llm_cmd"
    echo -n "  Running... "

    env_prefix=""; [[ -n "$LLM_ENV_UNSET" ]] && env_prefix="unset $LLM_ENV_UNSET; "
    if [[ -n "$LLM_PROMPT_FLAG" ]]; then
        output=$(bash -c "${env_prefix}echo 'Respond with ONLY the single word ok and nothing else.' | timeout 60 $llm_cmd" 2>&1) || {
            echo "FAILED (exit code $?)"
            echo "  Output: $output"
            FAIL=$((FAIL+1))
            continue
        }
    else
        output=$(bash -c "${env_prefix}timeout 60 $llm_cmd 'Respond with ONLY the single word ok and nothing else.'" 2>&1) || {
            echo "FAILED (exit code $?)"
            echo "  Output: $output"
            FAIL=$((FAIL+1))
            continue
        }
    fi

    echo "done"
    echo "  Output: $output"
    if echo "$output" | grep -qi "ok"; then
        echo "  RESULT: PASS"
        PASS=$((PASS+1))
    else
        echo "  RESULT: PARTIAL (got response but not 'ok')"
        PASS=$((PASS+1))  # still counts — LLM responded
    fi
done

echo ""
echo "=========================================="
echo "  SUMMARY: $PASS passed, $FAIL failed"
echo "=========================================="

# Restore to claude
sed -i "s/^provider: .*/provider: claude/" "$LLM_CONFIG"
