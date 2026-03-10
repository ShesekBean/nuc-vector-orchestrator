"""Tests for apps.test_harness.evolution_agent."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from apps.test_harness.evolution_agent import (
    EvolutionAgent,
    ParameterSpec,
    compute_weighted_score,
    mutate_parameters,
)


def _default_space() -> dict[str, dict[str, float | str]]:
    return {
        "kp": {"min": 0.0, "max": 1.0, "step": 0.05, "type": "float"},
        "kd": {"min": 0.0, "max": 0.5, "step": 0.01, "type": "float"},
        "deadzone": {"min": 0, "max": 20, "step": 1, "type": "int"},
    }


def test_mutate_parameters_stays_in_bounds_and_quantized():
    rng = __import__("random").Random(7)
    specs = {
        "kp": ParameterSpec(0.0, 1.0, step=0.05, value_type="float"),
        "deadzone": ParameterSpec(0, 20, step=1, value_type="int"),
    }
    base = {"kp": 0.45, "deadzone": 8}

    mutated, mutated_keys = mutate_parameters(
        base_params=base,
        parameter_space=specs,
        rng=rng,
        mutation_rate=1.0,
        sigma_scale=0.3,
    )

    assert mutated_keys
    assert 0.0 <= float(mutated["kp"]) <= 1.0
    assert 0 <= int(mutated["deadzone"]) <= 20
    assert abs((float(mutated["kp"]) - 0.0) / 0.05 - round((float(mutated["kp"]) - 0.0) / 0.05)) < 1e-9
    assert isinstance(mutated["deadzone"], int)


def test_mutate_forces_at_least_one_change_when_rate_is_low():
    rng = __import__("random").Random(12)
    specs = {
        "kp": ParameterSpec(0.0, 1.0, step=0.05, value_type="float"),
        "kd": ParameterSpec(0.0, 0.5, step=0.01, value_type="float"),
    }
    base = {"kp": 0.5, "kd": 0.25}

    mutated, mutated_keys = mutate_parameters(
        base_params=base,
        parameter_space=specs,
        rng=rng,
        mutation_rate=1e-9,
        sigma_scale=0.05,
    )

    assert mutated_keys
    assert mutated != base


def test_compute_weighted_score_handles_missing_metrics():
    score = compute_weighted_score(
        metrics={"success_rate": 0.8},
        weights={"success_rate": 0.7, "latency_penalty": -0.3},
    )
    assert abs(score - 0.56) < 1e-9


def test_compute_weighted_score_rejects_zero_weights():
    with pytest.raises(ValueError, match="non-zero magnitude"):
        compute_weighted_score({"a": 1.0}, {"a": 0.0})


def test_compute_weighted_score_rejects_non_finite_weights():
    with pytest.raises(ValueError, match="must be finite"):
        compute_weighted_score({"a": 1.0}, {"a": float("nan")})


def test_mutate_parameters_raises_when_no_legal_mutation_possible():
    rng = __import__("random").Random(1)
    specs = {"locked": ParameterSpec(0.0, 1.0, step=10.0, value_type="float")}
    with pytest.raises(ValueError, match="unable to mutate"):
        mutate_parameters(
            base_params={"locked": 0.0},
            parameter_space=specs,
            rng=rng,
            mutation_rate=1e-9,
            sigma_scale=0.1,
        )


def test_evolution_agent_tracks_experiments_and_best_candidate():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        agent = EvolutionAgent(
            parameter_space=_default_space(),
            metric_weights={"success": 1.0, "stability": 0.5},
            state_path=state_path,
            seed=3,
        )

        proposal_a = agent.propose_candidate(base_params={"kp": 0.4, "kd": 0.1, "deadzone": 5})
        assert proposal_a["status"] == "proposed"

        result_a = agent.record_result(
            experiment_id=proposal_a["id"],
            metrics={"success": 0.7, "stability": 0.6},
            succeeded=True,
        )
        assert result_a["status"] == "completed"
        assert result_a["score"] is not None

        proposal_b = agent.propose_candidate(base_params={"kp": 0.4, "kd": 0.1, "deadzone": 5})
        agent.record_result(
            experiment_id=proposal_b["id"],
            metrics={"success": 0.4, "stability": 0.4},
            succeeded=True,
        )

        best = agent.best_experiment()
        assert best is not None
        assert best["id"] == proposal_a["id"]

        state = json.loads(state_path.read_text())
        assert state["best_experiment_id"] == proposal_a["id"]
        assert len(state["experiments"]) == 2


def test_evolution_agent_record_result_requires_valid_experiment_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        agent = EvolutionAgent(
            parameter_space=_default_space(),
            metric_weights={"success": 1.0},
            state_path=Path(tmpdir) / "state.json",
            seed=1,
        )

        with pytest.raises(ValueError, match="not found"):
            agent.record_result("exp-404", metrics={"success": 1.0}, succeeded=True)


def test_cli_best_command_does_not_require_parameter_or_metric_args():
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "state.json"
        proc = subprocess.run(
            ["python3", "apps/test_harness/evolution_agent.py", "--state", str(state_path), "best"],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path(__file__).resolve().parent.parent,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == "null"
