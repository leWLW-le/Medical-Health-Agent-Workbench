"""Resolve report fields needed to answer a focused question."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from .scan_analysis import ScanFinding

LookupStatus = Literal["confirmed", "missing", "pending_confirmation", "ambiguous"]


@dataclass(frozen=True)
class ReportLookup:
    rows: tuple[int, ...]
    status: LookupStatus


class EvidenceCheck(BaseModel):
    indicator: str
    reason: str
    action: Literal["reuse_report_evidence", "lookup_report"]
    status: LookupStatus
    rows: list[int] = Field(default_factory=list)
    detail: str


@dataclass(frozen=True)
class EvidencePurpose:
    reason: str
    explanation: str
    related: tuple[str, ...]
    source_ids: tuple[str, ...]


EVIDENCE_PURPOSES = {
    "BAS#": EvidencePurpose(
        "结合绝对计数解读嗜碱性粒细胞占比异常",
        "查找 BAS# 是为了看嗜碱性粒细胞的绝对计数是否也超出参考范围。BAS% 表示这类细胞占白细胞的比例，"
        "BAS# 表示单位体积血液中这类细胞的数量。比例偏高时，绝对计数仍可能在参考范围内。"
        "把同次采样的两个结果放在一起，才能区分这两种情况。",
        ("BAS%",), ("basophils",),
    ),
    "LYM%": EvidencePurpose(
        "对照淋巴细胞的绝对计数和占比",
        "查找 LYM% 是为了看淋巴细胞的占比是否也有变化。LYM# 表示绝对计数，LYM% 表示它在白细胞中的占比。"
        "同次采样中，占比在参考范围内，绝对计数仍可能偏低。",
        ("LYM#", "WBC"), ("differential",),
    ),
    "PLT": EvidencePurpose(
        "区分血小板计数和体积相关指标的变化",
        "查找 PLT 是为了看血小板的数量。MPV 反映平均大小，PDW 反映大小差异。"
        "结合同次血小板计数，可以区分数量的变化和体积相关指标的变化。",
        ("MPV", "PDW"), ("platelets", "platelet_size"),
    ),
    "WBC": EvidencePurpose(
        "对照分类比例和白细胞总数",
        "查找 WBC 是为了知道白细胞总数。某类白细胞的占比相同时，总数不同，该类细胞的绝对计数也会不同。"
        "结合同次采样的总数和分类结果，可以解释比例与绝对计数为什么会出现不同变化。",
        (), ("differential",),
    ),
}


def resolve_report_field(rows: list[ScanFinding], keys_by_row: list[tuple[str, ...]], key: str) -> ReportLookup:
    matches = tuple(index for index, keys in enumerate(keys_by_row) if key in keys)
    if not matches:
        return ReportLookup(matches, "missing")
    if len(matches) > 1:
        return ReportLookup(matches, "ambiguous")
    return ReportLookup(matches, "pending_confirmation" if rows[matches[0]].status == "REVIEW" else "confirmed")


def describe_report_lookup(rows: list[ScanFinding], key: str, reason: str, lookup: ReportLookup,
                           action: Literal["reuse_report_evidence", "lookup_report"] = "lookup_report") -> EvidenceCheck:
    if lookup.status == "confirmed":
        detail = f"已确认第 {rows[lookup.rows[0]].page} 页的 {key} 字段。涉及联算时仍需确认是否为同次采样。"
    elif lookup.status == "missing":
        detail = f"已查找本报告的识别结果，未找到 {key}。"
    elif lookup.status == "pending_confirmation":
        detail = f"第 {rows[lookup.rows[0]].page} 页的 {key} 仍待核对，确认后才能用于解读。"
    else:
        detail = f"识别到多条 {key}，现有字段无法确定哪条属于同次采样。请按原页的采样时间或样本编号核对。"
    return EvidenceCheck(indicator=key, reason=reason, action=action, status=lookup.status,
                         rows=list(lookup.rows[:8]), detail=detail)


def check_report_evidence(rows: list[ScanFinding], keys_by_row: list[tuple[str, ...]], primary: tuple[int, ...],
                          related: tuple[int, ...]) -> list[EvidenceCheck]:
    abnormal = {key for index in primary if rows[index].status in {"HIGH", "LOW"} for key in keys_by_row[index]}
    required: list[str] = []
    if "BAS%" in abnormal:
        required.append("BAS#")
    if "LYM#" in abnormal:
        required.append("LYM%")
    if abnormal & {"MPV", "PCT", "PDW"}:
        required.append("PLT")
    selected = set(primary) | set(related)
    checked: list[EvidenceCheck] = []
    for key in required:
        existing = [index for index in selected if key in keys_by_row[index] and rows[index].status != "REVIEW"]
        if len(existing) == 1 and sum(key in keys for keys in keys_by_row) == 1:
            lookup = ReportLookup(tuple(existing), "confirmed")
            action = "reuse_report_evidence"
        else:
            lookup = resolve_report_field(rows, keys_by_row, key)
            action = "lookup_report"
        checked.append(describe_report_lookup(rows, key, EVIDENCE_PURPOSES[key].reason, lookup, action))
    return checked
