"""Tests for deploy/vector/experiment_decision.py."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "vector" / "experiment_decision.py"


class TestExperimentDecision(unittest.TestCase):
    def run_decision(self, *args: str) -> tuple[int, dict[str, object]]:
        proc = subprocess.run(
            ["python3", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertTrue(proc.stdout.strip(), f"expected JSON output, stderr={proc.stderr}")
        payload = json.loads(proc.stdout.strip())
        return proc.returncode, payload

    def _make_rollback_script(self, tmpdir: Path, marker_name: str, exit_code: int) -> Path:
        marker = tmpdir / marker_name
        script = tmpdir / f"rollback_{marker_name}.sh"
        script.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            f"echo called > {marker}\n"
            f"exit {exit_code}\n"
        )
        script.chmod(0o755)
        return script

    def test_accepts_successful_high_confidence_result(self):
        code, payload = self.run_decision(
            "--result-json",
            '{"success": true, "confidence": 0.91}',
            "--min-confidence",
            "0.70",
        )

        self.assertEqual(code, 0)
        self.assertTrue(payload["accepted"])
        self.assertFalse(payload["rollback_attempted"])

    def test_rejects_failed_result_and_runs_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            rollback_script = self._make_rollback_script(tmpdir, "marker_ok", 0)
            marker = tmpdir / "marker_ok"

            code, payload = self.run_decision(
                "--result-json",
                '{"success": false, "confidence": 0.99}',
                "--rollback-cmd",
                f"bash {rollback_script}",
            )

            self.assertEqual(code, 10)
            self.assertFalse(payload["accepted"])
            self.assertTrue(payload["rollback_attempted"])
            self.assertTrue(payload["rollback_succeeded"])
            self.assertTrue(marker.exists())

    def test_rejects_low_confidence_even_when_success_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            rollback_script = self._make_rollback_script(tmpdir, "marker_low", 0)

            code, payload = self.run_decision(
                "--result-json",
                '{"success": true, "confidence": 0.41}',
                "--min-confidence",
                "0.70",
                "--rollback-cmd",
                f"bash {rollback_script}",
            )

            self.assertEqual(code, 10)
            self.assertIn("below threshold", str(payload["reason"]))
            self.assertTrue(payload["rollback_attempted"])
            self.assertTrue(payload["rollback_succeeded"])

    def test_invalid_json_returns_error(self):
        code, payload = self.run_decision(
            "--result-json",
            "not-json",
        )

        self.assertEqual(code, 20)
        self.assertFalse(payload["accepted"])
        self.assertIn("Invalid JSON", str(payload["error"]))

    def test_invalid_min_confidence_returns_error(self):
        code, payload = self.run_decision(
            "--result-json",
            '{"success": true, "confidence": 0.8}',
            "--min-confidence",
            "1.2",
        )

        self.assertEqual(code, 20)
        self.assertFalse(payload["accepted"])
        self.assertIn("between 0.0 and 1.0", str(payload["error"]))

    def test_rollback_failure_has_distinct_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            rollback_script = self._make_rollback_script(tmpdir, "marker_fail", 2)
            marker = tmpdir / "marker_fail"

            code, payload = self.run_decision(
                "--result-json",
                '{"success": false, "confidence": 0.2}',
                "--rollback-cmd",
                f"bash {rollback_script}",
            )

            self.assertEqual(code, 30)
            self.assertFalse(payload["accepted"])
            self.assertTrue(payload["rollback_attempted"])
            self.assertFalse(payload["rollback_succeeded"])
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
