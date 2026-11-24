from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from math import isfinite
import re
from statistics import median
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import BaseModel, Field, StringConstraints

from skills.lab_interpreter.skill import compare_report_range

from .evidence import EvidenceContext, EvidenceSource, UnknownEncounter
from .models import AuditInput, AuditResult, CODES_BY_LABEL
from .parser import InvalidReport, parse_report
from .workflow import audit_graph


ScanStatus = Literal["HIGH", "LOW", "NORMAL", "REPORTED", "REVIEW"]
ReviewReason = Literal[
    "low_confidence", "value_missing", "value_format", "unit_missing",
    "shifted_columns", "reference_missing", "reference_format", "unit_format",
]
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")
QUALITATIVE = re.compile(r"阴性|弱阳性|阳性|未见|未检出|检出|正常|无|neg(?:ative)?|pos(?:itive)?|\(?[-+±]\)?", re.I)
NOTE_LABEL = re.compile(r"^(B超|超声(?:检查)?|心电图(?:检查)?|体检结论|总检结论)\s*[:：]\s*(.*)$", re.I)
NOTE_CONTENT = re.compile(r"未见|无明显|异常|正常|窦性|节律|形态|未发现")
NON_RESULT = re.compile(r"姓名|编号|身份证|手机号|联系方式|电话|地址|签名|报告人|审核人|打印|检查所见|医院|第\d+页")
UNIT = re.compile(r"%|(?:[a-zA-Zμµ×*^0-9.]*/[a-zA-Zμµ][a-zA-Z0-9μµ]*)|[a-zA-Z]{1,5}")
RATIO_RANGE = re.compile(rf"({NUMBER.pattern})\s*[-–—~〜至到]\s*({NUMBER.pattern})")
REFERENCE_BOUND = re.compile(rf"(?:<=|>=|≤|≥|<|>)\s*{NUMBER.pattern}")
REFERENCE_UNIT = re.compile(r"%|[fpmnuμ]?L|s|秒|(?:[a-zA-Zμµ]+|[×x*]?10\^?\d+)/[a-zA-Zμµ]+")
REFERENCE_UNIT_OCR = re.compile(r"(?:[a-zA-Zμµ]+[01][a-zA-Zμµ]*|[a-zA-Zμµ]+)/[a-zA-Zμµ]+|[×x*]?10[~^]?\d+/[a-zA-Zμµ]+")

HEADERS = {
    "name": {"项目名称", "检验项目", "项目", "ITEM", "TEST", "ANALYTE"},
    "code": {"代号", "项目代号", "项目代码", "缩写", "CODE"},
    "value": {"结果", "检验结果", "检测结果", "测定值", "RESULT", "VALUE"},
    "reference": {"参考值", "参考范围", "参考区间", "参考值范围", "REFERENCE", "REFERENCERANGE", "REFRANGE", "REF"},
    "unit": {"单位", "UNIT", "UNITS"},
    "flag": {"提示", "标志", "FLAG"},
}


@dataclass(frozen=True)
class TextBlock:
    text: str
    uncertain: bool
    x: float
    y: float
    height: float
    bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class TableLayout:
    columns: dict[str, float]
    left: float | None = None
    right: float | None = None


class ScanFinding(BaseModel):
    page: int
    name: str
    value: str
    unit: str
    reference: str
    status: ScanStatus
    region: tuple[float, float, float, float] | None = None
    review_reasons: list[ReviewReason] = Field(default_factory=list)


class ScanFields(AuditInput):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
    unit: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
    reference: Annotated[str, StringConstraints(strip_whitespace=True, max_length=240)]


class ScanComparison(BaseModel):
    status: ScanStatus


class ScanReadingComparison(ScanComparison):
    review_reasons: list[ReviewReason]


class ReportNote(BaseModel):
    page: int
    name: str
    text: str


class ScanAnalysis(BaseModel):
    findings: list[ScanFinding]
    notes: list[ReportNote]
    source_audit: AuditResult | None = None


Reread = Callable[[str, str, tuple[float, float, float, float]], str | None]


def group_rows(blocks: list[TextBlock]) -> list[list[TextBlock]]:
    if not blocks:
        return []
    tolerance = max(3.0, median(block.height for block in blocks))
    rows: list[list[TextBlock]] = []
    for block in sorted(blocks, key=lambda cell: cell.y):
        if not rows or block.y - rows[-1][0].y > tolerance:
            rows.append([])
        rows[-1].append(block)
    return [sorted(row, key=lambda cell: cell.x) for row in rows]


