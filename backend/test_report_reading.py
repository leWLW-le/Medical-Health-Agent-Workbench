from io import BytesIO
import unittest
from unittest.mock import Mock, patch

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from evals.report_audit import render_scan
from report_audit.parser import InvalidReport
from report_audit.pdf_scan import _reread_cell, scan_source_page
from report_audit.reading import ReadingPrompt, answer_reading, extract_reading
from report_audit.scan_analysis import reading_comparison


def positioned_table_pdf(rows: list[tuple[str, str, str, str]]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=420, height=500)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    instructions = ["BT /F1 12 Tf"]
    for number, row in enumerate([("ITEM", "RESULT", "UNIT", "REF"), *rows]):
        y = 450 - number * 36
        for x, value in zip((24, 153, 241, 316), row, strict=True):
            instructions.append(f"1 0 0 1 {x} {y} Tm ({value}) Tj")
    instructions.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(instructions).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ReportReadingTest(unittest.TestCase):
    def test_photo_reads_side_by_side_tables_with_inline_units(self):
        with Image.new("RGB", (2200, 410), "white") as image:
            draw = ImageDraw.Draw(image)
            font = ImageFont.load_default(size=32)
            for offset, rows in (
                (24, [("AST", "AST", "21", "0~37U/L"), ("CREA", "Creatinine", "45.1", "31.8~93.7umo1/L")]),
                (1140, [("HGB", "Hemoglobin", "142", "110~160g/L"), ("ALT", "ALT", "56", "0~42U/L")]),
            ):
                for i, row in enumerate([("CODE", "ITEM", "RESULT", "REF"), *rows]):
                    for x, value in zip((offset, offset + 226, offset + 530, offset + 760), row, strict=True):
                        draw.text((x, 65 + i * 82), value, font=font, fill="black")
            output = BytesIO()
            image.save(output, "PNG")
        report = extract_reading(output.getvalue())
        self.assertEqual([row.value for row in report.findings], ["21", "45.1", "142", "56"])
        self.assertEqual([row.status for row in report.findings], ["NORMAL", "REVIEW", "NORMAL", "HIGH"])
        self.assertEqual((report.findings[0].reference, report.findings[0].unit), ("0~37", "U/L"))
        self.assertTrue({"unit_format", "reference_format"}.intersection(report.findings[1].review_reasons))
        self.assertTrue(all(row.region[2] < report.findings[2].region[0] for row in report.findings[:2]))
        self.assertTrue(all(row.region[0] > 0.5 for row in report.findings[2:]))

    def test_inline_ocr_unit_is_preserved_for_review(self):
        from report_audit.scan_analysis import TextBlock, analyze_scan

        rows = [[TextBlock(text, False, x, y * 30, 20) for text, x in zip(cells, (40, 150, 320, 425), strict=True)]
                for y, cells in enumerate((("代号", "项目", "结果", "参考值"),
                                            ("CREA", "肌酐", "45.1", "31.8~93.7umo1/L")), start=1)]
        row, = analyze_scan([(200, rows)], include_source_audit=False).findings
        self.assertEqual((row.value, row.unit, row.reference, row.status), ("45.1", "umo1/L", "31.8~93.7", "REVIEW"))
        self.assertEqual(row.review_reasons, ["unit_format"])

    def test_photo_with_text_but_no_table_does_not_create_an_empty_report(self):
        from report_audit.scan_analysis import TextBlock

        with Image.new("RGB", (250, 200), "white") as image:
            data = BytesIO()
            image.save(data, "PNG")
        with patch("report_audit.pdf_scan._image_blocks", return_value=(200, [TextBlock("示例文字", False, 30, 50, 10)])):
            with self.assertRaisesRegex(InvalidReport, "未找到检验项目和结果"):
                extract_reading(data.getvalue())

    def test_native_pdf_text_keeps_exact_decimal_values_and_only_allows_supported_blank_units(self):
        data = positioned_table_pdf([
            ("SG", "1.027", "", "1. 005~1.030"),
            ("PH NH-PH", "5.5000", "", "4.5-8"),
            ("HCT", "0.44", "", "0.40~0.50"),
            ("HCT", "44", "", "40~50"),
            ("EOS#", "0.05", "", "0.02~0.52"),
            ("URBC", "5.10", "/ul", "0~13.60"),
        ])
        with patch("report_audit.pdf_scan._image_blocks", side_effect=AssertionError("native text should be used")):
            findings = extract_reading(data).findings
        self.assertEqual([row.status for row in findings], ["NORMAL", "NORMAL", "NORMAL", "REVIEW", "REVIEW", "NORMAL"])
        self.assertEqual([row.value for row in findings], ["1.027", "5.5000", "0.44", "44", "0.05", "5.10"])
        self.assertEqual(findings[4].review_reasons, ["unit_missing"])
        self.assertTrue(all(row.region is not None and row.page == 1 for row in findings))

    def test_reread_requires_agreement_and_preserves_uncertain_punctuation(self):
        for candidates, expected in [
            ([("5:10", .95), ("5. 10", .85), ("5.10", .86), ("5.10.", .92)], "5.10"),
            ([("5.10", .95), ("5.10", .94), ("5.70", .87), ("", 0)], None),
            ([("5.10", .79), ("5.10", .76), ("", 0), ("", 0)], None),
        ]:
            with self.subTest(candidates=candidates), Image.new("RGB", (180, 70), "white") as image:
                engine = Mock(side_effect=[([[text, score]], []) for text, score in candidates])
                with patch("report_audit.pdf_scan._engine", return_value=engine):
                    self.assertEqual(_reread_cell(image, "value", "g/L", (.2, .2, .7, .6)), expected)
        self.assertEqual(reading_comparison("5:10", "/ul", "0~13.60").review_reasons, ["value_format"])

    def test_pdf_and_photo_use_identical_result_and_source_contracts(self):
        pdf = render_scan([{
            "columns": ["ITEM", "RESULT", "UNIT", "REF"],
            "rows": [["HGB", "170", "110-160", "g/L"], ["AST", "58", "U/L", "10-40"]],
        }])
        with pdfium.PdfDocument(pdf) as document:
            page = document[0]
            bitmap = page.render(scale=2)
            try:
                photo = BytesIO()
                bitmap.to_pil().save(photo, "PNG")
            finally:
                bitmap.close()
                page.close()
        for kind, data in (("pdf", pdf), ("photo", photo.getvalue())):
            with self.subTest(kind=kind):
                result = extract_reading(data)
                self.assertEqual((result.source_kind, result.page_count), (kind, 1))
                row = result.findings[0]
                self.assertEqual((row.name, row.status, row.review_reasons), ("HGB", "REVIEW", ["shifted_columns"]))
                self.assertNotIn("source_audit", result.model_dump())
                self.assertEqual(scan_source_page(data, 1, result.source_sha256)[:2], bytes.fromhex("ffd8"))
                with self.assertRaisesRegex(InvalidReport, "不一致"):
                    scan_source_page(data, 1, "0" * 64)

    def test_pending_fields_are_excluded_from_explanation_and_followup(self):
        from report_audit.scan_analysis import ScanFinding

        row = ScanFinding(page=2, name="HGB", value="5:10", unit="g/L", reference="110-160", status="REVIEW")
        for question in ("", "这项结果是多少？", "HGB 是什么意思？"):
            answer = answer_reading(ReadingPrompt(findings=[row], row=0, question=question))
            self.assertIn("待确认", answer.text)
            self.assertNotIn("5:10", answer.text)

        updated = reading_comparison("170", "g/L", "110-160")
        self.assertEqual((updated.status, updated.review_reasons), ("HIGH", []))
        row = row.model_copy(update={"value": "170", "status": updated.status})
        answer = answer_reading(ReadingPrompt(findings=[row], row=0, question="HGB 代表什么？"))
        self.assertIn("携带氧气", answer.text)
        self.assertIn("170 g/L", answer.text)
        self.assertEqual((answer.row, answer.page), (0, 2))

    def test_missing_value_and_reference_have_specific_reasons(self):
        self.assertEqual(reading_comparison("", "g/L", "110-160").review_reasons, ["value_missing"])
        self.assertEqual(reading_comparison("131", "g/L", "").review_reasons, ["reference_missing"])
