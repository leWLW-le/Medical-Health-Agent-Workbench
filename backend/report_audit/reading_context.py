"""Choose report-specific questions from confirmed indicator rows."""

from dataclasses import dataclass
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .models import AuditInput
from .reading_knowledge import finding_keys
from .scan_analysis import ScanFinding

LIVER_INDICATORS = frozenset({"ALT", "AST", "LIVER"})
MEDICATION_DETAIL_LIMIT = 100
MAX_REPORT_FOLLOW_UPS = 3
FINISH_REPORT_INTERVIEW = "先给结论"


class ReadingTaskRequest(AuditInput):
    id: UUID
    checkpoint: UUID | None = None
    findings_revision: int = Field(ge=1)
    answered: int = Field(default=0, ge=0, le=MAX_REPORT_FOLLOW_UPS)


class ReadingTaskProgress(BaseModel):
    id: UUID
    checkpoint: UUID | None = None
    findings_revision: int
    answered: int
    status: Literal["awaiting_reply", "complete", "limited", "urgent"]
    stop_reason: Literal["evidence_ready", "awaiting_patient", "user_finished", "question_limit",
                         "evidence_missing", "provider_fallback", "urgent"]
    answered_field: str | None = None
    resumed: bool = False
    gaps: list[str] = Field(default_factory=list)


