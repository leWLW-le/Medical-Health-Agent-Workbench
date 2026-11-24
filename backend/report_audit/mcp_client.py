import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from .models import KnowledgeCitation, LabCode


class ReferenceResult(BaseModel):
    result: list[KnowledgeCitation]


async def get_references(url: str, code: LabCode) -> list[KnowledgeCitation]:
    async with httpx.AsyncClient(timeout=12, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("lookup_lab_reference", {"code": code})
                if result.isError:
                    raise RuntimeError("MCP reference tool failed")
                return ReferenceResult.model_validate(result.structuredContent).result
