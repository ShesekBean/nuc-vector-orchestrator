#!/bin/bash
# Refresh Withings + Strava tokens and update fitness-log.json
# Runs every 2h via cron — keeps tokens fresh so the agent never hits 401
#
# Add to crontab: 0 */2 * * * ~/openclaw-refresh-tokens.sh
#
# Credentials are read from openclaw.json env block (not hardcoded here).
# Set these environment variables or replace the placeholders below:
#   WITHINGS_CLIENT_ID, WITHINGS_CLIENT_SECRET
#   STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET

LOG="/tmp/openclaw-token-refresh.log"

echo "[$(date)] Starting token refresh" >> "$LOG"

python3 << PYEOF >> "$LOG" 2>&1
import json, urllib.request, urllib.parse, time, os

fitness_path = os.path.expanduser("~/.openclaw/workspace/memory/fitness-log.json")

# Read credentials from openclaw.json env block
config_path = os.path.expanduser("~/.openclaw/openclaw.json")
import re
with open(config_path) as f:
    raw = f.read()
# Strip JS-style comments for JSON parsing
raw = re.sub(r'//.*', '', raw)
raw = re.sub(r',\s*}', '}', raw)
raw = re.sub(r',\s*]', ']', raw)
cfg = json.loads(raw)
env = cfg.get("env", {})

WITHINGS_CLIENT_ID     = env.get("WITHINGS_CLIENT_ID", "")
WITHINGS_CLIENT_SECRET = env.get("WITHINGS_CLIENT_SECRET", "")
STRAVA_CLIENT_ID       = env.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET   = env.get("STRAVA_CLIENT_SECRET", "")

with open(fitness_path) as f:
    log = json.load(f)

now = int(time.time())

# ── Withings ─────────────────────────────────────────────────────────────────
w_cache = log.get("withings_token_cache", {})
if w_cache.get("expires_at", 0) - now < 3600:  # refresh if < 1h left
    data = urllib.parse.urlencode({
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": WITHINGS_CLIENT_ID,
        "client_secret": WITHINGS_CLIENT_SECRET,
        "refresh_token": w_cache.get("refresh_token", ""),
    }).encode()
    req = urllib.request.Request("https://wbsapi.withings.net/v2/oauth2",
                                  data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if resp["status"] == 0:
        b = resp["body"]
        log["withings_token_cache"]["access_token"] = b["access_token"]
        log["withings_token_cache"]["refresh_token"] = b["refresh_token"]
        log["withings_token_cache"]["expires_at"] = now + b["expires_in"]
        print(f"Withings refreshed, expires in {b['expires_in']}s")
    else:
        print(f"Withings refresh failed: {resp}")
else:
    print(f"Withings token still valid ({w_cache['expires_at']-now}s left)")

# ── Strava ────────────────────────────────────────────────────────────────────
s_cache = log.get("strava_token_cache", {})
if s_cache.get("expires_at", 0) - now < 3600:
    data = urllib.parse.urlencode({
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": s_cache.get("refresh_token", ""),
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://www.strava.com/oauth/token",
                                  data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    if "access_token" in resp:
        log["strava_token_cache"]["access_token"] = resp["access_token"]
        log["strava_token_cache"]["refresh_token"] = resp["refresh_token"]
        log["strava_token_cache"]["expires_at"] = resp["expires_at"]
        print(f"Strava refreshed, expires at {resp['expires_at']}")
    else:
        print(f"Strava refresh failed: {resp}")
else:
    print(f"Strava token still valid ({s_cache['expires_at']-now}s left)")

with open(fitness_path, "w") as f:
    json.dump(log, f, indent=2)
print("fitness-log.json saved")
PYEOF
