"""Read-only Redmine tools exposed through the official MCP stdio server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from servicedesk_agent.config import RedmineSettings
from servicedesk_agent.domain import BusinessError
from servicedesk_agent.redmine import RedmineClient

server = FastMCP("ServiceDesk")


@server.tool()
def service_info() -> dict:
    """Return this read-only service's configured project scope."""
    settings = RedmineSettings.from_env()
    return {"name": "ServiceDesk", "read_only": True, "project_id": settings.project_id}


@server.tool()
def read_ticket(ticket_id: int) -> dict:
    """Read an authorized Redmine ticket with version and source evidence."""
    try:
        with RedmineClient(RedmineSettings.from_env()) as client:
            return {"ok": True, "ticket": client.get_ticket(ticket_id).model_dump(mode="json")}
    except BusinessError as exc:
        return exc.as_dict()


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
