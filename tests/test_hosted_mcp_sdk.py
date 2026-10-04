import os
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.server.auth.provider import AccessToken

from lofgren_intelligence.hosted.mcp_sdk import build_mcp

from .test_public_hosted import FakeStore
from .helpers import TEXTS
from lofgren_intelligence.discovery.fixtures import warehouse_design


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
                self.assertIn("discover", names)
                self.assertIn("get_discovery_receipt", names)
                self.assertIn("create_v3_handoff", names)
                result = await client.call_tool("account_status", {})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["activation_number"], 1)
                self.assertTrue(result.structured_content["founding_free"])
                # The current official SDK negotiates the current protocol era.
                self.assertIsNotNone(client.protocol_version)

    async def test_authenticated_remote_v2_is_durable_across_tool_calls(self):
        store = FakeStore(quota=1000)
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
            patch.dict(os.environ, {
                "LI_MCP_REQUESTS_PER_MINUTE": "60",
                "LOFGREN_PROVIDER": "heuristic",
                "LI_INFRA_USD_PER_RUN": "0",
                "LI_RETRIEVAL_USD_PER_CALL": "0",
                "LI_PUBLIC_MAX_DISCOVERY_UNITS": "100",
            }, clear=False),
        ):
            server = build_mcp("https://li.example")
            async with Client(server) as client:
                research = await client.call_tool("investigate", {
                    "objective": "Is industrial construction in the Phoenix metro increasing?",
                    "texts": TEXTS,
                })
                self.assertFalse(research.is_error)
                run_id = research.structured_content["run_id"]

                discovery = await client.call_tool("discover", {
                    "run_id": run_id,
                    "objective": "Choose a fictional warehouse size",
                    "design": warehouse_design(),
                })
                self.assertFalse(discovery.is_error)
                discovery_id = discovery.structured_content["discovery_id"]

                receipt = await client.call_tool("get_discovery_receipt", {"discovery_id": discovery_id})
                self.assertFalse(receipt.is_error)
                self.assertTrue(receipt.structured_content["intact"])

                verification = await client.call_tool("verify_discovery", {"discovery_id": discovery_id})
                self.assertFalse(verification.is_error)
                self.assertTrue(verification.structured_content["receipt_intact"])

            # New server/service instance, same durable store.
            server2 = build_mcp("https://li.example")
            async with Client(server2) as client2:
                receipt2 = await client2.call_tool("get_discovery_receipt", {"discovery_id": discovery_id})
                self.assertFalse(receipt2.is_error)
                self.assertTrue(receipt2.structured_content["intact"])

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
