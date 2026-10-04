"""Vercel ASGI entry point for the Lofgren Intelligence public MCP service."""

from lofgren_intelligence.hosted.web_app import build_app

app = build_app()
