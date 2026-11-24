"""Shared, observable RAG tool for the health agents."""
import logging

from langchain_core.tools import BaseTool, tool

from agents.streaming import emit_card
from rag.knowledge_base import HealthKnowledgeBase

logger = logging.getLogger(__name__)


def knowledge_search_tool(
    knowledge_base: HealthKnowledgeBase,
    *,
    source: str | None = None,
    hide_queries: bool = False,
) -> BaseTool:
    @tool
    async def search_health_knowledge(queries: list[str], limit: int = 3) -> str:
        """Search the health knowledge base and return labeled excerpts as answer context."""
        queries = list(dict.fromkeys(query.strip() for query in queries if query.strip()))
        trace = {"queries": [] if hide_queries else queries, "hits": [], "status": "empty"}
        if not queries:
            emit_card("knowledge_retrieval", trace)
            return ""

        try:
            if len(queries) == 1:
                candidates = await knowledge_base.aretrieve(queries[0], k=7)
            else:
                candidates = await knowledge_base.amulti_query_retrieve(queries, k=7)
        except Exception as exc:
            logger.warning("Health knowledge retrieval failed (%s)", type(exc).__name__)
            emit_card("knowledge_retrieval", {**trace, "status": "unavailable"})
            return ""

        docs = [
            doc for doc in candidates
            if doc.metadata.get("source") != "insurance_policies"
            and (source is None or doc.metadata.get("source") == source)
        ][:limit]
        context = knowledge_base.format_context(docs)
        if context:
            hits = [
                {
                    "source": doc.metadata.get("source", ""),
                    "section": doc.metadata.get("section", ""),
                    "excerpt": doc.page_content,
                }
                for doc in docs
            ]
            emit_card("knowledge_retrieval", {**trace, "hits": hits, "status": "used"})
        else:
            emit_card("knowledge_retrieval", trace)
        return context

    return search_health_knowledge
