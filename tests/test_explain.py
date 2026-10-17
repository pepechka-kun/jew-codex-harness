import json
from pathlib import Path
import tempfile
import unittest
from harness import explain


class ExplainTests(unittest.TestCase):
    def test_latest_scope_and_privacy_with_partial_log(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "codex-router/jev-router-live.jsonl"
            path.parent.mkdir()
            rows = [{"cache_scope": "first", "model": "chosen", "prompt": "secret-prompt",
                     "attempts": [{"model": "sent", "response_model": "reported", "requested_effort": "high",
                                   "effective_effort": "low", "key": "secret-key", "body": "secret-body"}]},
                    {"cache_scope": "last", "model": "last-model", "attempts": []}]
            path.write_text("\n".join(json.dumps(row) for row in rows) + '\n{"partial":')
            self.assertEqual(explain.latest(root)["model"], "last-model")
            report = explain.latest(root, "first")
            self.assertEqual(report["observed_model"], "reported")
            self.assertEqual(report["reasoning_enforcement"], "unknown")
            self.assertNotIn("secret", json.dumps(report))
            self.assertFalse(explain.latest(root, "absent")["found"])
            path.write_text('{"model":"fixture","attempts":null}\n')
            self.assertEqual(explain.latest(root)["attempts"], [])

    def test_jev_diagnostic_codes_are_included_without_raw_failure_data(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "codex-router/jev-router-live.jsonl"
            path.parent.mkdir()
            details = {"jev_error_stage":"transport", "jev_error_reason":"http_error",
                       "jev_error_http_status":429, "jev_error_field":None}
            path.write_text(json.dumps({"model":"fixture", **details,
                "exception":"private-key", "provider_body":"private-prompt", "traceback":"private-trace"}) + "\n")
            report = explain.latest(root)
            self.assertEqual({k:report[k] for k in details}, details)
            self.assertNotIn("private", json.dumps(report))

    def test_missing_log_does_not_create_profile(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(explain.latest(Path(root) / "absent")["found"])
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_timing_exports_only_known_phases_and_numeric_measurements(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "codex-router/jev-router-live.jsonl"
            path.parent.mkdir()
            path.write_text(json.dumps({"model": "fixture", "jev_timing": {
                "phase": "reading_body", "transport": "node", "observed": True,
                "elapsed_ms": 2001, "headers_at_ms": 120, "startup_ms": "private-key",
                "body_done_at_ms": float("nan"), "url": "private-url", "body": "private-body"}}) + "\n")
            report = explain.latest(root)
            self.assertEqual(report["jev_timing"], {"phase": "reading_body", "transport": "node",
                "observed": True, "elapsed_ms": 2001, "headers_at_ms": 120})
            self.assertNotIn("private", json.dumps(report))

    def test_explicit_provider_model_refusal_remains_distinct_from_a_jev_decision_error(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "codex-router/jev-router-live.jsonl"
            path.parent.mkdir()
            path.write_text(json.dumps({"attempts": [{"status": 400,
                "failure_class": "model_unavailable", "provider_error_stage": "upstream_response",
                "provider_error_reason": "model_not_supported_for_account",
                "upstream_details": "fixture-private", "body": "fixture-private"}]}) + "\n")
            report = explain.latest(root)
            self.assertEqual(report["attempts"][0]["provider_error_stage"], "upstream_response")
            self.assertEqual(report["attempts"][0]["provider_error_reason"], "model_not_supported_for_account")
            self.assertNotIn("fixture-private", json.dumps(report))
