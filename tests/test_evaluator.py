"""Tests for action_evaluator and camera_capture modules.

Tests the CameraCapture class (laptop + phone sources), config loading,
before/after frame comparison, Claude Vision API evaluation logic,
and camera capture functionality with mocked HTTP responses.

Run with: python3 -m pytest tests/test_evaluator.py -v
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add repo root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock SDKs before importing action_evaluator (may not be installed in test env)
sys.modules.setdefault("openai", MagicMock())
sys.modules.setdefault("anthropic", MagicMock())
sys.modules.setdefault("livekit", MagicMock())
sys.modules.setdefault("livekit.api", MagicMock())
sys.modules.setdefault("livekit.rtc", MagicMock())
sys.modules.setdefault("numpy", MagicMock())
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

# Mock livekit + deps before importing camera_capture (not installed in CI)
_mock_livekit = MagicMock()
sys.modules.setdefault("livekit", _mock_livekit)
sys.modules.setdefault("livekit.api", _mock_livekit.api)
sys.modules.setdefault("livekit.rtc", _mock_livekit.rtc)
sys.modules.setdefault("numpy", MagicMock())
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

from apps.test_harness.camera_capture import (  # noqa: E402
    CameraCapture,
    save_frame,
)
from apps.test_harness.action_evaluator import (  # noqa: E402
    ACTION_CHECKS,
    classify_action_type,
    build_prompt,
    evaluate,
    evaluate_action,
    load_image_b64,
    _load_success_metrics_for_action,
    _parse_llm_provider_yaml,
    _resolve_model,
    SYSTEM_PROMPT,
)


# --- CameraCapture class tests ---


class TestCameraCaptureInit(unittest.TestCase):
    """Test CameraCapture initialization."""

    @patch("apps.test_harness.camera_capture._load_credentials")
    def test_default_room(self, mock_creds):
        mock_creds.return_value = ("wss://test.livekit.cloud", "key", "secret")
        cam = CameraCapture()
        self.assertEqual(cam.room, "robot-cam")

    @patch("apps.test_harness.camera_capture._load_credentials")
    def test_custom_room(self, mock_creds):
        mock_creds.return_value = ("wss://test.livekit.cloud", "key", "secret")
        cam = CameraCapture(room="custom-room")
        self.assertEqual(cam.room, "custom-room")

    @patch("apps.test_harness.camera_capture._load_credentials")
    def test_custom_captures_dir(self, mock_creds):
        mock_creds.return_value = ("wss://test.livekit.cloud", "key", "secret")
        cam = CameraCapture(captures_dir=Path("/tmp/test_captures"))
        self.assertEqual(cam.captures_dir, Path("/tmp/test_captures"))

    @patch("apps.test_harness.camera_capture._load_credentials")
    def test_custom_delay(self, mock_creds):
        mock_creds.return_value = ("wss://test.livekit.cloud", "key", "secret")
        cam = CameraCapture(capture_delay_ms=1000)
        self.assertEqual(cam.capture_delay_ms, 1000)


class TestCameraCaptureDelay(unittest.TestCase):
    """Test the delay method."""

    @patch("apps.test_harness.camera_capture._load_credentials")
    @patch("apps.test_harness.camera_capture.time.sleep")
    def test_delay_uses_config(self, mock_sleep, mock_creds):
        mock_creds.return_value = ("wss://test.livekit.cloud", "key", "secret")
        cam = CameraCapture(capture_delay_ms=750)
        cam.delay()
        mock_sleep.assert_called_once_with(0.75)


class TestSaveFrame(unittest.TestCase):
    """Test saving frames to disk."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.captures = self.tmpdir / "captures"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_creates_directory(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        path = save_frame(data, self.captures, "before")
        self.assertTrue(self.captures.exists())
        self.assertTrue(path.exists())

    def test_save_before_label(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        path = save_frame(data, self.captures, "before")
        self.assertIn("before_", path.name)
        self.assertTrue(path.name.endswith(".jpg"))

    def test_save_after_label(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        path = save_frame(data, self.captures, "after")
        self.assertIn("after_", path.name)

    def test_save_no_label(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        path = save_frame(data, self.captures, None)
        self.assertIn("capture_", path.name)

    def test_save_creates_latest_symlink(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        save_frame(data, self.captures, "before")
        latest = self.captures / "before_latest.jpg"
        self.assertTrue(latest.exists())
        self.assertEqual(latest.read_bytes(), data)

    def test_save_content_matches(self):
        data = b"\xff\xd8\xff\xe0" + os.urandom(500)
        path = save_frame(data, self.captures, "test")
        self.assertEqual(path.read_bytes(), data)


# --- LLM provider config tests ---


class TestLLMProviderConfig(unittest.TestCase):
    """Test parsing of config/llm-provider.yaml (shared with agent-loop)."""

    def test_parse_openai_provider(self):
        import apps.test_harness.action_evaluator as ae
        orig = ae._LLM_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "provider: openai\n"
            "openai:\n"
            "  binary: \"codex exec\"\n"
            "  models:\n"
            "    heavy: \"\"\n"
            "    medium: \"gpt-5-codex-mini\"\n"
            "    light: \"gpt-5-codex-mini\"\n"
        )
        f.close()
        try:
            ae._LLM_CONFIG_PATH = Path(f.name)
            provider, model = _parse_llm_provider_yaml()
            self.assertEqual(provider, "openai")
            self.assertEqual(model, "gpt-5-codex-mini")
        finally:
            ae._LLM_CONFIG_PATH = orig
            os.unlink(f.name)

    def test_parse_claude_provider(self):
        import apps.test_harness.action_evaluator as ae
        orig = ae._LLM_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "provider: claude\n"
            "claude:\n"
            "  binary: claude\n"
            "  models:\n"
            "    heavy: \"\"\n"
            "    medium: sonnet\n"
            "    light: haiku\n"
        )
        f.close()
        try:
            ae._LLM_CONFIG_PATH = Path(f.name)
            provider, model = _parse_llm_provider_yaml()
            self.assertEqual(provider, "claude")
            self.assertEqual(model, "sonnet")
        finally:
            ae._LLM_CONFIG_PATH = orig
            os.unlink(f.name)

    def test_missing_config_uses_defaults(self):
        import apps.test_harness.action_evaluator as ae
        orig = ae._LLM_CONFIG_PATH
        try:
            ae._LLM_CONFIG_PATH = Path("/nonexistent/llm-provider.yaml")
            provider, model = _parse_llm_provider_yaml()
            self.assertEqual(provider, "openai")
            self.assertEqual(model, "gpt-5-codex-mini")
        finally:
            ae._LLM_CONFIG_PATH = orig

    def test_resolve_claude_short_names(self):
        self.assertEqual(_resolve_model("claude", "sonnet"), "claude-sonnet-4-6-20250725")
        self.assertEqual(_resolve_model("claude", "haiku"), "claude-haiku-4-5-20251001")
        self.assertEqual(_resolve_model("claude", ""), "claude-sonnet-4-6-20250725")

    def test_resolve_openai_passthrough(self):
        self.assertEqual(_resolve_model("openai", "gpt-5-codex-mini"), "gpt-5-codex-mini")

# --- action_evaluator tests ---


class TestActionClassification(unittest.TestCase):
    """Test action-description classification."""

    def test_person_following(self):
        self.assertEqual(classify_action_type("follow person"), "person_following")

    def test_face_tracking(self):
        self.assertEqual(classify_action_type("track face to center"), "face_tracking")

    def test_servo_movement(self):
        self.assertEqual(classify_action_type("move camera servo left"), "servo_movement")

    def test_default_fallback(self):
        self.assertEqual(classify_action_type("do the thing"), "servo_movement")


class TestBuildPrompt(unittest.TestCase):
    """Test prompt construction for decomposed action checks."""

    def test_prompt_includes_face_tracking_check(self):
        prompt = build_prompt("Track the user's face")
        self.assertIn("face_tracking", prompt)
        self.assertIn("approximately centered", prompt)

    def test_prompt_includes_servo_movement_check(self):
        prompt = build_prompt("Move the camera servo")
        self.assertIn("servo_movement", prompt)
        self.assertIn("camera angle", prompt)

    def test_prompt_includes_person_following_check(self):
        prompt = build_prompt("Follow the person")
        self.assertIn("person_following", prompt)
        self.assertIn("closer to the person", prompt)

    def test_defined_action_checks(self):
        expected = {"face_tracking", "servo_movement", "person_following"}
        self.assertEqual(set(ACTION_CHECKS.keys()), expected)

    def test_unknown_action_type_falls_back(self):
        prompt = build_prompt("track face", action_type="invalid_type")
        self.assertIn("Action type: face_tracking", prompt)

    def test_face_tracking_prompt_includes_success_metrics(self):
        prompt = build_prompt("Track face to center")
        self.assertIn("SUCCESS_METRICS (measurable pass/fail)", prompt)
        self.assertIn("face_detected_after", prompt)
        self.assertIn("<= 20%", prompt)
        self.assertIn("confidence >=", prompt)

    def test_non_face_tracking_uses_generic_success_metrics(self):
        prompt = build_prompt("Move camera servo")
        self.assertIn("SUCCESS_METRICS: Use checklist evidence", prompt)


class TestSuccessMetricsConfig(unittest.TestCase):
    """Test loading measurable metrics from vision config."""

    def test_default_face_tracking_metrics_exist(self):
        metrics = _load_success_metrics_for_action("face_tracking")
        self.assertIn("required", metrics)
        self.assertIn("pass_rules", metrics)
        self.assertGreater(metrics["pass_rules"]["center_tolerance_pct"], 0)

    def test_custom_face_tracking_metrics_from_config(self):
        import apps.test_harness.action_evaluator as ae

        orig = ae._VISION_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "vision_oracle:\n"
            "  success_metrics:\n"
            "    face_tracking:\n"
            "      required:\n"
            "        - face_detected_after\n"
            "        - centering_improved\n"
            "      pass_rules:\n"
            "        center_tolerance_pct: 15\n"
            "        min_centering_improvement_pct: 25\n"
            "        min_confidence: 0.85\n"
        )
        f.close()
        try:
            ae._VISION_CONFIG_PATH = Path(f.name)
            ae._load_success_metrics_for_action_cached.cache_clear()
            metrics = _load_success_metrics_for_action("face_tracking")
            self.assertEqual(metrics["required"], ["face_detected_after", "centering_improved"])
            self.assertEqual(metrics["pass_rules"]["center_tolerance_pct"], 15.0)
            self.assertEqual(metrics["pass_rules"]["min_centering_improvement_pct"], 25.0)
            self.assertEqual(metrics["pass_rules"]["min_confidence"], 0.85)
        finally:
            ae._VISION_CONFIG_PATH = orig
            ae._load_success_metrics_for_action_cached.cache_clear()
            os.unlink(f.name)

    def test_face_tracking_metrics_scoped_to_vision_oracle_success_metrics(self):
        import apps.test_harness.action_evaluator as ae

        orig = ae._VISION_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "other_section:\n"
            "  success_metrics:\n"
            "    face_tracking:\n"
            "      pass_rules:\n"
            "        center_tolerance_pct: 99\n"
            "vision_oracle:\n"
            "  success_metrics:\n"
            "    face_tracking:\n"
            "      required:\n"
            "        - face_detected_after\n"
            "      pass_rules:\n"
            "        center_tolerance_pct: 12\n"
            "        min_centering_improvement_pct: 11\n"
            "        min_confidence: 0.71\n"
        )
        f.close()
        try:
            ae._VISION_CONFIG_PATH = Path(f.name)
            ae._load_success_metrics_for_action_cached.cache_clear()
            metrics = _load_success_metrics_for_action("face_tracking")
            self.assertEqual(metrics["pass_rules"]["center_tolerance_pct"], 12.0)
            self.assertEqual(metrics["pass_rules"]["min_centering_improvement_pct"], 11.0)
            self.assertEqual(metrics["pass_rules"]["min_confidence"], 0.71)
        finally:
            ae._VISION_CONFIG_PATH = orig
            ae._load_success_metrics_for_action_cached.cache_clear()
            os.unlink(f.name)

    def test_custom_metrics_allow_inline_yaml_comments(self):
        import apps.test_harness.action_evaluator as ae

        orig = ae._VISION_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "vision_oracle:\n"
            "  success_metrics:\n"
            "    face_tracking:\n"
            "      required:\n"
            "        - face_detected_after\n"
            "      pass_rules:\n"
            "        center_tolerance_pct: 13  # percent\n"
            "        min_centering_improvement_pct: 9 # percent\n"
            "        min_confidence: 0.66 # confidence floor\n"
        )
        f.close()
        try:
            ae._VISION_CONFIG_PATH = Path(f.name)
            ae._load_success_metrics_for_action_cached.cache_clear()
            metrics = _load_success_metrics_for_action("face_tracking")
            self.assertEqual(metrics["pass_rules"]["center_tolerance_pct"], 13.0)
            self.assertEqual(metrics["pass_rules"]["min_centering_improvement_pct"], 9.0)
            self.assertEqual(metrics["pass_rules"]["min_confidence"], 0.66)
        finally:
            ae._VISION_CONFIG_PATH = orig
            ae._load_success_metrics_for_action_cached.cache_clear()
            os.unlink(f.name)

    def test_custom_metrics_are_bounded_to_valid_ranges(self):
        import apps.test_harness.action_evaluator as ae

        orig = ae._VISION_CONFIG_PATH
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        f.write(
            "vision_oracle:\n"
            "  success_metrics:\n"
            "    face_tracking:\n"
            "      required:\n"
            "        - face_detected_after\n"
            "      pass_rules:\n"
            "        center_tolerance_pct: -4\n"
            "        min_centering_improvement_pct: 140\n"
            "        min_confidence: 1.9\n"
        )
        f.close()
        try:
            ae._VISION_CONFIG_PATH = Path(f.name)
            ae._load_success_metrics_for_action_cached.cache_clear()
            metrics = _load_success_metrics_for_action("face_tracking")
            self.assertEqual(metrics["pass_rules"]["center_tolerance_pct"], 0.0)
            self.assertEqual(metrics["pass_rules"]["min_centering_improvement_pct"], 100.0)
            self.assertEqual(metrics["pass_rules"]["min_confidence"], 1.0)
        finally:
            ae._VISION_CONFIG_PATH = orig
            ae._load_success_metrics_for_action_cached.cache_clear()
            os.unlink(f.name)


