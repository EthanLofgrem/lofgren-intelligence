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

    def test_catalog_estimate_is_explicit_and_never_claims_host_loading(self):
        result = Server().handlers['li_analyze_context']({'prompt':'Research a source','include_server_catalog':True})['structured']
        self.assertEqual(result['context']['supplied_tool_count'],len(TOOLS))
        self.assertGreater(result['context']['tool_tokens_estimate'],0)
        self.assertEqual(result['context']['catalog_scope'],'server_advertised_not_host_loaded')
        with self.assertRaises(ToolError):
            Server().handlers['li_analyze_context']({'prompt':'hi','include_server_catalog':1})
