"""LLM command builder and runner."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config

log = logging.getLogger("agent-loop")


def build_llm_cmd(cfg: Config, tier: str) -> list[str]:
    """Build LLM command args for a given tier (heavy/medium/light)."""
    model = cfg.llm.models.get(tier, cfg.llm.models.get("medium", ""))
    parts = cfg.llm.binary.split()  # handles "codex exec"
    parts.append(cfg.llm.skip_permissions)
    if model:
        parts.extend([cfg.llm.model_flag, model])
    if cfg.llm.prompt_flag:
        parts.append(cfg.llm.prompt_flag)
    return parts


def run_llm(
    cfg: Config,
    tier: str,
    prompt: str,
    *,
    cwd: Path | None = None,
    timeout: int = 1800,
    agent_role: str = "unknown",
    issue_key: str = "",
) -> tuple[str, int]:
    """Run LLM with a prompt, return (stdout, exit_code).

    Writes prompt to a temp file to avoid ARG_MAX issues.
    Uses --output-format json to capture token usage data.
    """
    cmd_parts = build_llm_cmd(cfg, tier)
    # Add JSON output for token tracking
    cmd_parts.extend(["--output-format", "json"])

    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", prefix="claude-prompt-", suffix=".txt", delete=False
    )
    prompt_file.write(prompt)
    prompt_file.close()
    prompt_path = prompt_file.name

    env_unset = cfg.llm.env_unset
    env_prefix = f"unset {env_unset}; " if env_unset else ""

    try:
        if cfg.llm.prompt_flag:
            # Claude-style: pipe stdin
            shell_cmd = f"{env_prefix}cat '{prompt_path}' | {' '.join(cmd_parts)}"
        else:
            # Codex-style: prompt as positional argument
            shell_cmd = f"{env_prefix}{' '.join(cmd_parts)} \"$(cat '{prompt_path}')\""

        result = subprocess.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )

        # Append stderr to log file
        if result.stderr:
            with open(cfg.log_file, "a") as f:
                f.write(result.stderr)

        # Parse JSON output for token usage + text result
        text_result = result.stdout
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                text_result = data.get("result", "")
                _log_token_usage(cfg, agent_role, issue_key, tier, data)
            except (json.JSONDecodeError, TypeError):
                pass  # Not JSON — use raw stdout

        # Include stderr in output so quota detection catches CLI errors
        combined = text_result
        if result.returncode != 0 and result.stderr:
            combined = f"{text_result}\n{result.stderr}"
        return combined, result.returncode

    except subprocess.TimeoutExpired:
        log.warning("LLM timed out after %ds for %s", timeout, issue_key or agent_role)
        return "", 124  # same as bash timeout exit code

    finally:
        Path(prompt_path).unlink(missing_ok=True)


def _log_token_usage(cfg: Config, agent_role: str, issue_key: str,
                     tier: str, data: dict) -> None:
    """Log token usage from JSON output to TSV file."""
    usage = data.get("usage", {})
    cost = data.get("total_cost_usd", 0)
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    duration_ms = data.get("duration_ms", 0)

    if input_tokens == 0 and output_tokens == 0:
        return

    ts = int(time.time())
    line = (f"{ts}\t{agent_role}\t{issue_key}\t{tier}\t"
            f"{input_tokens}\t{output_tokens}\t{cache_read}\t{cache_create}\t"
            f"{cost:.6f}\t{duration_ms}\n")

    try:
        with open(cfg.token_usage_file, "a") as f:
            f.write(line)
    except OSError:
        pass


_QUOTA_ALERT_LAST = 0
_QUOTA_ALERT_INTERVAL = 5400  # 1.5 hours between alerts


def check_quota_exhausted(output: str, cfg: Config | None = None) -> bool:
    """Check if LLM output indicates quota/rate-limit exhaustion.

    If cfg is provided, sends a rate-limited Signal alert (1.5h cooldown).
    """
    import re
    import time

    patterns = [
        r"rate.limit", r"over.capacity", r"quota.*exceeded",
        r"429", r"too many requests", r"billing", r"credit",
        r"usage.limit", r"token.limit", r"rate_limit_exceeded",
        r"insufficient_quota", r"capacity", r"Too Many Requests",
        r"hit your usage limit", r"Try again at",
    ]
    combined = "|".join(patterns)
    is_quota = bool(re.search(combined, output, re.IGNORECASE))

    if is_quota and cfg:
        global _QUOTA_ALERT_LAST
        now = int(time.time())
        if now - _QUOTA_ALERT_LAST > _QUOTA_ALERT_INTERVAL:
            _QUOTA_ALERT_LAST = now
            provider = cfg.llm.provider or "unknown"
            snippet = " ".join(output.split("\n")[:3])
            alert = (f"📊 PGM: ⚠️ LLM QUOTA EXHAUSTED ({provider}). "
                     f"Workers paused until quota resets. Output: {snippet}")
            log.warning("QUOTA EXHAUSTED: %s", alert)
            # Send via signal gate
            gate = cfg.repo_dir / "scripts/pgm-signal-gate.sh"
            if gate.exists():
                try:
                    subprocess.run(
                        ["bash", str(gate), "general", "0", alert],
                        capture_output=True, text=True, timeout=30,
                        cwd=str(cfg.repo_dir),
                    )
                except (subprocess.TimeoutExpired, OSError):
                    pass
        else:
            log.info("Quota still exhausted (%s) — alert already sent within 1.5h",
                     cfg.llm.provider or "unknown")

    return is_quota
