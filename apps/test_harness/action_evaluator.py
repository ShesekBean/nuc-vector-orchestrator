#!/usr/bin/env python3
"""Evaluate robot actions from before/after images using vision LLM.

Provider/model config is read from config/llm-provider.yaml (same config
used by agent-loop.sh for all LLM dispatch).  Shells out to the configured
CLI binary (claude / codex exec) — no Python SDK or API keys needed.
"""

from __future__ import annotations

import argparse
import base64
import functools
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

ACTION_CHECKS = {
    "face_tracking": [
        "Is a person's face visible in the AFTER frame?",
        "Is the face approximately centered in the AFTER frame?",
        "Did framing improve from BEFORE to AFTER?",
    ],
    "servo_movement": [
        "Did camera angle/orientation change between BEFORE and AFTER?",
        "Is the viewpoint change clear enough to indicate servo movement?",
        "If unchanged, explain what suggests no movement.",
    ],
    "person_following": [
        "Is the robot closer to the person in AFTER compared to BEFORE?",
        "Does relative scale/position indicate reduced distance?",
        "If unclear, state uncertainty and what is missing.",
    ],
}

# Backward-compatible legacy prompts used by older CLI/tests.
TEST_PROMPTS = {
    "person_following": "Robot should move closer to a person.",
    "led_check": "Robot LEDs should change as requested.",
    "obstacle_avoidance": "Robot should avoid obstacles safely.",
    "servo_tracking": "Camera servo should reorient toward target.",
}

SYSTEM_PROMPT = (
    "You are a robot vision evaluator. Compare BEFORE and AFTER images and score the action result. "
    "Return ONLY valid JSON with exact keys: "
    '"success" (boolean), "confidence" (float 0.0-1.0), "explanation" (string), "suggestion" (string). '
    "No markdown. No additional keys."
)

# LLM provider config — read from config/llm-provider.yaml (shared with agent-loop)
_LLM_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "llm-provider.yaml"

# Claude short-name → full model ID mapping (Anthropic SDK needs full IDs)
_CLAUDE_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6-20250725",
    "haiku": "claude-haiku-4-5-20251001",
    "opus": "claude-opus-4-6-20250725",
}

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL_OPENAI = "gpt-5-codex-mini"
DEFAULT_MODEL_CLAUDE = "claude-sonnet-4-6-20250725"
_VISION_CONFIG_PATH = Path(__file__).resolve().parent / "vision_config.yaml"
_DEFAULT_SUCCESS_METRICS = {
    "face_tracking": {
        "required": [
            "face_detected_after",
            "face_centered_after",
            "centering_improved",
        ],
        "pass_rules": {
            "center_tolerance_pct": 20.0,
            "min_centering_improvement_pct": 10.0,
            "min_confidence": 0.70,
        },
    }
}


def _parse_llm_provider_yaml() -> tuple[str, str]:
    """Parse config/llm-provider.yaml — same config used by agent-loop.sh.

    Returns (provider, model) where provider is 'openai' or 'claude',
    and model is the medium-tier model name.
    """
    import re

    if not _LLM_CONFIG_PATH.exists():
        return DEFAULT_PROVIDER, DEFAULT_MODEL_OPENAI

    try:
        content = _LLM_CONFIG_PATH.read_text()
    except OSError:
        return DEFAULT_PROVIDER, DEFAULT_MODEL_OPENAI

    # Extract top-level provider
    m = re.search(r"^provider:\s*(\S+)", content, re.MULTILINE)
    provider = m.group(1) if m else DEFAULT_PROVIDER

    # Extract provider block (indented lines after "provider_name:")
    block_match = re.search(
        rf"^{re.escape(provider)}:\s*\n((?:[ ]{{2}}.+\n)*)", content, re.MULTILINE
    )
    block = block_match.group(1) if block_match else ""

    # Extract models sub-block
    models_match = re.search(r"  models:\s*\n((?:    .+\n)*)", block)
    models_block = models_match.group(1) if models_match else ""

    # Get medium-tier model (used for vision evaluation)
    model_match = re.search(r"^\s+medium:\s*\"?([^\"\n]*)\"?", models_block, re.MULTILINE)
    model = model_match.group(1).strip().strip('"') if model_match else ""

    return provider, model


def _resolve_model(provider: str, model: str) -> str:
    """Resolve short model names to full SDK model IDs."""
    if provider == "claude":
        if not model or model in _CLAUDE_MODEL_MAP:
            return _CLAUDE_MODEL_MAP.get(model, DEFAULT_MODEL_CLAUDE)
        return model  # already a full model ID
    # OpenAI: use as-is, or fall back
    return model if model else DEFAULT_MODEL_OPENAI


