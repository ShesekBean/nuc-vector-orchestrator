#!/usr/bin/env python3
"""
Monarch Money login helper for OpenClaw.

Saves a session token that Shon uses to query your Monarch Money data.
The token typically lasts several months before needing renewal.

Usage:
    # Option 1 (recommended): Paste token from your browser
    python3 scripts/monarch-login.py --token

    # Option 2: Log in with email/password (supports MFA)
    python3 scripts/monarch-login.py --login

How to get your token from the browser:
    1. Log into https://app.monarchmoney.com in your browser
    2. Open DevTools (F12 or Cmd+Opt+I)
    3. Go to Application tab → Storage → Local Storage → https://app.monarchmoney.com
    4. Find the key containing your auth token (look for a long alphanumeric string)
    -- OR --
    3. Go to Network tab, refresh the page
    4. Click any request to api.monarchmoney.com
    5. In the Headers tab, find "Authorization: Token <your_token>"
    6. Copy just the token value (after "Token ")

The token is saved to ~/.openclaw/workspace/memory/monarch-log.json
"""

import argparse
import getpass
import json
import sys
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

LOGIN_URL = "https://api.monarch.com/auth/login/"
GRAPHQL_URL = "https://api.monarch.com/graphql"

# Where OpenClaw reads the token from
MONARCH_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "monarch-log.json"


def api_post(url: str, body: dict, token: str | None = None) -> dict:
    """POST JSON to Monarch Money API."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Client-Platform": "web",
        "Origin": "https://app.monarch.com",
        "Monarch-Client": "monarch-core-web-app-graphql",
        "Monarch-Client-Version": "v1.0.1600",
        "User-Agent": "Mozilla/5.0 (compatible; MonarchMoneyOpenClaw/1.0)",
    }
    if token:
        headers["Authorization"] = f"Token {token}"

    req = Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    with urlopen(req) as resp:
        return json.loads(resp.read())


def login(email: str, password: str, totp: str | None = None) -> str:
    """Log in and return the session token."""
    body = {
        "username": email,
        "password": password,
        "supports_mfa": True,
        "trusted_device": False,
    }
    if totp:
        body["totp"] = totp

    try:
        result = api_post(LOGIN_URL, body)
    except HTTPError as e:
        if e.code == 403:
            raise RuntimeError("MFA_REQUIRED") from e
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Login failed ({e.code}): {error_body}") from e

    token = result.get("token")
    if not token:
        raise RuntimeError(f"No token in response: {result}")
    return token


def verify_token(token: str) -> bool:
    """Verify the token works by fetching subscription details."""
    query = {
        "operationName": "GetSubscriptionDetails",
        "query": "query GetSubscriptionDetails { subscription { id paymentSource referralCode } }",
        "variables": {},
    }
    try:
        result = api_post(GRAPHQL_URL, query, token=token)
        return "data" in result
    except HTTPError:
        return False


def save_token(token: str) -> None:
    """Save token to the OpenClaw workspace memory."""
    data = {}
    if MONARCH_LOG_PATH.exists():
        try:
            data = json.loads(MONARCH_LOG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    data["session_token"] = token
    data["session_created_at"] = date.today().isoformat()

    MONARCH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    MONARCH_LOG_PATH.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Token saved to {MONARCH_LOG_PATH}")


def mode_token() -> None:
    """Paste a token from the browser."""
    print("=== Monarch Money — Browser Token ===\n")
    print("How to get your token:")
    print("  1. Log into https://app.monarchmoney.com")
    print("  2. Open DevTools (F12)")
    print("  3. Go to Network tab, refresh the page")
    print("  4. Click any request to api.monarchmoney.com")
    print("  5. Find the 'Authorization' header")
    print("  6. Copy the value after 'Token '\n")

    raw = input("Paste your token: ").strip()
    if not raw:
        print("No token provided.")
        sys.exit(1)

    # Strip "Token " prefix if they copied the whole header value
    token = raw.removeprefix("Token ").strip()

    if not token:
        print("No token provided.")
        sys.exit(1)

    print("\nVerifying token...")
    if not verify_token(token):
        print("Token verification failed — it may be expired or invalid.")
        print("Make sure you copied the full token string.")
        sys.exit(1)

    print("Token verified!")
    save_token(token)
    print("\nDone! Shon can now query your Monarch Money data.")
    print("Try: 'monarch balances' or 'how much did I spend this month'")


def mode_login() -> None:
    """Log in with email/password."""
    print("=== Monarch Money — Email/Password Login ===\n")
    print("Note: If you use 'Continue with Google', you need to set a")
    print("password first in Monarch's account settings.\n")

    email = input("Email: ").strip()
    if not email:
        print("Email required.")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Password required.")
        sys.exit(1)

    token = None
    try:
        token = login(email, password)
    except RuntimeError as e:
        if "MFA_REQUIRED" in str(e):
            print("\nMFA/2FA required.")
            totp = input("Enter your 2FA code: ").strip()
            if not totp:
                print("2FA code required.")
                sys.exit(1)
            try:
                token = login(email, password, totp=totp)
            except RuntimeError as e2:
                print(f"\nLogin failed: {e2}")
                sys.exit(1)
        else:
            print(f"\nLogin failed: {e}")
            sys.exit(1)

    print("\nVerifying token...")
    if not verify_token(token):
        print("Token verification failed.")
        sys.exit(1)

    print("Token verified!")
    save_token(token)
    print("\nDone! Shon can now query your Monarch Money data.")
    print("Try: 'monarch balances' or 'how much did I spend this month'")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Save a Monarch Money session token for OpenClaw (Shon).",
        epilog="The token is saved to ~/.openclaw/workspace/memory/monarch-log.json",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--token", action="store_true",
        help="Paste a token from your browser (recommended, no password needed)",
    )
    group.add_argument(
        "--login", action="store_true",
        help="Log in with email and password (supports MFA)",
    )

    args = parser.parse_args()

    if args.token:
        mode_token()
    else:
        mode_login()


if __name__ == "__main__":
    main()
