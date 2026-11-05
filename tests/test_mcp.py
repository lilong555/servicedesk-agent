"""Real stdio transport regressions; local Redmine integration is read-only."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from dotenv import dotenv_values

from servicedesk_agent.config import RedmineSettings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mcp_env():
    return {
        "DESK_REDMINE_URL": "https://desk.test",
        "DESK_REDMINE_API_KEY": "",
        "DESK_PROJECT_ID": "37",
    }


def mcp_connection(env):
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "servicedesk_agent.mcp_server"],
        "cwd": str(ROOT),
        "env": env,
    }


@pytest.mark.asyncio
async def test_official_mcp_stdio_service_info(mcp_env):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "servicedesk_agent.mcp_server"],
        cwd=str(ROOT),
        env=mcp_env,
    )
    async with asyncio.timeout(30), stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "ServiceDesk"
            advertised = await session.list_tools()
            assert {tool.name for tool in advertised.tools} == {"service_info", "read_ticket"}
            result = await session.call_tool("service_info", {})
            assert result.isError is False
            expected = {"name": "ServiceDesk", "read_only": True, "project_id": 37}
            assert json.loads(result.content[0].text) == expected
            if result.structuredContent is not None:
                assert result.structuredContent == expected
            invalid = await session.call_tool("read_ticket", {"ticket_id": -1})
            assert invalid.isError is False
            invalid_payload = json.loads(invalid.content[0].text)
            assert invalid_payload["ok"] is False
            assert invalid_payload["error"] == "invalid_ticket"


@pytest.mark.asyncio
async def test_multiserver_mcp_client_service_info(mcp_env):
    pytest.importorskip("langchain_mcp_adapters")
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"desk": mcp_connection(mcp_env)})
    async with asyncio.timeout(30):
        tools = {tool.name: tool for tool in await client.get_tools(server_name="desk")}
        assert set(tools) == {"service_info", "read_ticket"}
        result = await tools["service_info"].ainvoke(
            {"name": "service_info", "args": {}, "id": "mcp-info", "type": "tool_call"}
        )
        expected = {"name": "ServiceDesk", "read_only": True, "project_id": 37}
        assert result.status == "success"
        assert result.tool_call_id == "mcp-info"
        assert json.loads(result.content[0]["text"]) == expected
        if result.artifact is not None:
            assert result.artifact["structured_content"] == expected


@pytest.fixture
def local_redmine_settings():
    path = ROOT / ".runtime" / "redmine.env"
    if not path.is_file():
        pytest.skip("Local Redmine configuration .runtime/redmine.env is absent.")
    values = dotenv_values(path)
    required = ("DESK_REDMINE_URL", "DESK_REDMINE_API_KEY", "DESK_PROJECT_ID")
    if any(not values.get(key) for key in required):
        pytest.skip("Local Redmine configuration is incomplete.")
    if urlsplit(values["DESK_REDMINE_URL"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("The read-only integration requires a loopback Redmine service.")
    return RedmineSettings(
        url=values["DESK_REDMINE_URL"],
        api_key=values["DESK_REDMINE_API_KEY"],
        project_id=int(values["DESK_PROJECT_ID"]),
        timeout=5,
    )


@pytest.mark.redmine
@pytest.mark.asyncio
async def test_multiserver_mcp_read_ticket_from_local_redmine(local_redmine_settings):
    pytest.importorskip("langchain_mcp_adapters")
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from servicedesk_agent.domain import Ticket, fingerprint

    settings = local_redmine_settings
    project_id = settings.project_id
    async with asyncio.timeout(45):
        async with httpx.AsyncClient(
            base_url=settings.url + "/",
            headers={"X-Redmine-API-Key": settings.api_key.get_secret_value()},
            timeout=settings.timeout,
            trust_env=False,
            follow_redirects=False,
        ) as http:
            try:
                listing = await http.get(
                    "issues.json", params={"project_id": project_id, "status_id": "*", "limit": 1}
                )
            except httpx.TransportError:
                pytest.skip("The configured local Redmine service is unavailable.")
            assert listing.status_code == 200, "Local Redmine ticket listing failed."
            issues = listing.json()["issues"]
            if not issues:
                pytest.skip("The configured local Redmine project has no tickets.")
            ticket_id = issues[0]["id"]
            client = MultiServerMCPClient(
                {
                    "desk": mcp_connection(
                        {
                            "DESK_REDMINE_URL": settings.url,
                            "DESK_REDMINE_API_KEY": settings.api_key.get_secret_value(),
                            "DESK_PROJECT_ID": str(project_id),
                        }
                    )
                }
            )
            tools = {tool.name: tool for tool in await client.get_tools(server_name="desk")}
            assert set(tools) == {"service_info", "read_ticket"}
            result = await tools["read_ticket"].ainvoke(
                {
                    "name": "read_ticket",
                    "args": {"ticket_id": ticket_id},
                    "id": "local-redmine-read",
                    "type": "tool_call",
                }
            )
            assert result.status == "success"
            payload = json.loads(result.content[0]["text"])
            assert payload["ok"] is True
            if result.artifact is not None:
                assert result.artifact["structured_content"] == payload
            ticket = Ticket.model_validate(payload["ticket"])
            assert ticket.id == ticket_id
            assert ticket.project_id == project_id
            assert ticket.evidence.source_kind == "ticket"
            assert ticket.evidence.source_uri == f"{settings.url}/issues/{ticket_id}"
            assert ticket.evidence.entity_scope == f"{settings.url}/projects/{project_id}"
            assert ticket.evidence.source_version == fingerprint(ticket.evidence.content)
            assert ticket.evidence.observed_at.tzinfo is not None
            assert ticket.relations == ticket.evidence.content.get("relations", [])
            assert ticket.journals == ticket.evidence.content.get("journals", [])
            assert ticket.subject == ticket.evidence.content["subject"]
            if settings.api_key.get_secret_value() in result.model_dump_json():
                pytest.fail("MCP output contains credential material.", pytrace=False)