def _get_provider_and_model() -> tuple[str, str]:
    """Get provider and model from config/llm-provider.yaml (medium tier)."""
    provider, raw_model = _parse_llm_provider_yaml()
    model = _resolve_model(provider, raw_model)
    return provider, model


def load_image_b64(path: Path) -> str:
    """Load an image file and return base64-encoded data."""
    return base64.standard_b64encode(path.read_bytes()).decode("ascii")


def classify_action_type(action_description: str) -> str:
    """Classify free-text action description into evaluator buckets."""
    text = action_description.lower()

    if any(keyword in text for keyword in ("servo", "pan", "tilt", "camera angle", "ptu")):
        return "servo_movement"
    if any(keyword in text for keyword in ("follow", "closer", "distance", "approach")):
        return "person_following"
    if any(keyword in text for keyword in ("face", "center", "centred", "tracking", "head")):
        return "face_tracking"
    return "servo_movement"


def build_action_prompt(action_description: str) -> tuple[str, str]:
    """Build focused prompt based on action type decomposition."""
    action_type = classify_action_type(action_description)
    checks = ACTION_CHECKS[action_type]

    checklist = "\n".join(f"{idx}. {item}" for idx, item in enumerate(checks, start=1))
    prompt = (
        f"Requested action: {action_description}\n"
        f"Action type: {action_type}\n\n"
        "Evaluate BEFORE vs AFTER using this checklist:\n"
        f"{checklist}\n\n"
        "Return strict JSON with keys success/confidence/explanation/suggestion."
    )
    return action_type, prompt


def build_prompt(action_description: str, action_type: str | None = None) -> str:
    """Build prompt from free-text action description and optional forced action type."""
    resolved_action_type = action_type or classify_action_type(action_description)
    if resolved_action_type not in ACTION_CHECKS:
        resolved_action_type = classify_action_type(action_description)

    checks = ACTION_CHECKS[resolved_action_type]
    checklist = "\n".join(f"{idx}. {item}" for idx, item in enumerate(checks, start=1))
    success_metrics = _load_success_metrics_for_action(resolved_action_type)
    metrics_block = _build_metrics_prompt_block(success_metrics, resolved_action_type)
    return (
        f"Requested action: {action_description}\n"
        f"Action type: {resolved_action_type}\n\n"
        "Evaluate BEFORE vs AFTER using this checklist:\n"
        f"{checklist}\n\n"
        f"{metrics_block}\n\n"
        "Return strict JSON with keys success/confidence/explanation/suggestion."
    )


def _load_success_metrics_for_action(action_type: str) -> dict[str, Any]:
    return _load_success_metrics_for_action_cached(action_type)


@functools.lru_cache(maxsize=8)
def _load_success_metrics_for_action_cached(action_type: str) -> dict[str, Any]:
    metrics = _DEFAULT_SUCCESS_METRICS.get(action_type)
    if not metrics or not _VISION_CONFIG_PATH.exists():
        return metrics or {}
    default_required = cast(list[str], metrics.get("required", []))
    default_rules = cast(dict[str, float], metrics.get("pass_rules", {}))

    try:
        content = _VISION_CONFIG_PATH.read_text()
    except OSError:
        return metrics

    required: list[str] = []
    center_tolerance = None
    min_improvement = None
    min_confidence = None
    in_vision_oracle = False
    in_success_metrics = False
    in_required = False
    in_pass_rules = False
    in_target_action = False
    action_header = f"    {action_type}:"

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if line == "vision_oracle:":
            in_vision_oracle = True
            in_success_metrics = False
            in_required = False
            in_pass_rules = False
            in_target_action = False
            continue
        if in_vision_oracle and line and not line.startswith("  "):
            in_vision_oracle = False
            in_success_metrics = False
            in_required = False
            in_pass_rules = False
            in_target_action = False
        if not in_vision_oracle:
            continue
        if line == "  success_metrics:":
            in_success_metrics = True
            in_required = False
            in_pass_rules = False
            in_target_action = False
            continue
        if in_success_metrics and line.startswith("  ") and not line.startswith("    "):
            in_success_metrics = False
            in_required = False
            in_pass_rules = False
            in_target_action = False
        if not in_success_metrics:
            continue
        if line == action_header:
            in_target_action = True
            in_required = False
            in_pass_rules = False
            continue
        if in_target_action and line.startswith("    ") and not line.startswith("      "):
            # Switched to another peer action block under success_metrics.
            in_target_action = False
            in_required = False
            in_pass_rules = False
        if not in_target_action:
            continue
        if line == "      required:":
            in_required = True
            in_pass_rules = False
            continue
        if line == "      pass_rules:":
            in_required = False
            in_pass_rules = True
            continue
        if in_required and line.startswith("        - "):
            item = line.replace("        - ", "", 1).strip()
            if item:
                required.append(item)
            continue
        if in_pass_rules and line.startswith("        center_tolerance_pct:"):
            center_tolerance = _parse_metric_float(line)
            continue
        if in_pass_rules and line.startswith("        min_centering_improvement_pct:"):
            min_improvement = _parse_metric_float(line)
            continue
        if in_pass_rules and line.startswith("        min_confidence:"):
            min_confidence = _parse_metric_float(line)
            continue

    return {
        "required": required or default_required,
        "pass_rules": {
            "center_tolerance_pct": _bound_metric(
                center_tolerance,
                default_rules["center_tolerance_pct"],
                minimum=0.0,
                maximum=100.0,
            ),
            "min_centering_improvement_pct": _bound_metric(
                min_improvement,
                default_rules["min_centering_improvement_pct"],
                minimum=0.0,
                maximum=100.0,
            ),
            "min_confidence": _bound_metric(
                min_confidence,
                default_rules["min_confidence"],
                minimum=0.0,
                maximum=1.0,
            ),
        },
    }