def _table_layouts(row: list[TextBlock]) -> list[TableLayout]:
    groups: list[dict[str, float]] = [{}]
    for cell in row:
        label = _compact(cell.text).upper()
        kind = next((name for name, aliases in HEADERS.items() if label in aliases), None)
        if kind is None or cell.uncertain:
            continue
        if kind in groups[-1] or (kind == "code" and {"name", "value"} <= groups[-1].keys()):
            groups.append({})
        groups[-1][kind] = cell.x
    if not all(("name" in group or "code" in group) and "value" in group and
               {"reference", "unit"}.intersection(group) for group in groups):
        return []
    dividers = [(max(left.values()) + min(right.values())) / 2 for left, right in zip(groups, groups[1:])]
    layouts = []
    for group, left, right in zip(groups, [None, *dividers], [*dividers, None], strict=True):
        if "name" not in group:
            group["name"] = group.pop("code")
        layouts.append(TableLayout(group, left, right))
    return layouts


def _section_row(row: list[TextBlock], layout: TableLayout) -> list[TextBlock]:
    return [cell for cell in row if (layout.left is None or cell.x > layout.left)
            and (layout.right is None or cell.x <= layout.right)]


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize("NFKC", text))


def _row_cells(row: list[TextBlock], columns: dict[str, float]) -> dict[str, list[TextBlock]]:
    # Header positions establish column boundaries. Cells outside the lab table can
    # be excluded even where the PDF contains a second table beside it.
    ordered = sorted(columns.items(), key=lambda entry: entry[1])
    boundaries = [(left[1] + right[1]) / 2 for left, right in zip(ordered, ordered[1:])]
    # Unit and flag columns may be absent from the header. Do not let the last
    # required column capture something from the far right of the page.
    last_edge = ordered[-1][1] + (ordered[-1][1] - ordered[-2][1]) / 2
    cells: dict[str, list[TextBlock]] = {}
    for block in row:
        # Inline units can make the final reference cell wider than its header.
        if block.x <= last_edge or (ordered[-1][0] == "reference" and "unit" not in columns):
            column = ordered[sum(block.x > edge for edge in boundaries)][0]
            cells.setdefault(column, []).append(block)
    return cells


def _combined(cells: list[TextBlock]) -> str:
    return " ".join(cell.text for cell in cells).strip()


def _numeric_text(text: str) -> str:
    return re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalize("NFKC", text).strip().replace("−", "-"))


def _embedded_reference_unit(reference: str) -> tuple[str, str] | None:
    text = _numeric_text(reference)
    match = RATIO_RANGE.match(text) or REFERENCE_BOUND.match(text)
    if match is None:
        return None
    suffix = text[match.end():].strip()
    compact = _compact(suffix)
    if (REFERENCE_UNIT.fullmatch(compact) or REFERENCE_UNIT_OCR.fullmatch(compact)) and compare_report_range(0, match[0], "") is not None:
        return match[0], suffix
    return None


def _allows_blank_unit(name: str, value: str, reference: str) -> bool:
    label = _compact(name).upper()
    if re.fullmatch(r"尿?比重(?:SG)?|SG|PH(?:NH-?PH)?|NH-?PH|尿?酸碱度(?:PH)?", label):
        return True
    if not re.fullmatch(r"红细胞(?:压积|比容)(?:HCT)?|HCT|血小板压积(?:PCT1?|PET1)?", label):
        return False
    # These reports can print a decimal ratio without a unit. Compare only within
    # that printed scale and never fill in a unit or infer a conversion.
    interval = RATIO_RANGE.fullmatch(_numeric_text(reference))
    return bool(interval and all(0 <= float(piece) <= 1 for piece in (value, *interval.groups())))


def compare_scan_fields(fields: ScanFields) -> ScanComparison:
    return ScanComparison(status=_compare_fields(fields.value, fields.unit, fields.reference))


def reading_comparison(value: str, unit: str, reference: str) -> ScanReadingComparison:
    status = _compare_fields(value, unit, reference)
    reasons: list[ReviewReason] = []
    if status == "REVIEW":
        if not value.strip():
            reasons.append("value_missing")
        elif not NUMBER.fullmatch(_numeric_text(value)):
            reasons.append("value_format")
        if NUMBER.fullmatch(_numeric_text(unit)) or compare_report_range(0, unit, "") is not None:
            reasons.append("shifted_columns")
        elif not reference.strip():
            reasons.append("reference_missing")
        elif compare_report_range(0, _numeric_text(reference), unit) is None or re.match(r"^[-−]\s+\d", reference):
            reasons.append("reference_format")
    return ScanReadingComparison(status=status, review_reasons=reasons)


