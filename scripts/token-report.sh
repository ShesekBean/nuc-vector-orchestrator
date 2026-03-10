#!/usr/bin/env bash
# token-report.sh — Token consumption report from agent-loop usage data.
#
# Reads .claude/state/token-usage.tsv (written by llm.py after each call).
# TSV columns: timestamp, role, issue_key, tier, input, output, cache_read, cache_create, cost_usd[, duration_ms]
#
# Usage:
#   bash scripts/token-report.sh              # today's report
#   bash scripts/token-report.sh --all        # all-time report
#   bash scripts/token-report.sh --since 24h  # last 24 hours
#   bash scripts/token-report.sh 2026-03-05   # specific date
#
# Env overrides:
#   TOKEN_USAGE_FILE — override default NUC TSV location
# (Vector has no separate token tracking — all calls are from NUC)

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.."; pwd)}"
NUC_TSV="${TOKEN_USAGE_FILE:-$REPO_DIR/.claude/state/token-usage.tsv}"
# Need at least one file
if [[ ! -f "$NUC_TSV" ]]; then
    exit 0
fi

python3 -c "
import sys, re, os
from collections import defaultdict
from datetime import datetime, timezone

nuc_tsv = sys.argv[1]
filter_arg = sys.argv[2] if len(sys.argv) > 2 else ''
extra = sys.argv[3] if len(sys.argv) > 3 else ''

# Parse filter
since_epoch = 0
until_epoch = 2_000_000_000
period_label = 'today'

if filter_arg == '--all':
    since_epoch = 0
    period_label = 'all time'
elif filter_arg == '--since':
    import time
    val = extra or '24h'
    m = re.match(r'(\d+)([hd])', val)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        since_epoch = int(time.time() - n * (3600 if unit == 'h' else 86400))
    period_label = f'last {val}'
elif re.match(r'\d{4}-\d{2}-\d{2}', filter_arg):
    dt = datetime.strptime(filter_arg, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    since_epoch = int(dt.timestamp())
    until_epoch = since_epoch + 86400
    period_label = filter_arg
else:
    import time
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day)
    since_epoch = int(today_start.timestamp())

def parse_ts(val):
    try:
        return int(val)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
        return int(dt.timestamp())
    except ValueError:
        return 0

def read_tsv(path, machine):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('timestamp'):
                continue
            parts = line.split('\t')
            if len(parts) < 9:
                continue
            ts = parse_ts(parts[0])
            if ts < since_epoch or ts >= until_epoch:
                continue
            rows.append({
                'ts': ts, 'role': parts[1], 'issue': parts[2],
                'tier': parts[3], 'input': int(parts[4]), 'output': int(parts[5]),
                'cache_read': int(parts[6]) if len(parts) > 6 else 0,
                'cost': float(parts[8]) if len(parts) > 8 else 0.0,
                'duration': int(parts[9]) if len(parts) > 9 else 0,
                'machine': machine,
            })
    return rows

nuc_rows = read_tsv(nuc_tsv, 'nuc')
all_rows = nuc_rows

if not all_rows:
    sys.exit(0)

by_role = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cost': 0.0, 'duration': 0})
by_issue = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cost': 0.0, 'duration': 0})
by_machine = defaultdict(lambda: {'calls': 0, 'input': 0, 'output': 0, 'cost': 0.0})
total = {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cost': 0.0, 'duration': 0}

for r in all_rows:
    for d in [by_role[r['role']], total]:
        d['calls'] += 1; d['input'] += r['input']; d['output'] += r['output']
        d['cache_read'] += r['cache_read']; d['cost'] += r['cost']; d['duration'] += r['duration']
    if not r['issue'].startswith('agent:'):
        d2 = by_issue[r['issue']]
        d2['calls'] += 1; d2['input'] += r['input']; d2['output'] += r['output']
        d2['cache_read'] += r['cache_read']; d2['cost'] += r['cost']; d2['duration'] += r['duration']
    m = by_machine[r['machine']]
    m['calls'] += 1; m['input'] += r['input']; m['output'] += r['output']; m['cost'] += r['cost']

def fmt_tokens(n):
    if n >= 1_000_000: return f'{n/1_000_000:.1f}M'
    if n >= 1_000: return f'{n/1_000:.1f}K'
    return str(n)

def fmt_dur(ms):
    s = ms / 1000
    if s >= 3600: return f'{s/3600:.1f}h'
    if s >= 60: return f'{s/60:.0f}m'
    return f'{s:.0f}s'

print(f'Token Usage Report ({period_label})')
print(f'Total: {total[\"calls\"]} calls, {fmt_tokens(total[\"input\"])} in, {fmt_tokens(total[\"output\"])} out, {fmt_tokens(total[\"cache_read\"])} cached, \${total[\"cost\"]:.2f}, {fmt_dur(total[\"duration\"])}')
print()

# By role
print('By role:')
for role, d in sorted(by_role.items(), key=lambda x: -x[1]['cost']):
    print(f'  {role}: {d[\"calls\"]} calls, {fmt_tokens(d[\"input\"])} in, {fmt_tokens(d[\"output\"])} out, \${d[\"cost\"]:.2f}')
print()

# Top issues by cost (exclude agent: refs, top 5)
if by_issue:
    print('Top issues by cost:')
    for issue, d in sorted(by_issue.items(), key=lambda x: -x[1]['cost'])[:5]:
        print(f'  {issue}: \${d[\"cost\"]:.2f} ({d[\"calls\"]} calls, {fmt_tokens(d[\"input\"])} in, {fmt_tokens(d[\"output\"])} out)')
" "$NUC_TSV" "${@}"
