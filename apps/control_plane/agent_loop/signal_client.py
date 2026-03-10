"""Signal messaging client."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from .config import Config

log = logging.getLogger("agent-loop")


def send_signal(cfg: Config, message: str) -> bool:
    """Send a Signal message to the alert group via OpenClaw gateway."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "send",
        "params": {"groupId": cfg.alert_group_id, "message": message},
        "id": 1,
    })

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="sig-", delete=False
    ) as f:
        f.write(payload)
        tmp_path = f.name

    try:
        cp_result = subprocess.run(
            ["sg", "docker", "-c",
             f"docker cp '{tmp_path}' '{cfg.bot_container}:/tmp/sig-alert.json'"],
            capture_output=True, text=True, timeout=10,
        )
        if cp_result.returncode != 0:
            log.warning("Signal notification failed (docker cp)")
            return False

        exec_result = subprocess.run(
            ["sg", "docker", "-c",
             f"docker exec '{cfg.bot_container}' curl -sf -X POST "
             "http://127.0.0.1:8080/api/v1/rpc "
             "-H 'Content-Type: application/json' "
             "-d @/tmp/sig-alert.json"],
            capture_output=True, text=True, timeout=10,
        )
        if exec_result.returncode != 0:
            log.warning("Signal notification failed (curl)")
            return False

        log.info("Signal notification sent")
        return True

    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Signal notification failed: %s", e)
        return False

    finally:
        Path(tmp_path).unlink(missing_ok=True)


def send_signal_gated(cfg: Config, event_type: str, issue_id: int | str, message: str) -> bool:
    """Send Signal message through the rate-limiting gate script."""
    gate_script = cfg.repo_dir / "scripts/pgm-signal-gate.sh"
    if not gate_script.exists():
        log.warning("pgm-signal-gate.sh not found, sending directly")
        return send_signal(cfg, message)

    try:
        result = subprocess.run(
            ["bash", str(gate_script), event_type, str(issue_id), message],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "REPO_DIR": str(cfg.repo_dir)},
        )
        if result.stdout:
            log.info("Signal gate: %s", result.stdout.strip())
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Signal gate failed: %s", e)
        return False
