"""
Deterministic tests for agent-owned `card` SSE events.

Since v0.4.0 `main.py` no longer maps tool names to card types: every
card-producing tool (or its agent node) pushes the payload itself through
`agents.streaming`, and `_stream_agent_events` forwards the `custom` stream
verbatim. These tests pin both the payloads and their position in the stream.
"""
import json
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from starlette.datastructures import Headers, UploadFile

import main
from agents import pharmacy, report
from agents.graph import build_graph
from agents.knowledge_search import knowledge_search_tool
from rag.knowledge_base import HealthKnowledgeBase
from skills.lab_interpreter.skill import LabInterpreterSkill
from test_fakes import FakeKnowledgeBase, scripted, tool_call


def _parse_sse(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class MainIsToolNameAgnosticTests(unittest.TestCase):
    def test_the_tool_name_to_card_type_maps_are_gone(self):
        for attribute in (
            "_INSURANCE_TOOL_TO_CARD_TYPE",
            "_REPORT_TOOL_TO_CARD_TYPE",
            "PHARMACY_TOOL_TO_CARD_TYPE",
        ):
            self.assertFalse(hasattr(main, attribute), attribute)

    def test_the_ui_label_tables_are_still_there(self):
        self.assertIn("find_nearby_pharmacy", main._SKILL_LABELS)
        self.assertIn("insurance_node", main._NODE_LABELS)

    def test_only_card_and_text_may_come_off_the_custom_stream(self):
        self.assertEqual(main._STREAMABLE_CUSTOM_TYPES, {"card", "text"})


class ToolLevelEmissionTests(unittest.TestCase):
    def test_pharmacy_tools_emit_their_own_cards(self):
        emitted: list[tuple] = []
        with patch.object(pharmacy, "emit_card", side_effect=lambda *a: emitted.append(a)):
            pharmacy.find_nearby_pharmacy.invoke({"location": "中关村"})
        self.assertEqual(emitted[0][0], "hospital_list")
        self.assertIn("pharmacies", emitted[0][1])

    def test_emitting_outside_a_graph_run_is_a_no_op(self):
        # No writer is installed here, so the tool must still return normally.
        raw = pharmacy.find_nearby_pharmacy.invoke({"location": "中关村"})
        self.assertIn("pharmacies", json.loads(raw))


class ReportNodeCardTests(unittest.TestCase):
    def test_report_comparison_preserves_source_and_refuses_missing_or_incompatible_ranges(self):
        result = LabInterpreterSkill().run(require_report_reference=True, items=[
            {"name": "总胆红素", "value": 6, "unit": "umol/L", "reference_range": "4～12 µmol/L"},
            {"name": "谷丙转氨酶", "value": 5, "unit": "U/L", "reference_range": "5–10"},
            {"name": "谷草转氨酶", "value": 10, "unit": "U/L", "reference_range": "<10"},
            {"name": "肌酐", "value": 45, "unit": "μmol/L", "reference_range": "1–2 mg/dL"},
            {"name": "尿酸", "value": 80, "unit": "μmol/L"},
        ])
        self.assertEqual([item.status for item in result.findings], ["NORMAL", "NORMAL", "HIGH", "UNKNOWN", "UNKNOWN"])
        self.assertEqual([item.reference_source for item in result.findings], ["report", "report", "report", "report", None])
        self.assertEqual(result.unknown_count, 2)
        self.assertIn("另有 2 项待核对", result.summary)

    def test_all_unknown_and_empty_results_never_claim_normal(self):
        skill = LabInterpreterSkill()
        result = skill.run(require_report_reference=True, items=[{"name": "ALT", "value": 15, "unit": "U/L"}])
        self.assertEqual(result.unknown_count, 1)
        self.assertIn("均缺少可比对的参考范围", result.summary)
        self.assertIn("未识别", skill.run(items=[]).summary)
        builtin = skill.run(items=[{"name": "谷丙转氨酶", "value": 15, "unit": "U/L"}])
        self.assertEqual(builtin.findings[0].reference_source, "builtin")

    def test_lab_interpreter_results_become_report_analysis_cards(self):
        payload = {"skill_name": "lab_interpreter", "success": True, "results": []}
        messages = [
            AIMessage(content="先解读一下。"),
            ToolMessage(
                content=json.dumps(payload),
                name="lab_interpreter",
                tool_call_id="c1",
            ),
            ToolMessage(content="{}", name="load_skill", tool_call_id="c2"),
            ToolMessage(content="not json", name="lab_interpreter", tool_call_id="c3"),
        ]

        emitted: list[tuple] = []
        with patch.object(report, "emit_card", side_effect=lambda *a: emitted.append(a)):
            report._emit_report_cards(messages)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0], ("report_analysis", payload))


class CardSseSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_history_restores_comparison_and_later_questions_keep_exact_rows(self):
        app = build_graph(checkpointer=MemorySaver())
        answer = scripted(AIMessage(content="仍可读取已核对的项目。"))
        thread = "a" * 32
        owner = "00000000-0000-4000-8000-000000000001"
        other = "00000000-0000-4000-8000-000000000002"
        first = main.ChatRequest(
            messages=[main.ChatMessage(role="user", content="确认合成报告")],
            chat_mode="report", thread_id=thread,
            report_items=[{"name": "白蛋白", "value": 42.123, "unit": "g/L", "reference_range": "35–50"}],
        )

        with (
            patch.object(main, "_master_app", app),
            patch.object(main, "resolve_model_settings"),
            patch("agents.report.get_chat_llm", return_value=answer),
            patch("agents.report.get_knowledge_base", return_value=FakeKnowledgeBase()),
        ):
            response = await main.gateway_report_photo_chat(first, owner)
            self.assertEqual(_parse_sse([chunk async for chunk in response.body_iterator])[-1]["type"], "finish")
            config = main._text_thread_config(thread, report=True, account=owner)
            await app.aupdate_state(config, {"messages": [
                HumanMessage(content="之前问过这项检查的用途"),
                AIMessage(content="古早长回答" * 1800),
                HumanMessage(content="我刚刚问的是哪一项？"),
                AIMessage(content="白蛋白。"),
            ]}, as_node="report_node")
            response = await main.gateway_report_photo_chat(main.ChatRequest(
                messages=[main.ChatMessage(role="user", content="它在我这份报告上是多少？")],
                chat_mode="report", thread_id=thread,
            ), owner)
            events = _parse_sse([chunk async for chunk in response.body_iterator])

            restored = await main.gateway_report_photo_history(thread, owner)
            self.assertEqual(restored.headers["cache-control"], "no-store")
            history = json.loads(restored.body)["messages"]
            card = next(message["cards"][0] for message in history if "cards" in message)
            self.assertEqual(card["data"]["findings"][0]["value"], 42.123)
            self.assertTrue(any("古早长回答" in message["text"] for message in history))
            with self.assertRaises(main.HTTPException) as missing:
                await main.gateway_report_photo_history(thread, other)
            self.assertEqual(missing.exception.status_code, 404)
            await main.gateway_clear_report_photo_history(thread, other)
            self.assertTrue((await app.aget_state(config)).values)
            await main.gateway_clear_report_photo_history(thread, owner)
            self.assertFalse((await app.aget_state(config)).values)

        self.assertEqual(events[-1]["type"], "finish")
        model_messages = answer.invocations[-1]
        self.assertIn('["白蛋白",42.123,"g/L","35–50","NORMAL"]', model_messages[0].content)
        self.assertIn("之前问过这项检查的用途", model_messages[0].content)
        self.assertTrue(any(message.content == "白蛋白。" for message in model_messages))
        self.assertFalse(any("古早长回答" in message.content for message in model_messages))
        self.assertEqual(model_messages[-1].content, "它在我这份报告上是多少？")

    async def test_photo_requires_review_and_only_compares_user_confirmed_ranges(self):
        app = build_graph(checkpointer=MemorySaver())
        photo = UploadFile(BytesIO(b"test"), headers=Headers({"content-type": "image/png"}))
        ocr_result = json.dumps({"items": [
            {"name": "总胆红素", "value": 6, "unit": "umol/L", "reference_range": "4–12"},
            {"name": "谷丙转氨酶", "value": 9, "unit": "U/L", "reference_range": "1–10"},
            {"name": "白蛋白", "value": 60, "unit": "g/L", "reference_range": "35–50"},
        ]}, ensure_ascii=False)
        with (
            patch.object(main, "_master_app", app),
            patch.object(main, "resolve_model_settings"),
            patch.object(main, "recognize_image", AsyncMock(return_value=ocr_result)),
            patch("agents.report.get_chat_llm", side_effect=AssertionError("Photo verdict must use the tool")),
        ):
            response = await main.vision_chat(photo, "report", "{}", "[]", "")
            draft_events = _parse_sse([chunk async for chunk in response.body_iterator])
            draft = next(e["payload"] for e in draft_events if e["type"] == "card")
            self.assertEqual(draft["type"], "report_ocr_review")
            self.assertFalse(any(e.get("tool") == "lab_interpreter" for e in draft_events))
            items = draft["data"]["items"]
            items[1]["reference_range"] = None
            response = await main.chat(main.ChatRequest(
                messages=[main.ChatMessage(role="user", content="已确认")],
                chat_mode="report", thread_id=draft_events[0]["thread_id"], report_items=items,
            ))
            events = _parse_sse([chunk async for chunk in response.body_iterator])

        self.assertTrue(any(e.get("type") == "tool_start" and e.get("tool") == "lab_interpreter" for e in events))
        card = next(e["payload"] for e in events if e["type"] == "card")
        self.assertEqual(card["type"], "report_analysis")
        self.assertEqual([row["status"] for row in card["data"]["findings"]], ["NORMAL", "UNKNOWN", "HIGH"])
        explanation = "".join(e["content"] for e in events if e["type"] == "text")
        self.assertIn("### 报告解读", explanation)
        self.assertIn("1 项待核对", explanation)
        self.assertIn("**待核对的项目**　谷丙转氨酶", explanation)
        self.assertIn("**白蛋白**（60 g/L）高于原报告参考范围（35–50）", explanation)
        self.assertIn("**这些检查在看什么**", explanation)
        self.assertIn("**胆红素**（总胆红素）", explanation)
        self.assertIn("**血清蛋白**（白蛋白）", explanation)
        self.assertEqual(events[-1]["type"], "finish")

    async def test_retrieval_tool_streams_the_same_excerpt_sent_to_the_model(self):
        app = build_graph(checkpointer=MemorySaver())
        model = scripted(AIMessage(content="这里是日常健康管理建议。"))
        excerpt = "定期记录血压，复诊时向医生提供近期读数。"
        kb = HealthKnowledgeBase()
        kb.aretrieve = AsyncMock(return_value=[
            Document(page_content=excerpt, metadata={"source": "medical_knowledge", "section": "高血压"}),
            Document(page_content="医保资料", metadata={"source": "insurance_policies"}),
        ])
        state = {"messages": [HumanMessage(content="高血压日常管理有哪些建议？")], "active_agent": "advisor_agent"}

        with (
            patch.object(main, "_master_app", app),
            patch("agents.router.get_chat_llm", return_value=scripted(AIMessage(content="advisor_agent"))),
            patch("agents.advisor.get_chat_llm", return_value=model),
            patch("agents.advisor.get_knowledge_base", return_value=kb),
        ):
            events = _parse_sse([
                payload async for payload in main._stream_agent_events(
                    state, {"configurable": {"thread_id": "knowledge-card-test"}},
                )
            ])

        calls = [e["type"] for e in events if e.get("tool") == "search_health_knowledge"]
        self.assertEqual(calls, ["tool_start", "tool_end"])
        card = next(e for e in events if e["type"] == "card")["payload"]
        self.assertEqual(card["type"], "knowledge_retrieval")
        self.assertEqual(card["data"]["status"], "used")
        self.assertEqual(card["data"]["queries"], ["高血压日常管理有哪些建议？"])
        self.assertEqual(card["data"]["hits"], [
            {"source": "medical_knowledge", "section": "高血压", "excerpt": excerpt},
        ])
        self.assertIn(excerpt, model.invocations[-1][0].content)
        self.assertNotIn("医保资料", model.invocations[-1][0].content)
        self.assertEqual(events[-1]["type"], "finish")

    async def test_report_search_filters_sources_and_hides_the_image_query(self):
        kb = HealthKnowledgeBase()
        kb.aretrieve = AsyncMock(return_value=[
            Document(page_content="检验参考片段", metadata={"source": "lab_reference", "section": "血常规"}),
            Document(page_content="其它资料", metadata={"source": "medical_knowledge"}),
        ])
        search = knowledge_search_tool(kb, source="lab_reference", hide_queries=True)
        with patch("agents.knowledge_search.emit_card") as emit:
            context = await search.ainvoke({"queries": ["合成报告：测试姓名，血红蛋白"]})
        self.assertIn("检验参考片段", context)
        self.assertNotIn("其它资料", context)
        self.assertEqual(emit.call_args.args[1]["queries"], [])
        self.assertEqual(emit.call_args.args[1]["status"], "used")

    async def test_unavailable_retrieval_emits_no_evidence(self):
        kb = HealthKnowledgeBase()
        kb.aretrieve = AsyncMock(side_effect=RuntimeError("test outage"))
        with self.assertLogs("agents.knowledge_search", level="WARNING"), patch(
            "agents.knowledge_search.emit_card"
        ) as emit:
            context = await knowledge_search_tool(kb).ainvoke({"queries": ["测试查询"]})
        self.assertEqual(context, "")
        emit.assert_called_once_with("knowledge_retrieval", {
            "queries": ["测试查询"], "hits": [], "status": "unavailable",
        })

    async def test_pharmacy_card_lands_after_the_tool_and_before_the_summary(self):
        app = build_graph(checkpointer=MemorySaver())
        model = scripted(
            tool_call("find_nearby_pharmacy", {"location": "中关村"}, "c1"),
            AIMessage(content="请查看药店清单。"),
        )
        state = {"messages": [HumanMessage(content="中关村附近有药店吗？")], "active_agent": "pharmacy_agent"}

        with patch.object(main, "_master_app", app), patch("agents.pharmacy.get_chat_llm", return_value=model):
            events = _parse_sse([
                payload async for payload in main._stream_agent_events(
                    state, {"configurable": {"thread_id": "pharmacy-card-test"}},
                )
            ])

        tool_end = next(i for i, event in enumerate(events)
                        if event["type"] == "tool_end" and event.get("tool") == "find_nearby_pharmacy")
        card = next(i for i, event in enumerate(events) if event["type"] == "card")
        summary = next(i for i, event in enumerate(events)
                       if event["type"] == "text" and "请查看药店清单" in event["content"])
        self.assertLess(tool_end, card)
        self.assertLess(card, summary)
        self.assertEqual(events[card]["payload"]["type"], "hospital_list")
        self.assertEqual(events[-1]["type"], "finish")

    async def test_legacy_insurance_route_returns_guidance_only(self):
        app = build_graph(checkpointer=MemorySaver())
        router_model = scripted(AIMessage(content="insurance_agent"))
        state = {"messages": [HumanMessage(content="我医保卡里还有多少钱？")], "active_agent": "advisor_agent"}
        with patch.object(main, "_master_app", app), patch("agents.router.get_chat_llm", return_value=router_model):
            events = _parse_sse([
                payload async for payload in main._stream_agent_events(
                    state, {"configurable": {"thread_id": "insurance-guidance-test"}},
                )
            ])

        self.assertTrue(any(event["type"] == "text" and "国家医保服务平台" in event["content"] for event in events))
        self.assertFalse(any(event["type"] in {"tool_start", "card", "error"} for event in events))
        self.assertEqual(events[-1]["type"], "finish")


if __name__ == "__main__":
    unittest.main()
