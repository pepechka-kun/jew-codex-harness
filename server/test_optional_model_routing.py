"""Optional model tiers and exact routing choices."""
import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import jev_server as jev
from provider_ladder import Ladder, NATIVE_MODELS, REQUIRED_NATIVE_MODELS, validate_config
from routing_policy import (ASTRA, MODEL_IDS, QUESTIONS, SOL61, decision_from_answers,
                            automatic_models, workhorse, questions_for_models, route_choice)


class OptionalModelRouting(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state.json'

    def test_existing_maps_remain_valid_and_do_not_offer_unmapped_choices(self):
        config = {'providers': [{'id': 'gateway', 'models': {
            model: 'gateway/' + model for model in REQUIRED_NATIVE_MODELS}}]}
        ladder = Ladder(config, self.state)
        self.assertEqual(ladder.available_models(), REQUIRED_NATIVE_MODELS)
        for model in REQUIRED_NATIVE_MODELS:
            self.assertEqual(ladder.route('scope', model, 'high')['model'], 'gateway/' + model)
        with self.assertRaises(ValueError):
            ladder.route('scope', SOL61, 'high')

    def test_optional_choice_preserves_exact_model_and_requires_fallback_coverage(self):
        config = {'providers': [{'id': 'gateway', 'models': {
            model: 'gateway/' + model for model in NATIVE_MODELS}}]}
        ladder = Ladder(config, self.state)
        self.assertIn(SOL61, ladder.available_models())
        self.assertEqual(ladder.route('scope', SOL61, 'max')['model'], 'gateway/' + SOL61)
        config['providers'].append({'id': 'fallback', 'models': {
            model: 'fallback/' + model for model in REQUIRED_NATIVE_MODELS}})
        self.assertNotIn(SOL61, Ladder(config, self.state).available_models())
        config['providers'][1] = {'id': 'fallback', 'transport': 'native'}
        self.assertIn(SOL61, Ladder(config, self.state).available_models())
        config['providers'][0]['models']['unknown-model'] = 'gateway/unknown'
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_auto_replaces_legacy_sol_but_explicit_routes_remain_valid(self):
        self.assertNotIn("gpt-6-sol", automatic_models(NATIVE_MODELS))
        self.assertIn("gpt-6-sol", automatic_models(REQUIRED_NATIVE_MODELS))
        self.assertEqual(workhorse(NATIVE_MODELS), SOL61)
        self.assertEqual(workhorse(REQUIRED_NATIVE_MODELS), "gpt-6-sol")
        self.assertNotEqual(workhorse(("gpt-6-luna", ASTRA)), ASTRA)
        with self.assertRaises(ValueError):
            workhorse((ASTRA,))

    def test_known_unavailable_tier_is_not_offered_again_without_a_healthy_configured_destination(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as root:
                providers = [{'id': 'primary', 'models': {m: 'gateway/' + m for m in NATIVE_MODELS}}]
                if fallback:
                    providers.append({'id': 'reserve', 'models': {m: 'reserve/' + m for m in NATIVE_MODELS}})
                ladder = Ladder({'providers': providers}, Path(root) / 'state.json')
                with mock.patch('provider_ladder.time.time', return_value=1000):
                    route = ladder.route('prior', SOL61, 'high')
                    ladder.advance('prior', route, 'model_unavailable', cooldown_seconds=300)
                    self.assertEqual(SOL61 in ladder.model_candidates(), fallback)
                    # Structural/manual mappings are preserved regardless of live cooldowns.
                    self.assertIn(SOL61, ladder.available_models())
                    handler = object.__new__(jev.Handler)
                    handler.path = '/v1/responses'
                    handler._body = lambda _: {'model': 'jev/auto', 'input': 'bounded fixture',
                                               'prompt_cache_key': 'fixture'}
                    handler._serve_ladder = mock.Mock()
                    result = {'answers': {key: {'choice': value, 'confidence': 0.99}
                                          for key, value in route_choice('gpt-6-luna', 'low').items()}}
                    with mock.patch.dict(os.environ, {'JEV_LADDER_MODE': 'active'}), \
                         mock.patch.object(jev, '_provider_ladder', return_value=ladder), \
                         mock.patch.object(jev, 'load_key', return_value='fixture'), \
                         mock.patch.object(jev, 'route_lease', return_value=None), \
                         mock.patch.object(jev.os.path, 'exists', return_value=False), \
                         mock.patch.object(jev, 'call_jev_routed', return_value=result) as classify:
                        handler._post()
                    offered = classify.call_args.kwargs['questions']['model']['criteria']
                    self.assertEqual('sol61' in offered, fallback)
                    self.assertEqual(handler._serve_ladder.call_args.args[2], 'gpt-6-luna')
                with mock.patch('provider_ladder.time.time', return_value=1301):
                    self.assertIn(SOL61, ladder.model_candidates())

    def test_all_cooled_destinations_fail_before_paying_for_another_classifier_call(self):
        ladder = Ladder({'providers': [{'id': 'primary', 'model': 'gateway/fixed'}]}, self.state)
        with mock.patch('provider_ladder.time.time', return_value=1000):
            route = ladder.route('prior', SOL61, 'high')
            ladder.advance('prior', route, 'model_unavailable', cooldown_seconds=300)
            handler = object.__new__(jev.Handler)
            handler.path = '/v1/responses'
            handler._body = lambda _: {'model': 'jev/auto', 'input': 'fixture'}
            handler._serve_ladder = mock.Mock()
            handler._json = mock.Mock()
            with mock.patch.dict(os.environ, {'JEV_LADDER_MODE': 'active'}), \
                 mock.patch.object(jev, '_provider_ladder', return_value=ladder), \
                 mock.patch.object(jev, 'route_lease', return_value=None), \
                 mock.patch.object(jev, 'call_jev_routed') as classify:
                handler._post()
            classify.assert_not_called()
            handler._serve_ladder.assert_not_called()
            self.assertEqual(handler._json.call_args.args[0], 503)
            self.assertEqual(handler._json.call_args.args[1]['error']['code'], 'model_cooldown')

    def test_kill_switch_bypasses_unhealthy_or_unreadable_provider_ladders(self):
        for unavailable in ('cooldown', 'state_error'):
            for disable_astra in ('0', '1'):
                with self.subTest(unavailable=unavailable, disable_astra=disable_astra), \
                     tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                    for name in ('OFF_PATH', 'SHADOW_PATH', 'DEBUG_PATH', 'DRY_STATE_PATH', 'SIGNATURE_PATH'):
                        stack.enter_context(mock.patch.object(jev, name, str(Path(directory) / name)))
                    Path(jev.OFF_PATH).touch()
                    stack.enter_context(mock.patch.object(jev, 'STATE', directory))
                    stack.enter_context(mock.patch.dict(os.environ, {'JEV_LADDER_MODE': 'active',
                                         'JEV_DISABLE_AUTO_ASTRA': disable_astra}))
                    ladder = Ladder({'providers': [{'id': 'primary', 'model': 'gateway/fixed'}]},
                                    Path(directory) / 'state.json')
                    ladder.advance('prior', ladder.route('prior', SOL61, 'medium'),
                                   'model_unavailable', cooldown_seconds=300)
                    self.assertEqual(ladder.model_candidates(), ())
                    loader = stack.enter_context(mock.patch.object(jev, '_provider_ladder',
                        side_effect=ValueError('unavailable state') if unavailable == 'state_error' else None,
                        return_value=ladder))
                    classifier = stack.enter_context(mock.patch.object(jev, 'call_jev_routed'))
                    for name in ('log_line', 'remember_cache_model', 'remember_route_lease', 'route_lease', 'native_dry'):
                        stack.enter_context(mock.patch.object(jev, name, return_value=None))
                    stack.enter_context(mock.patch.object(jev, 'load_key', side_effect=AssertionError('Jev must stay off')))
                    handler = object.__new__(jev.Handler)
                    handler.path = '/v1/responses'
                    handler._body = lambda _: {'model': 'jev/auto', 'input': 'synthetic',
                                               'prompt_cache_key': 'synthetic-scope'}
                    handler._json = mock.Mock()
                    handler._serve_ladder = mock.Mock()
                    handler._forward = mock.Mock(return_value=(200, 'json', 'application/json', False, None, None))
                    handler._post()
                    loader.assert_not_called()
                    classifier.assert_not_called()
                    handler._json.assert_not_called()
                    handler._serve_ladder.assert_not_called()
                    expected = jev.SOL if disable_astra == '1' else ASTRA
                    self.assertEqual(handler._forward.call_args.args[0]['model'], expected)

    def test_classifier_failure_never_escalates_to_the_only_healthy_frontier_model(self):
        for key in (None, 'fixture'):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as root:
                config = {'providers': [{'id': 'primary', 'models': {
                    m: 'gateway/' + m for m in NATIVE_MODELS}}]}
                ladder = Ladder(config, Path(root) / 'state.json')
                with mock.patch('provider_ladder.time.time', return_value=1000):
                    for model in NATIVE_MODELS:
                        if model != ASTRA:
                            route = ladder.route('prior-' + model, model, 'low')
                            ladder.advance('prior-' + model, route, 'model_unavailable', cooldown_seconds=300)
                    self.assertEqual(ladder.model_candidates(), (ASTRA,))
                    handler = object.__new__(jev.Handler)
                    handler.path = '/v1/responses'
                    handler._body = lambda _: {'model': 'jev/auto', 'input': 'fixture'}
                    handler._serve_ladder = mock.Mock()
                    handler._json = mock.Mock()
                    with mock.patch.dict(os.environ, {'JEV_LADDER_MODE': 'active'}), \
                         mock.patch.object(jev, '_provider_ladder', return_value=ladder), \
                         mock.patch.object(jev, 'route_lease', return_value=None), \
                         mock.patch.object(jev, 'load_key', return_value=key), \
                         mock.patch.object(jev.os.path, 'exists', return_value=False), \
                         mock.patch.object(jev, 'log_line') as logged, \
                         mock.patch.object(jev, 'call_jev_routed', side_effect=TimeoutError('fixture-private')):
                        handler._post()
                    handler._serve_ladder.assert_not_called()
                    self.assertEqual(handler._json.call_args.args[0], 503)
                    self.assertEqual(handler._json.call_args.args[1]['error']['code'], 'model_cooldown')
                    if key:
                        self.assertEqual(logged.call_args.args[0]['jev_error_reason'], 'timeout')
                        self.assertNotIn('fixture-private', str(logged.call_args))

    def test_ladder_receives_bounded_failure_reason_instead_of_frontier_escalation(self):
        for models, expected in ((NATIVE_MODELS, SOL61), (REQUIRED_NATIVE_MODELS, "gpt-6-sol")):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in models}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": "jev/auto", "input": "read fixture", "prompt_cache_key": "fixture-bounded"}
            handler._serve_ladder = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "load_key", return_value="fixture-key"), \
                 mock.patch.object(jev, "route_lease", return_value=None), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed", side_effect=TimeoutError("private fixture")):
                handler._post()
            self.assertEqual(handler._serve_ladder.call_args.args[2:4], (expected, "medium"))
            self.assertEqual(handler._serve_ladder.call_args.kwargs["routing_gate"], "jev_error:TimeoutError")

    def test_explicit_models_use_the_ladder_without_classification_or_a_cache_key(self):
        for requested in (SOL61, "gpt-6-sol", ASTRA):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in NATIVE_MODELS}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": requested, "input": "bounded fixture", "reasoning": {"effort": "low"}}
            handler._serve_ladder = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "route_lease", return_value={"model": "gpt-6-luna"}), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed") as classify:
                handler._post()
            classify.assert_not_called()
            self.assertEqual(handler._serve_ladder.call_args.args[2:4], (requested, "low"))
            self.assertEqual(handler._serve_ladder.call_args.args[-1], "manual")
            self.assertEqual(handler._serve_ladder.call_args.kwargs["routing_gate"], "manual")

    def test_unmapped_fixed_models_and_invalid_effort_fail_without_substitution(self):
        for requested, effort in ((SOL61, "low"), ("gpt-6-sol", "invalid")):
            config = {"providers": [{"id": "gateway", "models": {m: "gateway/" + m for m in REQUIRED_NATIVE_MODELS}}]}
            handler = object.__new__(jev.Handler)
            handler.path = "/v1/responses"
            handler._body = lambda _: {"model": requested, "input": "fixture", "reasoning": {"effort": effort}}
            handler._serve_ladder = mock.Mock()
            handler._json = mock.Mock()
            with mock.patch.dict(os.environ, {"JEV_LADDER_MODE": "active"}), \
                 mock.patch.object(jev, "_provider_ladder", return_value=Ladder(config, self.state)), \
                 mock.patch.object(jev, "route_lease", return_value=None), \
                 mock.patch.object(jev.os.path, "exists", return_value=False), \
                 mock.patch.object(jev, "call_jev_routed") as classify:
                handler._post()
            self.assertEqual(handler._json.call_args.args[0], 400)
            handler._serve_ladder.assert_not_called()
            classify.assert_not_called()

    def test_choice_distribution_is_validated_against_the_offered_set(self):
        original = copy.deepcopy(QUESTIONS)
        for models in (REQUIRED_NATIVE_MODELS, NATIVE_MODELS):
            questions = questions_for_models(models)
            choices = questions['model']['criteria']
            self.assertEqual({MODEL_IDS[key] for key in choices}, set(automatic_models(models)))
            selected = SOL61 if SOL61 in models else REQUIRED_NATIVE_MODELS[0]
            pairs = route_choice(selected, 'high')
            answer = {key: {'choice': value, 'confidence': 0.99} for key, value in pairs.items()}
            answer['model']['probabilities'] = {key: float(key == pairs['model']) for key in choices}
            self.assertEqual(decision_from_answers(answer, models=models)['model'], selected)
            answer['astra_policy']['choice'] = 'astra'
            self.assertEqual(decision_from_answers(answer, models=models)['model'], ASTRA)
        self.assertNotIn('sol61', questions_for_models(REQUIRED_NATIVE_MODELS)['model']['instructions'])
        with self.assertRaises(ValueError):
            decision_from_answers({key: {'choice': value} for key, value in
                                   route_choice(SOL61, 'high').items()}, models=REQUIRED_NATIVE_MODELS)
        self.assertEqual(QUESTIONS, original)


if __name__ == '__main__':
    unittest.main()
