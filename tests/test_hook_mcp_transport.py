"""Installer-owned lifecycle transport preserves private launch context."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from harness import core

REPO = Path(__file__).resolve().parents[1]


class HookTransportTests(unittest.TestCase):
    def test_config_forwards_only_named_context_to_mcp_without_changing_shell_policy(self):
        source = 'model="fixture"\n[shell_environment_policy]\ninherit="none"\n'
        result = core.merge_config(source, REPO)
        parsed = tomllib.loads(result)
        self.assertEqual(parsed['shell_environment_policy'], {'inherit': 'none', 'exclude': ['CANVASTTY_RUNTIME_CAPABILITY']})
        self.assertEqual(parsed['mcp_servers']['jev-workers'], core._worker_config(REPO))
        self.assertNotIn('synthetic-capability', result)
        self.assertTrue(parsed['mcp_servers']['jev-workers']['required'])

    def test_private_exclusion_preserves_existing_policy_and_multiline_patterns(self):
        source = """[shell_environment_policy]
inherit = "core"
exclude = [
  "OTHER_*", # keep the existing rule
  "bracket]pattern",
]
include_only = ["PATH", "HOME"]
[shell_environment_policy.set]
PUBLIC_FIXTURE = "value"
"""
        result = core.merge_config(source, REPO)
        policy = tomllib.loads(result)['shell_environment_policy']
        self.assertEqual(policy['inherit'], 'core')
        self.assertEqual(policy['include_only'], ['PATH', 'HOME'])
        self.assertEqual(policy['set'], {'PUBLIC_FIXTURE': 'value'})
        self.assertEqual(policy['exclude'], ['OTHER_*', 'bracket]pattern', 'CANVASTTY_RUNTIME_CAPABILITY'])
        self.assertEqual(core.merge_config(result, REPO), result)

    def test_exact_legacy_mcp_is_upgraded_and_foreign_fields_are_preserved_or_refused(self):
        for key in ('jev-workers', '"jev-workers"'):
            source = 'model="fixture"\n[mcp_servers.' + key + ']\ncommand=' + json.dumps(sys.executable)
            source += '\nargs=' + json.dumps([str(REPO / 'integrations/workers/mcp.py')])
            source += '\n[mcp_servers.other]\ncommand="other"\n'
            result = core.merge_config(source, REPO)
            self.assertEqual(tomllib.loads(result)['mcp_servers']['jev-workers'], core._worker_config(REPO))
            self.assertIn('[mcp_servers.other]\ncommand="other"\n', result)
            self.assertEqual(core.merge_config(result, REPO), result)
            with self.assertRaisesRegex(core.InstallError, 'migration'):
                core.merge_config(source.replace('[mcp_servers.other]', 'env_vars=["FOREIGN"]\n[mcp_servers.other]'), REPO)

    def test_only_exact_owned_command_hooks_are_migrated_to_mcp(self):
        foreign = {'hooks': [{'type': 'command', 'command': 'foreign'}]}
        old = {'hooks': core._legacy_hook_entries(REPO)}
        old['hooks']['PreToolUse'].insert(0, foreign)
        result = core.merge_hooks(old, REPO)
        self.assertEqual(result['hooks']['PreToolUse'][0], foreign)
        self.assertEqual(core.merge_hooks(result, REPO), result)
        for event, rows in core._hook_entries(REPO).items():
            handler = rows[0]['hooks'][0]
            self.assertEqual(handler['server'], 'jev-workers')
            self.assertEqual(handler['type'], 'mcp_tool')
            self.assertEqual(handler['input']['event']['hook_event_name'], '${hook_event_name}')
            self.assertIn(rows[0], result['hooks'][event])
            self.assertNotIn('command', handler)
        old = {'hooks': core._legacy_hook_entries(REPO)}
        old['hooks']['PreToolUse'][0]['hooks'][0]['timeout'] += 1
        with self.assertRaisesRegex(core.InstallError, 'migration'):
            core.merge_hooks(old, REPO)
        modern = {'hooks': core._hook_entries(REPO)}
        modern['hooks']['PreToolUse'][0]['hooks'][0]['timeout'] += 1
        with self.assertRaisesRegex(core.InstallError, 'migration'):
            core.merge_hooks(modern, REPO)

    def test_dispatch_checks_thread_binding_preserves_values_and_denies_handler_errors(self):
        path = REPO / 'integrations/jev/global/hook_dispatch.py'
        spec = importlib.util.spec_from_file_location('hook_dispatch_test', path)
        adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
        handler = mock.Mock(return_value={'hookSpecificOutput': {'permissionDecision': 'deny'}})
        deny = mock.Mock(return_value={'denied': True})
        adapter._hook = mock.Mock(HANDLERS={'PreToolUse': handler}, deny=deny)
        event = {'session_id': 'fixture-thread', 'hook_event_name': 'PreToolUse',
                 'tool_input': {'count': 3, 'flags': [False, None]}}
        result = adapter.dispatch(event, {'threadId': 'fixture-thread'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')
        handler.assert_called_once_with(event)
        for metadata in (None, {}, {'threadId': 'other-thread'}):
            with self.assertRaises(ValueError): adapter.dispatch(event, metadata)
        handler.side_effect = RuntimeError('private capability material')
        self.assertEqual(adapter.dispatch(event, {'threadId': 'fixture-thread'}), {'denied': True})
        self.assertNotIn('private', str(deny.call_args))
