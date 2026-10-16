from pathlib import Path
import unittest


WORKFLOW = (Path(__file__).resolve().parents[1]
            / '.github' / 'workflows' / 'hosted-readiness.yml')


class HostedReadinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding='utf-8')

    def test_dispatch_pins_workflow_and_checkout_to_one_exact_sha(self):
        self.assertIn('workflow_dispatch:', self.text)
        self.assertNotIn('pull_request:', self.text)
        self.assertNotIn('\n  push:', self.text)
        self.assertIn('ref: ${{ inputs.expected_sha }}', self.text)
        self.assertIn('[[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]', self.text)
        self.assertIn('test "$GITHUB_SHA" = "$EXPECTED_SHA"', self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"', self.text)

    def test_origins_are_trusted_variables_and_bypasses_are_secrets(self):
        self.assertNotIn('base_url:', self.text)
        for name in ('LI_WEB_PREVIEW_BASE_URL', 'LI_MCP_PREVIEW_BASE_URL'):
            self.assertIn('${{ vars.' + name + ' }}', self.text)
        for name in ('LI_WEB_VERCEL_AUTOMATION_BYPASS_SECRET',
                     'LI_MCP_VERCEL_AUTOMATION_BYPASS_SECRET'):
            self.assertIn('${{ secrets.' + name + ' }}', self.text)
        self.assertNotIn('--bypass', self.text)

    def test_both_receipts_are_retained_even_when_preflight_fails(self):
        self.assertIn('--output hosted-readiness-web.json', self.text)
        self.assertIn('--output hosted-readiness-mcp.json', self.text)
        self.assertIn('if: ${{ always() }}', self.text)
        self.assertIn('if-no-files-found: error', self.text)
        self.assertIn('retention-days: 30', self.text)


if __name__ == '__main__':
    unittest.main()
