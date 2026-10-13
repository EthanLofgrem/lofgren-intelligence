import unittest
from lofgren_intelligence.mcp.server import Server, ToolError, TOOLS

class TokenMachineMCPTests(unittest.TestCase):
    def test_offline_analysis_is_registered_and_discloses_no_prompt(self):
        tool = next(t for t in TOOLS if t['name'] == 'li_analyze_context')
        self.assertTrue(tool['annotations']['readOnlyHint'])
        result = Server().handlers['li_analyze_context']({'prompt':'Research a source and fix a blocker.'})
        self.assertEqual(result['structured']['schema'],'li.token-machine/1')
        self.assertFalse(result['structured']['authority']['executes_actions'])
        self.assertNotIn('Research a source',result['text'])
    def test_invalid_budget_and_extra_authority_args_fail_closed(self):
        for args in ({'prompt':'hi','context_window':True},{'prompt':'hi','approve':True},{'prompt':'hi','existing_context':8192},{'prompt':'secret','reserved_output':float('nan')}):
            with self.assertRaises(ToolError):
                Server().handlers['li_analyze_context'](args)
