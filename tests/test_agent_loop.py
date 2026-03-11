"""Tests for the Python agent loop (apps/control_plane/agent_loop/)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


# Ensure the repo root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Config tests ───────────────────────────────────────────────────────────────

class TestConfig:
    def test_parse_llm_config_claude(self, tmp_path):
        from apps.control_plane.agent_loop.config import parse_llm_config
        config = tmp_path / "llm-provider.yaml"
        config.write_text("""provider: claude

claude:
  binary: claude
  skip_permissions: "--dangerously-skip-permissions"
  prompt_flag: "-p"
  model_flag: "--model"
  env_unset: "CLAUDECODE"
  models:
    heavy: ""
    medium: sonnet
    light: haiku
""")
        llm = parse_llm_config(config)
        assert llm.binary == "claude"
        assert llm.prompt_flag == "-p"
        assert llm.models["medium"] == "sonnet"
        assert llm.models["light"] == "haiku"
        assert llm.models["heavy"] == ""
        assert llm.env_unset == "CLAUDECODE"

    def test_parse_llm_config_openai(self, tmp_path):
        from apps.control_plane.agent_loop.config import parse_llm_config
        config = tmp_path / "llm-provider.yaml"
        config.write_text("""provider: openai

openai:
  binary: "codex exec"
  skip_permissions: "--dangerously-bypass-approvals-and-sandbox"
  prompt_flag: ""
  model_flag: "--model"
  env_unset: ""
  models:
    heavy: ""
    medium: "gpt-5-codex-mini"
    light: "gpt-5-codex-mini"
