#!/usr/bin/env python3
"""Evolution agent for parameter mutation and experiment tracking.

This module provides:
- bounded/quantized parameter mutation
- persistent experiment ledger
- weighted score aggregation for selecting best candidates
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

DEFAULT_MUTATION_RATE = 0.35
DEFAULT_SIGMA_SCALE = 0.15
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "experiments" / "evolution_state.json"


@dataclass(frozen=True)
class ParameterSpec:
    """Describes the valid range/shape for a tunable parameter."""

    minimum: float
    maximum: float
    step: float | None = None
    value_type: str = "float"

    def __post_init__(self) -> None:
        if self.maximum <= self.minimum:
            raise ValueError("maximum must be greater than minimum")
        if self.step is not None and self.step <= 0:
            raise ValueError("step must be > 0 when provided")
        if self.value_type not in {"float", "int"}:
            raise ValueError("value_type must be 'float' or 'int'")

    def normalize(self, value: float) -> float | int:
        """Clamp to bounds, quantize to step, and cast to declared type."""
        clamped = min(max(value, self.minimum), self.maximum)

        if self.step is not None:
            steps = round((clamped - self.minimum) / self.step)
            clamped = self.minimum + steps * self.step
            clamped = min(max(clamped, self.minimum), self.maximum)

        if self.value_type == "int":
            return int(round(clamped))
        return float(clamped)


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_parameter_space(raw_space: dict[str, dict[str, Any]]) -> dict[str, ParameterSpec]:
    parameter_space: dict[str, ParameterSpec] = {}
    for name, raw in raw_space.items():
        try:
            minimum = float(raw["min"])
            maximum = float(raw["max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid parameter bounds for '{name}'") from exc

        step_raw = raw.get("step")
        step = float(step_raw) if step_raw is not None else None

        value_type = str(raw.get("type", "float"))
        parameter_space[name] = ParameterSpec(
            minimum=minimum,
            maximum=maximum,
            step=step,
            value_type=value_type,
        )
    return parameter_space


def mutate_parameters(
    base_params: dict[str, float | int],
    parameter_space: dict[str, ParameterSpec],
    rng: random.Random,
    mutation_rate: float = DEFAULT_MUTATION_RATE,
    sigma_scale: float = DEFAULT_SIGMA_SCALE,
) -> tuple[dict[str, float | int], list[str]]:
    """Generate a mutated parameter set constrained by the parameter space."""
    if not 0 < mutation_rate <= 1:
        raise ValueError("mutation_rate must be in (0, 1]")
    if sigma_scale <= 0:
        raise ValueError("sigma_scale must be > 0")

    params = dict(base_params)
    mutated_names: list[str] = []

    for name, spec in parameter_space.items():
        if name not in params:
            # For missing keys, seed from midpoint.
            params[name] = spec.normalize((spec.minimum + spec.maximum) / 2)

        if rng.random() > mutation_rate:
            params[name] = spec.normalize(float(params[name]))
            continue

        span = spec.maximum - spec.minimum
        sigma = span * sigma_scale
        delta = rng.gauss(0.0, sigma)
        new_value = spec.normalize(float(params[name]) + delta)
        if new_value != params[name]:
            mutated_names.append(name)
        params[name] = new_value

    if not parameter_space:
        return params, mutated_names

    if not mutated_names:
        # Force at least one mutation to keep exploration moving.
        forced = rng.choice(sorted(parameter_space.keys()))
        spec = parameter_space[forced]
        span = spec.maximum - spec.minimum
        direction = -1.0 if rng.random() < 0.5 else 1.0
        delta = max(spec.step or (span * 0.05), span * 0.01) * direction
        updated = spec.normalize(float(params[forced]) + delta)
        if updated == params[forced]:
            updated = spec.normalize(float(params[forced]) - delta)
        if updated == params[forced]:
            # Final fallback: attempt hard-bound flips.
            for candidate in (spec.normalize(spec.minimum), spec.normalize(spec.maximum)):
                if candidate != params[forced]:
                    updated = candidate
                    break
        if updated == params[forced]:
            raise ValueError(f"unable to mutate parameter '{forced}' with current bounds/step")
        params[forced] = updated
        mutated_names.append(forced)

    return params, sorted(set(mutated_names))


def compute_weighted_score(metrics: dict[str, float], weights: dict[str, float]) -> float:
    """Compute weighted metric score. Missing metrics default to 0."""
    if not weights:
        raise ValueError("weights must not be empty")

    validated_weights: dict[str, float] = {}
    for metric_name, raw_weight in weights.items():
        weight = float(raw_weight)
        if math.isnan(weight) or math.isinf(weight):
            raise ValueError(f"weight '{metric_name}' must be finite")
        validated_weights[metric_name] = weight

    total_weight = sum(abs(weight) for weight in validated_weights.values())
    if total_weight == 0:
        raise ValueError("weights must have non-zero magnitude")

    weighted_sum = 0.0
    for metric_name, weight in validated_weights.items():
        value = float(metrics.get(metric_name, 0.0))
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"metric '{metric_name}' must be finite")
        weighted_sum += value * weight

    return weighted_sum / total_weight


class ExperimentStore:
    """Persistent JSON store for experiment history and best candidate."""

    def __init__(self, state_path: Path | str = DEFAULT_STATE_PATH):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "version": STATE_VERSION,
                "created_at": _utcnow_iso(),
                "updated_at": _utcnow_iso(),
                "best_experiment_id": None,
                "best_score": None,
                "experiments": [],
            }
        state = json.loads(self.state_path.read_text())
        if state.get("version") != STATE_VERSION:
            raise ValueError(
                f"unsupported state version: {state.get('version')} (expected {STATE_VERSION})"
            )
        return state

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _utcnow_iso()
        with NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="evolution-state-",
            dir=str(self.state_path.parent),
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            json.dump(state, tmp_file, indent=2, sort_keys=True)
            tmp_file.write("\n")
        tmp_path.replace(self.state_path)


class EvolutionAgent:
    """Coordinates candidate mutation and experiment result tracking."""

    def __init__(
        self,
        parameter_space: dict[str, dict[str, Any]] | dict[str, ParameterSpec],
        metric_weights: dict[str, float],
        state_path: Path | str = DEFAULT_STATE_PATH,
        seed: int | None = None,
    ):
        if not metric_weights:
            raise ValueError("metric_weights must not be empty")

        self.parameter_space: dict[str, ParameterSpec]
        if parameter_space and all(isinstance(v, ParameterSpec) for v in parameter_space.values()):
            self.parameter_space = cast(dict[str, ParameterSpec], parameter_space)
        else:
            self.parameter_space = _load_parameter_space(cast(dict[str, dict[str, Any]], parameter_space))
        self.metric_weights = {k: float(v) for k, v in metric_weights.items()}
        self.store = ExperimentStore(state_path)
        self.rng = random.Random(seed)

    def propose_candidate(
        self,
        base_params: dict[str, float | int],
        parent_experiment_id: str | None = None,
        mutation_rate: float = DEFAULT_MUTATION_RATE,
        sigma_scale: float = DEFAULT_SIGMA_SCALE,
        notes: str = "",
    ) -> dict[str, Any]:
        state = self.store.load()
        if parent_experiment_id is None:
            parent_experiment_id = state.get("best_experiment_id")

        mutated_params, mutated_keys = mutate_parameters(
            base_params=base_params,
            parameter_space=self.parameter_space,
            rng=self.rng,
            mutation_rate=mutation_rate,
            sigma_scale=sigma_scale,
        )

        experiment_id = f"exp-{len(state['experiments']) + 1:04d}"
        record = {
            "id": experiment_id,
            "parent_id": parent_experiment_id,
            "status": "proposed",
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
            "params": mutated_params,
            "mutated_keys": mutated_keys,
            "metrics": {},
            "score": None,
            "succeeded": None,
            "notes": notes,
        }
        state["experiments"].append(record)
        self.store.save(state)
        return record

    def record_result(
        self,
        experiment_id: str,
        metrics: dict[str, float],
        succeeded: bool,
        notes: str = "",
    ) -> dict[str, Any]:
        state = self.store.load()

        target: dict[str, Any] | None = None
        for record in state["experiments"]:
            if record["id"] == experiment_id:
                target = record
                break

        if target is None:
            raise ValueError(f"experiment_id not found: {experiment_id}")

        score = compute_weighted_score(metrics, self.metric_weights)
        target["metrics"] = {k: float(v) for k, v in metrics.items()}
        target["score"] = score
        target["succeeded"] = bool(succeeded)
        target["status"] = "completed"
        target["updated_at"] = _utcnow_iso()
        if notes:
            target["notes"] = notes

        best_score = state.get("best_score")
        if succeeded and (best_score is None or score > float(best_score)):
            state["best_experiment_id"] = experiment_id
            state["best_score"] = score

        self.store.save(state)
        return target

    def best_experiment(self) -> dict[str, Any] | None:
        state = self.store.load()
        best_id = state.get("best_experiment_id")
        if not best_id:
            return None
        for record in state["experiments"]:
            if record["id"] == best_id:
                return record
        return None


def _parse_json_arg(raw: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolution agent for parameter mutation and experiment tracking")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Path to evolution state JSON")
    parser.add_argument("--parameter-space", default=None, help="JSON object defining parameter bounds")
    parser.add_argument("--metric-weights", default=None, help="JSON object defining metric weights")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducible mutation")

    subparsers = parser.add_subparsers(dest="command", required=True)

    propose_parser = subparsers.add_parser("propose", help="Propose a mutated parameter candidate")
    propose_parser.add_argument("--base-params", required=True, help="JSON object of base parameter values")
    propose_parser.add_argument("--parent-experiment-id", default=None)
    propose_parser.add_argument("--mutation-rate", type=float, default=DEFAULT_MUTATION_RATE)
    propose_parser.add_argument("--sigma-scale", type=float, default=DEFAULT_SIGMA_SCALE)
    propose_parser.add_argument("--notes", default="")

    record_parser = subparsers.add_parser("record", help="Record experiment outcome")
    record_parser.add_argument("--experiment-id", required=True)
    record_parser.add_argument("--metrics", required=True, help="JSON object of measured metrics")
    record_parser.add_argument("--succeeded", choices=["true", "false"], required=True)
    record_parser.add_argument("--notes", default="")

    subparsers.add_parser("best", help="Print current best experiment")

    args = parser.parse_args()
    output: Any

    if args.command in ("propose", "record"):
        if args.parameter_space is None or args.metric_weights is None:
            raise ValueError(f"--parameter-space and --metric-weights are required for {args.command}")
        agent = EvolutionAgent(
            parameter_space=_parse_json_arg(args.parameter_space, "--parameter-space"),
            metric_weights=_parse_json_arg(args.metric_weights, "--metric-weights"),
            state_path=args.state,
            seed=args.seed,
        )

    if args.command == "propose":
        output = agent.propose_candidate(
            base_params=_parse_json_arg(args.base_params, "--base-params"),
            parent_experiment_id=args.parent_experiment_id,
            mutation_rate=args.mutation_rate,
            sigma_scale=args.sigma_scale,
            notes=args.notes,
        )
    elif args.command == "record":
        output = agent.record_result(
            experiment_id=args.experiment_id,
            metrics={k: float(v) for k, v in _parse_json_arg(args.metrics, "--metrics").items()},
            succeeded=args.succeeded == "true",
            notes=args.notes,
        )
    else:
        store = ExperimentStore(args.state)
        state = store.load()
        best_id = state.get("best_experiment_id")
        output = next(
            (r for r in state.get("experiments", []) if r.get("id") == best_id),
            None,
        ) if best_id else None

    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