def _parse_metric_float(line: str) -> float | None:
    _, _, val = line.partition(":")
    text = val.split("#", 1)[0].strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _bound_metric(
    value: float | None, default: float, *, minimum: float, maximum: float
) -> float:
    metric = default if value is None else value
    if metric < minimum:
        return minimum
    if metric > maximum:
        return maximum
    return metric


def _build_metrics_prompt_block(success_metrics: dict[str, Any], action_type: str) -> str:
    if action_type != "face_tracking" or not success_metrics:
        return "SUCCESS_METRICS: Use checklist evidence and explain pass/fail clearly."

    required = success_metrics["required"]
    rules = success_metrics["pass_rules"]

    lines = [
        "SUCCESS_METRICS (measurable pass/fail):",
        f"- required_checks: {', '.join(required)}",
        "- face_detected_after: at least one human face is visible in AFTER",
        (
            "- face_centered_after: absolute face-center offset <= "
            f"{rules['center_tolerance_pct']:.0f}% of frame center on both axes"
        ),
        (
            "- centering_improved: AFTER center offset improves by at least "
            f"{rules['min_centering_improvement_pct']:.0f}% vs BEFORE"
        ),
        f"- minimum_decision_confidence: confidence >= {rules['min_confidence']:.2f}",
        (
            "- success_rule: set success=true only if all required_checks pass "
            "and confidence meets minimum_decision_confidence"
        ),
    ]
    return "\n".join(lines)


def _extract_json_object(response_text: str) -> dict[str, Any] | None:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def _normalize_result(raw: dict[str, Any] | None, fallback_explanation: str = "") -> dict[str, Any]:
    raw = raw or {}

    confidence_raw = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    success_raw = raw.get("success", False)
    if isinstance(success_raw, str):
        success = success_raw.strip().lower() in {"true", "1", "yes", "pass", "passed", "success"}
    else:
        success = bool(success_raw)

    explanation = str(raw.get("explanation", "")).strip()
    if not explanation and fallback_explanation:
        explanation = fallback_explanation
    if not explanation:
        explanation = "No explanation provided by model."

    suggestion = str(raw.get("suggestion", "")).strip()
    if not suggestion:
        suggestion = "none" if success else "Retry with clearer before/after framing."

    return {
        "success": success,
        "confidence": confidence,
        "explanation": explanation,
        "suggestion": suggestion,
    }


