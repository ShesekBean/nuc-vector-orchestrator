#!/usr/bin/env python3
"""
Save ChatGPT auth token for the chatgpt-query script.

Usage:
  python3 scripts/chatgpt-save-token.py <access_token>

How to get the token:
  1. Open https://chatgpt.com in Chrome and log in
  2. Open DevTools (F12) → Network tab
  3. Visit https://chatgpt.com/api/auth/session
  4. Copy the "accessToken" value from the JSON response
  5. Run this script with that token
"""

import json
import os
import sys
from datetime import datetime

AUTH_FILE = os.path.expanduser("~/.openclaw/secrets/chatgpt-auth.json")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/chatgpt-save-token.py <access_token>")
        print()
        print("To get your token:")
        print("  1. Log into https://chatgpt.com in Chrome")
        print("  2. Visit https://chatgpt.com/api/auth/session")
        print("  3. Copy the 'accessToken' value")
        sys.exit(1)

    token = sys.argv[1].strip()

    # Strip "Bearer " prefix if they copied the whole header
    if token.lower().startswith("bearer "):
        token = token[7:]

    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)

    data = {
        "access_token": token,
        "saved_at": datetime.now().isoformat(),
    }

    with open(AUTH_FILE, "w") as f:
        json.dump(data, f, indent=2)

    os.chmod(AUTH_FILE, 0o600)
    print(f"Token saved to {AUTH_FILE} (permissions: 600)")
    print("You can now use: python3 scripts/chatgpt-query.py 'your question'")


if __name__ == "__main__":
    main()
