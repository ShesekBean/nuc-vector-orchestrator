"""Configuration and LLM provider parsing."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    provider: str = "claude"
    binary: str = "claude"
    skip_permissions: str = "--dangerously-skip-permissions"
    prompt_flag: str = "-p"
    model_flag: str = "--model"
    env_unset: str = "CLAUDECODE"
    models: dict[str, str] = field(default_factory=lambda: {
        "heavy": "",
        "medium": "sonnet",
        "light": "haiku",
    })


@dataclass
class Config:
    repo_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("REPO_DIR", str(Path.home() / "Documents/claude/nuc-vector-orchestrator"))
    ))
    poll_interval: int = field(default_factory=lambda: int(os.environ.get("POLL_INTERVAL", "60")))
    issue_timeout: int = field(default_factory=lambda: int(os.environ.get("ISSUE_TIMEOUT", "1800")))
    max_cycles: int = field(default_factory=lambda: int(os.environ.get("MAX_CYCLES", "0")))
    dispatch_enabled: bool = field(default_factory=lambda: os.environ.get("DISPATCH_ENABLED", "1") == "1")
    inbox_poll: int = 10

    nuc_repo: str = "ShesekBean/nuc-vector-orchestrator"
    dispatch_label: str = "assigned:worker"
    max_workers: int = field(default_factory=lambda: int(os.environ.get("MAX_WORKERS", "4")))
    max_vector_workers: int = field(default_factory=lambda: int(os.environ.get("MAX_VECTOR_WORKERS", "2")))

    alert_group_id: str = "BUrA+nRRpsfdYgftby/jpJ7Ugy5PBzYWg89oNNr4nF4="
    bot_container: str = "openclaw-gateway"
    ophir_number: str = "+14084758230"

    board_project_id: str = "PVT_kwHOBckgic4BQy5M"
    board_status_field_id: str = "PVTSSF_lAHOBckgic4BQy5Mzg-z-B4"
    board_in_progress_option: str = "3fc103da"
    board_done_option: str = "f2fc3ac0"
    board_needs_input_option: str = "87ab6021"
    board_inbox_option: str = "c0ffb956"

    llm: LLMConfig = field(default_factory=LLMConfig)

    @property
    def vector_code_dir(self) -> Path:
        return self.repo_dir / "apps" / "vector"

    @property
    def state_dir(self) -> Path:
        return self.repo_dir / ".claude/state"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "agent-loop.log"

    @property
    def inbox_file(self) -> Path:
        return self.state_dir / "signal-inbox.jsonl"

    @property
    def physical_test_state(self) -> Path:
        return self.state_dir / "physical-test-pending.json"

    @property
    def token_usage_file(self) -> Path:
        return self.state_dir / "token-usage.tsv"


def _get_val(key: str, text: str) -> str:
    m = re.search(rf"^\s+{key}:\s*\"?([^\"\n]*)\"?", text, re.MULTILINE)
    return m.group(1).strip().strip('"') if m else ""


def parse_llm_config(config_path: Path) -> LLMConfig:
    """Parse llm-provider.yaml and return LLMConfig."""
    if not config_path.exists():
        return LLMConfig()

    content = config_path.read_text()
    provider_match = re.search(r"^provider:\s*(\S+)", content, re.MULTILINE)
    if not provider_match:
        return LLMConfig()

    provider = provider_match.group(1)
    block_match = re.search(
        rf"^{re.escape(provider)}:\s*\n((?:[ ]{{2}}.+\n)*)", content, re.MULTILINE
    )
    block = block_match.group(1) if block_match else ""

    models_match = re.search(r"  models:\s*\n((?:    .+\n)*)", block)
    models_block = models_match.group(1) if models_match else ""

    return LLMConfig(
        provider=provider,
        binary=_get_val("binary", block) or "claude",
        skip_permissions=_get_val("skip_permissions", block) or "--dangerously-skip-permissions",
        prompt_flag=_get_val("prompt_flag", block),
        model_flag=_get_val("model_flag", block) or "--model",
        env_unset=_get_val("env_unset", block),
        models={
            "heavy": _get_val("heavy", models_block),
            "medium": _get_val("medium", models_block) or "sonnet",
            "light": _get_val("light", models_block) or "haiku",
        },
    )


def load_config() -> Config:
    """Load full configuration."""
    cfg = Config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    llm_config_path = cfg.repo_dir / "config/llm-provider.yaml"
    cfg.llm = parse_llm_config(llm_config_path)
    return cfg
