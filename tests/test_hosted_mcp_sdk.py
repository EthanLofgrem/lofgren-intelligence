import os
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.server.auth.provider import AccessToken

from lofgren_intelligence.hosted.mcp_sdk import build_mcp

from .test_public_hosted import FakeStore


class OfficialMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_sdk_lists_public_tools_and_calls_with_tenant_identity(self):
        store = FakeStore()
        token = AccessToken(
            token="test",
            client_id="client",
            scopes=["mcp"],
            resource="https://li.example/mcp",
            subject="u1",
        )
        with (
            patch("lofgren_intelligence.hosted.mcp_sdk.get_access_token", return_value=token),
            patch("lofgren_intelligence.hosted.mcp_sdk.SupabaseStore", return_value=store),
            patch.dict(os.environ, {"LI_MCP_REQUESTS_PER_MINUTE": "60"}, clear=False),
        ):
            server = build_mcp("https://li.example")
            async with Client(server) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertIn("investigate", names)
                self.assertIn("export_knowledge_map2", names)
                self.assertIn("account_status", names)
                self.assertIn("create_checkout", names)
                result = await client.call_tool("account_status", {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["activation_number"], 1)
                self.assertTrue(result.structured_content["founding_free"])
                # The current official SDK negotiates the current protocol era.
                self.assertIsNotNone(client.protocol_version)

    async def test_tool_schema_does_not_expose_server_local_file_paths(self):
        server = build_mcp("https://li.example")
        async with Client(server) as client:
            tools = await client.list_tools()
        investigate = next(t for t in tools.tools if t.name == "investigate")
        props = investigate.input_schema.get("properties", {})
        self.assertNotIn("files", props)
        self.assertNotIn("tle_path", props)


if __name__ == "__main__":
    unittest.main()
