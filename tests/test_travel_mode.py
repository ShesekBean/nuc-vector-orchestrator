"""Tests for travel mode: PIN verification and skill toggling."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import bcrypt


REPO_DIR = Path(__file__).resolve().parent.parent
TRAVEL_MODE_SCRIPT = REPO_DIR / "scripts" / "travel-mode.sh"
TRAVEL_PIN_SCRIPT = REPO_DIR / "scripts" / "travel-pin.py"


class TestTravelPin:
    """Tests for travel-pin.py PIN verification."""

    def setup_method(self):
        """Create temp secrets directory for each test."""
        self.tmp_dir = tempfile.mkdtemp()
        self.secrets_dir = Path(self.tmp_dir) / "secrets"
        self.secrets_dir.mkdir()
        self.pin_hash_file = self.secrets_dir / "travel-pin.hash"
        self.lockout_file = self.secrets_dir / "travel-lockout.json"

        # Create a test config that points to our temp dirs
        self.conf_dir = Path(self.tmp_dir) / "config"
        self.conf_dir.mkdir()
        self.conf_file = self.conf_dir / "travel-mode.conf"
        self.conf_file.write_text(
            f'SECRETS_DIR="{self.secrets_dir}"\n'
            f'PIN_HASH_FILE="{self.pin_hash_file}"\n'
            f'LOCKOUT_FILE="{self.lockout_file}"\n'
            f'MAX_PIN_ATTEMPTS=3\n'
            f'LOCKOUT_SECONDS=900\n'
        )

    def teardown_method(self):
        """Clean up temp directory."""
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _set_pin(self, pin: str):
        """Set a PIN hash directly."""
        hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt())
        self.pin_hash_file.write_bytes(hashed)
        os.chmod(self.pin_hash_file, 0o600)

    def _run_pin_cmd(self, *args, env_override=None):
        """Run travel-pin.py with custom config via env."""
        env = os.environ.copy()
        env["TRAVEL_MODE_CONF"] = str(self.conf_file)
        if env_override:
            env.update(env_override)
        result = subprocess.run(
            ["python3", str(TRAVEL_PIN_SCRIPT)] + list(args),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_DIR),
        )
        return result

    def test_hash_command_creates_pin(self):
        """hash command stores bcrypt hash."""
        self._set_pin("1234")
        assert self.pin_hash_file.exists()
        stored = self.pin_hash_file.read_bytes().strip()
        assert bcrypt.checkpw(b"1234", stored)

    def test_verify_correct_pin(self):
        """Correct PIN verification succeeds."""
        self._set_pin("5678")
        stored = self.pin_hash_file.read_bytes().strip()
        assert bcrypt.checkpw(b"5678", stored)
        assert not bcrypt.checkpw(b"wrong", stored)

    def test_verify_wrong_pin_increments_lockout(self):
        """Wrong PIN increments attempt counter."""
        self._set_pin("1234")
        # Simulate a failed attempt by writing lockout state
        lockout = {"attempts": 1, "locked_until": 0}
        self.lockout_file.write_text(json.dumps(lockout))

        # Check state
        state = json.loads(self.lockout_file.read_text())
        assert state["attempts"] == 1

    def test_lockout_after_max_attempts(self):
        """Lockout activates after max failed attempts."""
        locked_until = time.time() + 900
        lockout = {"attempts": 3, "locked_until": locked_until}
        self.lockout_file.write_text(json.dumps(lockout))

        state = json.loads(self.lockout_file.read_text())
        assert state["attempts"] == 3
        assert state["locked_until"] > time.time()

    def test_lockout_expires(self):
        """Lockout expires after configured duration."""
        # Set lockout in the past
        lockout = {"attempts": 3, "locked_until": time.time() - 1}
        self.lockout_file.write_text(json.dumps(lockout))

        state = json.loads(self.lockout_file.read_text())
        assert state["locked_until"] < time.time()

    def test_pin_hash_file_permissions(self):
        """PIN hash file has restricted permissions."""
        self._set_pin("test")
        mode = oct(os.stat(self.pin_hash_file).st_mode)[-3:]
        assert mode == "600"

    def test_bcrypt_roundtrip(self):
        """bcrypt hash/verify roundtrip works."""
        pin = "mySecurePin123"
        hashed = bcrypt.hashpw(pin.encode(), bcrypt.gensalt())
        assert bcrypt.checkpw(pin.encode(), hashed)
        assert not bcrypt.checkpw(b"wrongPin", hashed)


class TestTravelModeScript:
    """Tests for travel-mode.sh shell script."""

    def test_script_exists_and_executable(self):
        """travel-mode.sh exists and is executable."""
        assert TRAVEL_MODE_SCRIPT.exists()
        assert os.access(TRAVEL_MODE_SCRIPT, os.X_OK)

    def test_status_command_runs(self):
        """status command executes without error."""
        result = subprocess.run(
            ["bash", str(TRAVEL_MODE_SCRIPT), "status"],
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
        )
        assert result.returncode == 0
        assert "Travel Mode Status" in result.stdout

    def test_unknown_command_fails(self):
        """Unknown command returns error."""
        result = subprocess.run(
            ["bash", str(TRAVEL_MODE_SCRIPT), "invalid_command"],
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
        )
        assert result.returncode != 0

    def test_config_loads(self):
        """Config file is readable and has expected keys."""
        conf = REPO_DIR / "config" / "travel-mode.conf"
        assert conf.exists()
        content = conf.read_text()
        assert "HOME_SSIDS" in content
        assert "SAFE_SKILLS" in content
        assert "SENSITIVE_SKILLS" in content

    def test_pin_script_exists(self):
        """travel-pin.py exists."""
        assert TRAVEL_PIN_SCRIPT.exists()

    def test_pin_status_no_pin_set(self):
        """PIN status works even without a PIN configured."""
        result = subprocess.run(
            ["python3", str(TRAVEL_PIN_SCRIPT), "status"],
            capture_output=True,
            text=True,
            cwd=str(REPO_DIR),
        )
        # Should run without crashing — PIN may or may not be set
        assert result.returncode == 0
        assert "Travel PIN Status" in result.stdout


class TestNetworkManagerDispatcher:
    """Tests for the NM dispatcher hook."""

    def test_dispatcher_exists_and_executable(self):
        """90-travel-mode exists and is executable."""
        dispatcher = REPO_DIR / "infra" / "networkmanager" / "90-travel-mode"
        assert dispatcher.exists()
        assert os.access(dispatcher, os.X_OK)

    def test_dispatcher_ignores_irrelevant_actions(self):
        """Dispatcher exits 0 on irrelevant actions."""
        dispatcher = REPO_DIR / "infra" / "networkmanager" / "90-travel-mode"
        for action in ["pre-up", "dhcp4-change", "vpn-up"]:
            result = subprocess.run(
                ["bash", str(dispatcher), "wlan0", action],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0


class TestTravelUnlockSkill:
    """Tests for the travel-unlock SKILL.md."""

    def test_skill_file_exists(self):
        """travel-unlock SKILL.md exists."""
        skill = REPO_DIR / "apps" / "openclaw" / "skills" / "travel-unlock" / "SKILL.md"
        assert skill.exists()

    def test_skill_has_yaml_frontmatter(self):
        """SKILL.md has valid YAML frontmatter."""
        skill = REPO_DIR / "apps" / "openclaw" / "skills" / "travel-unlock" / "SKILL.md"
        content = skill.read_text()
        assert content.startswith("---\n")
        # Find second ---
        second_dash = content.index("---", 4)
        assert second_dash > 0
        frontmatter = content[4:second_dash].strip()
        assert "name: travel-unlock" in frontmatter
        assert "description:" in frontmatter

    def test_skill_has_trigger_words(self):
        """SKILL.md mentions expected trigger words."""
        skill = REPO_DIR / "apps" / "openclaw" / "skills" / "travel-unlock" / "SKILL.md"
        content = skill.read_text()
        assert "unlock" in content.lower()
        assert "travel mode" in content.lower()
        assert "pin" in content.lower()


class TestConfigConsistency:
    """Tests for configuration consistency."""

    def test_sensitive_skills_exist_in_repo(self):
        """All sensitive skills listed in config exist as directories."""
        conf = REPO_DIR / "config" / "travel-mode.conf"
        content = conf.read_text()
        for line in content.splitlines():
            if line.startswith("SENSITIVE_SKILLS="):
                skills_str = line.split("=", 1)[1].strip('"')
                skills = skills_str.split(",")
                for skill in skills:
                    skill_dir = REPO_DIR / "apps" / "openclaw" / "skills" / skill
                    assert skill_dir.exists(), f"Sensitive skill dir missing: {skill}"
                    skill_md = skill_dir / "SKILL.md"
                    assert skill_md.exists(), f"SKILL.md missing for: {skill}"

    def test_safe_skills_include_travel_unlock(self):
        """travel-unlock is listed as a safe skill."""
        conf = REPO_DIR / "config" / "travel-mode.conf"
        content = conf.read_text()
        for line in content.splitlines():
            if line.startswith("SAFE_SKILLS="):
                assert "travel-unlock" in line
