"""MCP server: outgoing email. Agents only write drafts; a human reviews and sends them from the dashboard."""

import json
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

from ap_agents.config import STATE

server = MCPServer("mail")


@server.tool(structured_output=False)
def draft_email(to: str, subject: str, body: str) -> str:
    """Write an email draft (to a supplier or the finance team). A human reviews it before anything is sent."""
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / "emails.json"
    emails = json.loads(path.read_text()) if path.exists() else []
    emails.append({"id": len(emails) + 1, "at": datetime.now(timezone.utc).isoformat(), "to": to,
                   "subject": subject, "body": body, "status": "draft"})
    path.write_text(json.dumps(emails, ensure_ascii=False, indent=2))
    return f"draft saved for {to}, waiting for human review (not sent)"


if __name__ == "__main__":
    server.run()
