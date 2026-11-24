import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

import main
from agents.graph import build_graph
from agents.web_search import LiteratureSearch, LiteratureSource, search_medical_literature
from test_fakes import FakeKnowledgeBase, scripted


class LiteratureSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_keywords_and_returns_traceable_pubmed_sources(self):
        model = scripted(AIMessage(content="hypertension guideline"))
        client_class = httpx.AsyncClient

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.host == "wsearch.nlm.nih.gov":
                self.assertEqual(request.url.params["term"], "hypertension guideline")
                return httpx.Response(200, text="<nlmSearchResult><list/></nlmSearchResult>")
            self.assertEqual(request.url.host, "www.ebi.ac.uk")
            self.assertEqual(request.url.params["query"], "(hypertension guideline) AND SRC:MED")
            self.assertEqual(request.url.params["sort"], "FIRST_PDATE desc")
            return httpx.Response(200, json={"resultList": {"result": [
                {"id": "1234", "source": "MED", "title": "<b>Example</b> guideline", "pubYear": "2025",
                 "abstractText": "Example abstract.", "journalInfo": {"journal": {"title": "Example Journal"}}},
                {"id": "4567", "source": "MED", "title": "Retracted paper",
                 "pubTypeList": {"pubType": ["Retracted Publication"]}},
            ]}})

        transport = httpx.MockTransport(respond)
        with (
            patch("agents.web_search.get_chat_llm", return_value=model),
            patch("agents.web_search.httpx.AsyncClient", side_effect=lambda **_: client_class(transport=transport)),
        ):
            result = await search_medical_literature("姓名：示例患者\n请查找高血压最新指南")
        self.assertNotIn("示例患者", model.invocations[0][-1].content)
        self.assertEqual(result.status, "found")
        self.assertEqual(result.provider, "Europe PMC")
        self.assertEqual(result.sources, [
            LiteratureSource("Example guideline", "https://pubmed.ncbi.nlm.nih.gov/1234/", "Example Journal", "2025", "Example abstract."),
        ])

    async def test_health_topics_are_available_when_literature_search_times_out(self):
        topic = LiteratureSource("High Blood Pressure", "https://medlineplus.gov/highbloodpressure.html", "MedlinePlus.gov", "", "Summary.")
        with (
            patch("agents.web_search._public_keywords", AsyncMock(return_value="hypertension")),
            patch("agents.web_search._lookup", AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))),
            patch("agents.web_search._lookup_health_topics", AsyncMock(return_value=[topic])),
        ):
            result = await search_medical_literature("查询高血压资料")
        self.assertEqual(result.provider, "MedlinePlus.gov")
        self.assertIn(topic.url, result.answer_context())

    async def test_search_evidence_is_per_turn_and_visible_in_the_stream(self):
        graph = build_graph(checkpointer=MemorySaver())
        model = scripted(AIMessage(content="合成回答。"))
        search = AsyncMock(return_value=LiteratureSearch("found", "albumin", [
            LiteratureSource("Synthetic reference", "https://pubmed.ncbi.nlm.nih.gov/1234/", "Example Journal", "2025", "Source excerpt."),
        ]))
        question = "白蛋白相关的公开文献有哪些？"

        async def turn(web_search: bool):
            response = await main.chat(main.ChatRequest(
                messages=[main.ChatMessage(role="user", content=question)],
                chat_mode="report", thread_id="a" * 32, web_search=web_search,
            ))
            return [
                json.loads(line[6:]) async for frame in response.body_iterator
                for line in frame.splitlines() if line.startswith("data: ")
            ]

        with (
            patch.object(main, "_master_app", graph),
            patch.object(main, "resolve_model_settings"),
            patch.object(main, "search_medical_literature", search),
            patch("agents.report.get_chat_llm", return_value=model),
            patch("agents.report.get_knowledge_base", return_value=FakeKnowledgeBase()),
        ):
            enabled = await turn(True)
            disabled = await turn(False)
        search.assert_awaited_once_with(question)
        self.assertTrue(any(frame.get("payload", {}).get("type") == "web_search" for frame in enabled))
        self.assertFalse(any(frame.get("payload", {}).get("type") == "web_search" for frame in disabled))
        self.assertEqual(enabled[-1]["type"], "finish")
        self.assertEqual(disabled[-1]["type"], "finish")
        self.assertIn("Source excerpt.", model.invocations[0][0].content)
        self.assertNotIn("Source excerpt.", model.invocations[-1][0].content)