class TestLoadImageB64(unittest.TestCase):
    """Test image loading and base64 encoding."""

    def test_roundtrip(self):
        data = b"\xff\xd8\xff\xe0" + os.urandom(500)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(data)
            f.flush()
            b64 = load_image_b64(Path(f.name))
        os.unlink(f.name)
        decoded = base64.standard_b64decode(b64)
        self.assertEqual(decoded, data)


class TestEvaluate(unittest.TestCase):
    """Test API evaluation calls and response normalization."""

    def _make_test_images(self):
        before = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        before.write(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
        before.close()
        after = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        after.write(b"\xff\xd8\xff\xe0" + b"\x00" * 200)
        after.close()
        return Path(before.name), Path(after.name)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_successful_evaluation(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = json.dumps({
                "success": True,
                "confidence": 0.85,
                "explanation": "Face is centered in after frame",
                "suggestion": "none",
            })
            result = evaluate_action(before, after, "track face")
            self.assertTrue(result["success"])
            self.assertAlmostEqual(result["confidence"], 0.85)
            self.assertIn("centered", result["explanation"])
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_malformed_json_response(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = "this is not valid json"
            result = evaluate_action(before, after, "servo movement")
            self.assertFalse(result["success"])
            self.assertEqual(result["confidence"], 0.0)
            self.assertIn("Failed to parse", result["explanation"])
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_json_in_code_fence(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = '```json\n{"success": true, "confidence": 0.9, "explanation": "OK", "suggestion": "none"}\n```'
            result = evaluate_action(before, after, "follow person")
            self.assertTrue(result["success"])
            self.assertAlmostEqual(result["confidence"], 0.9)
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_missing_fields_get_defaults(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = '{"success": true}'
            result = evaluate_action(before, after, "servo movement")
            self.assertTrue(result["success"])
            self.assertEqual(result["confidence"], 0.0)
            self.assertEqual(result["suggestion"], "none")
            self.assertIn("No explanation", result["explanation"])
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_string_false_success_is_coerced(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = '{"success": "false", "confidence": 0.8}'
            result = evaluate_action(before, after, "servo movement")
            self.assertFalse(result["success"])
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_confidence_clamped(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = '{"success": true, "confidence": 4.2, "explanation": "ok", "suggestion": "none"}'
            result = evaluate_action(before, after, "servo movement")
            self.assertEqual(result["confidence"], 1.0)
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_api_exception_returns_structured_failure(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.side_effect = RuntimeError("timeout")
            result = evaluate_action(before, after, "follow person")
            self.assertFalse(result["success"])
            self.assertIn("failed", result["explanation"].lower())
            self.assertIn("retry", result["suggestion"].lower())
        finally:
            os.unlink(before)
            os.unlink(after)

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_legacy_evaluate_wrapper(self, mock_call):
        before, after = self._make_test_images()
        try:
            mock_call.return_value = '{"success": false, "confidence": 0.7, "explanation": "not closer", "suggestion": "retry"}'
            result = evaluate(before, after, "person_following", "closer")
            self.assertFalse(result["success"])
            self.assertEqual(result["suggestion"], "retry")
        finally:
            os.unlink(before)
            os.unlink(after)


class TestEvaluateActionCameraCompat(unittest.TestCase):
    """Test backward compatibility camera mode for evaluate_action."""

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_camera_capture_compat_mode(self, mock_call):
        mock_call.return_value = '{"success": true, "confidence": 0.9, "explanation": "moved", "suggestion": "none"}'

        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        tmpdir = Path(tempfile.mkdtemp())
        try:
            mock_cam = MagicMock()
            call_count = [0]

            def mock_capture_and_save(label):
                call_count[0] += 1
                path = tmpdir / f"{label}_{call_count[0]}.jpg"
                path.write_bytes(fake_jpeg)
                return path

            mock_cam.capture_and_save.side_effect = mock_capture_and_save

            result = evaluate_action(mock_cam, action_description="servo movement")

            self.assertTrue(result["success"])
            self.assertIn("before_path", result)
            self.assertIn("after_path", result)
            mock_cam.capture_and_save.assert_any_call("before")
            mock_cam.capture_and_save.assert_any_call("after")
            mock_cam.delay.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestEvaluateActionValidation(unittest.TestCase):
    """Test input validation for path-based API mode."""

    def test_missing_before_file_raises(self):
        with self.assertRaises(ValueError) as ctx:
            evaluate_action("/tmp/does-not-exist-before.jpg", "/tmp/does-not-exist-after.jpg", "track face")
        self.assertIn("before_image_path not found", str(ctx.exception))

    @patch("apps.test_harness.action_evaluator._call_llm")
    def test_camera_capture_legacy_positional_description(self, mock_call):
        mock_call.return_value = '{"success": true, "confidence": 0.9, "explanation": "moved", "suggestion": "none"}'

        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        tmpdir = Path(tempfile.mkdtemp())
        try:
            mock_cam = MagicMock()
            call_count = [0]

            def mock_capture_and_save(label):
                call_count[0] += 1
                path = tmpdir / f"{label}_{call_count[0]}.jpg"
                path.write_bytes(fake_jpeg)
                return path

            mock_cam.capture_and_save.side_effect = mock_capture_and_save

            result = evaluate_action(mock_cam, "track face")
            self.assertTrue(result["success"])
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestSystemPrompt(unittest.TestCase):
    """Validate the system prompt structure."""

    def test_requires_json_output(self):
        self.assertIn("JSON", SYSTEM_PROMPT)

    def test_specifies_required_fields(self):
        self.assertIn("success", SYSTEM_PROMPT)
        self.assertIn("confidence", SYSTEM_PROMPT)
        self.assertIn("explanation", SYSTEM_PROMPT)
        self.assertIn("suggestion", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
