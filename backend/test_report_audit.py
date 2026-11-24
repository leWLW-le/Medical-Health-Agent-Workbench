from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from decimal import localcontext
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from evals.report_audit import render_scan
from report_audit.api import router
from report_audit.agent_service import app as agent_app
from report_audit.evidence import EvidenceContext, get_evidence_source
from report_audit.fixtures import DEMO_REPORT, OBSERVATIONS, SyntheticEvidenceSource, observation
from report_audit.models import LabRow, Snapshot
from report_audit.parser import InvalidReport, parse_report
from report_audit.scan_analysis import TextBlock, analyze_scan, group_rows
from report_audit.store import create_case, get_snapshot, open_db, revise_case
from report_audit.workflow import RULES_VERSION, Route, audit_graph, compare_row

BASE = "/api/report-audit"


def make_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=420, height=600)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    lines = "\n".join(f"({line}) Tj T*" for line in text.splitlines())
    stream.set_data(f"BT /F1 12 Tf 16 TL 45 540 Td\n{lines}\nET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def make_xlsx(text: str) -> bytes:
    book = Workbook()
    sheet = book.active
    lines = text.strip().splitlines()
    sheet.append(["合成检验报告"])
    for line in lines[:2]:
        sheet.append(line.split())
    sheet.append(["项目代码", "数值", "单位", "采样日期"])
    for line in lines[2:]:
        code, value, unit, collected = line.split()
        sheet.append([code, float(value), unit, date.fromisoformat(collected)])
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


class ReportAuditRulesTest(unittest.TestCase):
    def test_import_resolves_aliases_before_validating_duplicate_codes(self):
        text = (
            "PATIENT DEMO-002\nENCOUNTER VISIT-003\n"
            "项目名称 结果 单位 采样日期\n"
            "红细胞计数 4.52 ×10¹²/L 2026-08-12\n红细胞比容 0.421 L/L 2026-08-12\n"
            "ｍｃｖ 91.3 fL 2026-08-12\n谷草转氨酶 21 U/L 2026-08-12\nScr 0.083 mmol/L 2026-08-12"
        )
        draft = parse_report(text)
        self.assertEqual([row.code for row in draft.rows], ["RBC", "HCT", "MCV", "AST", "CREA"])
        self.assertTrue(all(not row.issues for row in audit_graph.invoke({"draft": draft})["result"].findings))
        with self.assertRaises(InvalidReport):
            parse_report(text + "\nCREA 83 μmol/L 2026-08-12")

    def test_conversion_preserves_decimal_precision_and_keeps_unmapped_units_distinct(self):
        source = observation("OBS-X", "DEMO-001", "VISIT-002", date(2026, 8, 12), "HGB", "132.123450", "g/L")
        for value, unit, expected in [
            ("13.212345", "g/dL", []),
            ("13.212346", "g/dL", ["value_mismatch"]),
            ("132.12345", "g/L", []),
            ("132.123451", "g/L", ["value_mismatch"]),
            ("132.12345", "mg/L", ["unit_mismatch"]),
        ]:
            with self.subTest(value=value, unit=unit), localcontext(prec=4):
                row = LabRow(code="Hb", value=value, unit=unit, collected_on=source.collected_on)
                self.assertEqual(compare_row(row, source).issues, expected)
        wbc = OBSERVATIONS[1]
        row = LabRow(code="白细胞", value="6.4000", unit="10³/uL", collected_on=wbc.collected_on)
        self.assertEqual(compare_row(row, wbc).issues, [])

    def test_repeat_draws_match_date_and_flag_ambiguity_without_selecting_an_arbitrary_record(self):
        draft = parse_report(DEMO_REPORT)
        hgb = OBSERVATIONS[0]
        stale_revision = hgb.model_copy(update={"revision": 1})
        yesterday = hgb.model_copy(update={"id": "OBS-X", "collected_on": date(2026, 8, 11)})
        same_day = hgb.model_copy(update={"id": "OBS-Y"})
        for additional, expected_id, expected_issues in [
            ((stale_revision, yesterday), hgb.id, ["value_mismatch"]),
            ((same_day,), None, ["ambiguous_evidence"]),
        ]:
            with self.subTest(additional=additional):
                provider = SyntheticEvidenceSource(additional + OBSERVATIONS)
                result = audit_graph.invoke({"draft": draft}, context=EvidenceContext(provider))["result"]
                first = result.findings[0]
                self.assertEqual(first.evidence.id if first.evidence else None, expected_id)
                self.assertEqual(first.issues, expected_issues)
                if first.evidence:
                    self.assertEqual(first.evidence.revision, 2)
        provider = SyntheticEvidenceSource((yesterday, same_day) + OBSERVATIONS)
        changed = parse_report(DEMO_REPORT.replace("123 g/L 2026-08-12", "123 g/L 2026-08-10"))
        result = audit_graph.invoke({"draft": changed}, context=EvidenceContext(provider))["result"]
        self.assertEqual(result.findings[0].issues, ["ambiguous_evidence"])

    def test_rule_version_change_rechecks_identical_rows_and_preserves_legacy_snapshot(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {
            "REPORT_AUDIT_DB": str(Path(directory) / "report_audit.sqlite3"),
        }):
            draft = parse_report(DEMO_REPORT)
            original = create_case(draft)
            with open_db() as db:
                db.execute("UPDATE audit_versions SET snapshot = ? WHERE case_id = ? AND version = 1", (
                    original.model_dump_json(exclude={"rules_version"}), original.case_id,
                ))
            legacy = get_snapshot(original.case_id, 1)
            self.assertEqual(legacy.rules_version, 1)
            upgraded = revise_case(original.case_id, 1, draft)
            self.assertEqual((upgraded.rules_version, upgraded.checked_rows, upgraded.reused_rows), (RULES_VERSION, 3, 0))
            same_rules = revise_case(original.case_id, 2, draft)
            self.assertEqual((same_rules.checked_rows, same_rules.reused_rows), (0, 3))
            with patch("report_audit.workflow.RULES_VERSION", RULES_VERSION + 1):
                upgraded_again = revise_case(original.case_id, 3, draft)
            self.assertEqual(upgraded_again.checked_rows, 3)
            self.assertEqual(upgraded_again.rules_version, RULES_VERSION + 1)
            self.assertEqual(get_snapshot(original.case_id, 1), legacy)


class ReportAuditTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "REPORT_AUDIT_DB": str(Path(directory) / "report_audit.sqlite3"),
            "REPORT_AUDIT_KNOWLEDGE": "0", "REPORT_AUDIT_LLM_ROUTER": "0",
        }))
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = self.enterContext(TestClient(self.app))

    def parse(self, text: str = DEMO_REPORT) -> dict:
        result = self.client.post(f"{BASE}/parse", json={"text": text})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def create(self, text: str = DEMO_REPORT) -> tuple[str, dict]:
        response = self.client.post(f"{BASE}/cases", json=self.parse(text))
        self.assertEqual(response.status_code, 201, response.text)
        snapshot = response.json()
        return f"{BASE}/cases/{snapshot['case_id']}", snapshot

    def chat(self, case: str, version: int, text: str, focus: str | None = None) -> dict:
        result = self.client.post(f"{case}/chat", json={"version": version, "text": text, "focus": focus})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_text_pdf_and_excel_upload_normalize_the_same_draft(self):
        draft = self.parse()
        lines = DEMO_REPORT.strip().splitlines()
        formatted = "\n".join(["SYNTHETIC LAB REPORT", *lines[:2], "CODE VALUE UNIT DATE", *lines[2:]])
        for name, content in [
            ("report.txt", DEMO_REPORT.encode("utf-8")),
            ("report.pdf", make_pdf(formatted)),
            ("report.xlsx", make_xlsx(DEMO_REPORT)),
        ]:
            with self.subTest(name=name):
                result = self.client.post(f"{BASE}/upload", files={"file": (name, content)})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json(), draft)

    def test_rejects_missing_text_invalid_dates_and_oversized_uploads(self):
        blank = PdfWriter()
        blank.add_blank_page(width=420, height=600)
        output = BytesIO()
        blank.write(output)
        for content, message in [
            (output.getvalue(), "PDF 没有可提取的文字"),
            (b"x" * (1024 * 1024 + 1), "大小须在 1 MB 以内"),
        ]:
            result = self.client.post(f"{BASE}/upload", files={"file": ("report.pdf", content)})
            self.assertEqual(result.status_code, 422, result.text)
            self.assertIn(message, result.json()["detail"])
        malformed = DEMO_REPORT.replace("2026-08-12", "20000")
        self.assertEqual(self.client.post(f"{BASE}/parse", json={"text": malformed}).status_code, 422)

    def test_image_only_pdf_has_a_separate_uncached_ocr_preview(self):
        image = Image.new("RGB", (1000, 360), "white")
        ImageDraw.Draw(image).text((120, 80), "LAB SAMPLE", font=ImageFont.load_default(size=75), fill="black")
        data = BytesIO()
        image.save(data, format="PDF", resolution=144)
        file = {"file": ("scan.pdf", data.getvalue())}

        upload = self.client.post(f"{BASE}/upload", files=file)
        self.assertEqual(upload.status_code, 422, upload.text)
        self.assertEqual(upload.json()["code"], "PDF_OCR_REQUIRED")

        scan = self.client.post(f"{BASE}/scan", files=file)
        self.assertEqual(scan.status_code, 200, scan.text)
        self.assertEqual(scan.headers["Cache-Control"], "no-store")
        body = scan.json()
        self.assertEqual(body["analysis"], {"findings": [], "notes": [], "source_audit": None})
        self.assertEqual(body["pages"][0]["page"], 1)
        words = " ".join(line["text"] for line in body["pages"][0]["lines"])
        self.assertIn("LABSAMPLE", "".join(words.split()))

    def test_page_preview_checks_original_file_and_recomparison_uses_edited_fields(self):
        pdf = render_scan([
            {"columns": ["ITEM", "RESULT", "UNIT", "REF"], "rows": [["WBC", "6.4", "G/L", "4.0-10.0"]]},
            {"columns": ["ITEM", "RESULT", "UNIT", "REF"], "rows": [["HGB", "170", "110-160", "g/L"]]},
        ])
        file = {"file": ("synthetic.pdf", pdf)}
        scan = self.client.post(f"{BASE}/scan", files=file)
        self.assertEqual(scan.status_code, 200, scan.text)
        preview = scan.json()
        original = preview["analysis"]["findings"][1]
        self.assertEqual((original["page"], original["status"]), (2, "REVIEW"))
        left, top, right, bottom = original["region"]
        self.assertTrue(0 <= left < right <= 1 and 0 <= top < bottom <= 1)
        query = f"expected_sha256={preview['source_sha256']}"
        page = self.client.post(f"{BASE}/scan/page?page=2&{query}", files=file)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["Content-Type"], "image/jpeg")
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        with Image.open(BytesIO(page.content)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertGreater(image.width, 500)
        other_page = self.client.post(f"{BASE}/scan/page?page=1&{query}", files=file)
        self.assertNotEqual(other_page.content, page.content)
        self.assertEqual(self.client.post(f"{BASE}/scan/page?page=3&{query}", files=file).status_code, 422)
        wrong_file = {"file": ("other.pdf", make_pdf("Different original"))}
        mismatch = self.client.post(f"{BASE}/scan/page?page=2&{query}", files=wrong_file)
        self.assertEqual(mismatch.status_code, 422)
        self.assertIn("与扫描原件不一致", mismatch.json()["detail"])

        fields = {"name": "HGB", "value": "170", "unit": "g/L", "reference": "110-160"}
        compared = self.client.post(f"{BASE}/scan/compare", json=fields)
        self.assertEqual(compared.json(), {"status": "HIGH"})
        fields["unit"], fields["reference"] = fields["reference"], fields["unit"]
        self.assertEqual(self.client.post(f"{BASE}/scan/compare", json=fields).json(), {"status": "REVIEW"})

        with patch.dict("os.environ", {"AUDIT_AGENT_TOKEN": "test"}), TestClient(agent_app) as client:
            internal = f"/internal/report-audit/scan/page?page=2&{query}"
            self.assertEqual(client.post(internal, content=pdf).status_code, 401)
            forwarded = client.post(internal, content=pdf, headers={"X-Agent-Token": "test"})
            self.assertEqual(forwarded.status_code, 200)
            self.assertEqual(forwarded.content, page.content)

    def test_scan_compares_each_row_using_report_columns_and_preserves_uncertainty(self):
        blocks = [
            TextBlock(text, uncertain, x, y, 15)
            for text, uncertain, x, y in [
                ("项目名称", False, 100, 50), ("结果", False, 280, 56),
                ("参考值", False, 390, 53), ("单位", False, 510, 55),
                ("白细胞 WBC", False, 108, 100), ("11.2", False, 282, 108),
                ("4.0~10.0", False, 395, 103), ("10^9/L", False, 516, 108),
                ("血红蛋白 HGB", False, 110, 148), ("135", False, 280, 155),
                ("110~160", False, 387, 150), ("g/L", False, 515, 153),
                ("红细胞 RBC", False, 107, 197), ("3.8", True, 276, 204),
                ("4.0~5.5", False, 393, 198), ("10^12/L", False, 510, 204),
                ("葡萄糖 GLU", False, 100, 248), ("5:10", False, 280, 255),
                ("3.9~6.1", False, 395, 252), ("mmol/L", False, 510, 255),
                ("检查所见：", False, 100, 290), ("未见", False, 280, 293),
                ("说明", False, 389, 290), ("项目", False, 510, 294),
            ]
        ]
        result = analyze_scan([(600, group_rows(list(reversed(blocks))))])
        self.assertEqual([f.status for f in result.findings], ["HIGH", "NORMAL", "REVIEW", "REVIEW"])
        self.assertEqual(result.findings[-1].value, "5:10")
        self.assertIsNone(result.source_audit)

    def test_scan_only_runs_source_audit_for_a_supported_identity(self):
        def run(text):
            lines = [[TextBlock(line, False, 100, index * 50, 16)]
                     for index, line in enumerate(text.strip().splitlines())]
            return analyze_scan([(600, lines)])

        audit = run(DEMO_REPORT).source_audit
        self.assertIsNotNone(audit)
        self.assertEqual(audit.findings[0].issues, ["value_mismatch"])
        self.assertIsNone(run(DEMO_REPORT.replace("DEMO-001", "DEMO-999")).source_audit)

    def test_valid_excel_with_unmatched_identity_explains_the_problem(self):
        workbook = make_xlsx(DEMO_REPORT.replace("DEMO-001", "DEMO-999"))
        result = self.client.post(f"{BASE}/upload", files={"file": ("report.xlsx", workbook)})
        self.assertEqual(result.status_code, 422, result.text)
        self.assertIn("未匹配原始记录", result.json()["detail"])

    def test_both_api_entry_points_use_the_same_replaceable_record_source(self):
        current = observation("OBS-EXT", "EXTERNAL-1", "NEW-VISIT", date(2026, 8, 12), "HGB", "123", "g/L")
        history = observation("OBS-OLD", "EXTERNAL-1", "OLD-VISIT", date(2026, 8, 1), "HGB", "121", "g/L")
        provider = SyntheticEvidenceSource((current, history))
        self.app.dependency_overrides[get_evidence_source] = lambda: provider
        text = "PATIENT EXTERNAL-1\nENCOUNTER NEW-VISIT\nHb 123 g/L 2026-08-12"
        case, snapshot = self.create(text)
        self.assertEqual(snapshot["findings"][0]["evidence"]["id"], current.id)
        response = self.chat(case, 1, "Hb 上次记录")
        self.assertEqual([record["id"] for record in response["records"]], [current.id, history.id])
        with (
            patch.dict("os.environ", {"AUDIT_AGENT_TOKEN": "test"}),
            patch.dict(agent_app.dependency_overrides, {get_evidence_source: lambda: provider}),
            TestClient(agent_app, headers={"X-Agent-Token": "test"}) as agent_client,
        ):
            internal = "/internal/report-audit"
            parsed = agent_client.post(f"{internal}/parse", json={"text": text})
            self.assertEqual(parsed.status_code, 200, parsed.text)
            evaluated = agent_client.post(f"{internal}/evaluate", json={
                "case_id": str(uuid4()), "version": 1, "draft": parsed.json(),
            })
            self.assertEqual(evaluated.status_code, 200, evaluated.text)
            agent_snapshot = Snapshot.model_validate(evaluated.json())
            self.assertEqual(agent_snapshot.findings[0].evidence.id, current.id)
            result = agent_client.post(f"{internal}/chat", json={
                "snapshot": evaluated.json(), "question": {"version": 1, "text": "Hb 上次记录"},
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([record["id"] for record in result.json()["records"]], [current.id, history.id])

    def test_finds_discrepancy_scoped_to_patient_and_encounter(self):
        _, snapshot = self.create()
        self.assertEqual((snapshot["checked_rows"], snapshot["reused_rows"]), (3, 0))
        first, second, third = snapshot["findings"]
        self.assertEqual(first["issues"], ["value_mismatch"])
        self.assertEqual(first["evidence"]["id"], "OBS-101")
        self.assertEqual(first["evidence"]["revision"], 2)
        self.assertFalse(second["issues"])
        self.assertFalse(third["issues"])
        other_patient = DEMO_REPORT.replace("DEMO-001", "DEMO-002")
        self.assertEqual(self.client.post(f"{BASE}/parse", json={"text": other_patient}).status_code, 422)

    def test_distinguishes_wrong_unit_wrong_date_and_missing_evidence(self):
        report = DEMO_REPORT.replace("123 g/L", "123 mg/L").replace(
            "6.4 10^9/L 2026-08-12", "6.4 10^9/L 2026-08-11",
        )
        _, snapshot = self.create(report + "GLU 1 mmol/L 2026-08-12\n")
        findings = snapshot["findings"]
        self.assertEqual(findings[0]["issues"], ["unit_mismatch"])
        self.assertEqual(findings[1]["issues"], ["date_mismatch"])
        self.assertEqual(findings[3]["issues"], ["missing_evidence"])

    def test_correction_reuses_two_rows_and_keeps_original_snapshot(self):
        case, original = self.create()
        draft = deepcopy(original["draft"])
        draft["rows"][0]["value"] = "132"
        payload = {"expected_version": 1, "draft": draft}
        result = self.client.post(f"{case}/revisions", json=payload)
        self.assertEqual(result.status_code, 201, result.text)
        revised = result.json()
        self.assertEqual(revised["version"], 2)
        self.assertEqual((revised["checked_rows"], revised["reused_rows"]), (1, 2))
        self.assertFalse(revised["findings"][0]["issues"])
        self.assertEqual(self.client.get(f"{case}?version=1").json(), original)
        self.assertEqual(self.client.post(f"{case}/revisions", json=payload).status_code, 409)
        draft["patient_id"] = "DEMO-002"
        draft["encounter_id"] = "VISIT-003"
        self.assertEqual(self.client.post(f"{case}/revisions", json={"expected_version": 2, "draft": draft}).status_code, 422)

    def test_concurrent_revisions_allow_one_writer(self):
        case, original = self.create()

        def submit(value: str) -> int:
            draft = deepcopy(original["draft"])
            draft["rows"][0]["value"] = value
            response = self.client.post(f"{case}/revisions", json={"expected_version": 1, "draft": draft})
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(submit, ["132", "130"])), [201, 409])
        self.assertEqual(self.client.get(case).json()["version"], 2)

    def test_chat_cites_sources_and_follows_previous_lab_code(self):
        case, _ = self.create()
        result = self.chat(case, 1, "血红蛋白的原始记录是什么？")
        self.assertEqual(result["focus"], "HGB")
        self.assertEqual(result["records"][0]["id"], "OBS-101")
        self.assertIn("HGB.value", result["dependencies"])
        self.assertIn("HGB.unit", result["dependencies"])
        history = self.chat(case, 1, "上次的记录呢？", focus=result["focus"])
        self.assertEqual([record["id"] for record in history["records"]], ["OBS-101", "OBS-081"])
        self.assertEqual(history["dependencies"], ["HGB.presence", "HGB.evidence"])
        unknown = self.chat(case, 1, "别的患者的血红蛋白记录呢？", focus=result["focus"])
        self.assertFalse(unknown["records"])
        self.assertFalse(unknown["dependencies"])
        self.assertEqual(self.chat(case, 1, "概览核对结果")["dependencies"], ["summary"])

    def test_glossary_and_hybrid_retrieval_keep_sources_separate(self):
        case, _ = self.create()
        local = self.chat(case, 1, "血红蛋白是什么？")
        self.assertEqual(local["knowledge"][0]["section"], "HGB")
        self.assertFalse(local["records"])
        self.assertFalse(local["dependencies"])
        retriever = AsyncMock(return_value=[
            Document(page_content="其他检验", metadata={"source": "lab_reference"}),
            Document(page_content="糖化血红蛋白（HbA1c）", metadata={"source": "lab_reference", "section": "血常规参考范围"}),
            Document(page_content="血红蛋白（HGB）资料片段", metadata={"source": "lab_reference", "section": "血常规参考范围"}),
            Document(page_content="血红蛋白（HGB）其他样本", metadata={"source": "lab_reference", "section": "尿常规参考范围"}),
            Document(page_content="血红蛋白无关内容", metadata={"source": "insurance_policies"}),
        ])
        with (
            patch.dict("os.environ", {"REPORT_AUDIT_KNOWLEDGE": "1"}),
            patch("report_audit.workflow.retrieve_hybrid", retriever),
        ):
            hybrid = self.chat(case, 1, "血红蛋白是什么？")
        retriever.assert_awaited_once_with("血红蛋白 血常规 参考范围")
        self.assertEqual([item["excerpt"] for item in hybrid["knowledge"]], ["血红蛋白（HGB）资料片段"])

    def test_optional_llm_routes_to_records_and_falls_back_on_error(self):
        case, _ = self.create()
        with (
            patch.dict("os.environ", {"REPORT_AUDIT_LLM_ROUTER": "1"}),
            patch("report_audit.workflow.classify_with_model", AsyncMock(return_value=Route(intent="record", focus="HGB"))),
        ):
            answer = self.chat(case, 1, "帮忙核一下第一项")
        self.assertEqual(answer["records"][0]["id"], "OBS-101")
        with (
            patch.dict("os.environ", {"REPORT_AUDIT_LLM_ROUTER": "1"}),
            patch("report_audit.workflow.classify_with_model", AsyncMock(side_effect=TimeoutError)),
        ):
            answer = self.chat(case, 1, "帮忙核一下第一项")
        self.assertFalse(answer["records"])
        self.assertIn("请指定", answer["text"])


if __name__ == "__main__":
    unittest.main()
