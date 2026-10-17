import copy
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, validate_config
from project_policy import resolve, restrict, final_required


CONFIG = {"providers": [{"id": "paid", "model": "vendor/paid", "billing": "paid"},
                         {"id": "free", "model": "vendor/free", "billing": "free"},
                         {"id": "unknown", "model": "vendor/unknown"},
                         {"id": "native", "transport": "native"}],
          "project_policies": {"/project": {"no_paid_fallback": True, "allowed_providers": ["free"],
                                              "require_astra_final": True}},
          "project_scopes": {"0123456789abcdef": "/project"}}


class ProjectPolicyTests(unittest.TestCase):
    def test_off_preserves_project_restrictions_without_provider_state_or_discovered_fallbacks(self):
        policies = ({"native_only": True}, {"no_paid_fallback": True},
                    {"allowed_providers": ["free"]}, {"require_astra_final": True}, None)
        for policy in policies:
            for failure in ("healthy", "quota", "already_dry", "off_and_shadow"):
                with self.subTest(policy=policy, failure=failure), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                    payload = {"model": "jev/auto", "input": "synthetic task", "prompt_cache_key": "session"}
                    if policy and policy.get("require_astra_final"):
                        payload["metadata"] = {"jev_phase": "final_review"}
                    scope = jev.cache_scope(payload, "synthetic task")
                    config = copy.deepcopy(CONFIG)
                    config["project_policies"]["/project"] = policy or {"no_paid_fallback": True}
                    config["project_scopes"] = {scope: "/project"} if policy is not None else {}
                    ladder = Ladder(config, Path(root) / "state.json")
                    ladder.path.write_text("invalid sticky provider state")
                    for name in ("OFF_PATH", "SHADOW_PATH", "DEBUG_PATH", "DRY_STATE_PATH", "SIGNATURE_PATH"):
                        stack.enter_context(mock.patch.object(jev, name, str(Path(root) / name)))
                    Path(jev.OFF_PATH).touch()
                    if failure == "off_and_shadow":
                        Path(jev.SHADOW_PATH).touch()
                    stack.enter_context(mock.patch.object(jev, "STATE", root))
                    stack.enter_context(mock.patch.dict(jev.os.environ, {"JEV_LADDER_MODE": "active", "JEV_DISABLE_AUTO_ASTRA": "1"}))
                    stack.enter_context(mock.patch.object(jev, "_provider_ladder", return_value=ladder))
                    stack.enter_context(mock.patch.object(Ladder, "model_candidates", side_effect=AssertionError("off must not read model health")))
                    stack.enter_context(mock.patch.object(jev, "fallback_candidates", side_effect=AssertionError("project restrictions forbid discovered fallback")))
                    classifier = stack.enter_context(mock.patch.object(jev, "call_jev_routed"))
                    for name in ("route_lease", "remember_cache_model", "remember_route_lease", "log_line", "mark_native_dry"):
                        stack.enter_context(mock.patch.object(jev, name, return_value=None))
                    stack.enter_context(mock.patch.object(jev, "native_dry", return_value="manual" if failure == "already_dry" else None))
                    handler = object.__new__(jev.Handler)
                    handler.path = "/v1/responses"
                    handler._body = lambda _: copy.deepcopy(payload)
                    handler._json = mock.Mock()
                    handler.send_response = mock.Mock()
                    handler.send_header = mock.Mock()
                    handler.end_headers = mock.Mock()
                    handler.wfile = io.BytesIO()
                    handler._serve_ladder = mock.Mock()
                    handler._forward = mock.Mock(return_value=(429 if failure == "quota" else 200, "json", "application/json", failure == "quota", None, None))
                    handler._post()
                    classifier.assert_not_called()
                    handler._serve_ladder.assert_not_called()
                    if failure == "already_dry":
                        handler._forward.assert_not_called()
                        handler.send_response.assert_called_once_with(503)
                    else:
                        handler._forward.assert_called_once()
                        expected = jev.ASTRA if policy is None or policy.get("native_only") or policy.get("require_astra_final") else jev.SOL
                        self.assertEqual(handler._forward.call_args.args[0]["model"], expected)

    def test_project_health_uses_the_execution_inventory_and_fingerprint(self):
        for fixed in (False, True):
            with self.subTest(fixed=fixed), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                payload = {"model": "jev/auto", "input": "synthetic task", "prompt_cache_key": "session"}
                scope = jev.cache_scope(payload, "synthetic task")
                provider = {"id": "allowed", **({"model": "gateway/shared"} if fixed else
                    {"models": {model: "gateway/" + model for model in jev.TIERS}})}
                config = {"providers": [provider, {"id": "excluded", "model": "gateway/healthy"}],
                    "project_policies": {"/project": {"allowed_providers": ["allowed"]}},
                    "project_scopes": {scope: "/project"}}
                ladder = Ladder(config, Path(root) / "state.json")
                restricted = Ladder(restrict(ladder.config, resolve(ladder.config, scope)), ladder.path)
                restricted.advance("earlier", restricted.route("earlier", jev.SOL61, "medium"), "model_unavailable", cooldown_seconds=300)
                self.assertIn(jev.SOL61, ladder.model_candidates())
                self.assertNotIn(jev.SOL61, restricted.model_candidates())
                stack.enter_context(mock.patch.dict(jev.os.environ, {"JEV_LADDER_MODE": "active"}))
                stack.enter_context(mock.patch.object(jev.os.path, "exists", return_value=False))
                stack.enter_context(mock.patch.object(jev, "_provider_ladder", return_value=ladder))
                stack.enter_context(mock.patch.object(jev, "route_lease", return_value=None))
                stack.enter_context(mock.patch.object(jev, "load_key", return_value="synthetic"))
                classifier = stack.enter_context(mock.patch.object(jev, "call_jev_routed", return_value={"answers": {
                    "astra_policy": {"choice": "normal"}, "model": {"choice": "luna"},
                    "effort": {"choice": "low"}, "lease": {"choice": "one_call"}}}))
                handler = object.__new__(jev.Handler)
                handler.path = "/v1/responses"
                handler._body = lambda _: payload
                handler._json = mock.Mock()
                handler._serve_ladder = mock.Mock()
                handler._post()
                self.assertEqual(handler._policy_ladder.fingerprint, restricted.fingerprint)
                if fixed:
                    classifier.assert_not_called()
                    self.assertEqual(handler._json.call_args.args[1]["error"]["code"], "model_cooldown")
                else:
                    self.assertNotIn("sol61", classifier.call_args.kwargs["questions"]["model"]["criteria"])
                    handler._serve_ladder.assert_called_once()

    def test_payment_and_allowlist_filter_and_unbound_fail_closed(self):
        config = validate_config(CONFIG)
        policy = resolve(config, "0123456789abcdef")
        self.assertEqual([p["id"] for p in restrict(config, policy)["providers"]], ["free", "native"])
        self.assertEqual([p["id"] for p in restrict(config, resolve(config, "unbound"))["providers"]], ["native"])
        no_paid = restrict(config, {"no_paid_fallback": True})
        self.assertEqual([p["id"] for p in no_paid["providers"]], ["free", "native"])

    def test_final_review_has_exact_astra_or_fails_not_downgrades(self):
        config = validate_config(CONFIG)
        policy = resolve(config, "0123456789abcdef")
        self.assertTrue(final_required(policy, {"metadata": {"jev_phase": "final_review"}}, None))
        self.assertTrue(final_required(policy, {}, {"astra_policy": "astra"}))
        restricted = restrict(config, policy, True)
        self.assertEqual([p["id"] for p in restricted["providers"]], ["native"])
        restricted["providers"][0]["model"] = "gpt-6-sol"
        with self.assertRaises(ValueError):
            restrict(restricted, policy, True)

    def test_bad_policy_and_binding_rejected(self):
        for change in ({"project_scopes": {"bad": "/project"}},
                       {"project_policies": {"relative": {}}},
                       {"project_policies": {"/project": {"native_only": "true"}}},
                       {"project_policies": {"/project": {"allowed_providers": [{}]}}}):
            with self.assertRaises(ValueError):
                validate_config({**CONFIG, **change})

    def test_native_only_skips_external_jev_even_off_or_shadow(self):
        payload = {"model": "jev/auto", "input": "private task", "prompt_cache_key": "session"}
        scope = jev.cache_scope(payload, "private task")
        config = copy.deepcopy(CONFIG)
        config["project_scopes"] = {scope: "/project"}
        config["project_policies"]["/project"] = {"native_only": True}
        handler = object.__new__(jev.Handler)
        handler.path = "/v1/responses"
        handler._body = lambda _: payload
        handler._serve_ladder = mock.Mock()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(jev.os.environ, {"JEV_LADDER_MODE": "active"}), \
             mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, Path(root) / "state.json")), \
             mock.patch.object(jev, "call_jev_routed") as decision:
            handler._post()
        decision.assert_not_called()
        self.assertEqual(handler._serve_ladder.call_args.args[2], "gpt-6-astra")
