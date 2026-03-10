"""State file management — TSV, JSONL, fingerprints."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("agent-loop")


def read_tsv(path: Path) -> dict[str, tuple[int, int]]:
    """Read TSV state file. Returns {key: (timestamp, count)}."""
    result: dict[str, tuple[int, int]] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            key = parts[0]
            try:
                ts = int(parts[1])
                count = int(parts[2]) if len(parts) >= 3 else 1
                result[key] = (ts, count)
            except ValueError:
                continue
    return result


def write_tsv(path: Path, data: dict[str, tuple[int, int]]) -> None:
    """Write TSV state file from {key: (timestamp, count)}."""
    lines = [f"{key}\t{ts}\t{count}" for key, (ts, count) in data.items()]
    path.write_text("\n".join(lines) + "\n" if lines else "")


def delete_tsv_entries(path: Path, pattern: str) -> None:
    """Delete TSV entries matching a pattern."""
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    filtered = [line for line in lines if not re.search(pattern, line)]
    path.write_text("\n".join(filtered) + "\n" if filtered else "")


def read_file_lines(path: Path) -> list[str]:
    """Read non-empty lines from a file."""
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def append_line(path: Path, line: str) -> None:
    """Append a line to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(line + "\n")


def read_json_file(path: Path) -> dict:
    """Read a JSON file, return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_json_file(path: Path, data: dict) -> None:
    """Write a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def mark_inbox_replied(inbox_file: Path, timestamps: set[int]) -> None:
    """Mark inbox messages with matching timestamps as replied."""
    if not inbox_file.exists() or not timestamps:
        return
    lines = []
    for line in inbox_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
            if m.get("ts") in timestamps:
                m["replied"] = True
            lines.append(json.dumps(m))
        except json.JSONDecodeError:
            continue
    inbox_file.write_text("\n".join(lines) + "\n" if lines else "")


def get_unreplied_messages(
    inbox_file: Path, ophir_number: str, group: str = "build-orchestrator"
) -> list[dict]:
    """Get unreplied messages from Ophir in the build-orchestrator group."""
    if not inbox_file.exists():
        return []
    messages = []
    for line in inbox_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("replied"):
            continue
        if m.get("from") != ophir_number:
            continue
        if m.get("group") != group:
            continue
        messages.append(m)
    return messages


def get_conversation_history(inbox_file: Path, limit: int = 20) -> str:
    """Get recent conversation history formatted for LLM context."""
    if not inbox_file.exists():
        return ""
    from datetime import datetime

    messages = []
    for line in inbox_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("group") != "build-orchestrator":
            continue
        messages.append(m)

    lines = []
    for m in messages[-limit:]:
        ts = datetime.fromtimestamp(m["ts"] / 1000).strftime("%H:%M")
        who = "Shon" if m.get("from") == "bot" else "Ophir"
        lines.append(f"[{ts}] {who}: {m['msg']}")
    return "\n".join(lines)
