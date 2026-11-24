"""Interpret precisely what the uploaded report says using its own evidence."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from dataclasses import dataclass, replace
import json
import logging
import re
from time import perf_counter
from typing import Annotated, Literal, TypedDict
from unicodedata import normalize

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage, message_chunk_to_message
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from openai import APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from agents.llm import LLMConfigurationError, get_chat_llm
from agents.streaming import emit_custom

from .models import AuditInput
from .parser import InvalidReport
from .pdf_scan import scan_pdf, scan_photo
from .reading_checks import ReadingCheck, check_blood_counts
from .reading_evidence import EVIDENCE_PURPOSES, EvidenceCheck, check_report_evidence, describe_report_lookup, resolve_report_field
from .reading_knowledge import Note, finding_keys as _finding_keys, indicator_keys, related_keys, retrieve_notes
from .reading_context import (
    LIVER_INDICATORS, MEDICATION_DETAIL_LIMIT, ContextFollowUp, PatientContext, ReadingTaskProgress, ReadingTaskRequest,
    infection_correction, is_context_reply_detail,
    report_context_form, reports_current_severe_bleeding,
)
from .reading_patient import PatientGuidance, ReadingSource, guidance_for_patient
from .reading_trace import ModelCallPhase, ModelSkipReason, ReadingModelCall, ReadingToolExecution, ReadingWorkRecord
from .scan_analysis import ScanFinding

MAX_READING_ROWS = 400
MODEL_TIMEOUT_SECONDS = 28
MODEL_CALL_TIMEOUT_SECONDS = 14
MODEL_RECOVERY_TIMEOUT_SECONDS = 10
DEFINITIONS = (
    (r"尿红细胞|URBC", "这是尿液样本中的红细胞计数。"),
    (r"尿白细胞|UWBC", "这是尿液样本中的白细胞计数。"),
    (r"平均血小板体积|\bMPV\b", "这个指标描述血小板的平均大小。"),
    (r"血小板分布宽度|\bPDW\b", "这个指标描述血小板大小之间的差异。"),
    (r"血小板压积|\bPCT\b(?=\s*血小板)", "这是血小板所占的体积比例。"),
    (r"嗜碱性?粒细胞|\bBAS(?:[%#]|\b)", "嗜碱性粒细胞是白细胞的一种。BAS% 表示比例，BAS# 表示绝对计数。"),
    (r"嗜酸性粒细胞|\bEOS\b", "嗜酸性粒细胞是白细胞的一种。报告可分别列出其计数和比例。"),
    (r"糖化血红蛋白|\bHBA1C\b", "这是报告测量的糖化血红蛋白项目。"),
    (r"血红蛋白|\bHGB\b|\bHB\b", "血红蛋白是红细胞中负责携带氧气的蛋白质。"),
    (r"红细胞压积|红细胞比容|\bHCT\b", "这个指标表示红细胞占血液体积的比例。"),
    (r"平均红细胞体积|\bMCV\b", "这个指标描述红细胞的平均大小。"),
    (r"红细胞|\bRBC\b", "这是报告所列样本中的红细胞计数。"),
    (r"白细胞|\bWBC\b", "这是报告所列样本中的白细胞计数。"),
    (r"血小板|\bPLT\b", "这是血小板计数。血小板参与止血。"),
    (r"葡萄糖|血糖|\bGLU\b", "这是报告所列样本的葡萄糖测量值，解读时要看报告标注的样本类型。"),
    (r"肌酐|\bCREA\b|\bSCR\b", "肌酐是人体代谢产生的物质，报告可测量血液或尿液中的肌酐。"),
    (r"转氨酶|\bALT\b|\bAST\b", "这是对报告所列转氨酶活性的测量。"),
    (r"\bPH\b|酸碱度", "pH 用来表示样本的酸碱度。"),
    (r"比重", "比重表示样本相对于水的密度。"),
)
RANGE_LABELS = {
    "HIGH": "高于报告参考范围",
    "LOW": "低于报告参考范围",
    "NORMAL": "在报告参考范围内",
    "REPORTED": "原报告记载的定性结果",
}


class ReadingExtraction(BaseModel):
    source_kind: Literal["pdf", "photo"]
    source_sha256: str
    page_count: int
    findings: list[ScanFinding]


class ReadingTopic(AuditInput):
    scope: Literal["report", "indicator"]
    rows: list[int] = Field(default_factory=list, max_length=8)


class ReadingPrompt(AuditInput):
    findings: list[ScanFinding] = Field(max_length=MAX_READING_ROWS)
    row: int | None = Field(default=None, ge=0)
    question: str = Field(default="", max_length=400)
    patient_context: PatientContext = Field(default_factory=PatientContext)
    previous_topic: ReadingTopic | None = None
    context_replies: dict[str, Annotated[str, StringConstraints(max_length=400)]] = Field(default_factory=dict, max_length=16)
    reply_field: str | None = None
    task: ReadingTaskRequest | None = None


class ReportCitation(BaseModel):
    row: int
    page: int
    name: str
    value: str
    unit: str
    reference: str
    status: str
    related: bool


class ReadingObservation(BaseModel):
    text: str
    rows: list[int]
    sources: list[ReadingSource]


class ReadingFollowUp(BaseModel):
    label: str
    question: str | None = None
    row: int | None = None


class ReadingAnswer(BaseModel):
    text: str
    direct_answer: str = ""
    row: int | None = None
    page: int | None = None

    scope: Literal["report", "indicator", "unresolved"] = "unresolved"
    mode: Literal["evidence", "model", "fallback"] = "evidence"
    model_is_primary: bool = False
    interpretation: list[str] = Field(default_factory=list)
    observations: list[ReadingObservation] = Field(default_factory=list)
    checks: list[ReadingCheck] = Field(default_factory=list)
    follow_ups: list[ReadingFollowUp] = Field(default_factory=list)
    guidance: PatientGuidance | None = None
    topic: ReadingTopic | None = None
    overview: str = ""
    context_question: str = ""
    context_follow_up: ContextFollowUp | None = None
    limitation: str = ""
    citations: list[ReportCitation] = Field(default_factory=list)
    sources: list[ReadingSource] = Field(default_factory=list)
    work_record: ReadingWorkRecord | None = None
    evidence_checks: list[EvidenceCheck] = Field(default_factory=list)
    task: ReadingTaskProgress | None = None


def extract_reading(data: bytes) -> ReadingExtraction:
    pdf = data.startswith(b"%PDF-")
    scan = scan_pdf(data, include_source_audit=False) if pdf else scan_photo(data)
    if not scan.analysis.findings:
        raise InvalidReport("读到了文字，但未找到检验项目和结果。请上传包含表头和完整结果行的原件。")
    if len(scan.analysis.findings) > MAX_READING_ROWS:
        raise InvalidReport("检验项超过 400 项，请拆分报告后分别上传")
    return ReadingExtraction(source_kind="pdf" if pdf else "photo", source_sha256=scan.source_sha256,
                             page_count=len(scan.pages), findings=scan.analysis.findings)


def _labels(value: str) -> str:
    return re.sub(r"\s+", "", normalize("NFKC", value)).upper()

_ABNORMAL = {"HIGH", "LOW"}
_SUMMARY = re.compile(r"整份|这份报告|全报告|汇总|概览|综合|总体|整体|有哪些|哪[几些项]|多少项|几个项目")
_WHY = re.compile(r"为什么|异常|偏[高低]|怎么(看|理解|回事|治|算)|对得?上|怎么算|交叉核验|意味着|说明|代表|含义|意思|有什么问题|什么病|影响|严不严重|严重吗|联系|关系|一起|趋势|病因|诊断|确诊|用药|停药|治疗|复查")
_ACROSS = re.compile(r"有关系|有联系|一起|对比|区别|分别|和.{1,25}相比")
_CARE = re.compile(r"就医|就诊|去医院|看医生")
_MEDICATION_QUESTION = re.compile(r"用药|换药|药物|药品|药名|药剂|吃药|停药|保健品")
_CONTINUATION = re.compile(r"这[个些项两几]|它[们的]?|^(?:那|所以|这样|如果)|刚才|前面|上面|刚说")
_LOOKUP = re.compile(r"是多少|什么单位|单位是什么|哪一页|哪页|原页|参考范围是什么")
_EVIDENCE_PURPOSE = re.compile(
    r"(?:为什么|为啥)(?:需要|还要|还得|要|得|应该|先|还)?\s*(?:自动)?(?>查找|查看|核对|检查|查|找|看|测)"
    r"(?:一下)?(?P<target>[^。！？?]+)",
)
_EVIDENCE_VALUE = re.compile(r"(?:查找|查看|核对|查|找|看|检查|测)\s*(?P<target>[^。！？?]+?)(?:有什么(?:用|意义|作用)|的(?:作用|目的|意义)是什么)")
_PRIORITY = re.compile(r"最重要|最该|最值得|最需要关注|先看|最担心|重点关注|先关注|优先|严重程度")
_TREND = re.compile(r"之前|以前|历次|持续|趋势|变[好了差坏]|上次|是不是一直")
_PATIENT_TERMS = (
    (r"(?<!血)小板", "血小板"),
    (r"血小板(?:的)?(?:大小|个头)", "平均血小板体积"),
    (r"淋巴细胞(?:的)?(?:实际数量|个数|数量)", "淋巴细胞计数"),
)
_LOG = logging.getLogger(__name__)
_ASK_WBC = "这份报告里有同次的白细胞总数（WBC）吗？"
_ASK_BAS = "这份报告里有同次的嗜碱性粒细胞绝对计数（BAS#）吗？"
_FIND_QUESTIONS = {
    _labels(_ASK_WBC): ("WBC", "白细胞总数（WBC）"),
    _labels(_ASK_BAS): ("BAS#", "嗜碱性粒细胞绝对计数（BAS#）"),
}


@dataclass(frozen=True)
class ReadingPlan:
    scope: Literal["report", "indicator", "unresolved"]
    primary: tuple[int, ...] = ()
    related: tuple[int, ...] = ()
    interpret: bool = False
    message: str = ""


@dataclass(frozen=True)
class LiverMedicationEvidence:
    status: Literal["outside", "within", "incomplete"]
    rows: tuple[int, ...]
    outside_rows: tuple[int, ...]
    asked: bool
    missing: tuple[str, ...]

    def fallback(self, findings: list[ScanFinding]) -> str:
        if self.outside_rows:
            results = "、".join(dict.fromkeys(
                f"{findings[index].name} {RANGE_LABELS[findings[index].status]}" for index in self.outside_rows
            ))
            return f"本次报告的 {results}。近期用药是值得核对的线索，单凭这些结果无法确定原因。"
        if self.asked and self.status == "within":
            names = "、".join(dict.fromkeys(findings[index].name for index in self.rows))
            return f"本轮核对的 {names} 在报告参考范围内。近期用药在这次解读中作为背景。"
        if self.asked:
            return "这份报告可用的肝功能结果还不完整，暂时无法据此判断近期用药与这些结果是否有关。"
        return ""


def _liver_medication_evidence(request: ReadingPrompt, plan: ReadingPlan) -> LiverMedicationEvidence | None:
    context = request.patient_context
    if not context.liver_medicine or not plan.interpret or plan.scope == "unresolved":
        return None
    candidates = range(len(request.findings)) if plan.scope == "report" else plan.primary
    indexes = [index for index in candidates if LIVER_INDICATORS.intersection(
        _finding_keys(request.findings[index].name, request.findings[index].unit)
    )]
    if not indexes:
        return None
    outside = tuple(index for index in indexes if request.findings[index].status in _ABNORMAL)[:4]
    within = tuple(index for index in indexes if request.findings[index].status == "NORMAL")
    status = "outside" if outside else "within" if len(within) == len(indexes) else "incomplete"
    asked = bool(_MEDICATION_QUESTION.search(request.question)) or plan.scope == "indicator"
    missing = tuple(key for key, detail in (("name", context.liver_medicine_name), ("since", context.liver_medicine_since))
                    if outside and not detail)
    return LiverMedicationEvidence(status, (*outside, *within[:4]), outside, asked, missing)


_YES_NO_OPTIONS = ("有", "没有", "不确定")
_LIVER_DETAILS = {
    "name": ("liver_medicine_name", "方便说一下，你最近新用或调整的药物、保健品叫什么名字吗？"),
    "since": ("liver_medicine_since", "你是什么时候开始用或调整用量的？大约在这次抽血之前还是之后？"),
}


def _patient_questions(request: ReadingPrompt, abnormal: dict[str, set[str]],
                       medication: LiverMedicationEvidence | None) -> list[tuple[str, ContextFollowUp]]:
    if not request.question:
        return []
    context = request.patient_context
    replies = request.context_replies
    if not request.task and request.reply_field and not is_context_reply_detail(replies.get(request.reply_field, "")):
        return []
    asks_medication = bool(_MEDICATION_QUESTION.search(request.question))
    abnormal_liver = bool(abnormal.keys() & LIVER_INDICATORS)
    liver_question = ("liver_medicine", "抽血前有没有新用或调整过药物、保健品？", _YES_NO_OPTIONS)
    in_medication_thread = request.reply_field is None or request.reply_field in {
        "liver_medicine", "liver_medicine_name", "liver_medicine_since",
    }
    questions: list[tuple[str, ContextFollowUp]] = []
    if medication and (in_medication_thread or request.task):
        for detail in medication.missing:
            field, question = _LIVER_DETAILS[detail]
            if field not in replies:
                questions.append((question, ContextFollowUp(field=field, max_length=MEDICATION_DETAIL_LIMIT)))
    if request.reply_field is not None and not request.task:
        return questions

    candidates: list[tuple[str, str, tuple[str, ...]]] = []
    care_question = bool(_CARE.search(request.question))
    if request.task and care_question and abnormal.keys() & {"WBC", "NEUT#", "LYM#"}:
        candidates.append(("ongoing_fever", "最近有没有持续或反复发热？", _YES_NO_OPTIONS))
    if care_question and abnormal.keys() & {"PLT", "MPV", "PCT", "PDW"}:
        candidates.append(("unusual_bleeding", "最近有没有反复出现说不清原因的瘀青或出血？", _YES_NO_OPTIONS))
    if asks_medication and abnormal_liver:
        candidates.append(liver_question)
    if "LOW" in abnormal.get("LYM#", ()):
        history_question = ("previous_low_lymphocytes", "以前查血时，医生也提过淋巴细胞数量偏低吗？", _YES_NO_OPTIONS)
        if _TREND.search(request.question) or (request.task and context.recent_infection):
            candidates.append(history_question)
        if not (context.immune_medicine or context.previous_low_lymphocytes):
            candidates.append(("recent_infection", "抽血前后有没有感冒、发烧或其他感染？", _YES_NO_OPTIONS))
        if request.task and not context.recent_infection and not _TREND.search(request.question):
            candidates.append(("immune_medicine", "最近有没有在用激素或医生开的免疫抑制药？", _YES_NO_OPTIONS))
            candidates.append(history_question)
    if not asks_medication and abnormal_liver:
        candidates.append(liver_question)
    if abnormal.keys() & {"URBC", "UWBC", "URINE", "URINE_BLOOD"}:
        candidates.append(("urinary_symptoms", "最近小便时会痛吗？有没有比平时更频繁？", _YES_NO_OPTIONS))
    if abnormal.keys() & {"GLU", "TG"}:
        candidates.append(("fasting", "这次抽血前，你有按要求空腹吗？", ("是", "否", "不确定")))
    if abnormal.keys() & {"CREA", "KIDNEY"}:
        candidates.append(("kidney_medicine", "最近有没有用药或服用肌酸等补充剂？", _YES_NO_OPTIONS))
    questions.extend((question, ContextFollowUp(field=field, options=options)) for field, question, options in candidates
                     if field not in replies and not getattr(context, field))
    return questions


def _ask_patient(request: ReadingPrompt, abnormal: dict[str, set[str]],
                 medication: LiverMedicationEvidence | None) -> tuple[str, ContextFollowUp | None]:
    return next(iter(_patient_questions(request, abnormal, medication)), ("", None))


def _question_terms(text: str) -> str:
    text = normalize("NFKC", text)
    for pattern, replacement in _PATIENT_TERMS:
        text = re.sub(pattern, replacement, text)
    return text


def _matches(question: str, keys: tuple[str, ...], row: ScanFinding, labels: tuple[str, ...]) -> bool:
    if labels:
        return bool(set(keys).intersection(labels) or ("BAS" in keys and {"BAS%", "BAS#"}.intersection(labels)))
    label = _labels(row.name)
    return len(label) >= 2 and label in question


def _plan(request: ReadingPrompt) -> ReadingPlan:
    rows = request.findings
    question = _question_terms(request.question).strip()
    explicit_lookup = _LOOKUP.search(question) and not (_WHY.search(question) or _CARE.search(question))
    interpretations = not question or not explicit_lookup
    keys = indicator_keys(question)
    labels = [_finding_keys(row.name, row.unit) for row in rows]
    normalized_question = _labels(question)
    named = tuple(i for i, row in enumerate(rows) if _matches(normalized_question, keys, row, labels[i]))
    # A broad abnormality question overrides the last focused indicator.
    whole_report = _SUMMARY.search(question) and (not named or re.search(r"整份|全报告|总体|整体", question))
    if whole_report or (not named and re.search(r"异常指标|异常项目|这些异常|哪些|超[出过]参考范围|身体有什么问题", question)):
        primary = tuple(i for i, row in enumerate(rows) if row.status in _ABNORMAL)[:8]
        scope = "report"
    elif len(named) > 1 and not (len(keys) > 1 or _ACROSS.search(question)):
        if request.row not in named:
            return ReadingPlan("unresolved", message="报告里有多项可能对应这个名称。请从检验列表选中具体一项，或写出完整的项目代码。")
        scope, primary = "indicator", (request.row,)
    elif named:
        scope, primary = "indicator", named[:8]
    elif keys:
        return ReadingPlan("unresolved", message="这份报告里没有找到你问的指标。可以使用报告中的项目名称或代码。")
    elif request.row is not None and request.row < len(rows):
        scope, primary = "indicator", (request.row,)
    elif request.previous_topic and request.previous_topic.scope == "indicator" and (
        _CONTINUATION.search(question) or _CARE.search(question) or _PRIORITY.search(question) or explicit_lookup
    ):
        primary = tuple(dict.fromkeys(index for index in request.previous_topic.rows if 0 <= index < len(rows)))
        if not primary:
            return ReadingPlan("unresolved", message="请在报告中点选你想继续了解的项目。")
        scope = "indicator"
    elif interpretations and (bool(_WHY.search(question) or _CARE.search(question) or _PRIORITY.search(question))
                              or bool(request.previous_topic and request.previous_topic.scope == "report")):
        scope = "report"
        primary = tuple(i for i, row in enumerate(rows) if row.status in _ABNORMAL)[:8]
    else:
        return ReadingPlan("unresolved", message="可以问「异常指标说明什么」，也可以选中一项后继续追问。")

    desired = {key for i in primary if rows[i].status != "REVIEW" for label in labels[i] for key in related_keys(label)}
    unique_related = {key for key in desired if sum(key in items for items in labels) == 1}
    related = tuple(i for i, label in enumerate(labels) if i not in primary and rows[i].status != "REVIEW"
                    and unique_related.intersection(label))[:8] if interpretations else ()
    return ReadingPlan(scope, primary, related, interpretations)


def _sources(notes: list[Note]) -> list[ReadingSource]:
    return [ReadingSource(title=title, url=url) for title, url in dict.fromkeys((note.title, note.url) for note in notes)]


def _explain_evidence_purpose(request: ReadingPrompt) -> ReadingAnswer | None:
    question = _question_terms(request.question).strip()
    match = _EVIDENCE_PURPOSE.search(question) or _EVIDENCE_VALUE.search(question)
    if not match:
        return None
    target = match.group("target").strip()
    if target.startswith(("不到", "不出", "不见", "不着", "到", "出", "了")):
        return None
    keys = [key for key in indicator_keys(target) if key in EVIDENCE_PURPOSES]
    if not keys:
        return None
    rows = request.findings
    labels = [_finding_keys(row.name, row.unit) for row in rows]
    checks: list[EvidenceCheck] = []
    citations: dict[int, ReportCitation] = {}
    explanations = []
    source_ids: set[str] = set()
    note_keys: dict[str, None] = {}
    for key in keys:
        purpose = EVIDENCE_PURPOSES[key]
        explanations.append(purpose.explanation)
        source_ids.update(purpose.source_ids)
        note_keys.update(dict.fromkeys((key, *purpose.related)))
        if not rows:
            continue
        lookup = resolve_report_field(rows, labels, key)
        checks.append(describe_report_lookup(rows, key, purpose.reason, lookup))
        if lookup.status == "confirmed":
            index = lookup.rows[0]
            citations[index] = _citation(index, rows[index], related=False)
    notes = [note for note in retrieve_notes(tuple(note_keys)) if note.id in source_ids]
    return ReadingAnswer(text="", direct_answer="\n\n".join(explanations), scope="report", citations=list(citations.values()),
                         sources=_sources(notes), evidence_checks=checks)


def _locate_report_field(request: ReadingPrompt) -> ReadingAnswer | None:
    target = _FIND_QUESTIONS.get(_labels(request.question).rstrip("?") + "?")
    if target is None:
        return None
    key, name = target
    keys_by_row = [_finding_keys(row.name, row.unit) for row in request.findings]
    lookup = resolve_report_field(request.findings, keys_by_row, key)
    matches = lookup.rows
    checks = [describe_report_lookup(request.findings, key, f"查找用户问到的{name}", lookup)]
    if not matches:
        return ReadingAnswer(scope="report", evidence_checks=checks, text=f"这份报告的已识别项目中没有找到{name}。"
                                                    "请在原报告的血常规表中查找该项目，核对采样信息、数值、单位和参考范围。")
    if len(matches) > 1:
        pages = "、".join(f"第 {page} 页" for page in sorted({request.findings[index].page for index in matches}))
        return ReadingAnswer(scope="report", evidence_checks=checks,
                             text=f"在{pages}识别到多条{name}。请按原报告的采样时间或样本编号选择同次的记录。",
                             follow_ups=[ReadingFollowUp(label=f"核对第 {request.findings[index].page} 页第 {index + 1} 项",
                                                         row=index) for index in matches[:3]])
    index = matches[0]
    row = request.findings[index]
    if row.status == "REVIEW":
        text = f"第 {row.page} 页识别到{name}，字段仍待确认。请在原页核对后再使用。"
    else:
        unit = f" {row.unit}" if row.unit else "（报告未标单位）"
        reference = f"，报告参考范围为 {row.reference}" if row.reference else ""
        text = f"第 {row.page} 页识别到{name}，结果为 {row.value}{unit}{reference}。请对照采样信息确认是否为同次采样。"
    return ReadingAnswer(text=text, scope="indicator", row=index, page=row.page, evidence_checks=checks)


def _follow_ups(request: ReadingPrompt, plan: ReadingPlan, checks: list[ReadingCheck]) -> list[ReadingFollowUp]:
    rows = request.findings
    pending = [index for index in plan.primary if rows[index].status == "REVIEW"]
    if plan.scope == "report":
        pending = [index for index, row in enumerate(rows) if row.status == "REVIEW"]
    to_review = dict.fromkeys((*pending, *(index for check in checks for index in check.review_rows)))
    return [ReadingFollowUp(label=f"核对{rows[index].name} · 第 {rows[index].page} 页", row=index)
            for index in list(to_review)[:4]]


def _interpret_findings(citations: list[ReportCitation], rows: list[ScanFinding], notes: list[Note]) -> list[str]:
    primary = [item for item in citations if not item.related]
    by_key: dict[str, list[ReportCitation]] = {}
    for citation in citations:
        for key in _finding_keys(citation.name, citation.unit):
            by_key.setdefault(key, []).append(citation)

    def unique(key: str) -> ReportCitation | None:
        items = by_key.get(key, [])
        if len(items) == 1 and sum(key in _finding_keys(row.name, row.unit) for row in rows) == 1:
            return items[0]
        return None

    interpretation: list[str] = []
    covered: set[int] = set()
    basophils = [item for item in by_key.get("BAS%", []) if item.status == "HIGH" and not item.related]
    if basophils:
        count = unique("BAS#")
        if len(basophils) == 1 and count is not None:
            detail = f"这份报告的 BAS% 偏高，报告中的 BAS# {RANGE_LABELS[count.status]}。"
        else:
            detail = "报告的 BAS% 偏高。目前缺少可唯一对应的已确认 BAS#，因此只能确认比例偏高。"
        interpretation.append(detail + "单凭比例偏高无法判断是否有过敏或反应有多强。")
        covered.update(item.row for item in basophils)

    lymphocytes = unique("LYM#")
    lymph_percentage = unique("LYM%")
    if lymphocytes is not None and lymphocytes.status in _ABNORMAL and not lymphocytes.related:
        detail = f"淋巴细胞绝对计数（LYM#）{RANGE_LABELS[lymphocytes.status]}。"
        if lymph_percentage is not None:
            detail += f"淋巴细胞比例（LYM%）{RANGE_LABELS[lymph_percentage.status]}。比例和绝对计数衡量的是不同的量。"
        interpretation.append(detail + "判断原因需要结合年龄、近期症状、用药和既往血常规。")
        covered.add(lymphocytes.row)

    platelet_labels = {"MPV": "平均体积（MPV）", "PCT": "压积（PCT）", "PDW": "体积分布宽度（PDW）"}
    platelet_rows = {key: unique(key) for key in platelet_labels}
    platelet_abnormal = any(item is not None and not item.related and item.status in _ABNORMAL
                            for item in platelet_rows.values())
    if platelet_abnormal:
        parts = [f"{platelet_labels[key]}{RANGE_LABELS[item.status]}" for key, item in platelet_rows.items()
                 if item is not None]
        count = unique("PLT")
        if count is not None:
            parts.insert(0, f"计数（PLT）{RANGE_LABELS[count.status]}")
        interpretation.append("血小板" + "，".join(parts) + "。压积与血小板个数和平均大小有关，"
                              "分布宽度反映大小差异。体积相关指标合起来仍不足以判断具体病因。")
        covered.update(item.row for item in (*platelet_rows.values(), count) if item is not None)

    other_primary = [item for item in primary if item.row not in covered]
    for note in notes:
        supported = [item for item in other_primary
                     if item.row not in covered and set(_finding_keys(item.name, item.unit)).intersection(note.keys)]
        if supported:
            interpretation.append(note.text)
            covered.update(item.row for item in supported)

    other_outside = [item for item in primary if item.status in _ABNORMAL and item.row not in covered]
    if other_outside:
        names = "、".join(item.name for item in other_outside)
        interpretation.append(f"{names}的偏高或偏低可见下方报告依据。当前资料不足以进一步解释这些项目。")
    return interpretation


def _evidence_answer(request: ReadingPrompt, plan: ReadingPlan, notes: list[Note],
                     medication: LiverMedicationEvidence | None, evidence_checks: list[EvidenceCheck]) -> ReadingAnswer:
    rows = request.findings
    context = request.patient_context
    emergency = guidance_for_patient({}, {}, context) if context.severe_bleeding else None
    location = _locate_report_field(request)
    if location is not None:
        if location.row is not None:
            row = request.findings[location.row]
            citations = [_citation(location.row, row, related=False)] if row.status != "REVIEW" else []
            return location.model_copy(update={"topic": ReadingTopic(scope="indicator", rows=[location.row]),
                                               "citations": citations, "guidance": emergency})
        return location.model_copy(update={"guidance": emergency})
    if plan.scope == "unresolved":
        return ReadingAnswer(text=plan.message, guidance=emergency)
    citations = [_citation(i, rows[i], related=i in plan.related)
                 for i in (*plan.primary, *plan.related) if rows[i].status != "REVIEW"]
    pending = sum(row.status == "REVIEW" for row in rows)
    interpretation: list[str] = []
    checks: list[ReadingCheck] = []
    limitation = ""
    index = plan.primary[0] if plan.scope == "indicator" and len(plan.primary) == 1 else None
    if index is not None:
        row = rows[index]
        if row.status == "REVIEW":
            return ReadingAnswer(row=index, page=row.page, scope=plan.scope,
                                 topic=ReadingTopic(scope="indicator", rows=[index]),
                                 guidance=emergency,
                                 follow_ups=_follow_ups(request, plan, []),
                                 text="这一项的识别结果待确认。请先对照原页核对字段，再查看数值解读。")
        definition = next((detail for pattern, detail in DEFINITIONS if re.search(pattern, row.name, re.I)), "")
        text = f"{row.name}。{definition}原报告第 {row.page} 页写的是 {row.value}{' ' + row.unit if row.unit else ''}。"
        if row.reference:
            text += f"该报告的参考范围为 {row.reference}。"
        text += f"比对结果为{RANGE_LABELS[row.status]}。"
    elif plan.scope == "report":
        outside = sum(row.status in _ABNORMAL for row in rows)
        text = f"报告共识别 {len(rows)} 项，{outside} 项超出报告参考范围，{pending} 项待确认。"
        if outside > len(plan.primary):
            text += f"这里列出前 {len(plan.primary)} 项，完整清单见上方摘要。"
    else:
        pending_selected = sum(rows[i].status == "REVIEW" for i in plan.primary)
        text = "可以结合下方已确认的项目一起看。" if citations else "所问的项目均待确认，请先对照原页核对。"
        if citations and pending_selected:
            text += f"所问项目中有 {pending_selected} 项待确认。"

    if plan.interpret and citations:
        interpretation = _interpret_findings(citations, rows, notes)
        checks = check_blood_counts(rows, [_finding_keys(row.name, row.unit) for row in rows], plan.primary,
                                    {citation.row for citation in citations})
        if not interpretation:
            interpretation.append("可依据原报告了解结果与参考范围的关系。当前资料不足以进一步解释该项目。")
        limitation = next(note.text for note in notes if note.id == "reference_ranges")
        if plan.scope == "report":
            limitation += " 这份报告单独无法确定具体病症。"
        if pending and plan.scope == "report":
            limitation += " 待确认的项目需要先核对原页。"
    evidence: dict[str, set[str]] = {}
    abnormal: dict[str, set[str]] = {}
    for citation in citations:
        for key in _finding_keys(citation.name, citation.unit):
            evidence.setdefault(key, set()).add(citation.status)
            if not citation.related and citation.status in _ABNORMAL:
                abnormal.setdefault(key, set()).add(citation.status)
    guidance = None
    has_context = bool(context.model_dump(exclude_defaults=True))
    has_abnormal = any(not citation.related and citation.status in _ABNORMAL for citation in citations)
    if plan.scope == "report" and has_context:
        for row in rows:
            if row.status != "REVIEW":
                for key in _finding_keys(row.name, row.unit):
                    evidence.setdefault(key, set()).add(row.status)
    for check in evidence_checks:
        if check.status != "confirmed":
            evidence.pop(check.indicator, None)
    if emergency or (plan.interpret and (has_abnormal or _CARE.search(request.question)
                                        or (has_context and (citations or plan.scope == "report")))):
        liver_context = medication.fallback(rows) if medication else ""
        guidance = guidance_for_patient(abnormal, evidence, context, liver_context=liver_context)
        if liver_context and medication:
            existing = {item.row for item in citations}
            citations.extend(_citation(i, rows[i], related=True) for i in medication.rows if i not in existing)
            guidance.sources.extend(_sources([note for note in notes if note.id == "liver_enzymes"]))
    overview = ""
    if _PRIORITY.search(request.question) and plan.scope == "report":
        if "LOW" in abnormal.get("LYM#", ()) and set(abnormal) <= {"LYM#", "BAS%", "MPV", "PCT", "PDW"}:
            overview = "可以先看淋巴细胞的实际数量以前是否也偏低。它是报告直接测得的异常。血小板压积与体积有关，嗜碱性粒细胞比例还要结合实际数量。"
        elif citations:
            overview = "可以先核对实际计数及历次是否有变化。超出参考范围的幅度本身无法决定哪个问题更紧急，有无不适也会影响下一步。"
    elif _TREND.search(request.question) and citations:
        overview = ("你提到以前也偏低。把前后两份报告的同项结果、单位和参考范围放在一起，才能判断变化幅度。"
                    if context.previous_low_lymphocytes and "LOW" in abnormal.get("LYM#", ()) else
                    "当前依据来自这份报告。判断是一次波动还是持续变化，需要对照以前同项的结果、单位和参考范围。")
    context_question, context_follow_up = _ask_patient(request, abnormal, medication) if guidance and not emergency else ("", None)
    return ReadingAnswer(text=text, row=index, page=rows[index].page if index is not None else None,
                         scope=plan.scope, citations=citations, interpretation=interpretation, checks=checks, limitation=limitation,
                         sources=_sources(notes) if interpretation else [],
                         follow_ups=_follow_ups(request, plan, checks) if plan.interpret or pending else [], guidance=guidance,
                         overview=overview, context_question=context_question, context_follow_up=context_follow_up,
                         evidence_checks=evidence_checks,
                         topic=ReadingTopic(scope=plan.scope, rows=list(plan.primary)))


def _note_keys(request: ReadingPrompt, plan: ReadingPlan, medication: LiverMedicationEvidence | None) -> tuple[str, ...]:
    keys = dict.fromkeys(key for index in (*plan.primary, *plan.related) if request.findings[index].status != "REVIEW"
                         for key in _finding_keys(request.findings[index].name, request.findings[index].unit))
    if medication:
        keys.setdefault("LIVER")
    return tuple(keys)


def answer_reading(request: ReadingPrompt) -> ReadingAnswer:
    """Deterministic interpretation for field lookup and when generation is unavailable."""
    request = _relevant_context(request)
    return _gather({"request": request, "plan": _plan(request)})["answer"]


class ModelObservation(AuditInput):
    explanation: str = Field(min_length=1, max_length=260)
    report_rows: list[int] = Field(min_length=1, max_length=8)
    source_ids: list[str] = Field(min_length=1, max_length=3)
    context_ref: Literal["liver_medication"] | None = None


class ModelReading(AuditInput):
    observations: list[ModelObservation] = Field(min_length=1, max_length=5)


def _citation(index: int, row: ScanFinding, *, related: bool) -> ReportCitation:
    return ReportCitation(row=index, related=related, **row.model_dump(include={
        "page", "name", "value", "unit", "reference", "status",
    }))


def _valid_observation(observation: ModelObservation, answer: ReadingAnswer, notes: list[Note]) -> bool:
    rows = {citation.row: citation for citation in answer.citations}
    sources = {note.id: note for note in notes}
    if not set(observation.report_rows) <= rows.keys() or not set(observation.source_ids) <= sources.keys():
        return False
    if answer.topic and answer.topic.scope == "indicator" and not set(observation.report_rows).intersection(answer.topic.rows):
        return False
    keys = {key for row in observation.report_rows for key in _finding_keys(rows[row].name, rows[row].unit)}
    if any(sources[id].keys and not keys.intersection(sources[id].keys) for id in observation.source_ids):
        return False
    explanation = normalize("NFKC", observation.explanation).strip()
    numeric_text = re.sub(r"\b(?:HBA1C|1YM|PET1)\b", "", explanation, flags=re.I)
    if re.search(r"\d|第.{1,4}页|确诊|诊断为|患有|处方|服用|停药|排除", numeric_text):
        return False
    # Use the reviewed, deterministic caveat for allergy instead of model claims.
    if re.search(r"过敏|变应原", explanation):
        return False
    if re.search(r"就医|就诊|急诊|急救|联系医生|复查|无需看医生|停用", explanation):
        return False
    if keys & LIVER_INDICATORS and re.search(r"肝功能(?:检查)?(?:结果)?(?:整体)?(?:正常|未见异常|没有异常)|肝脏?(?:损伤|损害)", explanation):
        return False
    if keys & {"MPV", "PCT", "PDW"} and re.search(r"血小板功能(?:异常|障碍|减退)", explanation):
        return False
    if re.search(r"(?:提示|考虑|怀疑|可能与|可能是|可能表示|可能意味着|表明|反映|由于)[^。！？；]*(?:感染|疾病|肿瘤|癌症|贫血)", explanation):
        return False
    # General explanations can mention white blood cells without claiming a WBC result.
    # Match explicit project labels directly before a result assertion.
    status_patterns = (
        (r"偏高|升高|高于", "HIGH"),
        (r"偏低|降低|低于", "LOW"),
        (r"在(?:报告参考|参考|正常)范围内|处于正常范围|(?<!不)正常(?!范围|值)", "NORMAL"),
    )
    for clause in re.split(r"[，。；、！？]|但|而|同时|意味着|说明|表示", explanation):
        for pattern, status in status_patterns:
            match = re.search(rf"([a-zA-Z0-9%#]+|[一-龥]{{2,16}})\s*(?:的?(?:结果|数值|水平))?\s*(?:{pattern})", clause)
            if not match:
                continue
            label = match.group(1)
            indicators = set(indicator_keys(label))
            if label.isascii() and indicators and not indicators <= keys:
                return False
            for indicator in indicators.intersection(keys):
                statuses = {rows[row].status for row in observation.report_rows
                            if indicator in _finding_keys(rows[row].name, rows[row].unit)}
                if status not in statuses:
                    return False
    return bool(explanation)


def _parse_model_reading(content: str) -> ModelReading:
    raw = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S | re.I)
    return ModelReading.model_validate_json(fenced.group(1) if fenced else raw)


def _complete_observations(content: str) -> Iterator[ModelObservation]:
    """Yield complete objects from the leading observations array as it streams."""
    start = re.match(r'\s*(?:```(?:json)?\s*)?\{\s*"observations"\s*:\s*\[', content, flags=re.I)
    if start is None:
        return
    decoder = json.JSONDecoder()
    position = start.end()
    while position < len(content):
        position += len(content[position:]) - len(content[position:].lstrip())
        try:
            value, position = decoder.raw_decode(content, position)
        except ValueError:
            return
        try:
            yield ModelObservation.model_validate(value)
        except ValidationError:
            pass
        position += len(content[position:]) - len(content[position:].lstrip())
        if content[position:position + 1] != ",":
            return
        position += 1


def _required_evidence_groups(request: ReadingPrompt, answer: ReadingAnswer) -> list[list[int]]:
    if answer.scope != "report" or not answer.guidance or any(pattern.search(request.question) for pattern in (
        _PRIORITY, _TREND, _MEDICATION_QUESTION,
    )):
        return []
    groups: dict[str | int, list[int]] = {}
    for citation in answer.citations:
        if citation.related or citation.status not in _ABNORMAL:
            continue
        key = next(iter(_finding_keys(citation.name, citation.unit)), citation.row)
        if key in {"PLT", "MPV", "PCT", "PDW"}:
            key = "platelets"
        elif isinstance(key, str) and key.endswith(("#", "%")):
            key = key[:-1]
        elif key in LIVER_INDICATORS:
            key = "liver"
        groups.setdefault(key, []).append(citation.row)
    return list(groups.values())


def _valid_medication_context(observation: ModelObservation, medication: LiverMedicationEvidence | None,
                              rows: list[tuple[str, ...]]) -> bool:
    text = observation.explanation
    cites_liver = any(LIVER_INDICATORS.intersection(rows[i]) for i in observation.report_rows)
    mentions_medicine = bool(_MEDICATION_QUESTION.search(text))
    if observation.context_ref is None:
        return not (mentions_medicine and (cites_liver or (medication is not None and "近期用药" in text)))
    if not medication or (medication.status != "outside" and not medication.asked):
        return False
    if not mentions_medicine or not set(observation.report_rows) <= set(medication.rows):
        return False
    if "liver_enzymes" not in observation.source_ids:
        return False
    if re.search(r"导致|引起|造成|药物性|肝损伤|副作用|关系不大|没有影响|无关|请补充|请提供|告诉我|什么药|什么时候开始", text):
        return False
    if medication.status == "outside":
        return bool(set(observation.report_rows).intersection(medication.outside_rows)
                    and re.search(r"偏[高低]|升高|降低|高于|低于|超出|异常", text))
    return not re.search(r"偏[高低]|升高|降低|高于|低于|超出|所有肝功能|肝功能正常", text)


async def _model_answer(request: ReadingPrompt, answer: ReadingAnswer, notes: list[Note],
                        medication: LiverMedicationEvidence | None, record: ReadingWorkRecord,
                        *, streaming: bool = False) -> ReadingAnswer:
    retrieved = {item.row: item for item in answer.citations}
    if medication:
        for index in medication.rows:
            retrieved.setdefault(index, _citation(index, request.findings[index], related=True))
    knowledge = {note.id: note for note in notes}
    keys_by_row = [_finding_keys(row.name, row.unit) for row in request.findings]

    @tool
    def lookup_report(terms: str) -> dict:
        """Look up a lab item or related findings in this report. Pass indicator names or codes only."""
        terms = _question_terms(terms[:100])
        labels = indicator_keys(terms)
        normalized = _labels(terms)
        matches = [index for index, row in enumerate(request.findings)
                   if _matches(normalized, labels, row, keys_by_row[index])]
        found = []
        for index in matches[:8]:
            row = request.findings[index]
            keys = keys_by_row[index]
            repeated = any(resolve_report_field(request.findings, keys_by_row, key).status == "ambiguous" for key in keys)
            if row.status == "REVIEW" or repeated:
                found.append({"row": index, "name": row.name, "page": row.page,
                              "status": "待确认" if row.status == "REVIEW" else "有多条记录，请先核对采样"})
            else:
                citation = retrieved.setdefault(index, _citation(index, row, related=False))
                found.append(citation.model_dump())
        return {"matches": found, "notice": "" if found else "报告已识别项目中未找到。"}

    @tool
    def search_lab_knowledge(terms: str) -> list[dict]:
        """Retrieve reviewed explanations for lab indicators. Pass names or indicator codes only."""
        keys = indicator_keys(_question_terms(terms[:100]))
        matches = retrieve_notes(keys)
        knowledge.update((note.id, note) for note in matches)
        return [{"id": note.id, "content": note.text} for note in matches]

    @tool
    def check_lab_relationships(terms: str) -> list[dict]:
        """Calculate supported relationships among confirmed blood count rows. Pass indicator names or codes."""
        keys = set(indicator_keys(_question_terms(terms[:100])))
        if not keys:
            return []
        wanted = keys | {related for key in keys for related in related_keys(key)}
        evidence = {index for index, row in enumerate(request.findings)
                    if row.status != "REVIEW" and wanted.intersection(keys_by_row[index])}
        primary = tuple(index for index in sorted(evidence) if keys.intersection(keys_by_row[index]))
        checks = check_blood_counts(request.findings, keys_by_row, primary, evidence)
        for check in checks:
            for index in check.rows:
                retrieved.setdefault(index, _citation(index, request.findings[index], related=True))
            if check not in completed_checks:
                completed_checks.append(check)
        return [check.model_dump() for check in checks]

    completed_checks = list(answer.checks)
    registered = {item.name: item for item in (lookup_report, search_lab_knowledge, check_lab_relationships)}
    tool_node = ToolNode(list(registered.values()), handle_tool_errors=False)
    llm = get_chat_llm("precise", streaming=streaming, temperature=0.1, purpose="report_reading",
                       timeout=MODEL_CALL_TIMEOUT_SECONDS, max_retries=0)
    with_tools = llm.bind_tools(list(registered.values()))
    previewed: set[str] = set()

    async def stream_model(model: Runnable, messages: list[BaseMessage], **options: object) -> AIMessage:
        combined = None
        allowed = answer.model_copy(update={"citations": list(retrieved.values())})
        async with aclosing(model.astream(messages, max_tokens=900, **options)) as chunks:
            async for chunk in chunks:
                combined = chunk if combined is None else combined + chunk
                if not isinstance(combined.content, str) or getattr(combined, "tool_calls", None):
                    continue
                for item in _complete_observations(combined.content):
                    if (item.explanation not in previewed and _valid_observation(item, allowed, list(knowledge.values()))
                            and _valid_medication_context(item, medication, keys_by_row)):
                        separator = "\n\n" if previewed else ""
                        previewed.add(item.explanation)
                        emit_custom({"type": "text", "content": separator + item.explanation})
        if combined is None:
            raise ValueError("Empty report model stream")
        return message_chunk_to_message(combined)

    async def invoke_model(model: Runnable, messages: list[BaseMessage], phase: ModelCallPhase,
                           **options: object) -> tuple[AIMessage, ReadingModelCall]:
        call = ReadingModelCall(phase=phase, status="returned")
        record.model_calls.append(call)
        started = perf_counter()
        is_recovery = phase in {"rewrite", "timeout_retry", "coverage_repair"}
        timeout = MODEL_RECOVERY_TIMEOUT_SECONDS if is_recovery else MODEL_CALL_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(timeout):
                reply = (await stream_model(model, messages, **options) if streaming
                         else await model.ainvoke(messages, max_tokens=900, **options))
        except APITimeoutError:
            call.status = "timeout"
            record.fallback_reason = "provider_timeout"
            raise
        except TimeoutError:
            call.status = "timeout"
            record.fallback_reason = "model_call_timeout"
            raise
        except asyncio.CancelledError:
            call.status = "timeout"
            raise
        except Exception as error:
            call.status = "failed"
            if isinstance(error, APIStatusError):
                call.http_status = error.status_code
            record.fallback_reason = "provider_error"
            raise
        finally:
            call.elapsed_ms = round((perf_counter() - started) * 1000)
        return reply, call

    context = request.patient_context
    required_groups = _required_evidence_groups(request, answer)
    needs_medication = bool(medication and (medication.status == "outside" or (medication.asked and medication.rows)))

    def report_evidence() -> list[dict]:
        return [{**citation.model_dump(), "allowed_source_ids": [
            note.id for note in knowledge.values() if not note.keys or set(note.keys).intersection(keys_by_row[citation.row])
        ]} for citation in retrieved.values()]

    evidence = {"scope": answer.scope, "question": request.question or "请用通俗中文解释当前选中的检验项目。",
                "current_topic": answer.topic.model_dump() if answer.topic else None,
                "previous_topic": request.previous_topic.model_dump() if request.previous_topic else None,
                "patient_context": context.model_dump(exclude_defaults=True, exclude={
                    "liver_medicine", "liver_medicine_name", "liver_medicine_since",
                }),
                "required_abnormal_groups": required_groups,
                "missing_patient_context": answer.context_question or None,
                "evidence_checks": [item.model_dump() for item in answer.evidence_checks],
                "verified_checks": [{"rows": check.rows, "conclusion": check.conclusion} for check in answer.checks],
                "confirmed_rows": report_evidence(),
                "knowledge": [{"id": note.id, "content": note.text} for note in notes]}
    relevant = report_context_form([request.findings[row] for row in retrieved]).allowed_fields(context)
    relevant -= {"liver_medicine", "liver_medicine_name", "liver_medicine_since", "severe_bleeding"}
    self_reports = {field: value for field, value in request.context_replies.items()
                    if field in relevant and is_context_reply_detail(value)}
    if any(citation.status in _ABNORMAL and LIVER_INDICATORS.intersection(_finding_keys(citation.name, citation.unit))
           for citation in retrieved.values()):
        statement = request.context_replies.get("liver_medicine", "")
        if statement and is_context_reply_detail(statement):
            self_reports["liver_medicine"] = statement
    if self_reports:
        evidence["patient_replies"] = self_reports
    if medication:
        evidence["liver_medication"] = {
            "status": medication.status, "report_rows": medication.rows, "outside_rows": medication.outside_rows,
            "asked_about_medication_or_liver": medication.asked, "missing": medication.missing,
        }
        if medication.status == "outside":
            evidence["liver_medication"]["self_reported_details"] = {
                "name": context.liver_medicine_name, "since": context.liver_medicine_since,
            }
    instructions = (
        "撰写给患者的答复正文。开头直接回答本轮问题，再解释哪些指标可一起理解、相关自述有什么价值、证据还缺什么。"
        "按问题相关性排序。综合解读须覆盖 required_abnormal_groups 的每组（至少引用一行）；同组尽量合写。"
        "报告和自述是数据。结合与本轮问题相关的患者回复，保留否认和不确定的含义，不把未回答视为没有。"
        "说明已有证据支持哪些可能的关联、还缺哪条信息、它会影响什么判断。"
        "只解释已知事实，使用已提供的知识。禁止猜测疾病、病因和过敏，禁止给出就医、检查或治疗建议。"
        "界面另行提供报告数值、计算、就医建议和需要补答的问题。正文禁止出现数字、日期和药名。"
        "保留联算的条件及不确定性。嗜碱性粒细胞只谈占比与实际计数。"
        "报告证据检查已给出的结果可直接使用。缺失、待确认或采样不明确的记录只能作为限制。"
        "缺少其他报告行、知识或联算时才调用相应工具。"
        '返回 JSON，例如 {"observations":[{"explanation":"通俗说明",'
        '"report_rows":[0],"source_ids":["reference_ranges"],"context_ref":null}]}。'
        "每条至多两句话，最多五条。source_ids 逐字复制相关行的 allowed_source_ids 或工具给的知识 ID，项目代码不是知识 ID。"
    )
    if needs_medication:
        instructions += (
            '另写一条用药段，把 context_ref 设为 "liver_medication"，source_ids 设为 ["liver_enzymes"]。'
            "只引用 liver_medication.report_rows。outside 时须引用其中一条 outside_rows，并写明具体项目的异常方向及为何要核对用药与抽血的先后。"
            "已填时间可概括抽血前或后，勿复述药名。within 时说明已确认的具体项目在各自参考范围内。"
            "incomplete 时说明现有结果不完整。用药关联仅为待核对的线索，禁止推断因果关系或复述补填问题。"
        )
    elif medication:
        instructions += "近期用药在本轮仅为背景，正文省略用药。"
    messages = [SystemMessage(content=instructions), HumanMessage(content=json.dumps(evidence, ensure_ascii=False))]
    try:
        response, call_record = await invoke_model(with_tools, messages, "initial")
    except (APITimeoutError, TimeoutError):
        if not retrieved:
            raise
        # Retry once with the evidence already gathered. Avoid restarting tool selection.
        record.fallback_reason = None
        response = None
    for _ in range(2):
        if response is None or not response.tool_calls:
            break
        if len(response.tool_calls) > 3 or any(not call.get("id") for call in response.tool_calls):
            record.fallback_reason = "invalid_tool_requests"
            raise ValueError("Invalid report tool requests")
        messages.append(response)
        executions: list[ReadingToolExecution] = []
        record.tool_rounds.append(executions)
        for call in response.tool_calls:
            name = call["name"]
            selected = registered.get(name)
            terms = call["args"].get("terms")
            if selected is None or not isinstance(terms, str):
                tool_reply = ToolMessage(content='{"notice":"无效的取证请求。"}', tool_call_id=call["id"])
                executions.append(ReadingToolExecution(name=name if selected else "unknown", status="invalid_request",
                                                       result="工具未注册或参数无效。"))
            else:
                if streaming:
                    emit_custom({"type": "tool_start", "tool": name, "content": {
                        "lookup_report": "正在查找报告指标…", "search_lab_knowledge": "正在检索检验知识…",
                        "check_lab_relationships": "正在核验指标间的计算关系…",
                    }[name]})
                try:
                    output = await tool_node.ainvoke([{
                        "type": "tool_call", "id": call["id"], "name": name, "args": {"terms": terms},
                    }])
                    tool_reply, = output["messages"]
                    result = json.loads(tool_reply.content)
                except Exception:
                    executions.append(ReadingToolExecution(name=name, status="failed", result="工具执行失败。"))
                    record.fallback_reason = "tool_failed"
                    raise
                finally:
                    if streaming:
                        emit_custom({"type": "tool_end", "tool": name})
                if name == "lookup_report":
                    matches = result["matches"]
                    count = len(matches)
                    confirmed = sum("value" in row for row in matches)
                    keys = "、".join(dict.fromkeys(key for row in matches for key in keys_by_row[row["row"]]))
                    summary = f"找到 {count} 条报告记录" + (f"（{keys}）" if keys else "")
                    summary += f"，{confirmed} 条已确认，{count - confirmed} 条需核对。"
                elif name == "search_lab_knowledge":
                    count = len(result)
                    titles = "、".join(dict.fromkeys(knowledge[note["id"]].title for note in result))
                    summary = f"检索到 {count} 条检验知识资料" + (f"（{titles}）。" if titles else "。")
                else:
                    count = len(result)
                    titles = "、".join(check["title"] for check in result)
                    summary = f"完成 {count} 项联算。" if not titles else f"完成 {count} 项联算（{titles}）。"
                executions.append(ReadingToolExecution(name=name, status="completed" if count else "empty", result=summary))
            messages.append(tool_reply)
        response, call_record = await invoke_model(with_tools, messages, "after_tools")
    if response is not None and response.tool_calls:
        record.fallback_reason = "tool_budget_exhausted"
        raise ValueError("Report tool budget exhausted")
    allowed = answer.model_copy(update={"citations": list(retrieved.values())})
    available_notes = list(knowledge.values())

    def validate_reply(content: object, call: ReadingModelCall) -> list[ModelObservation]:
        if not isinstance(content, str):
            call.rejection = "invalid_format"
            return []
        try:
            result = _parse_model_reading(content)
        except ValidationError:
            call.rejection = "invalid_format"
            return []
        items = [item for item in result.observations if _valid_observation(item, allowed, available_notes)
                 and _valid_medication_context(item, medication, keys_by_row)]
        if not items:
            call.rejection = "unverified_output"
        return items

    def missing_coverage(items: list[ModelObservation], call: ReadingModelCall | None) -> tuple[list[list[int]], bool]:
        covered = {row for item in items for row in item.report_rows}
        groups = [group for group in required_groups if not covered.intersection(group)]
        missing_medication = needs_medication and not any(item.context_ref == "liver_medication" for item in items)
        if call is not None:
            call.missing_groups = groups
            if call.rejection is None:
                if groups:
                    call.rejection = "missing_groups"
                elif missing_medication:
                    call.rejection = "missing_medication_evidence"
        return groups, missing_medication

    validated = validate_reply(response.content, call_record) if response is not None else []
    missing_groups, missing_medication = missing_coverage(validated, call_record if response is not None else None)
    if not validated or missing_groups or missing_medication:
        evidence["confirmed_rows"] = report_evidence()
        evidence["knowledge"] = [{"id": note.id, "content": note.text} for note in available_notes]
        evidence["verified_checks"] = [{"rows": check.rows, "conclusion": check.conclusion} for check in completed_checks]
        if validated:
            evidence["required_abnormal_groups"] = missing_groups
            evidence["missing_medication_observation"] = missing_medication
            phase = "coverage_repair"
            feedback = SystemMessage(content="已通过校验的段落由程序保留。只补写 required_abnormal_groups 列出的缺项，"
                                     "每组引用至少一条指定行号，从该行的 allowed_source_ids 选对应的知识 ID。"
                                     "仅在 missing_medication_observation 为 true 时补用药段。返回同结构的 JSON。")
        else:
            phase = "timeout_retry" if response is None else "rewrite"
            feedback = SystemMessage(content="仅使用已提供的证据直接生成可核验的 JSON。核对行号和 allowed_source_ids，覆盖必答组和用药段。"
                                     "正文仅解释确认的结果和自述，省略一切数字和猜测。")
        final_reply, call_record = await invoke_model(
            llm, [messages[0], feedback, HumanMessage(content=json.dumps(evidence, ensure_ascii=False))],
            phase, response_format={"type": "json_object"},
        )
        repaired = validate_reply(final_reply.content, call_record)
        if validated:
            target_rows = {row for group in missing_groups for row in group}
            repaired = [item for item in repaired if (
                (item.context_ref == "liver_medication" and missing_medication)
                or target_rows.intersection(item.report_rows)
            )]
        validated.extend(repaired)
        missing_groups, missing_medication = missing_coverage(validated, call_record)

    if not validated or missing_groups or missing_medication:
        record.fallback_reason = "output_rejected"
        if len(completed_checks) > len(answer.checks):
            checked_rows = {row for check in completed_checks for row in check.rows}
            existing = {item.row for item in answer.citations}
            additional = [item for row, item in retrieved.items() if row in checked_rows and row not in existing]
            return answer.model_copy(update={"mode": "fallback", "checks": completed_checks,
                                             "citations": [*answer.citations, *additional]})
        raise ValueError("Incomplete or unverified report explanation")
    observations = [ReadingObservation(text=item.explanation, rows=list(dict.fromkeys(item.report_rows)),
                                       sources=_sources([note for note in available_notes if note.id in item.source_ids]))
                    for item in validated]
    used = dict.fromkeys([row for item in observations for row in item.rows] +
                         [row for check in completed_checks for row in check.rows])
    existing = {item.row for item in answer.citations}
    citations = [*answer.citations, *(retrieved[row] for row in used if row not in existing)]
    update = {"mode": "model", "model_is_primary": True, "observations": observations,
              "citations": citations, "checks": completed_checks}
    if answer.scope == "unresolved":
        focused = list(dict.fromkeys(row for item in observations for row in item.rows))[:8]
        index = focused[0] if len(focused) == 1 else None
        update.update(text="", scope="indicator", topic=ReadingTopic(scope="indicator", rows=focused),
                      row=index, page=request.findings[index].page if index is not None else None)
    return answer.model_copy(update=update)


class ReadingState(TypedDict, total=False):
    request: ReadingPrompt
    plan: ReadingPlan
    notes: list[Note]
    medication: LiverMedicationEvidence | None
    answer: ReadingAnswer
    streaming: bool


def _route(state: ReadingState) -> dict:
    return {"plan": _plan(state["request"])}


def _gather(state: ReadingState) -> dict:
    request, plan = state["request"], state["plan"]
    if not request.patient_context.severe_bleeding and (purpose := _explain_evidence_purpose(request)):
        return {"plan": plan, "notes": [], "medication": None, "answer": purpose}
    checks = []
    if plan.interpret and not request.patient_context.severe_bleeding:
        keys_by_row = [_finding_keys(row.name, row.unit) for row in request.findings]
        checks = check_report_evidence(request.findings, keys_by_row, plan.primary, plan.related)
        added_rows = (row for item in checks if item.status == "confirmed" for row in item.rows if row not in plan.primary)
        plan = replace(plan, related=tuple(dict.fromkeys((*plan.related, *added_rows))))
    medication = _liver_medication_evidence(request, plan)
    notes = retrieve_notes(_note_keys(request, plan, medication))
    return {"plan": plan, "notes": notes, "medication": medication,
            "answer": _evidence_answer(request, plan, notes, medication, checks)}


def _model_skip_reason(request: ReadingPrompt, answer: ReadingAnswer) -> ModelSkipReason | None:
    if answer.guidance and answer.guidance.care_level == "emergency":
        return "emergency"
    if answer.direct_answer:
        return "evidence_purpose"
    if answer.row is not None and request.findings[answer.row].status == "REVIEW":
        return "pending_confirmation"
    if answer.citations or (request.question.strip() and any(row.status != "REVIEW" for row in request.findings)):
        return None
    return "no_evidence"


def _after_evidence(state: ReadingState) -> str:
    return END if _model_skip_reason(state["request"], state["answer"]) else "agent"


async def _generate(state: ReadingState) -> dict:
    request, answer = state["request"], state["answer"]
    record = ReadingWorkRecord()
    streaming = state.get("streaming", False)
    if streaming:
        emit_custom({"type": "node_start", "node": "report_reading", "content": "正在解读报告并核对引用…"})
    try:
        async with asyncio.timeout(MODEL_TIMEOUT_SECONDS):
            keys = _note_keys(request, state["plan"], state["medication"])
            try:
                notes = await asyncio.wait_for(asyncio.to_thread(retrieve_notes, keys, use_vectors=True), timeout=3)
            except Exception as error:
                _LOG.info("Report reading vector retrieval unavailable (%s)", type(error).__name__)
                notes = state["notes"]
            answer = await _model_answer(request, answer, notes, state["medication"], record, streaming=streaming)
    except Exception as error:
        if record.fallback_reason is None:
            if isinstance(error, TimeoutError):
                record.fallback_reason = "agent_timeout"
            elif isinstance(error, LLMConfigurationError):
                record.fallback_reason = "configuration_error"
            else:
                record.fallback_reason = "internal_error"
        answer = answer.model_copy(update={"mode": "fallback"})
    if record.fallback_reason:
        _LOG.info("Report reading fallback request_id=%s reason=%s model_calls=%d", record.request_id,
                  record.fallback_reason, record.model_call_count)
    if streaming:
        emit_custom({"type": "node_end", "node": "report_reading"})
    return {"answer": answer.model_copy(update={"work_record": record})}


_workflow = StateGraph(ReadingState)
_workflow.add_node("route", _route)
_workflow.add_node("evidence", _gather)
_workflow.add_node("agent", _generate)
_workflow.add_edge(START, "route")
_workflow.add_edge("route", "evidence")
_workflow.add_conditional_edges("evidence", _after_evidence, {"agent": "agent", END: END})
_workflow.add_edge("agent", END)
reading_graph = _workflow.compile()


async def reason_reading(request: ReadingPrompt) -> ReadingAnswer:
    request = _relevant_context(request)
    answer = (await reading_graph.ainvoke({"request": request}))["answer"]
    return _with_work_record(request, answer)


def _with_work_record(request: ReadingPrompt, answer: ReadingAnswer) -> ReadingAnswer:
    if answer.work_record is None:
        record = ReadingWorkRecord(skip_reason=_model_skip_reason(request, answer))
        return answer.model_copy(update={"work_record": record})
    return answer


async def stream_reading(request: ReadingPrompt) -> AsyncIterator[dict]:
    request = _relevant_context(request)
    answer = None
    async for mode, payload in reading_graph.astream({"request": request, "streaming": True}, stream_mode=["custom", "values"]):
        if mode == "custom":
            yield payload
        else:
            answer = payload.get("answer")
    if answer is None:
        raise ValueError("Report graph did not produce an answer")
    yield {"type": "answer", "payload": _with_work_record(request, answer).model_dump(mode="json")}
    yield {"type": "finish"}


def _relevant_context(request: ReadingPrompt) -> ReadingPrompt:
    form = report_context_form(request.findings)
    replies = request.context_replies.copy()
    for field, reply in replies.items():
        if field == "recent_infection" and (corrected := infection_correction(reply)):
            replies[field] = corrected
    context, replies = form.apply_replies(request.patient_context, replies)
    statement = replies.get(request.reply_field, "") if request.reply_field else request.question
    if reports_current_severe_bleeding(statement):
        context = context.model_copy(update={"severe_bleeding": True})
    return request.model_copy(update={"patient_context": context, "context_replies": replies})
