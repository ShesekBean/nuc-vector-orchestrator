#!/bin/bash
# claude-wrapper.sh — Wrapper for claude CLI that tracks token usage
#
# Install: cp scripts/claude-wrapper.sh ~/bin/claude && chmod +x ~/bin/claude
# Uninstall: rm ~/bin/claude (restores original behavior instantly)
#
# Passes ALL arguments to the real claude binary at ~/.local/bin/claude.
# Captures stderr to parse usage JSON on exit, appends to token-usage.tsv.
#
# CRITICAL: Fail-open — if anything goes wrong with tracking, real claude still works.
# CRITICAL: stdout/stderr behavior is preserved for the user.

set -uo pipefail

REAL_CLAUDE="${REAL_CLAUDE:-$HOME/.local/bin/claude}"

# Verify real binary exists
if [[ ! -x "$REAL_CLAUDE" ]]; then
    echo "claude-wrapper: real binary not found at $REAL_CLAUDE, trying PATH fallback" >&2
    # Remove ourselves from PATH and try again
    SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
    PATH="${PATH//$SELF_DIR:/}"
    PATH="${PATH//:$SELF_DIR/}"
    exec claude "$@"
fi

# Determine TSV file location
TOKEN_USAGE_FILE="${TOKEN_USAGE_FILE:-$HOME/Documents/claude/nuc-vector-orchestrator/.claude/state/token-usage.tsv}"

# Create a temp file for stderr capture
stderr_capture=$(mktemp /tmp/claude-stderr-XXXXXX.txt 2>/dev/null) || stderr_capture=""

if [[ -z "$stderr_capture" ]]; then
    # Can't create temp file — fail open, just run normally
    exec "$REAL_CLAUDE" "$@"
fi

# Run the real claude, teeing stderr to both the terminal and our capture file.
# This preserves normal stderr behavior while capturing for parsing.
# shellcheck disable=SC2064
trap "rm -f '$stderr_capture'" EXIT

"$REAL_CLAUDE" "$@" 2> >(tee "$stderr_capture" >&2)
exit_code=$?

# Wait for tee process substitution to finish writing
wait 2>/dev/null || true

# Parse usage from captured stderr — fail silently on any error
if [[ -s "$stderr_capture" ]]; then
    python3 - "$stderr_capture" "$TOKEN_USAGE_FILE" << 'PARSE_PY' 2>/dev/null || true
import json, sys, os
from datetime import datetime, timezone

stderr_file = sys.argv[1]
tsv_file = sys.argv[2]

data = None
with open(stderr_file, 'r', errors='replace') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and ('total_cost_usd' in parsed or 'usage' in parsed):
                data = parsed
        except (json.JSONDecodeError, ValueError):
            continue

if data is None:
    sys.exit(0)

usage = data.get('usage', {})
cost = data.get('total_cost_usd', data.get('cost_usd', 0))
model = data.get('model', '')
input_tokens = usage.get('input_tokens', 0)
output_tokens = usage.get('output_tokens', 0)
cache_read = usage.get('cache_read_input_tokens', 0)
cache_create = usage.get('cache_creation_input_tokens', 0)

if input_tokens == 0 and output_tokens == 0 and cost == 0:
    sys.exit(0)

timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

os.makedirs(os.path.dirname(tsv_file), exist_ok=True)

write_header = not os.path.exists(tsv_file) or os.path.getsize(tsv_file) == 0
with open(tsv_file, 'a') as f:
    if write_header:
        f.write('timestamp\tagent_role\tissue_ref\tmodel\tinput_tokens\toutput_tokens\tcache_read\tcache_create\tcost_usd\n')
    f.write(f'{timestamp}\tinteractive\t\t{model}\t{input_tokens}\t{output_tokens}\t{cache_read}\t{cache_create}\t{cost}\n')
PARSE_PY
fi

exit "$exit_code"