class PatientContext(AuditInput):
    age_group: Literal["under_18", "adult", "older_adult"] | None = None
    recent_infection: bool = False
    immune_medicine: bool = False
    previous_low_lymphocytes: bool = False
    ongoing_fever: bool = False
    unusual_bleeding: bool = False
    severe_bleeding: bool = False
    fasting: Literal["yes", "no", "unsure"] | None = None
    liver_medicine: bool = False
    liver_medicine_name: str | None = Field(default=None, max_length=MEDICATION_DETAIL_LIMIT)
    liver_medicine_since: str | None = Field(default=None, max_length=MEDICATION_DETAIL_LIMIT)
    kidney_medicine: bool = False
    urinary_symptoms: bool = False
    menstrual_collection: bool = False

    @field_validator("liver_medicine_name", "liver_medicine_since", mode="before")
    @classmethod
    def strip_medication_detail(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value


class ContextQuestion(BaseModel):
    key: str
    label: str
    kind: Literal["checkbox", "select", "text"] = "checkbox"
    options: dict[str, str] = Field(default_factory=dict)
    requires: str | None = None
    max_length: int | None = None


class ContextFollowUp(BaseModel):
    field: str
    options: tuple[str, ...] = ()
    max_length: int = 400


class ContextGroup(BaseModel):
    key: str
    title: str
    questions: list[ContextQuestion]


class ReportContextForm(BaseModel):
    groups: list[ContextGroup]

    def allowed_fields(self, answers: PatientContext) -> set[str]:
        return {question.key for group in self.groups for question in group.questions
                if question.requires is None or getattr(answers, question.requires) is True}

    def filter_answers(self, answers: PatientContext) -> PatientContext:
        # A patient-reported emergency remains actionable in the API.
        return PatientContext.model_validate(answers.model_dump(include=self.allowed_fields(answers) | {"severe_bleeding"}))

    def apply_replies(self, answers: PatientContext, replies: dict[str, str]) -> tuple[PatientContext, dict[str, str]]:
        values = answers.model_dump()
        for group in self.groups:
            for question in group.questions:
                text = replies.get(question.key, "").strip()
                if not text or (question.requires and values[question.requires] is not True):
                    continue
                value = _reply_value(question, text)
                if value is not None:
                    values[question.key] = value
        context = self.filter_answers(PatientContext.model_validate(values))
        return context, {key: replies[key] for key in self.allowed_fields(context) if replies.get(key, "").strip()}


_UNANSWERED = {"不确定", "不知道", "记不清", "不方便说", "没有旧报告", "找不到旧报告", "跳过", "暂时跳过", FINISH_REPORT_INTERVIEW}
_RECENT_INFECTION = re.compile(r"(?:我)?(?:最近|上周|前几天|抽血前|抽血后)(?:有点)?(?:感冒|发烧|发热)(?:了|过)?")
_NO_RECENT_INFECTION = re.compile(r"(?:我)?(?:最近|上周|前几天|抽血前|抽血后)(?:没|没有)(?:感冒|发烧|发热|感染)(?:过)?")
_PREVIOUS_LOW = re.compile(r"(?:我)?(?:以前|之前|上次)(?:也)?(?:有过)?(?:淋巴细胞(?:数量|计数)?)?(?:也)?偏低(?:过)?")
_CORRECTION = re.compile(r"^(?:纠正[：:]|(?:刚才|前面)(?:我)?说错了[，,：:]?)\s*")


def infection_correction(text: str) -> str | None:
    match = _CORRECTION.match(text)
    if not match:
        return None
    statement = text[match.end():].strip()
    short = statement.rstrip("。.!！ ")
    if _RECENT_INFECTION.fullmatch(short) or _NO_RECENT_INFECTION.fullmatch(short):
        return statement
    return None


def _reply_value(question: ContextQuestion, text: str) -> str | bool | None:
    short = text.rstrip("。.!！ ")
    if question.kind == "select":
        return next((key for key, label in question.options.items() if short == label), None)
    if short in _UNANSWERED:
        return None
    if question.kind == "text":
        return text if question.max_length and len(text) <= question.max_length else None
    first_clause = re.split(r"[，,。]", short, maxsplit=1)[0].strip()
    affirmed = {"recent_infection": _RECENT_INFECTION, "previous_low_lymphocytes": _PREVIOUS_LOW}.get(question.key)
    if first_clause in {"有", "是", "有过", "有的"} or (affirmed and affirmed.fullmatch(short)):
        return True
    if first_clause in {"没有", "否", "不是", "无", "没有过"} or (
        question.key == "recent_infection" and _NO_RECENT_INFECTION.fullmatch(short)
    ):
        return False
    return None


def is_context_reply_detail(text: str) -> bool:
    return text.strip().rstrip("。.!！ ") not in _UNANSWERED


def reports_current_severe_bleeding(text: str) -> bool:
    # Match explicit statements of the same conditions used by the structured input.
    return text.strip().rstrip("。.!！ ") in {
        "正在大量出血", "我正在大量出血", "我现在正在大量出血", "现在大量出血", "我现在大量出血",
        "出血按压后仍止不住", "出血按压后还是止不住", "我出血按压后还是止不住", "我按压后还是止不住血",
    }


@dataclass(frozen=True)
class QuestionRule:
    question: ContextQuestion
    indicators: frozenset[str]
    status: str | None = None

    def applies(self, confirmed: dict[str, set[str]]) -> bool:
        return any(key in confirmed and (self.status is None or self.status in confirmed[key])
                   for key in self.indicators)


def _checkbox(key: str, label: str, indicators: str, status: str | None = None) -> QuestionRule:
    return QuestionRule(ContextQuestion(key=key, label=label), frozenset(indicators.split()), status)


_WHITE_CELLS = "WBC NEUT# LYM#"
_PLATELETS = "PLT MPV PCT PDW"
_GROUPS = (
    ("blood_count", "血常规", (
        _checkbox("recent_infection", "最近有过感染，例如感冒", _WHITE_CELLS),
        _checkbox("immune_medicine", "正在用激素或免疫抑制药", _WHITE_CELLS),
        _checkbox("previous_low_lymphocytes", "以前查血时，淋巴细胞计数也偏低", "LYM#", "LOW"),
        _checkbox("ongoing_fever", "最近持续或反复发热", _WHITE_CELLS),
        _checkbox("unusual_bleeding", "反复出现不明原因的瘀青或出血", _PLATELETS),
        _checkbox("severe_bleeding", "正在大量出血，或出血按压后仍止不住", _PLATELETS),
    )),
    ("liver", "肝功能相关", (
        _checkbox("liver_medicine", "最近有新用或调整药物、保健品", "ALT AST LIVER"),
        QuestionRule(ContextQuestion(key="liver_medicine_name", label="药物或保健品名称（选填）", kind="text",
                                     requires="liver_medicine", max_length=MEDICATION_DETAIL_LIMIT), LIVER_INDICATORS),
        QuestionRule(ContextQuestion(key="liver_medicine_since", label="什么时候开始或调整？（例如抽血前一周，选填）",
                                     kind="text", requires="liver_medicine", max_length=MEDICATION_DETAIL_LIMIT),
                     LIVER_INDICATORS),
    )),
    ("kidney", "肾功能相关", (
        _checkbox("kidney_medicine", "近期正在用药，或在服用肌酸等补充剂", "CREA KIDNEY"),
    )),
    ("metabolic", "血糖与血脂相关", (
        QuestionRule(ContextQuestion(key="fasting", label="抽血前是否按要求空腹？", kind="select",
                                     options={"yes": "是", "no": "否", "unsure": "不确定"}), frozenset({"GLU", "TG"})),
    )),
    ("urinalysis", "尿检", (
        _checkbox("urinary_symptoms", "近期排尿时疼痛，或比平时更频繁", "URBC UWBC URINE URINE_BLOOD"),
        _checkbox("menstrual_collection", "采尿时正在月经期（如适用）", "URBC URINE_BLOOD"),
    )),
)


def report_context_form(rows: list[ScanFinding]) -> ReportContextForm:
    general = ContextGroup(key="general", title="基本情况", questions=[
        ContextQuestion(key="age_group", label="年龄范围", kind="select",
                        options={"under_18": "未满 18 岁", "adult": "18 至 64 岁", "older_adult": "65 岁及以上"}),
    ])
    confirmed: dict[str, set[str]] = {}
    for row in rows:
        if row.status == "REVIEW":
            continue
        for key in finding_keys(row.name, row.unit):
            confirmed.setdefault(key, set()).add(row.status)
    groups = [general]
    for key, title, rules in _GROUPS:
        questions = [rule.question for rule in rules if rule.applies(confirmed)]
        if questions:
            groups.append(ContextGroup(key=key, title=title, questions=questions))
    return ReportContextForm(groups=groups)
