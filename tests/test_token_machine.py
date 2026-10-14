import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from lofgren_intelligence.token_machine import MAX_PROMPT_BYTES, analyze_prompt


class TokenMachineTests(unittest.TestCase):
    def test_deterministic_advice_never_grants_authority(self):
        a = analyze_prompt('Fix the blocker, verify evidence, then deploy')
        self.assertEqual(a, analyze_prompt('Fix the blocker, verify evidence, then deploy'))
        self.assertEqual(a['intents'], ['research', 'diagnose', 'execute'])
        self.assertFalse(a['authority']['executes_actions'])
        self.assertFalse(a['authority']['grants_permissions'])
        self.assertIn('external_action_requires_separate_authorization', a['risk_flags'])

    def test_secrets_not_returned_and_no_io(self):
        secret = 'example-secret-123456789'
        with patch('socket.socket', side_effect=AssertionError('network')), patch('builtins.open', side_effect=AssertionError('disk')):
            result = analyze_prompt('my password is ' + secret)
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse(result['authority']['handles_oauth_credentials'])

    def test_utf8_bound_and_invalid_unicode(self):
        for prompt in ['💡' * (MAX_PROMPT_BYTES // 4 + 1), '\ud800', '', '   ', None]:
            with self.assertRaises(ValueError):
                analyze_prompt(prompt)
        self.assertEqual(analyze_prompt('💡')['context']['prompt_utf8_bytes'], 4)

    def test_budget_exhaustion_and_noninteger(self):
        for kwargs in [dict(context_window=True), dict(reserved_output=-1), dict(existing_context=8192), dict(context_window=10, reserved_output=10)]:
            with self.assertRaises(ValueError):
                analyze_prompt('research', **kwargs)
        result = analyze_prompt('research' * 100, context_window=100, reserved_output=10)
        self.assertFalse(result['context']['estimated_fit'])

    def test_tool_estimate_and_input_not_mutated(self):
        tools = [{'name': 'research', 'inputSchema': {'type': 'object'}}]
        original = json.dumps(tools)
        result = analyze_prompt('research', tool_definitions=tools)
        self.assertGreater(result['context']['tool_tokens_estimate'], 0)
        self.assertEqual(json.dumps(tools), original)
        self.assertIn('not exact', result['limitations'][0])

    def test_malformed_and_unbounded_tools(self):
        circular = {}; circular['loop'] = circular
        for tools in [[{}] * 65, [None], [{'v': float('nan')}], [{'v': 'x' * 131073}], [circular], [{'v': 2 ** 1000}]]:
            with self.assertRaises(ValueError):
                analyze_prompt('research', tool_definitions=tools)

    def test_high_stakes_and_ambiguous(self):
        self.assertTrue(analyze_prompt('legal research')['risk_flags'])
        self.assertEqual(analyze_prompt('hello')['intents'], ['clarify'])
        self.assertTrue(analyze_prompt('hello')['clarification_questions'])

    def test_cli_does_not_echo_credentials(self):
        run = subprocess.run([sys.executable, '-m', 'lofgren_intelligence.cli', 'token-machine'], input=b'password example-SECRET-123', capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn(b'example-SECRET-123', run.stdout + run.stderr)
        self.assertEqual(json.loads(run.stdout)['schema'], 'li.token-machine/1')

    def test_cli_rejects_invalid_and_oversized_input(self):
        for data in [b'\xff', b'x' * (MAX_PROMPT_BYTES + 1)]:
            run = subprocess.run([sys.executable, '-m', 'lofgren_intelligence.cli', 'token-machine'], input=data, capture_output=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stdout, b'')
