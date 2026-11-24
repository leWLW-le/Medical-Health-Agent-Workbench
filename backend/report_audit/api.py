from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from . import workflow
from .evidence import EvidenceContext, EvidenceSource, UnknownEncounter, get_evidence_source, load_encounter_records
from .fixtures import DEMO_REPORT
from .models import LAB_NAMES, Answer, AuditInput, Draft, Question, Revision, Snapshot
from .parser import MAX_REPORT_BYTES, MAX_REPORT_CHARS, InvalidReport, PdfOcrRequired, parse_report, parse_report_file
from .pdf_scan import ScanBusy, ScanPreview, scan_page_image, scan_pdf
from .scan_analysis import ScanComparison, ScanFields, compare_scan_fields
from .store import IdentityConflict, UnknownCase, VersionConflict, create_case, get_snapshot, revise_case

router = APIRouter(prefix="/api/report-audit", tags=["report-audit"])
ReportEvidence = Annotated[EvidenceSource, Depends(get_evidence_source)]
UNKNOWN_ENCOUNTER_MESSAGE = "患者与就诊编号未匹配原始记录。演示环境可使用页面提供的示例编号"


class TextReport(AuditInput):
    text: str = Field(max_length=MAX_REPORT_CHARS)


@router.get("/demo")
def demo() -> dict:
    return {"text": DEMO_REPORT, "lab_names": LAB_NAMES, "rules_version": workflow.RULES_VERSION}


def validate_encounter(draft: Draft, source: EvidenceSource) -> Draft:
    load_encounter_records(source, draft.patient_id, draft.encounter_id)
    return draft


@router.post("/parse", response_model=Draft)
def parse_text(report: TextReport, source: ReportEvidence) -> Draft:
    try:
        return validate_encounter(parse_report(report.text), source)
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc


def scan_required(exc: PdfOcrRequired) -> JSONResponse:
    return JSONResponse(status_code=422, content={"code": "PDF_OCR_REQUIRED", "detail": str(exc)})


def pdf_scan_preview(data: bytes, source: EvidenceSource) -> ScanPreview:
    try:
        return scan_pdf(data, source=source)
    except ScanBusy as exc:
        raise HTTPException(429, str(exc)) from exc
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/upload", response_model=Draft)
def parse_upload(file: Annotated[UploadFile, File()], source: ReportEvidence) -> Draft | JSONResponse:
    try:
        data = file.file.read(MAX_REPORT_BYTES + 1)
        return validate_encounter(parse_report_file(data), source)
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc
    except PdfOcrRequired as exc:
        return scan_required(exc)
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        file.file.close()


@router.post("/scan", response_model=ScanPreview)
def scan_upload(file: Annotated[UploadFile, File()], response: Response, source: ReportEvidence) -> ScanPreview:
    response.headers["Cache-Control"] = "no-store"
    try:
        return pdf_scan_preview(file.file.read(MAX_REPORT_BYTES + 1), source)
    finally:
        file.file.close()


def pdf_scan_page(data: bytes, page: int, expected_sha256: str | None) -> Response:
    try:
        image = scan_page_image(data, page, expected_sha256)
    except ScanBusy as exc:
        raise HTTPException(429, str(exc)) from exc
    except InvalidReport as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


ScanPageNumber = Annotated[int, Query(ge=1)]
ScanFingerprint = Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")]


@router.post("/scan/page", response_class=Response)
def scan_page_upload(
    file: Annotated[UploadFile, File()], page: ScanPageNumber, expected_sha256: ScanFingerprint = None,
) -> Response:
    try:
        return pdf_scan_page(file.file.read(MAX_REPORT_BYTES + 1), page, expected_sha256)
    finally:
        file.file.close()


@router.post("/cases", response_model=Snapshot, status_code=201)
def new_case(draft: Draft, source: ReportEvidence) -> Snapshot:
    try:
        return create_case(draft, source=source)
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc


@router.post("/scan/compare", response_model=ScanComparison)
def compare_scan(fields: ScanFields, response: Response) -> ScanComparison:
    response.headers["Cache-Control"] = "no-store"
    return compare_scan_fields(fields)


def load_case(case_id: UUID, version: int | None = None) -> Snapshot:
    try:
        return get_snapshot(str(case_id), version)
    except UnknownCase as exc:
        raise HTTPException(404, "未找到该报告或版本") from exc


@router.get("/cases/{case_id}", response_model=Snapshot)
def read_case(case_id: UUID, version: Annotated[int | None, Query(ge=1)] = None) -> Snapshot:
    return load_case(case_id, version)


@router.post("/cases/{case_id}/revisions", response_model=Snapshot, status_code=201)
def revise(case_id: UUID, revision: Revision, source: ReportEvidence) -> Snapshot:
    try:
        return revise_case(str(case_id), revision.expected_version, revision.draft, source=source)
    except UnknownEncounter as exc:
        raise HTTPException(422, UNKNOWN_ENCOUNTER_MESSAGE) from exc
    except UnknownCase as exc:
        raise HTTPException(404, "未找到该报告") from exc
    except VersionConflict as exc:
        raise HTTPException(409, "已有更新版本，请载入最新版本后再更正") from exc
    except IdentityConflict as exc:
        raise HTTPException(422, "更正版本的患者和就诊编号必须与原报告相同") from exc


@router.post("/cases/{case_id}/chat", response_model=Answer)
async def chat(case_id: UUID, question: Question, source: ReportEvidence) -> Answer:
    snapshot = await run_in_threadpool(load_case, case_id, question.version)
    state = await workflow.chat_graph.ainvoke({"snapshot": snapshot, "question": question},
                                              context=EvidenceContext(source))
    return state["answer"]