def _compare_fields(value: str, unit: str, reference: str) -> ScanStatus:
    value, reference = _numeric_text(value), _numeric_text(reference)
    if QUALITATIVE.fullmatch(value):
        return "REPORTED"
    # A range or bare number in the unit cell suggests shifted columns.
    if NUMBER.fullmatch(_numeric_text(unit)) or compare_report_range(0, unit, "") is not None:
        return "REVIEW"
    if NUMBER.fullmatch(value) and not re.match(r"^[-−]\s+\d", reference):
        number = float(value)
        if isfinite(number):
            return compare_report_range(number, reference, unit) or "REVIEW"
    return "REVIEW"


def _interpret_row(row: list[TextBlock], columns: dict[str, float], page: int) -> ScanFinding | None:
    cells = _row_cells(row, columns)
    names = cells.get("name", [])
    values = cells.get("value", [])
    units = cells.get("unit", [])
    references = cells.get("reference", [])
    name = _combined(names)
    value, unit, reference = map(_combined, (values, units, references))
    if (not name or len(name) > 100 or NON_RESULT.search(name) or name.endswith((":", "："))
            or not re.search(r"[A-Za-z\u3400-\u9fff]", name)):
        return None
    code = _combined(cells.get("code", []))
    if code and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/%#().+\-]{0,19}", code):
        name = f"{name} {code}"
    embedded = _embedded_reference_unit(reference) if not unit else None
    unit_uncertain = False
    if embedded:
        reference, unit = embedded
        unit_uncertain = not REFERENCE_UNIT.fullmatch(_compact(unit))
    normalized_value = _numeric_text(value)
    numeric = NUMBER.fullmatch(normalized_value)
    qualitative = QUALITATIVE.fullmatch(normalized_value)
    reference_text = _numeric_text(reference)
    # Insist on plausible values in the expected columns. Stamps, section
    # captions, and adjacent ECG columns sometimes sit under the same x range.
    comparable_ref = compare_report_range(0, reference_text, unit) is not None
    qualitative_ref = QUALITATIVE.fullmatch(reference_text)
    known = any(_compact(label).casefold() in CODES_BY_LABEL for label in (_combined(names), code))
    misplaced_reference = compare_report_range(0, unit, "") is not None
    if not (comparable_ref or ((numeric or qualitative) and (UNIT.fullmatch(_compact(unit)) or qualitative_ref))
            or (numeric and misplaced_reference) or (known and (value or unit or reference))):
        return None
    if _compact(name).upper() in HEADERS["name"]:
        return None

    source = cells.get("code", []) + names + values + units + references
    result = reading_comparison(value, unit, reference)
    if any(cell.uncertain for cell in source):
        result.review_reasons.append("low_confidence")
    if unit_uncertain:
        result.review_reasons.append("unit_format")
    if numeric and "unit" in columns and not unit and not _allows_blank_unit(name, normalized_value, reference):
        result.review_reasons.append("unit_missing")
    status = "REVIEW" if result.review_reasons else result.status
    boxes = [cell.bounds for cell in source if cell.bounds is not None]
    region = None
    if boxes:
        left, top, right, bottom = zip(*boxes)
        region = (min(left), min(top), max(right), max(bottom))
    return ScanFinding(page=page, name=name, value=value, unit=unit, reference=reference,
                       status=status, region=region, review_reasons=result.review_reasons)


def reread_candidate_key(field: str, text: str, unit: str) -> object | None:
    """Compare semantics across OCR passes while preserving genuine punctuation."""
    if field == "unit":
        compact = _compact(text)
        return compact if UNIT.fullmatch(compact) else None
    normalized = _numeric_text(text)
    if field == "value":
        return Decimal(normalized) if NUMBER.fullmatch(normalized) else None
    if re.match(r"^-\s+\d", normalized) or compare_report_range(0, normalized, unit) is None:
        return None
    interval = RATIO_RANGE.fullmatch(normalized)
    return tuple(Decimal(value) for value in interval.groups()) if interval else normalized


