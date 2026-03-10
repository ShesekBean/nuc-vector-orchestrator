#!/usr/bin/env bash
# log-token-usage.sh — Parse Claude CLI JSON output and append to TSV.
#
# Usage (standalone):
#   bash scripts/log-token-usage.sh <role> <issue_key> <stderr_file>
#
# Usage (sourced):
#   source scripts/log-token-usage.sh
#   log_token_usage <role> <issue_key> <stderr_file>
#
# Env:
#   TOKEN_USAGE_FILE — path to TSV output file

log_token_usage() {
    local role="${1:-unknown}"
    local issue_key="${2:-}"
    local stderr_file="${3:-}"
    local tsv_file="${TOKEN_USAGE_FILE:-}"

    [[ -z "$tsv_file" ]] && return 0
    [[ -z "$stderr_file" || ! -f "$stderr_file" ]] && return 0

    python3 - "$role" "$issue_key" "$tsv_file" "$stderr_file" << 'PYEOF'
import sys, json, time, os

role, issue, tsv_path, stderr_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

# Find JSON line with usage data
data = None
with open(stderr_path) as f:
    for line in f:
        line = line.strip()
        if not line or not line.startswith('{'):
            continue
        try:
            d = json.loads(line)
            if d.get('usage') or d.get('total_cost_usd'):
                data = d
        except (json.JSONDecodeError, TypeError):
            continue

if not data:
    sys.exit(0)

usage = data.get('usage', {})
cost = data.get('total_cost_usd', 0)
inp = usage.get('input_tokens', 0)
out = usage.get('output_tokens', 0)
cache_r = usage.get('cache_read_input_tokens', 0)
cache_c = usage.get('cache_creation_input_tokens', 0)

model = data.get('model', '')
if not model and data.get('modelUsage'):
    model = next(iter(data['modelUsage']), 'unknown')

if inp == 0 and out == 0 and cost == 0:
    sys.exit(0)

write_header = not os.path.exists(tsv_path) or os.path.getsize(tsv_path) == 0

with open(tsv_path, 'a') as f:
    if write_header:
        f.write('timestamp\tagent_role\tissue_ref\tmodel\tinput_tokens\toutput_tokens\tcache_read\tcache_create\tcost_usd\n')
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    f.write(f'{ts}\t{role}\t{issue}\t{model}\t{inp}\t{out}\t{cache_r}\t{cache_c}\t{cost}\n')
PYEOF
}

# If called directly (not sourced), run with args
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    log_token_usage "$@"
fi
