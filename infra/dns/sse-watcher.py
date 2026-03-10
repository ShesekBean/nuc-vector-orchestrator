#!/usr/bin/env python3
"""
sse-watcher.py — reads signal-cli SSE stream from stdin, processes ALLOW approvals.

Usage (called from openclaw-monitor.sh):
  docker exec <container> curl -sN http://127.0.0.1:8080/api/v1/events \
    | python3 sse-watcher.py <alert_number> <pending_dir> <dnsmasq_conf> \
                             <bot_container> <dns_container>
"""
import sys, json, os, re, subprocess, tempfile, datetime, time

alert_number  = sys.argv[1]
pending_dir   = sys.argv[2]
dnsmasq_conf  = sys.argv[3]
bot_container = sys.argv[4]
dns_container = sys.argv[5]
ALERT_GROUP_ID = "n5nNybjzxi33xFvofQPzvyvOMXBoBicOwet7UtLborQ="

KEYWORD       = "#ALLOW#"
PROCESSED_LOG = "/tmp/openclaw-processed-approvals"  # persists across restarts

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] APPROVALS: {msg}", flush=True)

def send_signal(msg):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "send",
        "params": {"groupId": ALERT_GROUP_ID, "message": msg},
        "id": 1
    })
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(payload)
        fname = f.name
    subprocess.run(["docker", "cp", fname, f"{bot_container}:/tmp/sig-approve.json"],
                   capture_output=True)
    subprocess.run(["docker", "exec", bot_container,
                    "curl", "-sf", "-X", "POST", "http://127.0.0.1:8080/api/v1/rpc",
                    "-H", "Content-Type: application/json",
                    "-d", "@/tmp/sig-approve.json"],
                   capture_output=True)
    os.unlink(fname)

def allow_domain(domain):
    with open(dnsmasq_conf) as f:
        content = f.read()
    new_entry = f"server=/{domain}/8.8.8.8\n"
    if new_entry in content:
        log(f"{domain} already in allowlist, skipping")
        return
    content = content.replace("address=/#/", new_entry + "address=/#/")
    with open(dnsmasq_conf, "w") as f:
        f.write(content)
    log(f"{domain} written to dnsmasq.conf")
    # Send confirmation BEFORE restart (restart kills watch_outbound → systemd restarts monitor)
    send_signal(f"✓ {domain} added to the DNS allowlist. Restarting DNS filter now...")
    subprocess.run(["docker", "restart", dns_container], capture_output=True)

# Load already-processed message timestamps so replays are never acted on twice.
processed = set()
try:
    with open(PROCESSED_LOG) as f:
        processed = set(line.strip() for line in f if line.strip())
except FileNotFoundError:
    pass

log(f"SSE stream connected, waiting for approvals... ({len(processed)} processed timestamps loaded)")

for raw in sys.stdin:
    line = raw.rstrip("\n")
    if not line.startswith("data:"):
        continue
    data_str = line[5:].strip()
    if not data_str:
        continue
    try:
        obj      = json.loads(data_str)
        envelope = obj.get("envelope", {})
        source   = envelope.get("sourceNumber") or envelope.get("source", "")
        dm       = envelope.get("dataMessage") or {}
        text     = (dm.get("message") or "").strip()
    except Exception:
        continue

    if source != alert_number or not text:
        continue

    # Deduplicate by Signal message timestamp — prevents the same approval
    # being acted on twice when signal-cli replays messages on SSE reconnect.
    msg_ts = str(envelope.get("timestamp", ""))
    if msg_ts in processed:
        log(f"already processed ts={msg_ts}, ignoring replay")
        continue

    # Match:  #ALLOW# domain.com
    m = re.match(r"^#ALLOW#\s+([a-z0-9.-]+\.[a-z]{2,})\s*$", text, re.IGNORECASE)
    if not m:
        continue

    domain = m.group(1).lower()

    # If already in the allowlist this is a replayed SSE message after a restart — skip silently.
    with open(dnsmasq_conf) as f:
        if f"server=/{domain}/8.8.8.8" in f.read():
            log(f"{domain} already in allowlist, ignoring replayed approval")
            # Clean up stale pending file if it somehow still exists
            try:
                os.unlink(os.path.join(pending_dir, domain))
            except FileNotFoundError:
                pass
            continue

    pending_file = os.path.join(pending_dir, domain)
    if not os.path.exists(pending_file):
        log(f"no pending approval for {domain}, ignoring")
        continue

    log(f"{domain} approved by {source}")
    os.unlink(pending_file)
    # Record timestamp before acting so replays after restart are ignored
    processed.add(msg_ts)
    with open(PROCESSED_LOG, "a") as f:
        f.write(msg_ts + "\n")
    allow_domain(domain)
