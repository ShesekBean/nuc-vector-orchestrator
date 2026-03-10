"""Claude account rotation — swap credentials when quota is hit."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

log = logging.getLogger("agent-loop")

CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


class AccountRotator:
    """Manages rotation between multiple Claude accounts on quota exhaustion.

    Accounts stored as account-{N}.json in state/accounts/.
    Tracks consecutive quota hits per account.  If all accounts hit quota
    twice each, stops trying and waits for cooldown.
    """

    def __init__(self, accounts_dir: Path):
        self.accounts_dir = accounts_dir
        self.accounts: list[Path] = sorted(accounts_dir.glob("account-*.json"))
        self.current_index = self._detect_current()
        # Track consecutive quota hits per account index
        self.quota_hits: dict[int, int] = {}
        # Timestamp of last full-rotation failure (all accounts exhausted)
        self.all_exhausted_at = 0
        self.cooldown = 300  # 5 min cooldown when all accounts exhausted

        log.info("Account rotator: %d accounts loaded, current=%d",
                 len(self.accounts), self.current_index)

    def _detect_current(self) -> int:
        """Detect which account is currently active by comparing tokens."""
        if not CREDENTIALS_FILE.exists() or not self.accounts:
            return 0
        try:
            current_token = json.loads(CREDENTIALS_FILE.read_text()
                                       ).get("claudeAiOauth", {}).get("accessToken", "")
            for i, acct_file in enumerate(self.accounts):
                acct_token = json.loads(acct_file.read_text()
                                        ).get("claudeAiOauth", {}).get("accessToken", "")
                if acct_token == current_token:
                    return i
        except (json.JSONDecodeError, OSError):
            pass
        return 0

    def on_quota_hit(self) -> bool:
        """Called when current account hits quota.

        Returns True if successfully rotated to another account.
        Returns False if all accounts are exhausted (caller should stop).
        """
        if len(self.accounts) < 2:
            log.warning("Account rotation: only 1 account, cannot rotate")
            return False

        # Track this hit
        self.quota_hits[self.current_index] = self.quota_hits.get(self.current_index, 0) + 1
        hits = self.quota_hits[self.current_index]
        log.info("Account %d: quota hit #%d", self.current_index, hits)

        # Check if all accounts have been hit twice
        if self._all_exhausted():
            now = int(time.time())
            self.all_exhausted_at = now
            log.warning("All %d accounts exhausted (2+ hits each) — stopping",
                        len(self.accounts))
            return False

        # Rotate to next account
        return self._rotate_next()

    def _all_exhausted(self) -> bool:
        """Check if every account has 2+ quota hits."""
        for i in range(len(self.accounts)):
            if self.quota_hits.get(i, 0) < 2:
                return False
        return True

    def is_cooling_down(self) -> bool:
        """Check if we're in cooldown after all accounts exhausted."""
        if self.all_exhausted_at == 0:
            return False
        elapsed = int(time.time()) - self.all_exhausted_at
        if elapsed >= self.cooldown:
            # Cooldown over — reset all hit counts
            log.info("Cooldown over — resetting quota hit counts")
            self.quota_hits.clear()
            self.all_exhausted_at = 0
            return False
        return True

    def _rotate_next(self) -> bool:
        """Switch to the next account with fewest quota hits."""
        # Find account with fewest hits (prefer accounts with 0 hits)
        candidates = []
        for i in range(len(self.accounts)):
            if i == self.current_index:
                continue
            candidates.append((self.quota_hits.get(i, 0), i))
        candidates.sort()  # lowest hits first

        if not candidates:
            return False

        next_index = candidates[0][1]
        return self._switch_to(next_index)

    def _switch_to(self, index: int) -> bool:
        """Swap credentials file to the given account."""
        acct_file = self.accounts[index]
        if not acct_file.exists():
            log.error("Account file missing: %s", acct_file)
            return False

        try:
            # Save current credentials back to its account file (token may have been refreshed)
            if CREDENTIALS_FILE.exists():
                shutil.copy2(CREDENTIALS_FILE, self.accounts[self.current_index])

            # Atomic swap: write to temp file in same dir, then os.rename()
            creds_dir = CREDENTIALS_FILE.parent
            fd, tmp_path = tempfile.mkstemp(dir=creds_dir, prefix=".creds-swap-")
            os.close(fd)
            shutil.copy2(acct_file, tmp_path)
            os.rename(tmp_path, str(CREDENTIALS_FILE))

            old_index = self.current_index
            self.current_index = index
            log.info("Account rotation: switched %d → %d", old_index, index)
            return True
        except OSError as e:
            log.error("Account rotation failed: %s", e)
            return False

    def on_success(self) -> None:
        """Called when a worker completes successfully — reset hits for current account."""
        if self.current_index in self.quota_hits:
            del self.quota_hits[self.current_index]

    @property
    def status(self) -> str:
        """Human-readable status string."""
        parts = []
        for i in range(len(self.accounts)):
            hits = self.quota_hits.get(i, 0)
            marker = " ◄" if i == self.current_index else ""
            parts.append(f"account-{i}: {hits} hits{marker}")
        return ", ".join(parts)