""")
        llm = parse_llm_config(config)
        assert llm.binary == "codex exec"
        assert llm.prompt_flag == ""
        assert llm.models["medium"] == "gpt-5-codex-mini"

    def test_parse_llm_config_missing_file(self, tmp_path):
        from apps.control_plane.agent_loop.config import parse_llm_config
        llm = parse_llm_config(tmp_path / "nonexistent.yaml")
        assert llm.binary == "claude"
        assert llm.models["medium"] == "sonnet"

    def test_config_defaults(self):
        from apps.control_plane.agent_loop.config import Config
        cfg = Config()
        assert cfg.nuc_repo == "ShesekBean/nuc-vector-orchestrator"
        assert cfg.dispatch_label == "assigned:worker"
        assert cfg.poll_interval == 60
        assert cfg.issue_timeout == 1800
        assert cfg.max_workers == 4
        assert cfg.vector_code_dir == cfg.repo_dir / "apps" / "vector"

    def test_config_env_override(self, monkeypatch):
        monkeypatch.setenv("POLL_INTERVAL", "120")
        monkeypatch.setenv("ISSUE_TIMEOUT", "3600")
        monkeypatch.setenv("MAX_CYCLES", "5")
        monkeypatch.setenv("DISPATCH_ENABLED", "0")
        from apps.control_plane.agent_loop.config import Config
        cfg = Config()
        assert cfg.poll_interval == 120
        assert cfg.issue_timeout == 3600
        assert cfg.max_cycles == 5
        assert cfg.dispatch_enabled is False


# ── State tests ────────────────────────────────────────────────────────────────

class TestState:
    def test_read_write_tsv(self, tmp_path):
        from apps.control_plane.agent_loop.state import read_tsv, write_tsv
        path = tmp_path / "test.tsv"
        data = {"stuck-42": (1700000000, 2), "closed-7": (1700001000, 1)}
        write_tsv(path, data)
        result = read_tsv(path)
        assert result == data

    def test_read_tsv_empty(self, tmp_path):
        from apps.control_plane.agent_loop.state import read_tsv
        assert read_tsv(tmp_path / "nonexistent.tsv") == {}

    def test_delete_tsv_entries(self, tmp_path):
        from apps.control_plane.agent_loop.state import write_tsv, delete_tsv_entries, read_tsv
        path = tmp_path / "test.tsv"
        write_tsv(path, {"stuck-42": (100, 1), "stuck-99": (200, 1), "closed-42": (300, 1)})
        delete_tsv_entries(path, r"-42\t")
        result = read_tsv(path)
        assert "stuck-42" not in result
        assert "closed-42" not in result
        assert "stuck-99" in result

    def test_mark_inbox_replied(self, tmp_path):
        from apps.control_plane.agent_loop.state import mark_inbox_replied
        inbox = tmp_path / "inbox.jsonl"
        inbox.write_text(
            json.dumps({"ts": 1000, "msg": "hello", "from": "+1234"}) + "\n" +
            json.dumps({"ts": 2000, "msg": "world", "from": "+1234"}) + "\n"
        )
        mark_inbox_replied(inbox, {1000})
        lines = [json.loads(ln) for ln in inbox.read_text().splitlines() if ln.strip()]
        assert lines[0]["replied"] is True
        assert "replied" not in lines[1] or lines[1].get("replied") is not True

    def test_get_unreplied_messages(self, tmp_path):
        from apps.control_plane.agent_loop.state import get_unreplied_messages
        inbox = tmp_path / "inbox.jsonl"
        inbox.write_text(
            json.dumps({"ts": 1000, "msg": "hi", "from": "+14084758230", "group": "build-orchestrator"}) + "\n" +
            json.dumps({"ts": 2000, "msg": "ok", "from": "+14084758230", "group": "build-orchestrator", "replied": True}) + "\n" +
            json.dumps({"ts": 3000, "msg": "yo", "from": "+9999999999", "group": "build-orchestrator"}) + "\n" +
            json.dumps({"ts": 4000, "msg": "new", "from": "+14084758230", "group": "build-orchestrator"}) + "\n"
        )
        msgs = get_unreplied_messages(inbox, "+14084758230")
        assert len(msgs) == 2
        assert msgs[0]["msg"] == "hi"
        assert msgs[1]["msg"] == "new"

    def test_get_conversation_history(self, tmp_path):
        from apps.control_plane.agent_loop.state import get_conversation_history
        inbox = tmp_path / "inbox.jsonl"
        inbox.write_text(
            json.dumps({"ts": 1000000, "msg": "hello", "from": "+14084758230", "group": "build-orchestrator"}) + "\n" +
            json.dumps({"ts": 1001000, "msg": "hi there", "from": "bot", "group": "build-orchestrator"}) + "\n"
        )
        history = get_conversation_history(inbox)
        assert "Ophir: hello" in history
        assert "Vector: hi there" in history


# ── LLM tests ──────────────────────────────────────────────────────────────────

class TestLLM:
    def test_build_llm_cmd_claude(self):
        from apps.control_plane.agent_loop.config import Config, LLMConfig
        from apps.control_plane.agent_loop.llm import build_llm_cmd
        cfg = Config(llm=LLMConfig())
        cmd = build_llm_cmd(cfg, "medium")
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "sonnet" in cmd
        assert "-p" in cmd

    def test_build_llm_cmd_heavy_no_model(self):
        from apps.control_plane.agent_loop.config import Config, LLMConfig
        from apps.control_plane.agent_loop.llm import build_llm_cmd
        cfg = Config(llm=LLMConfig(models={"heavy": "", "medium": "sonnet", "light": "haiku"}))
        cmd = build_llm_cmd(cfg, "heavy")
        assert "--model" not in cmd

    def test_build_llm_cmd_codex(self):
        from apps.control_plane.agent_loop.config import Config, LLMConfig
        from apps.control_plane.agent_loop.llm import build_llm_cmd
        cfg = Config(llm=LLMConfig(
            binary="codex exec",
            skip_permissions="--dangerously-bypass-approvals-and-sandbox",
            prompt_flag="",
            model_flag="--model",
            models={"heavy": "", "medium": "gpt-5-codex-mini", "light": "gpt-5-codex-mini"},
        ))
        cmd = build_llm_cmd(cfg, "medium")
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "gpt-5-codex-mini" in cmd
        assert "-p" not in cmd

    def test_check_quota_exhausted(self):
        from apps.control_plane.agent_loop.llm import check_quota_exhausted
        assert check_quota_exhausted("Error: rate limit exceeded")
        assert check_quota_exhausted("HTTP 429 Too Many Requests")
        assert check_quota_exhausted("quota exceeded for this billing period")
        assert not check_quota_exhausted("Hello, I completed the task!")
        assert not check_quota_exhausted("")


# ── Inbox tests ────────────────────────────────────────────────────────────────

class TestInbox:
    def test_is_physical_test_go(self):
        from apps.control_plane.agent_loop.inbox import is_physical_test_go
        assert is_physical_test_go("#go")
        assert is_physical_test_go("go")
        assert is_physical_test_go("#Go")
        assert is_physical_test_go("#go 319")
        assert is_physical_test_go("#go #319")
        assert is_physical_test_go("Go 71")
        assert not is_physical_test_go("going home")
        assert not is_physical_test_go("let's go")
        assert not is_physical_test_go("#good")

    def test_parse_go_issue_number(self):
        from apps.control_plane.agent_loop.inbox import parse_go_issue_number
        assert parse_go_issue_number("#go 319") == "319"
        assert parse_go_issue_number("#go #71") == "71"
        assert parse_go_issue_number("go 42") == "42"
        assert parse_go_issue_number("#go") == ""

    def test_parse_physical_test_result(self):
        from apps.control_plane.agent_loop.inbox import parse_physical_test_result
        assert parse_physical_test_result("pass") == "pass"
        assert parse_physical_test_result("PASS") == "pass"
        assert parse_physical_test_result("passed looks good") == "pass"
        assert parse_physical_test_result("lgtm") == "pass"
        assert parse_physical_test_result("fail") == "fail"
        assert parse_physical_test_result("FAIL") == "fail"
        assert parse_physical_test_result("failed something wrong") == "fail"
        assert parse_physical_test_result("not working") == "fail"
        assert parse_physical_test_result("hello") == ""
        assert parse_physical_test_result("") == ""

    def test_parse_result_issue_number(self):
        from apps.control_plane.agent_loop.inbox import parse_result_issue_number
        assert parse_result_issue_number("pass 71") == "71"
        assert parse_result_issue_number("fail #319") == "319"
        assert parse_result_issue_number("passed 42") == "42"
        assert parse_result_issue_number("pass") == ""

    def test_parse_physical_test_fields(self):
        from apps.control_plane.agent_loop.inbox import _parse_physical_test_fields
        comments = """## Worker: Physical Test Request