def _media_type_for_image(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "image/jpeg"


def _build_llm_command(binary: str, model: str, skip_perms: str,
                       prompt_flag: str, model_flag: str) -> list[str]:
    """Build CLI command list from llm-provider.yaml fields."""
    parts = binary.split()  # e.g. "codex exec" → ["codex", "exec"]
    if skip_perms:
        parts.append(skip_perms)
    if model and model_flag:
        parts.extend([model_flag, model])
    if prompt_flag:
        parts.append(prompt_flag)
    return parts


def _call_llm(prompt_text: str, before_path: Path, after_path: Path) -> str:
    """Call Claude Code CLI to evaluate before/after images.

    Uses claude -p with Read tool access — Claude Code natively reads image
    files (JPEG, PNG) via its Read tool, same as interactive sessions.
    """
    import os
    import tempfile

    # Build prompt that tells Claude to read the image files
    full_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Read these two image files and compare them:\n"
        f"BEFORE image: {before_path.resolve()}\n"
        f"AFTER image: {after_path.resolve()}\n\n"
        f"{prompt_text}\n\n"
        f"IMPORTANT: First read both image files using the Read tool, then evaluate. "
        f"Return ONLY the JSON result, no other text."
    )

    cmd = [
        "claude", "-p",
        "--model", "haiku",
        "--allowedTools", "Read",
        "--max-turns", "3",
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tf:
        tf.write(full_prompt)
        prompt_file = tf.name

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        with open(prompt_file) as pf:
            result = subprocess.run(
                cmd, stdin=pf, capture_output=True, text=True,
                timeout=120, env=env,
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()[:500] if result.stderr else ""
            raise RuntimeError(f"Claude CLI exited {result.returncode}: {stderr}")

        # Claude CLI may output to stderr when stdin is piped
        output = result.stdout.strip() or result.stderr.strip()
        return output
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def _evaluate_paths(before_path: Path, after_path: Path, action_description: str) -> dict[str, Any]:
    prompt_text = build_prompt(action_description)

    try:
        response_text = _call_llm(prompt_text, before_path, after_path)
    except Exception as exc:
        return _normalize_result(
            {"success": False, "confidence": 0.0},
            fallback_explanation=f"Evaluation failed due to API error: {exc}",
        )

    parsed = _extract_json_object(response_text)
    if parsed is None:
        return _normalize_result(
            {"success": False, "confidence": 0.0},
            fallback_explanation=(
                "Failed to parse model response as JSON: "
                f"{response_text[:200]}"
            ),
        )
    return _normalize_result(parsed)


def evaluate(
    before_path: Path,
    after_path: Path,
    test_type: str | None = None,
    criteria: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible evaluator for legacy callers/tests."""
    if criteria:
        action_description = criteria
    elif test_type and test_type in TEST_PROMPTS:
        action_description = TEST_PROMPTS[test_type]
    elif test_type:
        action_description = test_type.replace("_", " ")
    else:
        action_description = "Evaluate whether the robot action succeeded"
    return _evaluate_paths(before_path, after_path, action_description)


def evaluate_action(
    before_image_path: str | Path | Any,
    after_image_path: str | Path | None = None,
    action_description: str | None = None,
) -> dict[str, Any]:
    """Evaluate action result from before/after images.

    Primary API:
      evaluate_action(before_image_path, after_image_path, action_description)

    Legacy compatibility:
      evaluate_action(camera_capture_instance, test_type, criteria)
    """
    if hasattr(before_image_path, "capture_and_save") and hasattr(before_image_path, "delay"):
        camera = before_image_path
        test_type = after_image_path if isinstance(after_image_path, str) else None
        criteria = action_description

        before_path = camera.capture_and_save("before")
        camera.delay()
        after_path = camera.capture_and_save("after")
        result = evaluate(before_path, after_path, test_type=test_type, criteria=criteria)
        result["before_path"] = str(before_path)
        result["after_path"] = str(after_path)
        return result

    if after_image_path is None:
        raise ValueError("after_image_path is required")
    if not action_description:
        raise ValueError("action_description is required")

    before_path = Path(before_image_path)
    after_path = Path(after_image_path)
    if not before_path.is_file():
        raise ValueError(f"before_image_path not found: {before_path}")
    if not after_path.is_file():
        raise ValueError(f"after_image_path not found: {after_path}")
    return _evaluate_paths(before_path, after_path, action_description)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate robot actions via vision LLM")
    parser.add_argument("--before", type=Path, default=None, help="Path to before image")
    parser.add_argument("--after", type=Path, default=None, help="Path to after image")
    parser.add_argument("--action", type=str, default=None, help="Action description to evaluate")
    # Legacy options retained for compatibility.
    parser.add_argument("--test", type=str, default=None, choices=list(TEST_PROMPTS.keys()))
    parser.add_argument("--criteria", type=str, default=None)

    parser.add_argument("--live", action="store_true")
    parser.add_argument("--room", type=str, default=None,
                        help="LiveKit room name for --live mode (default: robot-cam)")

    args = parser.parse_args()

    if args.live:
        from monitoring.camera_capture import CameraCapture
        cam = CameraCapture(room=args.room) if args.room else CameraCapture()

        print("Capturing BEFORE frame...", file=sys.stderr)
        before_path = cam.capture_and_save("before")
        print(f"Before: {before_path}", file=sys.stderr)
        input("Press Enter after the robot action completes...")
        print("Capturing AFTER frame...", file=sys.stderr)
        after_path = cam.capture_and_save("after")
        print(f"After: {after_path}", file=sys.stderr)
    else:
        if not args.before or not args.after:
            print("Error: --before and --after are required (or use --live)", file=sys.stderr)
            sys.exit(1)
        before_path = args.before
        after_path = args.after

    action_description = (
        args.action
        or args.criteria
        or (TEST_PROMPTS[args.test] if args.test in TEST_PROMPTS else None)
        or "Evaluate whether the robot action succeeded"
    )

    try:
        result = evaluate_action(before_path, after_path, action_description)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
