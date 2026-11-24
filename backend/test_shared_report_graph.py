"""Exercise account report reading through the health assistant's shared graph."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.llm import LLMConfigurationError
from agents.state import MainAgentState
from agents.streaming import emit_text
from report_audit.reading import ReadingAnswer, ReadingPrompt
from report_audit.reading_context import ReadingTaskRequest
from report_audit.reading_knowledge import retrieve_notes
from report_audit.reading_tasks import build_reading_task_graph, run_reading_task
from report_audit.scan_analysis import ScanFinding

with patch("dotenv.load_dotenv"):
    import main


ANSWER_PATH = "/internal/health/report-reading/answer"
STREAM_PATH = f"{ANSWER_PATH}/stream"
GATEWAY_HEADERS = {"X-Agent-Token": "local-test-token"}


def reading_prompt(task: ReadingTaskRequest | None = None) -> ReadingPrompt:
    return ReadingPrompt(
        findings=[ScanFinding(name="LYM#", value="1.01", unit="10^9/L", reference="1.10~3.20", status="LOW", page=1)],
        row=None if task else 0,
        question="这些异常是什么原因造成？" if task else "",
        task=task,
    )


def report_reply(prompt: ReadingPrompt, first: ReadingAnswer, reply: str) -> ReadingPrompt:
    assert first.task is not None and first.context_follow_up is not None
    field = first.context_follow_up.field
    return prompt.model_copy(update={
        "task": ReadingTaskRequest(id=first.task.id, checkpoint=first.task.checkpoint,
                                   findings_revision=first.task.findings_revision, answered=first.task.answered),
        "reply_field": field,
        "context_replies": {field: reply},
    })


def sse_events(body: str) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in body.splitlines() if line.startswith("data: ")]


async def pharmacy_answer(state: MainAgentState) -> dict:
    reply = "测试药品助手答复"
    emit_text(reply)
    return {"messages": [AIMessage(content=reply)]}


class SharedReportGraphTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {"HEALTH_AGENT_TOKEN": GATEWAY_HEADERS["X-Agent-Token"]}))
        self.enterContext(patch("main.resolve_model_settings"))
        self.enterContext(patch("report_audit.reading.get_chat_llm", side_effect=LLMConfigurationError("offline")))
        self.enterContext(patch("report_audit.reading.retrieve_notes", side_effect=lambda keys, **_: retrieve_notes(keys)))
        self.client = await self.enterAsyncContext(AsyncClient(transport=ASGITransport(app=main.app), base_url="http://health-test"))

    async def test_report_and_pharmacy_share_the_master_graph(self):
        from agents.graph import build_graph

        with patch("agents.graph.pharmacy_node", pharmacy_answer):
            graph = build_graph(checkpointer=MemorySaver())
        self.enterContext(patch.object(main, "_master_app", graph))

        chat = await self.client.post("/internal/health/chat", headers=GATEWAY_HEADERS,
                                      json={"chat_mode": "pharmacy", "messages": [{"role": "user", "content": "药品说明"}]})
        self.assertEqual(chat.status_code, 200, chat.text)
        events = sse_events(chat.text)
        self.assertIn({"type": "text", "content": "测试药品助手答复"}, events)
        self.assertEqual(events[-1], {"type": "finish"})

        prompt = reading_prompt(ReadingTaskRequest(id=uuid4(), findings_revision=1))
        report = await self.client.post(ANSWER_PATH, headers=GATEWAY_HEADERS, json=prompt.model_dump(mode="json"))
        self.assertEqual(report.status_code, 200, report.text)
        first = ReadingAnswer.model_validate(report.json())
        self.assertEqual(first.context_follow_up.field, "recent_infection")
        checkpoint = {"configurable": {"thread_id": f"reading:{first.task.id}", "checkpoint_id": str(first.task.checkpoint)}}
        snapshot = await graph.aget_state(checkpoint)
        self.assertEqual(snapshot.next, ("await_patient",))

        # Ordinary chat cannot enter an account report task's reserved thread namespace.
        wrong_namespace = await self.client.post("/api/chat", json={
            "thread_id": checkpoint["configurable"]["thread_id"], "messages": [{"role": "user", "content": "尝试继续会话"}],
        })
        self.assertEqual(wrong_namespace.status_code, 400, wrong_namespace.text)
        still_waiting = await graph.aget_state(checkpoint)
        self.assertEqual(still_waiting.next, ("await_patient",))

        resumed = await self.client.post(STREAM_PATH, headers=GATEWAY_HEADERS,
                                         json=report_reply(prompt, first, "上周感冒了").model_dump(mode="json"))
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.headers["content-type"].startswith("text/event-stream"))
        events = sse_events(resumed.text)
        self.assertEqual([event["type"] for event in events[-2:]], ["answer", "finish"])
        self.assertTrue(any(event.get("node") == "report_evidence" for event in events[:-2]))
        answer = ReadingAnswer.model_validate(events[-2]["payload"])
        self.assertTrue(answer.task.resumed)
        self.assertEqual(answer.task.id, first.task.id)
        self.assertEqual(answer.context_follow_up.field, "previous_low_lymphocytes")

    async def test_legacy_sqlite_checkpoint_can_resume_through_health_endpoint(self):
        from agents.graph import build_graph

        prompt = reading_prompt(ReadingTaskRequest(id=uuid4(), findings_revision=1))
        with TemporaryDirectory() as folder:
            database = str(Path(folder) / "legacy-reading.sqlite")
            async with AsyncSqliteSaver.from_conn_string(database) as saver:
                legacy_graph = build_reading_task_graph(saver)
                events = [event async for event in run_reading_task(legacy_graph, prompt, streaming=True)]
                first = ReadingAnswer.model_validate(events[-2]["payload"])
            async with AsyncSqliteSaver.from_conn_string(database) as saver:
                with patch.object(main, "_master_app", build_graph(checkpointer=saver)):
                    result = await self.client.post(ANSWER_PATH, headers=GATEWAY_HEADERS,
                                                     json=report_reply(prompt, first, "没有").model_dump(mode="json"))
        self.assertEqual(result.status_code, 200, result.text)
        state = ReadingAnswer.model_validate(result.json())
        self.assertTrue(state.task.resumed)
        self.assertEqual(state.task.id, first.task.id)
        self.assertEqual(state.task.answered, 1)
        self.assertEqual(state.context_follow_up.field, "immune_medicine")

    async def test_internal_report_preview_requires_auth_and_does_not_create_an_interview(self):
        from agents.graph import build_graph

        saver = MemorySaver()
        self.enterContext(patch.object(main, "_master_app", build_graph(checkpointer=saver)))
        payload = reading_prompt().model_dump(mode="json")
        for endpoint in (ANSWER_PATH, STREAM_PATH):
            with self.subTest(endpoint=endpoint):
                response = await self.client.post(endpoint, json=payload)
                self.assertEqual(response.status_code, 403)

        response = await self.client.post(STREAM_PATH, headers=GATEWAY_HEADERS, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        events = sse_events(response.text)
        self.assertEqual([event["type"] for event in events[-2:]], ["answer", "finish"])
        preview = ReadingAnswer.model_validate(events[-2]["payload"])
        self.assertIsNone(preview.task)
        self.assertIsNone(preview.context_follow_up)
        self.assertEqual((preview.row, preview.citations[0].value), (0, "1.01"))
        self.assertEqual([checkpoint async for checkpoint in saver.alist(None)], [])

    async def test_report_storage_survives_health_restart_with_in_memory_chat(self):
        self.enterContext(patch.object(main, "_master_app", None))
        self.enterContext(patch.object(main._observability_runtime, "shutdown"))
        self.enterContext(patch("agents.graph.pharmacy_node", pharmacy_answer))
        with TemporaryDirectory() as folder:
            self.enterContext(patch.dict(os.environ, {
                "AUDIT_READING_CHECKPOINT_DB": str(Path(folder) / "reports.sqlite"),
                "CHECKPOINTER": "memory",
                "RAG_WARMUP": "0",
            }))
            chat_id = uuid4().hex
            chat_config = {"configurable": {"thread_id": chat_id}}
            prompt = reading_prompt(ReadingTaskRequest(id=uuid4(), findings_revision=1))
            async with main.lifespan(main.app):
                chat = await self.client.post("/internal/health/chat", headers=GATEWAY_HEADERS, json={
                    "thread_id": chat_id, "chat_mode": "pharmacy", "messages": [{"role": "user", "content": "药品说明"}],
                })
                self.assertIn({"type": "text", "content": "测试药品助手答复"}, sse_events(chat.text))
                self.assertTrue((await main.get_master_app().aget_state(chat_config)).values)
                result = await self.client.post(ANSWER_PATH, headers=GATEWAY_HEADERS, json=prompt.model_dump(mode="json"))
                self.assertEqual(result.status_code, 200, result.text)
                first = ReadingAnswer.model_validate(result.json())
                self.assertIsNotNone(first.task.checkpoint)

            async with main.lifespan(main.app):
                self.assertFalse((await main.get_master_app().aget_state(chat_config)).values)
                result = await self.client.post(ANSWER_PATH, headers=GATEWAY_HEADERS,
                                                json=report_reply(prompt, first, "上周感冒了").model_dump(mode="json"))
                self.assertEqual(result.status_code, 200, result.text)
                resumed = ReadingAnswer.model_validate(result.json())
                self.assertTrue(resumed.task.resumed)
                self.assertEqual(resumed.task.id, first.task.id)