**Setup command:** ssh vector 'cd /home/yahboom/claude && docker compose up -d muscle'
**What to observe:** Robot should track face with camera
**Pass criteria:** Camera follows face smoothly
**Fail criteria:** Camera doesn't move or jitters"""
        result = _parse_physical_test_fields(comments)
        assert "ssh vector" in result["setup_command"]
        assert "track face" in result["observe"]
        assert "smoothly" in result["pass_criteria"]
        assert "jitters" in result["fail_criteria"]

    def test_parse_physical_test_fields_header_codeblock(self):
        from apps.control_plane.agent_loop.inbox import _parse_physical_test_fields
        comments = """## Worker: Physical Test Request

### Setup command:
```bash
ssh vector 'docker compose up -d'
curl http://localhost:8081/health
```

### What to observe
Robot tracks the face

### Pass criteria
Face centered in frame

### Fail criteria
No tracking at all"""
        result = _parse_physical_test_fields(comments)
        assert "ssh vector" in result["setup_command"]
        assert "curl" in result["setup_command"]
        assert "tracks the face" in result["observe"]


# ── Dispatch tests ─────────────────────────────────────────────────────────────

class TestDispatch:
    def _make_repo_structure(self, tmp_path):
        """Create minimal repo structure for prompt tests."""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "issue-worker.md").write_text("# Issue Worker\nYou handle issues.")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "lessons-learned.jsonl").write_text(
            '{"issue":1,"lesson":"test"}\n'
        )
        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)

    def test_build_worker_prompt_has_required_sections(self, tmp_path):
        from apps.control_plane.agent_loop.config import Config
        from apps.control_plane.agent_loop.dispatch import build_worker_prompt

        self._make_repo_structure(tmp_path)
        cfg = Config(repo_dir=tmp_path)

        prompt = build_worker_prompt(cfg, "ShesekBean/nuc-vector-orchestrator", 42,
                                     "Fix the bug", "No comments")
        assert "YOUR ROLE DEFINITION" in prompt
        assert "Issue Worker" in prompt
        assert "RECENT LESSONS" in prompt
        assert "issue number 42" in prompt
        assert "Fix the bug" in prompt
        assert "COMMENT HEADER (MANDATORY)" in prompt
        # NUC issues should NOT have Vector SSH context
        assert "VECTOR WORKER CONTEXT" not in prompt

    @patch("apps.control_plane.agent_loop.dispatch._is_vector_issue", return_value=True)
    def test_build_worker_prompt_vector_has_grpc_context(self, mock_vector, tmp_path):
        from apps.control_plane.agent_loop.config import Config
        from apps.control_plane.agent_loop.dispatch import build_worker_prompt

        self._make_repo_structure(tmp_path)
        cfg = Config(repo_dir=tmp_path)

        prompt = build_worker_prompt(cfg, "ShesekBean/nuc-vector-orchestrator", 120,
                                     "Fix motor code", "No comments")
        assert "VECTOR WORKER CONTEXT" in prompt
        assert "gRPC" in prompt
        assert "apps/vector/" in prompt
        assert "issue number 120" in prompt

    @patch("apps.control_plane.agent_loop.dispatch.gh")
    def test_get_dispatchable_issues_single_repo(self, mock_gh, tmp_path):
        from apps.control_plane.agent_loop.config import Config
        from apps.control_plane.agent_loop.dispatch import get_dispatchable_issues

        mock_gh.issue_list.return_value = "329\tfalse\n330\ttrue\n120\tfalse"
        cfg = Config(repo_dir=tmp_path)
        issues = get_dispatchable_issues(cfg)
        assert ("ShesekBean/nuc-vector-orchestrator", 329, False) in issues
        assert ("ShesekBean/nuc-vector-orchestrator", 330, True) in issues
        assert ("ShesekBean/nuc-vector-orchestrator", 120, False) in issues
        # All issues come from the single repo
        assert all(r == "ShesekBean/nuc-vector-orchestrator" for r, _, _ in issues)

    @patch("apps.control_plane.agent_loop.dispatch.gh")
    def test_get_dispatchable_issues_empty(self, mock_gh, tmp_path):
        from apps.control_plane.agent_loop.config import Config
        from apps.control_plane.agent_loop.dispatch import get_dispatchable_issues

        mock_gh.issue_list.return_value = ""
        cfg = Config(repo_dir=tmp_path)
        assert get_dispatchable_issues(cfg) == []


