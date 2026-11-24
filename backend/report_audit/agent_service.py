from contextlib import asynccontextmanager
import os
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .api import (
    UNKNOWN_ENCOUNTER_MESSAGE, ReportEvidence, ScanFingerprint, ScanPageNumber, TextReport,
    compare_scan, demo, parse_text, pdf_scan_page, pdf_scan_preview, scan_required, validate_encounter,
)
from .evidence import EvidenceContext, UnknownEncounter
from .models import Answer, AuditInput, Draft, Question, Snapshot
from .parser import MAX_REPORT_BYTES, InvalidReport, PdfOcrRequired, parse_report_file
from .pdf_scan import MAX_PHOTO_BYTES, ScanBusy, ScanPreview, scan_source_page
from .reading import ReadingExtraction, extract_reading
from .reading_context import ReportContextForm, report_context_form
from .scan_analysis import ScanComparison, ScanFields, ScanFinding, ScanReadingComparison, reading_comparison
from .workflow import audit_graph, chat_graph


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_dotenv()
    if not os.getenv("AUDIT_AGENT_TOKEN"):
        raise RuntimeError("Set AUDIT_AGENT_TOKEN before starting the internal Agent service")
    yield


app = FastAPI(title="Report Audit Agent", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def authenticate(x_agent_token: Annotated[str, Header()] = "") -> None:
    token = os.getenv("AUDIT_AGENT_TOKEN", "")
    if not token or not compare_digest(token.encode(), x_agent_token.encode()):
        raise HTTPException(401, "Invalid internal service credentials")


internal = [Depends(authenticate)]


class Evaluation(AuditInput):
    case_id: UUID
    version: int = Field(ge=1)
    draft: Draft
    previous: Snapshot | None = None


class ChatRequest(AuditInput):
    snapshot: Snapshot
    question: Question


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "up"}


@app.get("/internal/report-audit/demo", dependencies=internal)
def demo_report() -> dict:
    return demo()


@app.post("/internal/report-audit/parse", dependencies=internal, response_model=Draft)
def parse_draft(report: TextReport, source: ReportEvidence) -> Draft:
    return parse_text(report, source)


async def _read_report_body(request: Request, limit: int = MAX_REPORT_BYTES) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, f"文件大小须在 {limit // (1024 * 1024)} MB 以内")
        data.extend(chunk)
    return bytes(data)


@app.post("/internal/report-audit/upload", dependencies=internal, response_model=Draft)
async def upload_report(request: Request, source: ReportEvidence) -> Draft | JSONResponse:
    data = await _read_report_body(request)
    try:
        draft = await run_in_threadpool(parse_report_file, data)
        return await run_in_threadpool(validate_encounter, draft, source)
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc
    except PdfOcrRequired as exc:
        return scan_required(exc)
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/internal/report-audit/scan", dependencies=internal, response_model=ScanPreview)
async def scan_report(request: Request, response: Response, source: ReportEvidence) -> ScanPreview:
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(pdf_scan_preview, await _read_report_body(request), source)


@app.post("/internal/report-audit/scan/page", dependencies=internal, response_class=Response)
async def scan_report_page(
    request: Request, page: ScanPageNumber, expected_sha256: ScanFingerprint = None,
) -> Response:
    return await run_in_threadpool(pdf_scan_page, await _read_report_body(request), page, expected_sha256)


@app.post("/internal/report-audit/evaluate", dependencies=internal, response_model=Snapshot)
def evaluate(payload: Evaluation, source: ReportEvidence) -> Snapshot:
    draft = payload.draft
    previous = payload.previous
    if (previous is None and payload.version != 1) or (
        previous is not None and (
            previous.case_id != str(payload.case_id) or previous.version + 1 != payload.version
            or (previous.draft.patient_id, previous.draft.encounter_id) != (draft.patient_id, draft.encounter_id)
        )
    ):
        raise HTTPException(422, "报告版本与基线不一致")
    try:
        result = audit_graph.invoke({"draft": draft, "previous": previous}, context=EvidenceContext(source))["result"]
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc
    return Snapshot(case_id=str(payload.case_id), version=payload.version, draft=draft, **result.model_dump())


@app.post("/internal/report-audit/reading/upload", dependencies=internal, response_model=ReadingExtraction)
async def reading_upload(request: Request) -> ReadingExtraction:
    data = await _read_report_body(request, MAX_PHOTO_BYTES)
    try:
        return await run_in_threadpool(extract_reading, data)
    except ScanBusy as exc:
        raise HTTPException(429, str(exc)) from exc
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/internal/report-audit/reading/page", dependencies=internal, response_class=Response)
async def reading_page(request: Request, page: ScanPageNumber, expected_sha256: ScanFingerprint) -> Response:
    if expected_sha256 is None:
        raise HTTPException(422, "请选择本次报告的原件")
    data = await _read_report_body(request, MAX_PHOTO_BYTES)
    try:
        image = await run_in_threadpool(scan_source_page, data, page, expected_sha256)
    except ScanBusy as exc:
        raise HTTPException(429, str(exc)) from exc
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/internal/report-audit/reading/compare", dependencies=internal, response_model=ScanReadingComparison)
def reading_compare(fields: ScanFields) -> ScanReadingComparison:
    return reading_comparison(fields.value, fields.unit, fields.reference)


@app.post("/internal/report-audit/reading/context-form", dependencies=internal, response_model=ReportContextForm)
def reading_context_form(findings: Annotated[list[ScanFinding], Field(max_length=400)]) -> ReportContextForm:
    return report_context_form(findings)


@app.post("/internal/report-audit/scan/compare", dependencies=internal, response_model=ScanComparison)
def compare_scan_row(fields: ScanFields, response: Response) -> ScanComparison:
    return compare_scan(fields, response)


@app.post("/internal/report-audit/chat", dependencies=internal, response_model=Answer)
async def chat(payload: ChatRequest, source: ReportEvidence) -> Answer:
    if payload.snapshot.version != payload.question.version:
        raise HTTPException(422, "问答版本与报告不一致")
    result = await chat_graph.ainvoke({"snapshot": payload.snapshot, "question": payload.question},
                                      context=EvidenceContext(source))
    return result["answer"]
