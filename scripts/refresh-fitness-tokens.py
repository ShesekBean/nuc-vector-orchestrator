#!/usr/bin/env python3
"""Refresh Strava and Withings OAuth tokens in OpenClaw fitness-log.json.

Run via systemd timer every 2 hours to prevent token expiry.
Executes inside the openclaw-gateway container via `docker exec`.

Tokens are stored in two places:
  1. ~/.openclaw/.env (refresh tokens — seed values, updated on rotation)
  2. ~/.openclaw/workspace/memory/fitness-log.json (cached access tokens — used by Vector)

Withings access tokens expire every ~3 hours. Strava every ~6 hours.
"""

import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG_PATH = "/home/node/.openclaw/workspace/memory/fitness-log.json"
ENV_PATH = "/home/node/.openclaw/.env"

# Pre-refresh window: refresh if token expires within 30 minutes
REFRESH_WINDOW = 1800


def load_env():
    """Load .env file into a dict."""
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def update_env_value(key, new_value):
    """Update a single key in the .env file."""
    with open(ENV_PATH) as f:
        content = f.read()
    content = re.sub(rf"{key}=.*", f"{key}={new_value}", content)
    with open(ENV_PATH, "w") as f:
        f.write(content)


def api_post(url, data):
    """POST form-encoded data, return parsed JSON."""
    encoded = urlencode(data).encode()
    req = Request(url, data=encoded, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def refresh_strava(log_data, env):
    """Refresh Strava access token if needed."""
    cache = log_data.get("strava_token_cache", {})
    expires_at = cache.get("expires_at", 0)

    if expires_at > time.time() + REFRESH_WINDOW:
        remaining = (expires_at - time.time()) / 3600
        print(f"Strava: still valid ({remaining:.1f}h remaining)")
        return False

    # Use cached refresh token first, fall back to .env
    refresh_token = cache.get("refresh_token") or env.get("STRAVA_REFRESH_TOKEN", "")
    result = api_post("https://www.strava.com/oauth/token", {
        "client_id": env["STRAVA_CLIENT_ID"],
        "client_secret": env["STRAVA_CLIENT_SECRET"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })

    at = result["access_token"]
    rt = result["refresh_token"]
    exp = result["expires_at"]

    log_data["strava_token_cache"] = {
        "access_token": at,
        "refresh_token": rt,
        "expires_at": exp,
    }

    # Update .env if refresh token rotated
    if rt != refresh_token:
        update_env_value("STRAVA_REFRESH_TOKEN", rt)
    update_env_value("STRAVA_ACCESS_TOKEN", at)

    print(f"Strava: refreshed, expires {time.ctime(exp)}")
    return True


def refresh_withings(log_data, env):
    """Refresh Withings access token if needed."""
    cache = log_data.get("withings_token_cache", {})
    expires_at = cache.get("expires_at", 0)

    if expires_at > time.time() + REFRESH_WINDOW:
        remaining = (expires_at - time.time()) / 3600
        print(f"Withings: still valid ({remaining:.1f}h remaining)")
        return False

    # Use cached refresh token first, fall back to .env
    refresh_token = cache.get("refresh_token") or env.get("WITHINGS_REFRESH_TOKEN", "")
    result = api_post("https://wbsapi.withings.net/v2/oauth2", {
        "action": "requesttoken",
        "client_id": env["WITHINGS_CLIENT_ID"],
        "client_secret": env["WITHINGS_CLIENT_SECRET"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })

    if result.get("status") != 0:
        raise RuntimeError(f"Withings API error: status={result.get('status')}")

    body = result["body"]
    at = body["access_token"]
    rt = body["refresh_token"]
    exp = int(time.time()) + body["expires_in"]

    log_data["withings_token_cache"] = {
        "access_token": at,
        "refresh_token": rt,
        "expires_at": exp,
    }

    # Update .env with rotated tokens
    update_env_value("WITHINGS_ACCESS_TOKEN", at)
    update_env_value("WITHINGS_REFRESH_TOKEN", rt)

    print(f"Withings: refreshed, expires {time.ctime(exp)}")
    return True


def main():
    env = load_env()

    with open(LOG_PATH) as f:
        log_data = json.load(f)

    errors = []
    updated = False

    for name, refresher in [("Strava", refresh_strava), ("Withings", refresh_withings)]:
        try:
            if refresher(log_data, env):
                updated = True
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"{name}: ERROR - {e}")

    if updated:
        with open(LOG_PATH, "w") as f:
            json.dump(log_data, f, indent=2)
        print("Cache updated.")

    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