# ── GitHub helper tests ────────────────────────────────────────────────────────

class TestGitHub:
    @patch("apps.control_plane.agent_loop.github.subprocess.run")
    def test_gh_success(self, mock_run):
        from apps.control_plane.agent_loop.github import gh
        mock_run.return_value = MagicMock(returncode=0, stdout="output\n")
        assert gh("issue", "list") == "output"

    @patch("apps.control_plane.agent_loop.github.subprocess.run")
    def test_gh_failure(self, mock_run):
        from apps.control_plane.agent_loop.github import gh
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert gh("issue", "list") == ""

    @patch("apps.control_plane.agent_loop.github.subprocess.run")
    def test_gh_json(self, mock_run):
        from apps.control_plane.agent_loop.github import gh_json
        mock_run.return_value = MagicMock(
            returncode=0, stdout='[{"number": 1}]'
        )
        result = gh_json("issue", "list", "--json", "number")
        assert result == [{"number": 1}]

    def test_find_pr_for_issue(self):
        from apps.control_plane.agent_loop.github import find_pr_for_issue
        with patch("apps.control_plane.agent_loop.github.pr_list") as mock_pr_list:
            mock_pr_list.return_value = [
                {"number": 10, "body": "Relates to #42"},
                {"number": 11, "body": "Relates to #99"},
            ]
            assert find_pr_for_issue("repo", 42) == 10
            assert find_pr_for_issue("repo", 99) == 11
            assert find_pr_for_issue("repo", 1) is None

    @patch("apps.control_plane.agent_loop.github.subprocess.run")
    def test_pr_checks_returns_stdout_on_failure_exit(self, mock_run):
        """gh pr checks exits 1 when checks fail — we still need stdout."""
        from apps.control_plane.agent_loop.github import pr_checks
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="lint\tfail\t2s\t...\ntest\tpass\t5s\t...\n",
            stderr="",
        )
        result = pr_checks("repo", 42)
        assert "fail" in result
        assert "lint" in result

    @patch("apps.control_plane.agent_loop.github.subprocess.run")
    def test_pr_checks_returns_stdout_on_success(self, mock_run):
        from apps.control_plane.agent_loop.github import pr_checks
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="lint\tpass\t2s\t...\ntest\tpass\t5s\t...\n",
            stderr="",
        )
        result = pr_checks("repo", 42)
        assert "pass" in result
        assert "fail" not in result


# ── Loop structure tests ──────────────────────────────────────────────────────

class TestLoop:
    def test_loop_respects_max_cycles(self, tmp_path):
        from apps.control_plane.agent_loop.config import Config
        from apps.control_plane.agent_loop.loop import AgentLoop

        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "agent-loop.log").touch()

        cfg = Config(
            repo_dir=tmp_path,
            max_cycles=1,
            poll_interval=0,
            dispatch_enabled=False,
        )

        loop = AgentLoop(cfg)
        with patch.object(loop, "_run_cycle"):
            loop.run()
        # Should exit after 1 cycle without hanging

    def test_agent_loop_modules_exist(self):
        """Verify all expected modules are importable."""
        import importlib
        for name in ["config", "llm", "signal_client", "state", "github",
                     "dispatch", "board", "pgm", "inbox", "loop", "log"]:
            mod = importlib.import_module(f"apps.control_plane.agent_loop.{name}")
            assert mod is not None
