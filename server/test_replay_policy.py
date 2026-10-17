"""Replay uses the live contract and compares exactly the same supported turns."""
import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import routing_policy

spec = importlib.util.spec_from_file_location(
    "backtest_under_test", Path(__file__).resolve().parents[1] / "poc/backtest_savings.py")
backtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backtest)


class ReplayPolicy(unittest.TestCase):
    def test_classifier_errors_use_the_same_bounded_workhorse_as_production(self):
        for response in (TimeoutError("synthetic private provider detail"), {"answers": {}}):
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_session(root, "supported", routing_policy.ASTRA, 1_000_000)
                result = root / "result.json"
                output = io.StringIO()
                with mock.patch.object(backtest, "SESS_ROOT", str(root)), \
                     mock.patch.object(backtest, "RESULT_PATH", str(result)), \
                     mock.patch.object(backtest.poc, "load_key", return_value="synthetic"), \
                     mock.patch.object(backtest.poc, "post_json", **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})), \
                     mock.patch("sys.argv", ["backtest"]), contextlib.redirect_stdout(output):
                    self.assertEqual(backtest.main(), 0)
                data = json.loads(result.read_text())
                expected = routing_policy.workhorse(routing_policy.automatic_models(routing_policy.TIERS))
                self.assertEqual(data["routes"][expected]["turns"], 1)
                self.assertEqual(data["routes"][routing_policy.ASTRA]["turns"], 0)
                self.assertEqual(data["jev_usd"], backtest.cost(expected, {"input_tokens": 1_000_000}))
                self.assertNotIn("synthetic private provider detail", output.getvalue())
                # Cached failed decisions from earlier runs have no tier. They
                # must project through the same current fallback without calls.
                for route in data["routes_detail"].values():
                    route.update(tier=None, route=routing_policy.ASTRA)
                result.write_text(json.dumps(data))
                with mock.patch.object(backtest, "SESS_ROOT", str(root)), \
                     mock.patch.object(backtest, "RESULT_PATH", str(result)), \
                     mock.patch.object(backtest.poc, "post_json") as provider, \
                     mock.patch("sys.argv", ["backtest", "--from-cache"]), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(backtest.main(), 0)
                provider.assert_not_called()
                self.assertEqual(json.loads(result.read_text())["jev_usd"], data["jev_usd"])
    def write_session(self, root, name, model, volume):
        folder = root / "2026/09/20"
        folder.mkdir(parents=True, exist_ok=True)
        records = [
            {"type": "session_meta", "payload": {"cwd": "/fixture"}},
            {"type": "event_msg", "payload": {"type": "thread_settings_applied",
                                            "thread_settings": {"model": model}}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": name}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": "Explain the fixture function " + name}]}},
            {"type": "token_usage_record", "payload": {"turn_id": name, "turn_token_usage": {
                "input_tokens": volume, "cached_input_tokens": 0, "output_tokens": 0}}},
        ]
        (folder / f"{name}.jsonl").write_text("\n".join(json.dumps(r) for r in records))

    def test_joint_decision_and_equal_baseline_population(self):
        pair = routing_policy.route_choice(routing_policy.LUNA, "low")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_session(root, "supported", routing_policy.SOL, 1_000_000)
            self.write_session(root, "unsupported", "unknown-model", 1_000_000_000)
            result = root / "result.json"
            with mock.patch.object(backtest, "SESS_ROOT", str(root)), \
                 mock.patch.object(backtest, "RESULT_PATH", str(result)), \
                 mock.patch.object(backtest.poc, "load_key", return_value="fixture"), \
                 mock.patch.object(backtest.poc, "post_json", return_value={"answers": {
                     "astra_policy": {"choice": pair["astra_policy"], "confidence": 0.95},
                     "model": {"choice": pair["model"], "confidence": 0.95},
                     "effort": {"choice": pair["effort"], "confidence": 0.1},
                     "lease": {"choice": pair["lease"], "confidence": 0.1},
                 }}) as judge, \
                 mock.patch("sys.argv", ["backtest"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(backtest.main(), 0)
            self.assertEqual(judge.call_args.args[2]["questions"], routing_policy.QUESTIONS)
            data = json.loads(result.read_text())
            self.assertEqual(data["turns"], 1)
            self.assertEqual(data["skipped_turns"], 1)
            self.assertEqual(data["jev_usd"], 0.1)
            self.assertEqual(data["scenarios_usd"][routing_policy.SOL], 2.0)
            self.assertEqual(data["policy_version"], routing_policy.POLICY_VERSION)

    def test_backtest_prices_every_policy_choice_including_cache_counters(self):
        tokens = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
                  "cache_write_input_tokens": 100_000, "output_tokens": 100_000}
        for model in routing_policy.TIERS:
            with self.subTest(model=model):
                self.assertGreater(backtest.cost(model, tokens), 0)
        self.assertAlmostEqual(backtest.cost(routing_policy.SOL61, tokens), 2.1)

    def test_optional_model_sessions_and_decisions_are_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_session(root, "current", routing_policy.SOL61, 1_000_000)
            self.write_session(root, "previous", routing_policy.SOL, 1_000_000)
            result = root / "result.json"
            pair = routing_policy.route_choice(routing_policy.SOL61, "medium")
            answers = {key: {"choice": value, "confidence": 0.95} for key, value in pair.items()}
            output = io.StringIO()
            with mock.patch.object(backtest, "SESS_ROOT", str(root)), \
                 mock.patch.object(backtest, "RESULT_PATH", str(result)), \
                 mock.patch.object(backtest.poc, "load_key", return_value="fixture"), \
                 mock.patch.object(backtest.poc, "post_json", return_value={"answers": answers}), \
                 mock.patch("sys.argv", ["backtest"]), contextlib.redirect_stdout(output):
                self.assertEqual(backtest.main(), 0)
            data = json.loads(result.read_text())
            self.assertEqual(data["turns"], 2)
            self.assertEqual(data["skipped_turns"], 0)
            self.assertEqual(data["actual_models"], {routing_policy.SOL61: 1, routing_policy.SOL: 1})
            self.assertEqual(data["actual_usd"], 4.0)
            self.assertEqual(data["jev_usd"], 4.0)
            self.assertEqual(data["routes"][routing_policy.SOL61]["turns"], 2)
            self.assertEqual(data["scenarios_usd"][routing_policy.SOL61], 4.0)
            breakdown = {model: int(count) for model, count in
                         re.findall(r"^  (\S+)\s+(\d+) turns", output.getvalue(), re.M)}
            self.assertEqual(breakdown, {routing_policy.SOL61: 2})

    def test_identical_short_replies_do_not_share_different_contexts(self):
        self.assertNotEqual(backtest.turn_key({"text": "continue", "prev": "first task"}),
                            backtest.turn_key({"text": "continue", "prev": "other task"}))

    def test_turns_keep_the_model_selected_when_they_were_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_session(root, "first", routing_policy.SOL, 1_000_000)
            self.write_session(root, "second", routing_policy.LUNA, 1_000_000)
            folder = root / "2026/09/20"
            first, second = folder / "first.jsonl", folder / "second.jsonl"
            first.write_text(first.read_text() + "\n" + second.read_text())
            second.unlink()
            result = root / "result.json"
            with mock.patch.object(backtest, "SESS_ROOT", str(root)), \
                 mock.patch.object(backtest, "RESULT_PATH", str(result)), \
                 mock.patch.object(backtest.poc, "load_key", return_value="fixture"), \
                 mock.patch.object(backtest.poc, "post_json", return_value={"answers": {
                     "astra_policy": {"choice": "normal"},
                     "model": {"choice": "luna"}, "effort": {"choice": "low"},
                     "lease": {"choice": "one_call"}}}), \
                 mock.patch("sys.argv", ["backtest"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(backtest.main(), 0)
            data = json.loads(result.read_text())
            self.assertEqual(data["actual_models"],
                             {routing_policy.SOL: 1, routing_policy.LUNA: 1})
            self.assertEqual(data["actual_usd"], 2.1)

    def test_shadow_uses_live_projection_and_retains_short_confirmations(self):
        spec = importlib.util.spec_from_file_location(
            "shadow_under_test", Path(__file__).resolve().parents[1] / "poc/shadow_replay.py")
        shadow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shadow)
        items = [
            {"type": "message", "role": "user", "content": "Improve export."},
            {"type": "message", "role": "assistant", "content": "Investigate consistency."},
            {"type": "message", "role": "user", "content": "<environment_context>metadata</environment_context>"},
            {"type": "message", "role": "user", "content": "oui"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps({"type": "response_item", "payload": i})
                                      for i in items))
            turns = shadow.extract_user_turns(path)
        self.assertEqual(len(turns), 2)
        actual = shadow.build_state(turns[-1])
        self.assertEqual(actual, shadow.jev.decision_dossier({"input": items}))
        self.assertEqual(actual["previous_proposal"], "Investigate consistency.")


if __name__ == "__main__":
    unittest.main()
