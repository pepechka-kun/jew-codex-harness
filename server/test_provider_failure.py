import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_failure import classify_failure
from provider_ladder import Ladder


CONFIG = {"providers": [{"id": "first", "model": "vendor/a", "connection_ids": ["a1", "a2"]},
                         {"id": "second", "model": "vendor/b"},
                         {"id": "native", "transport": "native"}]}


class FailureTests(unittest.TestCase):
    def test_explicit_model_refusals_are_distinct_from_bad_payloads_without_echoing_details(self):
        for model in ("vendor-model", "alternate-model"):
            detail = f"The '{model}' model is not supported when using Codex with a ChatGPT account."
            for body in ({"error": {"message": detail}},
                         {"detail": detail},
                         {"error": {"message": "Upstream error: 400", "code": "bad_request"},
                          "upstream_details": {"detail": detail}, "private": "fixture-private"}):
                with self.subTest(model=model, body=list(body)):
                    failure = classify_failure(400, body=json.dumps(body).encode())
                    self.assertEqual(failure, {"class": "model_account_unavailable", "cooldown_seconds": 300,
                        "retryable": True, "reason": "model_not_supported_for_account"})
                    self.assertNotIn(model, str(failure))
                    self.assertNotIn("fixture-private", str(failure))
        for code in ("model_not_found", "model_not_supported", "unsupported_model"):
            self.assertEqual(classify_failure(400, body=json.dumps({"error": {"code": code}}))[
                "reason"], "model_unavailable")
        for body in (b'not json', b'[]', b'{"upstream_details":"invalid"}',
                     b'{"error":{"message":"Unsupported parameter: reasoning"}}',
                     b'{"error":{"message":"user quoted: model is not supported"}}'):
            self.assertEqual(classify_failure(400, body=body)["class"], "bad_request")

    def test_rejected_model_uses_only_configured_fallback_and_keeps_safe_evidence(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as root:
                providers = [{"id": "first", "model": "vendor/a"}]
                if fallback:
                    providers.append({"id": "second", "model": "vendor/b"})
                ladder = Ladder({"providers": providers}, Path(root) / "state.json")
                handler = object.__new__(jev.Handler)
                handler.wfile = io.BytesIO()
                handler.send_response = mock.Mock()
                handler.send_header = mock.Mock()
                handler.end_headers = mock.Mock()
                body = json.dumps({"error": {"message": "Upstream error: 400"},
                    "upstream_details": {"detail": "The 'vendor/a' model is not supported when using Codex with a ChatGPT account."},
                    "private": "fixture-private"}).encode()
                calls = []
                def forward(payload, *args, **kwargs):
                    calls.append(payload["model"])
                    if payload["model"] == "vendor/a":
                        handler._attempts.append({"status": 400, "completion": "http_error"})
                        return 400, "json", "application/json", False, body, None
                    handler._attempts.append({"status": 200, "terminal_type": "response.completed"})
                    return 200, "sse", "text/event-stream", False, None, None
                handler._forward = forward
                with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                     mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                     mock.patch.object(jev, "remember_cache_model"), \
                     mock.patch.object(jev, "remember_route_lease"), \
                     mock.patch.object(jev, "log_line") as log:
                    handler._serve_ladder({"input": "fixture"}, "scope", "gpt-6-sol", "high",
                                          None, {"step_type": "user_turn"}, None, True, False)
                self.assertEqual(calls, ["vendor/a", "vendor/b"] if fallback else ["vendor/a"])
                self.assertEqual(handler._attempts[0]["provider_error_stage"], "upstream_response")
                self.assertEqual(handler._attempts[0]["provider_error_reason"], "model_not_supported_for_account")
                self.assertNotIn("fixture-private", str(log.call_args_list))
                if not fallback:
                    error = json.loads(handler.wfile.getvalue())["error"]
                    self.assertEqual(error["code"], "model_not_supported_for_account")
                    self.assertNotIn("fixture-private", str(error))
                    handler.send_response.assert_called_once_with(400)

    def test_model_cooldown_does_not_disable_other_models_and_expires(self):
        with tempfile.TemporaryDirectory() as root:
            models = {model: "vendor/" + model for model in (
                "gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-astra")}
            config = {"providers": [{"id": "first", "models": models},
                                     {"id": "second", "model": "vendor/fallback"}]}
            ladder = Ladder(config, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "model_unavailable", cooldown_seconds=300)
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "high")["stage"], "second")
                self.assertEqual(ladder.route("other", "gpt-6-luna", "low")["stage"], "first")
                self.assertEqual(ladder.route("scope", "gpt-6-luna", "low")["stage"], "first")
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["stage"], "second")
            with mock.patch("provider_ladder.time.time", return_value=1301):
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "high")["stage"], "first")

    def test_account_model_refusal_keeps_other_accounts_and_other_models_usable(self):
        with tempfile.TemporaryDirectory() as root:
            models = {model: "vendor/" + model for model in (
                "gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-astra")}
            config = {"providers": [{"id": "first", "models": models, "connection_ids": ["a1", "a2"]}]}
            ladder = Ladder(config, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "model_account_unavailable", cooldown_seconds=300)
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["account"], "a2")
                self.assertIn("gpt-6-sol", ladder.model_candidates())
                self.assertEqual(ladder.route("other", "gpt-6-luna", "low")["account"], "a1")
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "model_account_unavailable", cooldown_seconds=300)
                self.assertNotIn("gpt-6-sol", ladder.model_candidates())
                self.assertEqual(ladder.route("scope", "gpt-6-luna", "low")["account"], "a1")

    def test_account_refusal_crosses_http_relay_without_exposing_it_before_fallback(self):
        seen = []
        class Response:
            headers = {}
            def __init__(self, model):
                self.status = 400 if model == "vendor/a" else 200
            def getheader(self, name):
                return "application/json" if name == "Content-Type" else None
            def read(self, *args):
                if self.status == 400:
                    return json.dumps({"error": {"message": "Upstream error: 400", "code": "bad_request"},
                        "upstream_details": {"detail": "The 'vendor/a' model is not supported when using Codex with a ChatGPT account."},
                        "private": "fixture-private"}).encode()
                return b'{"id":"fixture","status":"completed","output":[]}'
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, body, headers):
                payload = json.loads(body)
                self.model = payload["model"]
                seen.append((self.model, payload["input"]))
            def getresponse(self): return Response(self.model)
            def close(self): pass
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder({"providers": [{"id": "first", "model": "vendor/a"},
                                             {"id": "second", "model": "vendor/b"}]}, Path(root) / "state.json")
            handler = object.__new__(jev.Handler)
            handler._response_started = False
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev.http.client, "HTTPConnection", Connection), \
                 mock.patch.object(jev, "remember_cache_model"), \
                 mock.patch.object(jev, "remember_route_lease"), \
                 mock.patch.object(jev, "log_line") as logged:
                handler._serve_ladder({"input": "canonical fixture"}, "scope", "gpt-6-sol", "high",
                                      None, {"step_type": "user_turn"}, None, False, False)
            self.assertEqual(seen, [("vendor/a", "canonical fixture"), ("vendor/b", "canonical fixture")])
            handler.send_response.assert_called_once_with(200)
            self.assertEqual(json.loads(handler.wfile.getvalue())["status"], "completed")
            self.assertEqual(handler._attempts[0]["provider_error_reason"], "model_not_supported_for_account")
            self.assertNotIn("fixture-private", str(logged.call_args_list))

    def test_classes_do_not_echo_bodies_and_bad_request_is_not_retryable(self):
        for status, category in ((400, "bad_request"), (422, "bad_request"), (401, "auth"),
                                 (403, "auth"), (402, "quota"), (429, "quota"),
                                 (404, "model_unavailable"), (503, "unavailable"), (504, "timeout")):
            row = classify_failure(status, body=b'{"error":{"message":"private-body"}}')
            self.assertEqual(row["class"], category)
            self.assertNotIn("private", str(row))
        self.assertFalse(classify_failure(400)["retryable"])
        self.assertEqual(classify_failure(429, body=b'{"error":{"code":"rate_limit"}}')["class"], "rate_limit")
        self.assertFalse(classify_failure(504, attempt={"timeout_phase": "total"})["retryable"])

    def test_account_auth_cooldown_is_shared_but_other_account_and_route_stay_healthy(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                first = ladder.route("one", "gpt-6-sol", "high")
                ladder.advance("one", first, "auth", cooldown_seconds=900)
                other = ladder.route("other", "gpt-6-sol", "low")
                self.assertEqual(other["account"], "a2")
                ladder.advance("other", other, "unavailable", cooldown_seconds=30)
                route = ladder.route("fresh", "gpt-6-sol", "low")
                self.assertEqual(route["stage"], "second")
            with mock.patch("provider_ladder.time.time", return_value=1031):
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "low")["account"], "a2")
            with mock.patch("provider_ladder.time.time", return_value=1901):
                self.assertEqual(ladder.route("new", "gpt-6-sol", "low")["account"], "a1")

    def test_bad_payload_does_not_exhaust_or_retry_healthy_accounts(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            handler = object.__new__(jev.Handler)
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            def forward(*args, **kwargs):
                handler._attempts.append({"status": 400, "completion": "http_error"})
                return 400, "json", "application/json", False, b'{"error":{"message":"bad payload"}}', None
            handler._forward = mock.Mock(side_effect=forward)
            with mock.patch.object(jev, "_provider_ladder", return_value=ladder), \
                 mock.patch.object(jev, "_omniroute_key", return_value="fixture"), \
                 mock.patch.object(jev, "log_line"):
                handler._serve_ladder({"input": "fixture"}, "scope", "gpt-6-sol", "high",
                                      None, {"step_type": "user_turn"}, None, True, False)
            self.assertEqual(handler._forward.call_count, 1)
            self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["account"], "a1")
            self.assertEqual(handler._attempts[0]["failure_class"], "bad_request")

    def test_exhausted_thread_can_recover_secondary_before_primary(self):
        with tempfile.TemporaryDirectory() as root:
            config = {"providers": [{"id": "first", "model": "a"}, {"id": "second", "model": "b"}]}
            ladder = Ladder(config, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "auth", cooldown_seconds=900)
                route = ladder.route("scope", "gpt-6-sol", "high")
                ladder.advance("scope", route, "timeout", cooldown_seconds=30)
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["stage"], "exhausted")
            with mock.patch("provider_ladder.time.time", return_value=1031):
                self.assertEqual(ladder.route("scope", "gpt-6-sol", "high")["stage"], "second")

    def test_missing_model_skips_all_accounts_not_other_destination(self):
        with tempfile.TemporaryDirectory() as root:
            ladder = Ladder(CONFIG, Path(root) / "state.json")
            with mock.patch("provider_ladder.time.time", return_value=1000):
                route = ladder.route("one", "gpt-6-sol", "high")
                ladder.advance("one", route, "model_unavailable", cooldown_seconds=300)
                self.assertEqual(ladder.route("fresh", "gpt-6-sol", "high")["stage"], "second")
