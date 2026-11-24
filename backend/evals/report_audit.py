"""Measure OCR, discrepancy detection and graph citations on synthetic reports."""

import argparse
import asyncio
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from io import BytesIO
import json
from pathlib import Path
from unicodedata import normalize
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from report_audit.models import Question, Snapshot
from report_audit.parser import parse_report
from report_audit.pdf_scan import scan_pdf
from report_audit.workflow import audit_graph, chat_graph

DATASET = Path(__file__).with_name("report_audit_cases.json")
FIELD_NAMES = ("name", "value", "unit", "reference")
ABNORMAL = {"HIGH", "LOW"}


@dataclass
class Count:
    numerator: int = 0
    denominator: int = 0

    def add(self, counted: bool) -> None:
        self.numerator += counted
        self.denominator += 1


@dataclass
class Score:
    extraction: Count = field(default_factory=Count)
    page_location: Count = field(default_factory=Count)
    range_missed: Count = field(default_factory=Count)
    deferred_abnormal: int = 0
    false_range_flags: int = 0
    unexpected_field_errors: int = 0
    status_mismatches: int = 0
    extra_rows: int = 0
    discrepancies_missed: Count = field(default_factory=Count)
    extra_discrepancies: int = 0
    evidence_answers: Count = field(default_factory=Count)


def render_scan(pages: list[dict]) -> bytes:
    font = ImageFont.load_default(size=44)
    images = []
    for page in pages:
        image = Image.new("RGB", (1120, 780), "white")
        ink = (page.get("ink", 18),) * 3
        draw = ImageDraw.Draw(image)
        positions = page.get("positions", (48, 305, 562, 825))
        for index, row in enumerate([page["columns"], *page["rows"]]):
            for x, text in zip(positions, row, strict=True):
                draw.text((x, 112 + index * 96), text, font=font, fill=ink)
        scale = page.get("scale", 1)
        if scale != 1:
            size = (int(image.width * scale), int(image.height * scale))
            image = image.resize(size, Image.Resampling.BILINEAR)
        if page.get("blur"):
            image = image.filter(ImageFilter.GaussianBlur(page["blur"]))
        images.append(image)
    output = BytesIO()
    try:
        images[0].save(output, format="PDF", save_all=True, append_images=images[1:], resolution=144)
        return output.getvalue()
    finally:
        for image in images:
            image.close()


def field_text(value: str) -> str:
    return "".join(normalize("NFKC", value).split())


def eval_scan(case: dict, score: Score, pdf: bytes) -> list[str]:
    actual = defaultdict(deque)
    notes = []
    for row in scan_pdf(pdf).analysis.findings:
        actual[(row.page, field_text(row.name))].append(row)
    reviewed_truth = {(page, field_text(name)): status for page, name, status in case.get("abnormal_after_review", [])}
    allowed_errors = {
        (page, field_text(name), column): field_text(value)
        for page, name, column, value in case.get("known_extraction_errors", [])
    }
    for page, name, value, unit, reference, expected_status in case["expected"]:
        key = (page, field_text(name))
        found = actual[key].popleft() if actual[key] else None
        region = found.region if found else None
        score.page_location.add(
            region is not None and 0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1
        )
        for name_of_field, expected in zip(FIELD_NAMES, (name, value, unit, reference), strict=True):
            known = getattr(found, name_of_field, None)
            correct = known is not None and field_text(known) == field_text(expected)
            score.extraction.add(correct)
            if not correct:
                allowed = allowed_errors.get((*key, name_of_field))
                score.unexpected_field_errors += known is None or field_text(known) != allowed
                notes.append(f"{case['id']} page={page} {name}.{name_of_field}: expected {expected!r}, got {known!r}")

        status = found.status if found else None
        truth = reviewed_truth.get(key, expected_status)
        if truth in ABNORMAL:
            score.range_missed.add(status != truth)
            score.deferred_abnormal += status == "REVIEW"
        score.false_range_flags += status in ABNORMAL and status != truth
        if status != expected_status:
            score.status_mismatches += 1
            notes.append(f"{case['id']} page={page} {name}.status: expected {expected_status}, got {status}")

    extra = sum(map(len, actual.values()))
    score.extra_rows += extra
    if extra:
        notes.append(f"{case['id']} unexpected rows: {extra}")
    return notes


