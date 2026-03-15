#!/usr/bin/env python3
"""travel-pin.py — PIN verification for travel mode with bcrypt + lockout.

Usage:
    python3 scripts/travel-pin.py set               — set a new PIN (interactive)
    python3 scripts/travel-pin.py verify <pin>       — verify PIN, unlock on success
    python3 scripts/travel-pin.py status             — show lockout state
    python3 scripts/travel-pin.py hash <pin>         — generate hash (non-interactive set)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import bcrypt

# ── Config ──
REPO_DIR = Path(__file__).resolve().parent.parent
CONF_FILE = REPO_DIR / "config" / "travel-mode.conf"

# Parse config
config = {}
if CONF_FILE.exists():
    for line in CONF_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            # Strip quotes and variable expansions
            value = value.strip().strip('"').strip("'")
            value = value.replace("${HOME}", str(Path.home()))
            config[key.strip()] = value

SECRETS_DIR = Path(config.get("SECRETS_DIR", Path.home() / ".openclaw" / "secrets"))
PIN_HASH_FILE = Path(config.get("PIN_HASH_FILE", SECRETS_DIR / "travel-pin.hash"))
LOCKOUT_FILE = Path(config.get("LOCKOUT_FILE", SECRETS_DIR / "travel-lockout.json"))
MAX_ATTEMPTS = int(config.get("MAX_PIN_ATTEMPTS", "3"))
LOCKOUT_SECONDS = int(config.get("LOCKOUT_SECONDS", "900"))


def ensure_secrets_dir():
    """Create secrets directory with restricted permissions."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)


def load_lockout() -> dict:
    """Load lockout state."""
    if LOCKOUT_FILE.exists():
        try:
            return json.loads(LOCKOUT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"attempts": 0, "locked_until": 0}
    return {"attempts": 0, "locked_until": 0}


def save_lockout(state: dict):
    """Save lockout state."""
    ensure_secrets_dir()
    LOCKOUT_FILE.write_text(json.dumps(state))
    os.chmod(LOCKOUT_FILE, 0o600)


def is_locked_out() -> tuple[bool, int]:
    """Check if PIN is locked out. Returns (locked, seconds_remaining)."""
    state = load_lockout()
    locked_until = state.get("locked_until", 0)
    if locked_until > time.time():
        remaining = int(locked_until - time.time())
        return True, remaining
    return False, 0


def cmd_set():
    """Set a new PIN interactively."""
    import getpass

    pin = getpass.getpass("Enter new travel PIN: ")
    if not pin:
        print("ERROR: PIN cannot be empty")
        sys.exit(1)
    confirm = getpass.getpass("Confirm PIN: ")
    if pin != confirm:
        print("ERROR: PINs do not match")
        sys.exit(1)

    ensure_secrets_dir()
    hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt())
    PIN_HASH_FILE.write_bytes(hashed)
    os.chmod(PIN_HASH_FILE, 0o600)
    # Reset lockout on new PIN
    save_lockout({"attempts": 0, "locked_until": 0})
    print("PIN set successfully.")


def cmd_hash(pin: str):
    """Generate and store a PIN hash non-interactively."""
    if not pin:
        print("ERROR: PIN cannot be empty")
        sys.exit(1)
    ensure_secrets_dir()
    hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt())
    PIN_HASH_FILE.write_bytes(hashed)
    os.chmod(PIN_HASH_FILE, 0o600)
    save_lockout({"attempts": 0, "locked_until": 0})
    print("PIN hash stored.")


def cmd_verify(pin: str):
    """Verify PIN. On success, call travel-mode.sh unlock."""
    if not PIN_HASH_FILE.exists():
        print("ERROR: No PIN configured. Run: python3 scripts/travel-pin.py set")
        sys.exit(1)

    # Check lockout
    locked, remaining = is_locked_out()
    if locked:
        minutes = remaining // 60
        print(f"LOCKED: Too many failed attempts. Try again in {minutes} minute(s).")
        sys.exit(2)

    # Verify
    stored_hash = PIN_HASH_FILE.read_bytes().strip()
    if bcrypt.checkpw(pin.encode(), stored_hash):
        # Success — reset lockout and unlock skills
        save_lockout({"attempts": 0, "locked_until": 0})
        print("PIN verified. Unlocking sensitive skills...")
        travel_mode_script = REPO_DIR / "scripts" / "travel-mode.sh"
        result = subprocess.run(
            ["bash", str(travel_mode_script), "unlock"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("Skills unlocked successfully.")
            print(result.stdout)
        else:
            print(f"ERROR unlocking skills: {result.stderr}")
            sys.exit(1)
    else:
        # Failure — increment attempts
        state = load_lockout()
        attempts = state.get("attempts", 0) + 1
        if attempts >= MAX_ATTEMPTS:
            locked_until = time.time() + LOCKOUT_SECONDS
            save_lockout({"attempts": attempts, "locked_until": locked_until})
            print(
                f"WRONG PIN. Account locked for {LOCKOUT_SECONDS // 60} minutes "
                f"({attempts}/{MAX_ATTEMPTS} attempts)."
            )
            sys.exit(2)
        else:
            save_lockout({"attempts": attempts, "locked_until": 0})
            remaining_attempts = MAX_ATTEMPTS - attempts
            print(
                f"WRONG PIN. {remaining_attempts} attempt(s) remaining "
                f"before {LOCKOUT_SECONDS // 60}-minute lockout."
            )
            sys.exit(1)


def cmd_status():
    """Show PIN and lockout status."""
    print("=== Travel PIN Status ===")
    print(f"PIN configured: {'yes' if PIN_HASH_FILE.exists() else 'no'}")

    locked, remaining = is_locked_out()
    if locked:
        print(f"Lockout:        LOCKED ({remaining}s remaining)")
    else:
        state = load_lockout()
        attempts = state.get("attempts", 0)
        print(f"Lockout:        clear ({attempts}/{MAX_ATTEMPTS} failed attempts)")

    print(f"Max attempts:   {MAX_ATTEMPTS}")
    print(f"Lockout time:   {LOCKOUT_SECONDS // 60} minutes")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    if command == "set":
        cmd_set()
    elif command == "hash":
        if len(sys.argv) < 3:
            print("Usage: python3 scripts/travel-pin.py hash <pin>")
            sys.exit(1)
        cmd_hash(sys.argv[2])
    elif command == "verify":
        if len(sys.argv) < 3:
            print("Usage: python3 scripts/travel-pin.py verify <pin>")
            sys.exit(1)
        cmd_verify(sys.argv[2])
    elif command == "status":
        cmd_status()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
