#!/usr/bin/env python3
"""
Monarch Money login helper for OpenClaw.

Authenticates with Monarch Money and saves the session token to the
OpenClaw workspace memory file. Supports MFA/2FA.

Usage:
    python3 scripts/monarch-login.py

The token is saved to ~/.openclaw/workspace/memory/monarch-log.json
and typically lasts several months before needing renewal.
"""

import getpass
import json
import sys
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

LOGIN_URL = "https://api.monarchmoney.com/auth/login/"
GRAPHQL_URL = "https://api.monarchmoney.com/graphql"

# Where OpenClaw reads the token from
MONARCH_LOG_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "monarch-log.json"

# Also save to repo backup
REPO_BACKUP_PATH = None  # Set if you want a repo copy


def api_post(url: str, body: dict, token: str | None = None) -> dict:
    """POST JSON to Monarch Money API."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Client-Platform": "web",
        "User-Agent": "MonarchMoneyOpenClaw/1.0",
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
    # Load existing data if present
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


def main() -> None:
    print("=== Monarch Money Login for OpenClaw ===\n")
    print("This will save a session token that Shon uses to query your finances.")
    print("The token typically lasts several months.\n")

    email = input("Monarch Money email: ").strip()
    if not email:
        print("Email required.")
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Password required.")
        sys.exit(1)

    # First attempt without MFA
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

    # Verify
    print("\nVerifying token...")
    if not verify_token(token):
        print("Token verification failed. The token may not be valid.")
        sys.exit(1)

    print("Token verified successfully!")

    # Save
    save_token(token)

    print("\nDone! Shon can now query your Monarch Money data.")
    print("Ask Shon: 'monarch balances' or 'how much did I spend this month'")


if __name__ == "__main__":
    main()