async def eval_audits(cases: list[dict], score: Score) -> list[str]:
    notes = []
    with patch.dict("os.environ", {
        "REPORT_AUDIT_LLM_ROUTER": "0", "REPORT_AUDIT_KNOWLEDGE": "0", "AUDIT_MCP_URL": "",
    }):
        for case in cases:
            draft = parse_report(case["report"])
            result = audit_graph.invoke({"draft": draft})["result"]
            actual = {(row.row.code, issue) for row in result.findings for issue in row.issues}
            expected = {(code, issue) for code, issues in case["issues"].items() for issue in issues}
            score.discrepancies_missed.denominator += len(expected)
            score.discrepancies_missed.numerator += len(expected - actual)
            score.extra_discrepancies += len(actual - expected)
            if actual != expected:
                notes.append(f"{case['id']} expected issues {sorted(expected)}, got {sorted(actual)}")

            snapshot = Snapshot(case_id=case["id"], version=1, draft=draft, **result.model_dump())
            turns = [{"text": case["question"], "records": case["records"]}, *case.get("followups", [])]
            focus = None
            for index, turn in enumerate(turns, start=1):
                question = Question(version=1, text=turn["text"], focus=focus)
                answer = (await chat_graph.ainvoke({"snapshot": snapshot, "question": question}))["answer"]
                focus = answer.focus
                citations = [[record.id, record.revision] for record in answer.records]
                score.evidence_answers.add(citations == turn["records"])
                if citations != turn["records"]:
                    notes.append(f"{case['id']} turn={index} expected citations {turn['records']}, got {citations}")
    return notes


def baseline_failures(score: Score) -> list[str]:
    failures = []
    if score.unexpected_field_errors or score.extra_rows:
        failures.append("OCR extraction introduced an unannotated field error or unexpected row")
    if score.page_location.numerator != score.page_location.denominator:
        failures.append("An annotated row could not be located on its original page")
    if score.status_mismatches:
        failures.append("A scan status changed from its annotation")
    if score.range_missed.numerator != score.deferred_abnormal or score.false_range_flags:
        failures.append("A range abnormality was missed without review, or an incorrect flag was generated")
    if score.discrepancies_missed.numerator or score.extra_discrepancies:
        failures.append("Source discrepancy detection disagreed with the annotations")
    if score.evidence_answers.numerator != score.evidence_answers.denominator:
        failures.append("A question cited unexpected source IDs or revisions")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print machine-readable counts and differences")
    parser.add_argument("--export-pdfs", type=Path, metavar="DIRECTORY", help="Write uploadable copies of the synthetic scans")
    args = parser.parse_args()
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    score = Score()
    notes = []
    if args.export_pdfs:
        args.export_pdfs.mkdir(parents=True, exist_ok=True)
    for case in dataset["scans"]:
        pdf = render_scan(case["pages"])
        if args.export_pdfs:
            (args.export_pdfs / f"{case['id']}.pdf").write_bytes(pdf)
        notes.extend(eval_scan(case, score, pdf))
    notes.extend(asyncio.run(eval_audits(dataset["audits"], score)))
    failures = baseline_failures(score)
    if args.json:
        print(json.dumps({"metrics": asdict(score), "differences": notes, "failures": failures}, ensure_ascii=False, indent=2))
    else:
        for label, count in (
            ("OCR field accuracy", score.extraction), ("OCR original-page location", score.page_location),
            ("OCR abnormality miss rate", score.range_missed), ("Source discrepancy miss rate", score.discrepancies_missed),
            ("Evidence citation accuracy", score.evidence_answers),
        ):
            percent = f"{count.numerator / count.denominator:.1%}" if count.denominator else "n/a"
            print(f"{label}: {count.numerator}/{count.denominator} ({percent})")
        print(f"Abnormal rows deferred for review: {score.deferred_abnormal}; incorrect range flags: {score.false_range_flags}")
        print(f"Unexpected extracted rows: {score.extra_rows}; unexpected source discrepancies: {score.extra_discrepancies}")
        for note in notes:
            print(note)
        for failure in failures:
            print(f"FAIL: {failure}")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
