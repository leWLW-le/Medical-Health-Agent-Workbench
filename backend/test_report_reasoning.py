"""Regression coverage for report questions, grounded generation and safe fallback."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from openai import APITimeoutError, RateLimitError

from report_audit.reading import (
    ModelObservation, ModelReading, ReadingPlan, ReadingPrompt, _gather, answer_reading, reason_reading, stream_reading,
)
from report_audit.reading_context import PatientContext, report_context_form
from report_audit.reading_knowledge import retrieve_notes
from report_audit.scan_analysis import ScanFinding


def finding(name, value, unit, reference, status, page=2):
    return ScanFinding(name=name, value=value, unit=unit, reference=reference, status=status, page=page)


class ReadingReasoningTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = [
            finding("嗜碱性粒细胞比率BAS%", "1.21", "%", "0~1.00", "HIGH"),
            finding("嗜碱性粒细胞计数BAS#", "0.08", "10^9/L", "0~0.10", "NORMAL"),
            finding("白细胞WBC", "7.12", "10^9/L", "4~10", "NORMAL", page=3),
            finding("HGB", "998", "g/L", "110~160", "REVIEW", page=4),
        ]
        self.question = "嗜碱性粒细胞比率BAS%异常说明身体有什么问题？"

    def test_preflight_reuses_or_resolves_critical_absolute_count(self):
        count = self.rows[1]
        for candidates, status, action, cited in (
            ([count], "confirmed", "reuse_report_evidence", True),
            ([], "missing", "lookup_report", False),
            ([count.model_copy(update={"status": "REVIEW"})], "pending_confirmation", "lookup_report", False),
            ([count, count.model_copy(update={"page": 4})], "ambiguous", "lookup_report", False),
        ):
            with self.subTest(status=status):
                request = ReadingPrompt(findings=[self.rows[0], *candidates], question=self.question)
                answer = answer_reading(request)
                check, = answer.evidence_checks
                self.assertEqual((check.indicator, check.status, check.action), ("BAS#", status, action))
                self.assertEqual(check.rows, list(range(1, 1 + len(candidates))))
                self.assertEqual(any(citation.row > 0 for citation in answer.citations), cited)
                if not cited:
                    self.assertIn("缺少可唯一对应", " ".join(answer.interpretation))
        # A critical field left outside the initial selection still reaches the verified report evidence.
        request = ReadingPrompt(findings=self.rows, question=self.question)
        answer = _gather({"request": request, "plan": ReadingPlan("indicator", (0,), interpret=True)})["answer"]
        self.assertEqual(answer.evidence_checks[0].action, "lookup_report")
        self.assertTrue(any(citation.row == 1 and citation.related for citation in answer.citations))

    async def test_ambiguous_evidence_is_a_limit_in_first_model_request(self):
        duplicate = self.rows[1].model_copy(update={"page": 4})
        request = ReadingPrompt(findings=[*self.rows, duplicate], question=self.question)
        observation = ModelObservation(explanation="嗜碱性粒细胞的占比高于报告参考范围。", report_rows=[0], source_ids=["basophils"])
        mock_llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=ModelReading(observations=[observation]).model_dump_json())))
        mock_llm.bind_tools.return_value = mock_llm
        with patch("report_audit.reading.get_chat_llm", return_value=mock_llm):
            answer = await reason_reading(request)
        evidence = json.loads(mock_llm.ainvoke.await_args.args[0][-1].content)
        self.assertEqual(evidence["evidence_checks"][0]["status"], "ambiguous")
        self.assertTrue({1, 4}.isdisjoint({row["row"] for row in evidence["confirmed_rows"]}))
        self.assertEqual(answer.work_record.tool_rounds, [])

    async def test_explains_why_to_check_bas_count_even_when_report_lacks_that_row(self):
        rows = [self.rows[0], *self.rows[2:]]
        with patch("report_audit.reading.get_chat_llm") as provider:
            answer = await reason_reading(ReadingPrompt(findings=rows, question="为什么要查找BAS#？"))
        provider.assert_not_called()
        self.assertIn("绝对计数仍可能在参考范围内", answer.direct_answer)
        self.assertEqual(answer.evidence_checks[0].status, "missing")
        self.assertEqual(answer.work_record.skip_reason, "evidence_purpose")
        self.assertEqual(answer.sources[0].url, "https://medlineplus.gov/lab-tests/blood-differential/")

        for question in ("查 BAS# 有什么用？", "为什么要看嗜碱性粒细胞的绝对计数？"):
            result = answer_reading(ReadingPrompt(findings=self.rows, question=question))
            self.assertIn("BAS%", result.direct_answer)
            self.assertEqual(result.evidence_checks[0].status, "confirmed")
            self.assertEqual([citation.row for citation in result.citations], [1])
        lookup = answer_reading(ReadingPrompt(findings=rows, question="这份报告里有同次的嗜碱性粒细胞绝对计数（BAS#）吗？"))
        self.assertIn("没有找到", lookup.text)
        self.assertEqual(lookup.direct_answer, "")
        self.assertEqual(answer_reading(ReadingPrompt(findings=rows, question="为什么查找不到 BAS#？")).direct_answer, "")

    def test_report_context_follows_confirmed_indicators_in_mixed_and_unknown_reports(self):
        liver = finding("总胆红素 TBIL", "8.8", "umol/L", "5~20", "NORMAL")
        kidney = finding("尿素 BUN", "2.5", "mmol/L", "2.5~6.3", "NORMAL")
        glucose = finding("GLU", "7.1", "mmol/L", "3.9~6.1", "HIGH")
        urine = finding("尿潜血 NH-BLD", "阳性", "", "阴性", "REPORTED")
        pending = finding("LYM#", "1.0", "10^9/L", "1.1~3.2", "REVIEW")
        rows = [liver, kidney, glucose, urine, pending]
        schema = report_context_form(rows)
        self.assertEqual([group.key for group in schema.groups], ["general", "liver", "kidney", "metabolic", "urinalysis"])
        self.assertEqual([q.key for q in schema.groups[-1].questions], ["urinary_symptoms", "menstrual_collection"])
        self.assertEqual([group.key for group in report_context_form([pending]).groups], ["general"])
        mixed = report_context_form([*rows, pending.model_copy(update={"status": "LOW"})])
        self.assertIn("previous_low_lymphocytes", [q.key for q in mixed.groups[1].questions])
        normal_biochemistry = answer_reading(ReadingPrompt(
            findings=[liver, kidney], question="结合情况解读整份报告", patient_context=PatientContext(
                liver_medicine=True, kidney_medicine=True,
            ),
        ))
        self.assertNotIn("近期用药", " ".join(normal_biochemistry.guidance.possibilities))
        self.assertIn("补充剂可影响肌酐", " ".join(normal_biochemistry.guidance.possibilities))
        for name, unit in (("尿葡萄糖 GLU", "mmol/L"), ("降钙素原 PCT", "ng/mL"), ("糖化血红蛋白 HbA1c", "%")):
            schema = report_context_form([finding(name, "1", unit, "0~2", "NORMAL")])
            self.assertNotIn("blood_count", [group.key for group in schema.groups])
            self.assertNotIn("metabolic", [group.key for group in schema.groups])

    def test_liver_medication_details_are_optional_bounded_and_conditionally_kept(self):
        schema = report_context_form([finding("ALT", "20", "U/L", "0~40", "NORMAL")])
        name, timing = schema.groups[1].questions[1:]
        self.assertEqual((name.kind, name.requires, name.max_length), ("text", "liver_medicine", 100))
        self.assertEqual(timing.key, "liver_medicine_since")
        context = PatientContext(liver_medicine=True, liver_medicine_name=" 合成药物 ", liver_medicine_since=" ")
        kept = schema.filter_answers(context)
        self.assertEqual(kept.liver_medicine_name, "合成药物")
        self.assertIsNone(kept.liver_medicine_since)
        self.assertIsNone(schema.filter_answers(context.model_copy(update={"liver_medicine": False})).liver_medicine_name)
        self.assertFalse(report_context_form(self.rows).filter_answers(context).liver_medicine)

    def test_medication_followup_depends_on_liver_results_and_missing_details(self):
        normal = finding("ALT", "20", "U/L", "0~40", "NORMAL")
        high = finding("AST", "56", "U/L", "0~40", "HIGH")
        pending = high.model_copy(update={"status": "REVIEW"})
        base = ReadingPrompt(findings=[*self.rows, normal], question="近期用药与这份报告有关系吗？",
                             patient_context=PatientContext(liver_medicine=True))
        within = answer_reading(base)
        self.assertIn("ALT 在报告参考范围内", " ".join(within.guidance.possibilities))
        self.assertEqual(within.context_question, "")
        self.assertEqual(within.citations[-1].value, "20")
        incomplete = answer_reading(base.model_copy(update={"findings": [*base.findings, pending]}))
        self.assertIn("结果还不完整", " ".join(incomplete.guidance.possibilities))
        self.assertEqual(incomplete.context_question, "")
        outside_request = base.model_copy(update={"findings": [*base.findings, high]})
        outside = answer_reading(outside_request)
        self.assertIn("AST 高于报告参考范围", " ".join(outside.guidance.possibilities))
        self.assertEqual(outside.context_follow_up.field, "liver_medicine_name")
        self.assertIn("叫什么名字", outside.context_question)
        updated = PatientContext(liver_medicine=True, liver_medicine_name="合成药物")
        name_only = answer_reading(outside_request.model_copy(update={"patient_context": updated}))
        self.assertIn("之前还是之后", name_only.context_question)
        self.assertNotIn("名称", name_only.context_question)
        updated = updated.model_copy(update={"liver_medicine_since": "抽血前一周"})
        both = answer_reading(outside_request.model_copy(update={"patient_context": updated}))
        self.assertEqual(both.context_question, "")

    def test_conversation_asks_only_when_relevant_and_remembers_each_reply(self):
        lymphocytes = finding("LYM#", "1.01", "10^9/L", "1.10~3.20", "LOW")
        glucose = finding("GLU", "7.1", "mmol/L", "3.9~6.1", "HIGH")
        request = ReadingPrompt(findings=[lymphocytes, glucose], question="这些异常是什么原因造成？")
        self.assertEqual(answer_reading(request).context_follow_up.field, "recent_infection")
        reply = request.model_copy(update={"context_replies": {"recent_infection": "上周感冒了"},
                                           "reply_field": "recent_infection"})
        answered = answer_reading(reply)
        self.assertTrue(answered.guidance.used_context)
        self.assertIn("你提到近期有过感染", " ".join(answered.guidance.possibilities))
        self.assertIsNone(answered.context_follow_up)

        for choice in ("没有", "不确定", "跳过", "上周没有感冒"):
            updated = request.model_copy(update={"context_replies": {"recent_infection": choice}})
            answer = answer_reading(updated)
            self.assertFalse(answer.guidance.used_context)
            self.assertEqual(answer.context_follow_up.field, "fasting")

        other_report = reply.model_copy(update={"findings": [glucose], "reply_field": None})
        unrelated = answer_reading(other_report)
        self.assertFalse(unrelated.guidance.used_context)
        self.assertEqual(unrelated.context_follow_up.field, "fasting")

    async def test_explicit_urgent_statement_uses_existing_rule_for_current_turn_only(self):
        request = ReadingPrompt(findings=self.rows, question="我现在正在大量出血。")
        with patch("report_audit.reading.get_chat_llm") as provider:
            answer = await reason_reading(request)
            provider.assert_not_called()
        self.assertEqual(answer.guidance.care_level, "emergency")
        self.assertIsNone(answer.context_follow_up)
        negated = answer_reading(request.model_copy(update={"question": "我现在没有大量出血"}))
        self.assertIsNone(negated.guidance)

    async def test_model_uses_medication_timeline_only_with_relevant_outside_liver_evidence(self):
        question = "结合我填写的情况，这份报告可能与什么有关？"
        normal = finding("ALT", "20", "U/L", "0~40", "NORMAL")
        high = finding("AST", "56", "U/L", "0~40", "HIGH")
        context = PatientContext(liver_medicine=True, liver_medicine_name="合成药物")
        for rows, related in (([self.rows[0], normal], False), ([self.rows[0], normal, high], True)):
            with self.subTest(related=related):
                observations = [ModelObservation(explanation="嗜碱性粒细胞的占比高于报告参考范围。",
                                                 report_rows=[0], source_ids=["basophils"])]
                if related:
                    observations.append(ModelObservation(
                        explanation="AST 超出范围，近期用药的时间线值得核对。现有资料不足以判断原因。",
                        report_rows=[2], source_ids=["liver_enzymes"], context_ref="liver_medication",
                    ))
                message = AIMessage(content=ModelReading(observations=observations).model_dump_json())
                llm = Mock(ainvoke=AsyncMock(return_value=message))
                llm.bind_tools.return_value = llm
                with patch("report_audit.reading.get_chat_llm", return_value=llm):
                    answer = await reason_reading(ReadingPrompt(findings=rows, question=question, patient_context=context))
                self.assertTrue(answer.model_is_primary)
                evidence = json.loads(llm.ainvoke.await_args.args[0][-1].content)
                medication = evidence["liver_medication"]
                self.assertEqual(medication["status"], "outside" if related else "within")
                self.assertEqual(medication["missing"], ["since"] if related else [])
                self.assertEqual("self_reported_details" in medication, related)
                self.assertEqual(bool(answer.context_question), related)
                if related:
                    self.assertEqual(medication["self_reported_details"], {"name": "合成药物", "since": None})
                    self.assertEqual(answer.observations[1].rows, [2])

    async def test_medication_association_with_the_wrong_row_cannot_be_published(self):
        rows = [finding("ALT", "20", "U/L", "0~40", "NORMAL"), finding("AST", "56", "U/L", "0~40", "HIGH")]
        for snippet, cited in (
            ("ALT 超出范围，近期用药值得核对。", 0),
            ("AST 升高由近期用药导致。", 1),
            ("AST 偏高，近期用药可作为线索。肝功能检查结果未见异常。", 1),
        ):
            with self.subTest(snippet=snippet):
                output = ModelReading(observations=[ModelObservation(
                    explanation=snippet, report_rows=[cited], source_ids=["liver_enzymes"], context_ref="liver_medication",
                )])
                llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
                llm.bind_tools.return_value = llm
                with patch("report_audit.reading.get_chat_llm", return_value=llm):
                    answer = await reason_reading(ReadingPrompt(findings=rows, question="解读整份报告",
                                                               patient_context=PatientContext(liver_medicine=True)))
                self.assertEqual(answer.mode, "fallback")
                self.assertFalse(answer.model_is_primary)
                self.assertTrue(answer.context_question)

    async def test_emergency_advice_remains_available_when_question_has_no_confirmed_answer(self):
        with patch("report_audit.reading.get_chat_llm") as provider:
            for question in ("HGB 异常说明什么", "请多说一点"):
                answer = await reason_reading(ReadingPrompt(findings=self.rows, question=question,
                                                           patient_context=PatientContext(severe_bleeding=True)))
                self.assertEqual(answer.guidance.care_level, "emergency")
                self.assertEqual(answer.mode, "evidence")
            provider.assert_not_called()

    async def test_irrelevant_context_is_discarded_before_model_invocation(self):
        rows = [finding("GLU", "7.1", "mmol/L", "3.9~6.1", "HIGH")]
        context = PatientContext(fasting="no", ongoing_fever=True, liver_medicine=True)
        reply = ModelReading(observations=[ModelObservation(
            explanation="血糖高于报告参考范围。", report_rows=[0], source_ids=["reference_ranges"],
        )])
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=reply.model_dump_json())))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=rows, question="整份报告如何解读？",
                                                        patient_context=context))
        self.assertIn("没有按要求空腹", " ".join(answer.guidance.possibilities))
        self.assertEqual(answer.guidance.care_level, "routine")
        payload = str(llm.ainvoke.await_args)
        self.assertIn('"fasting": "no"', payload)
        self.assertNotIn('"ongoing_fever"', payload)
        self.assertNotIn('"liver_medicine"', payload)

    def test_report_question_overrides_focus_and_excludes_pending_rows(self):
        answer = answer_reading(ReadingPrompt(findings=self.rows, row=1, question="异常指标说明身体有什么问题？"))
        self.assertEqual((answer.scope, answer.row, answer.mode), ("report", None, "evidence"))
        self.assertIn("1 项超出报告参考范围，1 项待确认", answer.text)
        self.assertEqual([(c.row, c.page, c.related) for c in answer.citations], [(0, 2, False), (1, 2, True), (2, 3, True)])
        self.assertIn("报告中的 BAS# 在报告参考范围内", " ".join(answer.interpretation))
        self.assertNotIn("998", answer.model_dump_json())

    async def test_named_question_uses_model_and_cites_only_supplied_evidence(self):
        output = ModelReading(observations=[ModelObservation(
            explanation="BAS% 偏高，而 BAS# 在报告参考范围内。这次变化体现在相对比例上。",
            report_rows=[0, 1], source_ids=["basophils"],
        )])
        completion = AsyncMock(return_value=AIMessage(content=output.model_dump_json()))
        llm = Mock(ainvoke=completion)
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=self.rows, row=3, question=self.question))
        self.assertEqual((answer.mode, answer.row, answer.page), ("model", 0, 2))
        self.assertEqual(answer.observations[0].rows, [0, 1])
        self.assertTrue(answer.observations[0].sources[0].url.endswith("/blood-differential/"))
        self.assertEqual(answer.citations[0].value, "1.21")
        self.assertNotIn("998", str(completion.await_args))

    async def test_explain_button_and_simple_value_question_use_model_by_default(self):
        output = ModelReading(observations=[ModelObservation(
            explanation="这是嗜碱性粒细胞在白细胞中所占的比例。",
            report_rows=[0], source_ids=["basophils"],
        )])
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            explanation = await reason_reading(ReadingPrompt(findings=self.rows, row=0))
            lookup = await reason_reading(ReadingPrompt(findings=self.rows, row=0, question="这项结果是多少？"))
        self.assertEqual(explanation.mode, "model")
        self.assertEqual(lookup.mode, "model")
        self.assertIn("1.21 %", lookup.text)
        self.assertEqual(llm.ainvoke.await_count, 2)

    async def test_unverified_model_claims_and_unavailable_provider_use_grounded_fallback(self):
        for explanation, rows, sources in [
            ("估计结果已超过 5。", [0], ["basophils"]),
            ("待确认指标偏高。", [3], ["basophils"]),
            ("此项与其他结果有关。", [0], ["invented"]),
            ("BAS% 偏高，而 BAS# 升高。", [0, 1], ["basophils"]),
            ("BAS% 偏高提示可能有过敏反应。", [0], ["basophils"]),
            ("BAS%高于正常范围，可能表示身体对某些过敏原的反应增强，但需要结合其他症状和检查结果进一步判断。",
             [0], ["basophils"]),
        ]:
            with self.subTest(explanation=explanation):
                output = ModelReading(observations=[ModelObservation(explanation=explanation, report_rows=rows, source_ids=sources)])
                llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
                llm.bind_tools.return_value = llm
                with patch("report_audit.reading.get_chat_llm", return_value=llm):
                    answer = await reason_reading(ReadingPrompt(findings=self.rows, question=self.question))
                self.assertEqual(answer.mode, "fallback")
                self.assertEqual(answer.observations, [])
                self.assertTrue(answer.interpretation)
                self.assertEqual(answer.work_record.fallback_reason, "output_rejected")
                self.assertEqual([call.rejection for call in answer.work_record.model_calls],
                                 ["unverified_output", "unverified_output"])
        with patch("report_audit.reading.get_chat_llm", side_effect=RuntimeError):
            answer = await reason_reading(ReadingPrompt(findings=self.rows, question=self.question))
        self.assertEqual(answer.mode, "fallback")

    async def test_work_record_distinguishes_provider_status_and_rewrite_timeout(self):
        request = httpx.Request("POST", "https://example.invalid/model")
        limit = RateLimitError("private text", response=httpx.Response(429, request=request), body=None)
        for replies, reason in (([limit], "provider_error"),
                                ([AIMessage(content="模型私有原文"), APITimeoutError(request)], "provider_timeout")):
            with self.subTest(reason=reason):
                llm = Mock(ainvoke=AsyncMock(side_effect=replies))
                llm.bind_tools.return_value = llm
                with patch("report_audit.reading.get_chat_llm", return_value=llm):
                    answer = await reason_reading(ReadingPrompt(findings=self.rows, question=self.question))
                record = answer.work_record
                self.assertEqual((answer.mode, record.fallback_reason, record.model_call_count),
                                 ("fallback", reason, len(replies)))
                self.assertEqual(record.tool_rounds, [])
                self.assertNotIn("模型私有原文", record.model_dump_json())
                self.assertNotIn("private text", record.model_dump_json())
                if reason == "provider_error":
                    self.assertEqual((record.model_calls[0].status, record.model_calls[0].http_status), ("failed", 429))
                else:
                    self.assertEqual(record.model_calls[0].rejection, "invalid_format")
                    self.assertEqual((record.model_calls[1].phase, record.model_calls[1].status), ("rewrite", "timeout"))

    async def test_work_record_tracks_agent_deadline_with_an_in_flight_model_call(self):
        async def never_replies(*args, **kwargs):
            await asyncio.Future()

        llm = Mock(ainvoke=AsyncMock(side_effect=never_replies))
        llm.bind_tools.return_value = llm
        with (patch("report_audit.reading.get_chat_llm", return_value=llm),
              patch("report_audit.reading.retrieve_notes", return_value=retrieve_notes(("BAS%",))),
              patch("report_audit.reading.MODEL_TIMEOUT_SECONDS", 0.15)):
            answer = await reason_reading(ReadingPrompt(findings=self.rows, question=self.question))
        self.assertEqual(answer.mode, "fallback")
        self.assertEqual(answer.work_record.fallback_reason, "agent_timeout")
        self.assertEqual(answer.work_record.model_call_count, 1)
        self.assertEqual(answer.work_record.model_calls[0].status, "timeout")

    async def test_initial_timeout_gets_one_bounded_retry_with_available_evidence(self):
        async def never_replies(*args, **kwargs):
            await asyncio.Future()

        tool_model = Mock(ainvoke=AsyncMock(side_effect=never_replies))
        output = ModelReading(observations=[ModelObservation(
            explanation="嗜碱性粒细胞占比偏高，可以和实际计数对照理解。",
            report_rows=[0, 1], source_ids=["basophils"],
        )])
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
        llm.bind_tools.return_value = tool_model
        with (patch("report_audit.reading.get_chat_llm", return_value=llm) as create_llm,
              patch("report_audit.reading.MODEL_CALL_TIMEOUT_SECONDS", 0.1)):
            answer = await reason_reading(ReadingPrompt(findings=self.rows, question=self.question))
        self.assertEqual(answer.mode, "model")
        self.assertEqual(answer.work_record.model_call_count, 2)
        self.assertEqual([(call.phase, call.status) for call in answer.work_record.model_calls],
                         [("initial", "timeout"), ("timeout_retry", "returned")])
        self.assertEqual(answer.work_record.tool_rounds, [])
        self.assertIsNone(answer.work_record.fallback_reason)
        self.assertEqual(create_llm.call_args.kwargs["max_retries"], 0)
        self.assertEqual(llm.ainvoke.await_args.kwargs["response_format"], {"type": "json_object"})

    async def test_initial_timeout_without_selected_evidence_does_not_retry_blindly(self):
        request = httpx.Request("POST", "https://example.invalid/model")
        llm = Mock(ainvoke=AsyncMock(side_effect=APITimeoutError(request)))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            reply = await reason_reading(ReadingPrompt(findings=self.rows, question="化验这一条怎么读？"))
        self.assertEqual(reply.mode, "fallback")
        self.assertEqual(reply.work_record.fallback_reason, "provider_timeout")
        self.assertEqual(reply.work_record.model_call_count, 1)

    async def test_pending_only_question_and_explanation_do_not_call_model(self):
        with patch("report_audit.reading.get_chat_llm") as provider:
            pending = await reason_reading(ReadingPrompt(findings=self.rows, question="HGB 异常说明什么"))
            explanation = await reason_reading(ReadingPrompt(findings=self.rows, row=3))
        self.assertEqual(explanation.mode, "evidence")
        self.assertEqual(pending.citations, [])
        self.assertIn("待确认", pending.text)
        self.assertEqual(pending.work_record.skip_reason, "pending_confirmation")
        self.assertEqual(pending.work_record.model_call_count, 0)
        provider.assert_not_called()

    async def test_stream_publishes_a_verified_paragraph_before_the_model_finishes(self):
        release = asyncio.Event()
        safe = "嗜碱性粒细胞的占比高于报告参考范围。"
        bad = ModelObservation(explanation="未经证实的HGB说明", report_rows=[3], source_ids=["reference_ranges"])
        good = ModelObservation(explanation=safe, report_rows=[0], source_ids=["basophils"])

        async def chunks(*args, **kwargs):
            yield AIMessageChunk(content='{"observations":[' + bad.model_dump_json() + ',' + good.model_dump_json()[:-1])
            yield AIMessageChunk(content="}")
            await release.wait()
            yield AIMessageChunk(content="]}")

        llm = Mock(astream=chunks)
        llm.bind_tools.return_value = llm
        events = []
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            async with asyncio.timeout(5):
                async for event in stream_reading(ReadingPrompt(findings=self.rows, question=self.question)):
                    events.append(event)
                    if event["type"] == "text":
                        self.assertEqual(event["content"], safe)
                        self.assertNotIn("answer", [previous["type"] for previous in events])
                        release.set()
        self.assertTrue(release.is_set())
        self.assertEqual([event["type"] for event in events], ["node_start", "text", "node_end", "answer", "finish"])
        self.assertEqual(events[-2]["payload"]["mode"], "model")
        self.assertEqual(len(events[-2]["payload"]["observations"]), 1)

    def test_repeated_samples_are_not_assumed_to_match_and_percent_differs_from_count(self):
        repeated = self.rows + [self.rows[1].model_copy(update={"page": 4, "status": "REVIEW"})]
        percentage = answer_reading(ReadingPrompt(findings=repeated, question=self.question))
        self.assertEqual([row.row for row in percentage.citations], [0, 2])
        self.assertIn("缺少可唯一对应的已确认 BAS#", " ".join(percentage.interpretation))
        ambiguous = answer_reading(ReadingPrompt(findings=repeated, question="嗜碱性粒细胞计数代表什么？"))
        self.assertEqual(ambiguous.scope, "unresolved")

    def test_urine_and_blood_counts_are_separate(self):
        rows = [finding("WBC", "8", "10^9/L", "4~10", "NORMAL"),
                finding("白细胞NH-UWBC", "2", "/uL", "0~10", "NORMAL")]
        answer = answer_reading(ReadingPrompt(findings=rows, question="WBC 说明什么？"))
        self.assertEqual([item.row for item in answer.citations], [0])

    async def test_sparse_model_response_falls_back_to_full_report_interpretation(self):
        rows = [
            finding("嗜碱性粒细胞比率BAS%", "1.24", "%", "0~1.00", "HIGH"),
            finding("淋巴细胞计数1YM#", "1.03", "10^9/L", "1.10~3.20", "LOW"),
            finding("平均血小板体积MPV", "6.50", "fL", "9.00~13.00", "LOW", page=3),
            finding("血小板压积pet1", "0.10", "%", "0.11~0.27", "LOW", page=3),
            finding("血小板分布宽度PDW", "19.80", "fL", "15.00~18.00", "HIGH", page=3),
            finding("淋巴细胞比率LYM%", "25.40", "%", "20.00~50.00", "NORMAL"),
            finding("血小板PLT", "150", "10^9/L", "125~350", "NORMAL", page=3),
        ]
        output = ModelReading(observations=[ModelObservation(
            explanation="BAS% 偏高表示这种白细胞所占比例超过报告参考范围。",
            report_rows=[0], source_ids=["basophils"],
        )])
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(
                findings=rows, question="异常指标说明身体有什么问题？可能有什么病症",
            ))
        self.assertEqual(answer.mode, "fallback")
        self.assertFalse(answer.model_is_primary)
        self.assertEqual(answer.observations, [])
        initial, repair = answer.work_record.model_calls
        self.assertEqual(initial.missing_groups, [[1], [2, 3, 4]])
        self.assertEqual((repair.phase, repair.missing_groups), ("coverage_repair", initial.missing_groups))
        self.assertEqual(len([row for row in answer.citations if not row.related]), 5)
        interpretation = " ".join(answer.interpretation)
        self.assertIn("单凭比例偏高无法判断是否有过敏", interpretation)
        self.assertIn("淋巴细胞绝对计数（LYM#）低于报告参考范围。淋巴细胞比例（LYM%）在报告参考范围内", interpretation)
        self.assertIn("压积（PCT）低于报告参考范围，体积分布宽度（PDW）高于报告参考范围", interpretation)
        self.assertIn("血小板计数（PLT）在报告参考范围内", interpretation)
        self.assertIn("这份报告单独无法确定具体病症", answer.limitation)
        self.assertTrue(any("PMC6517618" in source.url for source in answer.sources))
        self.assertTrue(any("lymphopenia" in source.url for source in answer.sources))

    async def test_coverage_repair_keeps_grounded_paragraphs_and_requests_only_missing_groups(self):
        rows = [self.rows[0], finding("LYM#", "1.03", "10^9/L", "1.10~3.20", "LOW"),
                finding("MPV", "6.50", "fL", "9.00~13.00", "LOW")]
        first = ModelReading(observations=[
            ModelObservation(explanation="嗜碱性粒细胞占比偏高，单凭这项只能判断比例。", report_rows=[0], source_ids=["basophils"]),
            ModelObservation(explanation="平均血小板体积偏低，提示这些细胞的平均大小较小。", report_rows=[2], source_ids=["platelet_size"]),
            ModelObservation(explanation="淋巴细胞数量低于报告范围。", report_rows=[1], source_ids=["invalid_source"]),
        ])
        supplement = ModelReading(observations=[
            first.observations[0],
            ModelObservation(explanation="淋巴细胞计数偏低，需要结合以往结果看这种变化是否持续。",
                             report_rows=[1], source_ids=["lymphocytes"]),
        ])
        replies = [AIMessage(content=reply.model_dump_json()) for reply in (first, supplement)]
        llm = Mock(ainvoke=AsyncMock(side_effect=replies))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=rows, question="这些异常是什么原因造成？"))
        self.assertTrue(answer.model_is_primary)
        self.assertEqual([item.text for item in answer.observations],
                         [first.observations[0].explanation, first.observations[1].explanation, supplement.observations[1].explanation])
        self.assertIsNone(answer.work_record.fallback_reason)
        self.assertEqual(answer.work_record.tool_rounds, [])
        self.assertEqual([(call.phase, call.missing_groups) for call in answer.work_record.model_calls],
                         [("initial", [[1]]), ("coverage_repair", [])])
        evidence = json.loads(llm.ainvoke.await_args.args[0][-1].content)
        self.assertEqual(evidence["required_abnormal_groups"], [[1]])

    def test_related_values_support_calculation_without_a_model(self):
        rows = [
            finding("嗜碱性粒细胞比率BAS%", "1.28", "%", "0~1.00", "HIGH"),
            finding("淋巴细胞计数1YM#", "1.02", "10~9/L", "1.10~3.20", "LOW"),
            finding("平均血小板体积MPV", "6.45", "fL", "9.00~13.00", "LOW", page=3),
            finding("血小板压积pet1", "0.09", "", "0.11~0.27", "LOW", page=3),
            finding("血小板分布宽度PDW", "19.88", "fL", "15.00~18.00", "HIGH", page=3),
            finding("淋巴细胞比率LYM%", "25.50", "%", "20.00~50.00", "NORMAL"),
            finding("血小板PLT", "147", "10⁹/L", "125~350", "NORMAL", page=3),
        ]
        answer = answer_reading(ReadingPrompt(findings=rows, question="这些异常怎么一起理解？"))
        platelet, lymphocytes = answer.checks
        self.assertEqual(platelet.rows, [6, 2, 3])
        self.assertIn("≈ 0.0948%", platelet.calculation)
        self.assertIn("按 % 试算", platelet.calculation)
        self.assertIn("到原页确认单位", platelet.conclusion)
        self.assertIn("平均体积偏小与压积偏低在数值上相符", platelet.conclusion)
        self.assertEqual(lymphocytes.rows, [1, 5])
        self.assertIn("≈ 4 ×10⁹/L", lymphocytes.calculation)
        self.assertIn("反推", lymphocytes.conclusion)
        self.assertIn("到原页确认计数单位", lymphocytes.conclusion)
        self.assertIn("缺少可唯一对应的已确认 BAS#", " ".join(answer.interpretation))
        actions = answer.follow_ups
        self.assertEqual(actions[0].row, 3)
        self.assertIsNone(actions[0].question)
        self.assertTrue(all(action.row is not None and action.question is None for action in actions))

    async def test_patient_context_changes_assessment_and_care_from_confirmed_results(self):
        from report_audit.reading import PatientContext

        rows = [*self.rows, finding("LYM#", "1.01", "10^9/L", "1.10~3.20", "LOW")]
        question = "结合我填写的情况，这份报告可能与什么有关？什么时候需要就医？"
        base = answer_reading(ReadingPrompt(findings=rows, question=question))
        self.assertEqual(base.guidance.care_level, "routine")
        self.assertFalse(base.guidance.used_context)
        self.assertNotIn("你填了", " ".join(base.guidance.possibilities))
        contextual = answer_reading(ReadingPrompt(
            findings=rows, question=question, patient_context=PatientContext(
                recent_infection=True, immune_medicine=True, previous_low_lymphocytes=True, ongoing_fever=True,
            ),
        ))
        self.assertEqual(contextual.guidance.care_level, "prompt")
        self.assertTrue(contextual.guidance.used_context)
        for phrase in ("你提到近期有过感染", "你填了在用激素", "你填了以前也出现过"):
            self.assertIn(phrase, " ".join(contextual.guidance.possibilities))
        self.assertIn("建议尽快联系医生", contextual.guidance.next_step)
        child = answer_reading(ReadingPrompt(findings=rows, question=question, patient_context=PatientContext(
            age_group="under_18", recent_infection=True,
        )))
        self.assertIn("请由儿科医生按年龄解读", child.guidance.next_step)
        self.assertNotIn("你提到近期有过感染", " ".join(child.guidance.possibilities))

        with patch("report_audit.reading.get_chat_llm") as provider:
            emergency = await reason_reading(ReadingPrompt(
                findings=rows, question=question, patient_context=PatientContext(severe_bleeding=True),
            ))
        self.assertEqual(emergency.guidance.care_level, "emergency")
        self.assertIn("立即拨打当地急救电话", emergency.guidance.next_step)
        provider.assert_not_called()

    async def test_model_receives_opted_in_context_and_cannot_override_care_level(self):
        from report_audit.reading import PatientContext

        rows = [finding("LYM#", "1.01", "10^9/L", "1.10~3.20", "LOW")]
        question = "整份报告的异常与我的情况有什么关系？"
        output = ModelReading(observations=[ModelObservation(
            explanation="淋巴细胞绝对计数低于报告参考范围。", report_rows=[0], source_ids=["lymphocytes"],
        )])
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=rows, question=question,
                                                       patient_context=PatientContext(ongoing_fever=True)))
        self.assertEqual(answer.mode, "model")
        self.assertEqual(answer.guidance.care_level, "prompt")
        self.assertIn('"ongoing_fever": true', str(llm.ainvoke.await_args))

    def test_report_field_lookup_ignores_prior_focus_and_links_only_the_matching_row(self):
        question = "这份报告里有同次的白细胞总数（WBC）吗？"
        answer = answer_reading(ReadingPrompt(findings=self.rows, row=0, question=question))
        self.assertEqual((answer.scope, answer.row, answer.page), ("indicator", 2, 3))
        self.assertIn("7.12", answer.text)
        self.assertIn("是否为同次采样", answer.text)

        pending = [*self.rows[:2], self.rows[2].model_copy(update={"status": "REVIEW", "value": "888"})]
        answer = answer_reading(ReadingPrompt(findings=pending, question=question))
        self.assertIn("待确认", answer.text)
        self.assertNotIn("888", answer.text)
        self.assertEqual(answer.row, 2)

        duplicated = self.rows + [self.rows[2].model_copy(update={"page": 4})]
        answer = answer_reading(ReadingPrompt(findings=duplicated, question=question))
        self.assertIn("采样时间或样本编号", answer.text)
        self.assertEqual([action.row for action in answer.follow_ups], [2, 4])

    def test_follow_up_uses_previous_report_topic_but_named_item_overrides_it(self):
        rows = [*self.rows, finding("LYM#", "1.01", "10^9/L", "1.10~3.20", "LOW")]
        first = answer_reading(ReadingPrompt(findings=rows, question="淋巴细胞计数偏低说明什么？"))
        second = answer_reading(ReadingPrompt(findings=rows, previous_topic=first.topic,
                                                   question="那这个是不是一直这样？"))
        self.assertEqual(second.topic.rows, first.topic.rows)
        self.assertIn("对照以前同项的结果", second.overview)
        self.assertIn("以前查血", second.context_question)
        switched = answer_reading(ReadingPrompt(findings=rows, previous_topic=second.topic,
                                                     question="那 BAS% 呢？"))
        self.assertEqual((switched.scope, switched.topic.rows), ("indicator", [0]))
        self.assertEqual(switched.row, 0)

    async def test_agent_fetches_hidden_rows_and_notes_and_cites_only_returned_evidence(self):
        from langchain_core.messages import ToolMessage

        rows = [*self.rows, finding("MPV", "6.45", "fL", "9~13", "LOW", page=3),
                finding("PCT", "0.09", "%", "0.11~0.27", "LOW", page=3),
                finding("PLT", "147", "10^9/L", "125~350", "NORMAL", page=3)]
        tool_request = AIMessage(content="", tool_calls=[
            {"name": "lookup_report", "args": {"terms": "PLT HGB"}, "id": "lookup"},
            {"name": "check_lab_relationships", "args": {"terms": "小板的个头"}, "id": "check"},
            {"name": "search_lab_knowledge", "args": {"terms": "PCT"}, "id": "source"},
        ])
        output = ModelReading(observations=[ModelObservation(
            explanation="平均体积与血小板压积有关，可以用它和血小板数量一起理解这项变化。",
            report_rows=[4, 5, 6], source_ids=["platelet_size"],
        )])
        llm = Mock(ainvoke=AsyncMock(side_effect=[tool_request, AIMessage(content=output.model_dump_json())]))
        llm.bind_tools.return_value = llm
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=rows, question="那这一栏能串起来吗？"))
        self.assertEqual(answer.mode, "model")
        self.assertEqual(answer.topic.rows, [4, 5, 6])
        self.assertEqual({item.row for item in answer.citations}, {4, 5, 6})
        self.assertIn("0.0948%", answer.checks[0].calculation)
        evidence = [message for message in llm.ainvoke.await_args.args[0] if isinstance(message, ToolMessage)]
        self.assertEqual(len(evidence), 3)
        self.assertIn("待确认", evidence[0].content)
        self.assertNotIn("998", evidence[0].content)
        record = answer.work_record
        self.assertEqual(record.model_call_count, 2)
        self.assertEqual([call.phase for call in record.model_calls], ["initial", "after_tools"])
        tools, = record.tool_rounds
        self.assertEqual([tool.name for tool in tools], ["lookup_report", "check_lab_relationships", "search_lab_knowledge"])
        self.assertTrue(all(tool.status == "completed" for tool in tools))
        self.assertIn("1 条需核对", tools[0].result)
        self.assertIn("血小板压积交叉核验", tools[1].result)
        self.assertNotIn("998", record.model_dump_json())
        self.assertNotIn("0.0948%", record.model_dump_json())

    async def test_tool_follow_up_can_recover_provider_prose_without_repeating_tools(self):
        tool_request = AIMessage(content="", tool_calls=[
            {"name": "lookup_report", "args": {"terms": "BAS%"}, "id": "lookup"},
        ])
        output = ModelReading(observations=[ModelObservation(
            explanation="嗜碱性粒细胞的占比高于报告参考范围。", report_rows=[0], source_ids=["reference_ranges"],
        )])
        tool_model = Mock(ainvoke=AsyncMock(side_effect=[tool_request, AIMessage(content="这是一段普通文本。")]))
        llm = Mock(ainvoke=AsyncMock(return_value=AIMessage(content=output.model_dump_json())))
        llm.bind_tools.return_value = tool_model
        with patch("report_audit.reading.get_chat_llm", return_value=llm):
            answer = await reason_reading(ReadingPrompt(findings=self.rows, question="化验这一条怎么读？"))
        self.assertEqual(answer.mode, "model")
        self.assertEqual(answer.topic.rows, [0])
        self.assertEqual(tool_model.ainvoke.await_count, 2)
        self.assertEqual(llm.ainvoke.await_args.kwargs["response_format"], {"type": "json_object"})
        self.assertEqual([call.phase for call in answer.work_record.model_calls], ["initial", "after_tools", "rewrite"])
        self.assertEqual(answer.work_record.model_calls[1].rejection, "invalid_format")
        self.assertIsNone(answer.work_record.fallback_reason)

    def test_calculation_compares_printed_rounding_and_flags_inconsistent_data(self):
        rows = [
            finding("白细胞WBC", "4.0", "×10^3/µL", "3.5~9", "NORMAL"),
            finding("淋巴细胞计数LYM#", "1.00", "10⁹/L", "1.10~3.20", "LOW"),
            finding("淋巴细胞比率LYM%", "25.5", "%", "20~50", "NORMAL"),
        ]
        question = "LYM# 和 LYM% 对得上吗？"
        original = answer_reading(ReadingPrompt(findings=rows, question=question))
        check, = original.checks
        self.assertEqual(check.rows, [0, 2, 1])
        self.assertIn("≈ 1.02 ×10⁹/L", check.calculation)
        self.assertIn("差异超过", check.conclusion)
        corrected = rows.copy()
        corrected[1] = rows[1].model_copy(update={"value": "1.01"})
        check, = answer_reading(ReadingPrompt(findings=corrected, question=question)).checks
        self.assertIn("在报告显示精度内可以对上", check.conclusion)

    def test_calculation_requires_compatible_units_and_unambiguous_same_page_data(self):
        rows = [
            finding("血小板PLT", "150", "10^9/L", "125~350", "NORMAL"),
            finding("平均血小板体积MPV", "6.40", "fL", "9~13", "LOW"),
            finding("血小板压积PCT", "0.09", "%", "0.11~0.27", "LOW"),
        ]
        question = "MPV 和血小板压积怎么算？"
        for changes in ({"page": 3}, {"unit": "g/L"}, {"status": "REVIEW"}):
            with self.subTest(changes=changes):
                updated = [rows[0].model_copy(update=changes), *rows[1:]]
                answer = answer_reading(ReadingPrompt(findings=updated, question=question))
                self.assertEqual(answer.checks, [])
        repeated = [*rows, rows[0].model_copy(update={"page": 3})]
        self.assertEqual(answer_reading(ReadingPrompt(findings=repeated, question=question)).checks, [])
        procalcitonin = [*rows[:2], rows[2].model_copy(update={"name": "PCT", "unit": "ng/mL"})]
        self.assertEqual(answer_reading(ReadingPrompt(findings=procalcitonin, question=question)).checks, [])