def _reread_row(row: list[TextBlock], columns: dict[str, float], finding: ScanFinding, reread: Reread) -> list[TextBlock]:
    reasons = set(finding.review_reasons)
    if "shifted_columns" in reasons:
        return row
    cells = _row_cells(row, columns)
    replacements: dict[TextBlock, TextBlock] = {}
    for field, invalid in (("unit", False), ("value", "value_format" in reasons),
                           ("reference", "reference_format" in reasons)):
        candidates = cells.get(field, [])
        if len(candidates) != 1:
            # A blank or split field has no single localized crop to verify.
            continue
        cell = candidates[0]
        if cell.bounds is None or not (cell.uncertain or invalid):
            continue
        unit = _combined([replacements.get(original, original) for original in cells.get("unit", [])])
        confirmed = reread(field, unit, cell.bounds)
        if confirmed is not None:
            replacements[cell] = replace(cell, text=confirmed, uncertain=False)
    return [replacements.get(cell, cell) for cell in row]


def read_page_rows(
    rows: list[list[TextBlock]], height: float, inherited_columns: list[TableLayout],
    reread: Reread | None = None,
) -> tuple[list[list[TextBlock]], list[TableLayout], int]:
    """Keep column positions for a continuation page and reread localized uncertain cells."""
    layouts = inherited_columns
    output = []
    count = 0
    last_result_y: dict[int, float] = {}
    for row in rows:
        header = _table_layouts(row)
        if header:
            layouts = header
            last_result_y.clear()
        elif layouts:
            replacements: dict[TextBlock, TextBlock] = {}
            for index, layout in enumerate(layouts):
                section = _section_row(row, layout)
                finding = _interpret_row(section, layout.columns, 1)
                if finding:
                    count += 1
                    last_result_y[index] = row[0].y
                    if reread is not None and finding.status == "REVIEW":
                        updated = _reread_row(section, layout.columns, finding, reread)
                        replacements.update(zip(section, updated, strict=True))
            row = [replacements.get(cell, cell) for cell in row]
        output.append(row)
    return output, [layout for index, layout in enumerate(layouts)
                    if last_result_y.get(index, 0.0) >= height * 0.85], count


def _report_notes(rows: list[list[TextBlock]], page: int) -> list[ReportNote]:
    notes = []
    for row_number, row in enumerate(rows):
        for position, block in enumerate(row):
            match = NOTE_LABEL.fullmatch(block.text)
            if not match or block.uncertain:
                continue
            name, content = match.groups()
            if not content:
                near = row[position + 1 : position + 2]
                if not near and row_number + 1 < len(rows):
                    next_block = rows[row_number + 1][0]
                    if next_block.y - block.y <= 4 * block.height:
                        near = [next_block]
                if near and not near[0].uncertain:
                    content = near[0].text
            if content and NOTE_CONTENT.search(content) and not NON_RESULT.search(content):
                notes.append(ReportNote(page=page, name=name, text=content[:300]))
    return notes


def analyze_scan(
    pages: list[tuple[float, list[list[TextBlock]]]], *, source: EvidenceSource | None = None,
    include_source_audit: bool = True,
) -> ScanAnalysis:
    findings: list[ScanFinding] = []
    notes: list[ReportNote] = []
    layouts: list[TableLayout] = []
    scan_lines: list[str] = []
    uncertain = False
    for page, (height, rows) in enumerate(pages, start=1):
        notes.extend(_report_notes(rows, page))
        last_result_y: dict[int, float] = {}
        table_findings: dict[int, list[ScanFinding]] = {}
        for row in rows:
            scan_lines.append(_combined(row))
            uncertain = uncertain or any(cell.uncertain for cell in row)
            header = _table_layouts(row)
            if header:
                for index in range(len(layouts)):
                    findings.extend(table_findings.get(index, []))
                table_findings.clear()
                last_result_y.clear()
                layouts = header
                continue
            for index, layout in enumerate(layouts):
                finding = _interpret_row(_section_row(row, layout), layout.columns, page)
                if finding:
                    last_result_y[index] = row[0].y
                    table_findings.setdefault(index, []).append(finding)
        for index in range(len(layouts)):
            findings.extend(table_findings.get(index, []))
        layouts = [layout for index, layout in enumerate(layouts) if last_result_y.get(index, 0.0) >= height * 0.85]

    # A full, high-confidence structured report can use the regular audit
    # workflow, provided the exact patient/encounter is present in the evidence set.
    source_audit = None
    if include_source_audit and not uncertain:
        try:
            draft = parse_report("\n".join(scan_lines))
            context = EvidenceContext(source) if source is not None else None
            source_audit = audit_graph.invoke({"draft": draft}, context=context)["result"]
        except (InvalidReport, UnknownEncounter):
            pass
    return ScanAnalysis(findings=findings, notes=notes, source_audit=source_audit)
