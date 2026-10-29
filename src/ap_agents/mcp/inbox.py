"""MCP server: the incoming invoice mailbox."""

import json

from mcp.server.mcpserver import MCPServer
from pypdf import PdfReader

from ap_agents.config import DATA

server = MCPServer("inbox")
INBOX = DATA / "inbox"


@server.tool(structured_output=False)
def list_invoices() -> str:
    """List invoice files waiting in the inbox."""
    return json.dumps(sorted(p.name for p in INBOX.glob("*.pdf")))


@server.tool(structured_output=False)
def read_invoice(file: str) -> str:
    """Return the text content of one invoice file (PDF or text) from the inbox, layout preserved."""
    path = (INBOX / file).resolve()
    if path.parent != INBOX.resolve() or not path.is_file():
        return f"error: no invoice named {file!r}"
    if path.suffix.lower() != ".pdf":
        return path.read_text()
    pages = [p.extract_text(extraction_mode="layout") for p in PdfReader(path).pages]
    return "\n".join(line.rstrip() for line in "\n".join(pages).splitlines() if line.strip())


if __name__ == "__main__":
    server.run()
