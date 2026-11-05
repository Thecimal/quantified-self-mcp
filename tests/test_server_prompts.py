"""
Tests for FastMCP Prompts registered on server.mcp (weekly_health_review, doctor_visit_prep).
"""

import sys
from pathlib import Path

import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import mcp


@pytest.mark.asyncio
async def test_server_registers_and_renders_prompts():
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        names = {p.name for p in prompts}
        assert "weekly_health_review" in names
        assert "doctor_visit_prep" in names

        # Render weekly_health_review with custom lookback
        res1 = await client.get_prompt("weekly_health_review", {"lookback_days": 14})
        text1 = res1.messages[0].content.text
        assert "past 14 days" in text1
        assert "get_data_status" in text1
        assert "get_recent_changes" in text1
        assert "get_baseline" in text1

        # Render doctor_visit_prep with custom lookback
        res2 = await client.get_prompt("doctor_visit_prep", {"lookback_days": 60})
        text2 = res2.messages[0].content.text
        assert "past 60 days" in text2
        assert "medical advice" in text2
