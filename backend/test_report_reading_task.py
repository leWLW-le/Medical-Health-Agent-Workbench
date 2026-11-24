"""Regression cases for a report investigation paused between HTTP requests."""

from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agents.llm import LLMConfigurationError
from report_audit.reading import ReadingAnswer, ReadingPrompt
from report_audit.reading_context import ReadingTaskRequest
from report_audit.reading_knowledge import retrieve_notes
from report_audit.reading_tasks import build_reading_task_graph, run_reading_task
from report_audit.scan_analysis import ScanFinding


def report_question():
    return ReadingPrompt(
        findings=[ScanFinding(name="LYM#", value="1.01", unit="10^9/L", reference="1.10~3.20", status="LOW", page=1)],
        question="这些异常是什么原因造成？",
        task=ReadingTaskRequest(id=uuid4(), findings_revision=1),
    )


def reply(request, answer, text, **prior):
    field = answer.context_follow_up.field
    task = ReadingTaskRequest.model_validate(answer.task.model_dump(include={"id", "checkpoint", "findings_revision", "answered"}))
    return request.model_copy(update={"task": task, "reply_field": field, "context_replies": {**prior, field: text}})


async def collect(graph, request):
    async with aclosing(run_reading_task(graph, request, streaming=True)) as stream:
        events = [event async for event in stream]
    assert events[-1]["type"] == "finish"
    return ReadingAnswer.model_validate(events[-2]["payload"])


class ReadingTaskTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = self.enterContext(TemporaryDirectory())
        self.db = str(Path(self.directory) / "checkpoint.sqlite")
        self.enterContext(patch("report_audit.reading.retrieve_notes", side_effect=lambda keys, **_: retrieve_notes(keys)))
        self.provider = self.enterContext(patch("report_audit.reading.get_chat_llm", side_effect=LLMConfigurationError("offline")))

    async def test_restores_published_checkpoint_and_branches_on_patient_answer(self):
        question = report_question()
        async with AsyncSqliteSaver.from_conn_string(self.db) as saver:
            first = await collect(build_reading_task_graph(saver), question)
        self.assertEqual(first.context_follow_up.field, "recent_infection")
        self.assertEqual(first.task.status, "awaiting_reply")
        self.assertIsNotNone(first.task.checkpoint)

        async with AsyncSqliteSaver.from_conn_string(self.db) as saver:
            graph = build_reading_task_graph(saver)
            positive = await collect(graph, reply(question, first, "上周感冒了"))
            negative = await collect(graph, reply(question, first, "没有"))
            final = await collect(graph, reply(question, positive, "先给结论", recent_infection="上周感冒了"))

        self.assertTrue(positive.task.resumed)
        self.assertEqual(positive.task.id, first.task.id)
        self.assertEqual(positive.context_follow_up.field, "previous_low_lymphocytes")
        self.assertTrue(negative.task.resumed)
        self.assertEqual(negative.context_follow_up.field, "immune_medicine")
        self.assertEqual(final.task.answered, 2)
        self.assertEqual(final.task.stop_reason, "user_finished")
        self.assertIsNone(final.context_follow_up)
        self.assertIn("既往淋巴细胞结果", " ".join(final.task.gaps))
        self.assertIn("你提到近期有过感染", " ".join(final.guidance.possibilities))

    async def test_correction_changes_the_active_fact_and_current_question(self):
        question = report_question()
        async with AsyncSqliteSaver.from_conn_string(self.db) as saver:
            graph = build_reading_task_graph(saver)
            first = await collect(graph, question)
            positive = await collect(graph, reply(question, first, "上周感冒了"))
            revised = await collect(graph, reply(question, positive, "纠正：我最近没有感冒", recent_infection="上周感冒了"))
        self.assertEqual(revised.task.answered_field, "recent_infection")
        self.assertEqual(revised.task.answered, positive.task.answered)
        self.assertNotIn("你提到近期有过感染", " ".join(revised.guidance.possibilities))
        self.assertEqual(revised.context_follow_up.field, "immune_medicine")

    async def test_timeout_keeps_evidence_and_replies_can_finish(self):
        self.provider.side_effect = None
        llm = Mock(ainvoke=AsyncMock(side_effect=TimeoutError()))
        async def time_out(*args, **kwargs):
            yield await llm.ainvoke(*args, **kwargs)

        llm.astream = time_out
        llm.bind_tools.return_value = llm
        self.provider.return_value = llm
        question = report_question()
        async with AsyncSqliteSaver.from_conn_string(self.db) as saver:
            graph = build_reading_task_graph(saver)
            answer = await collect(graph, question)
            final = await collect(graph, reply(question, answer, "先给结论"))
        self.assertTrue(final.task.resumed)
        self.assertEqual(final.task.stop_reason, "user_finished")
        self.assertEqual(final.citations[0].value, "1.01")
        self.assertEqual(final.work_record.fallback_reason, "model_call_timeout")
