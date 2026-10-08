"""Export the registered hosted SDK tool schema without calling any tool or provider."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import Client
from lofgren_intelligence.hosted.mcp_sdk import build_mcp


async def schema(name):
    async with Client(build_mcp("https://li.example")) as client:
        tools = await client.list_tools()
        for tool in tools.tools:
            if tool.name == name:
                return tool.model_dump(by_alias=True, exclude_none=True)
    raise ValueError("tool is not registered")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", default="li_assess_request")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(schema(args.tool)), indent=2, sort_keys=True))
