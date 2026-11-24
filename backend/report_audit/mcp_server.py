import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .models import KnowledgeCitation, LabCode
from .workflow import search_knowledge

# This service exposes only the bundled reference corpus and is bound to the private network.
mcp = FastMCP(
    "report-audit-knowledge", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=os.getenv(
            "AUDIT_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,knowledge:*",
        ).split(","),
    ),
)


@mcp.tool()
async def lookup_lab_reference(code: LabCode) -> list[KnowledgeCitation]:
    """Retrieve general lab references for the given supported test code, with citations."""
    return await search_knowledge(code)


app = mcp.streamable_http_app()
