from datetime import date
from decimal import Decimal
import re
from typing import Annotated, Literal
from unicodedata import normalize

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

LabCode = Literal["HGB", "WBC", "PLT", "GLU", "ALT", "RBC", "HCT", "MCV", "AST", "CREA"]
LabValue = Annotated[Decimal, Field(max_digits=14, decimal_places=6, allow_inf_nan=False)]
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9-]{1,32}$")]
Unit = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=24, pattern=r"^\S+$")]

LAB_NAMES: dict[LabCode, str] = {
    "HGB": "血红蛋白", "WBC": "白细胞计数", "PLT": "血小板计数",
    "GLU": "葡萄糖", "ALT": "丙氨酸氨基转移酶",
    "RBC": "红细胞计数", "HCT": "红细胞压积", "MCV": "平均红细胞体积",
    "AST": "天门冬氨酸氨基转移酶", "CREA": "血清肌酐",
}
LAB_ALIASES: dict[LabCode, tuple[str, ...]] = {
    "HGB": ("Hb", "血红蛋白"),
    "WBC": ("白细胞", "白细胞计数"),
    "PLT": ("血小板", "血小板计数"),
    "GLU": ("葡萄糖", "血糖"),
    "ALT": ("GPT", "丙氨酸氨基转移酶", "谷丙转氨酶"),
    "RBC": ("红细胞", "红细胞计数"),
    "HCT": ("红细胞压积", "红细胞比容", "血细胞比容"),
    "MCV": ("平均红细胞体积",),
    "AST": ("GOT", "天门冬氨酸氨基转移酶", "谷草转氨酶"),
    "CREA": ("CRE", "Scr", "肌酐", "血清肌酐"),
}
CODES_BY_LABEL = {
    label.casefold(): code for code, aliases in LAB_ALIASES.items() for label in (code, *aliases)
}


class AuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class LabRow(AuditInput):
    code: LabCode
    value: LabValue
    unit: Unit
    collected_on: date

    @field_validator("code", mode="before")
    @classmethod
    def resolve_code(cls, value: object) -> object:
        if isinstance(value, str):
            return CODES_BY_LABEL.get(normalize("NFKC", value).strip().casefold(), value)
        return value

    @field_validator("collected_on", mode="before")
    @classmethod
    def require_iso_date(cls, value: object) -> object:
        if isinstance(value, date) or (isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)):
            return value
        raise ValueError("采样日期格式须为 YYYY-MM-DD")


class Draft(AuditInput):
    patient_id: Identifier
    encounter_id: Identifier
    rows: list[LabRow] = Field(min_length=1, max_length=len(LAB_NAMES))

    @model_validator(mode="after")
    def require_unique_codes(self) -> "Draft":
        if len({row.code for row in self.rows}) != len(self.rows):
            raise ValueError("同一报告中的项目代码必须唯一")
        return self


class Observation(LabRow):
    id: Identifier
    revision: int
    patient_id: Identifier
    encounter_id: Identifier


Issue = Literal["date_mismatch", "unit_mismatch", "value_mismatch", "missing_evidence", "ambiguous_evidence"]


class Finding(BaseModel):
    row: LabRow
    evidence: Observation | None
    issues: list[Issue]


class AuditResult(BaseModel):
    findings: list[Finding]
    checked_rows: int
    reused_rows: int
    rules_version: int = 1


class Snapshot(AuditResult):
    case_id: str
    version: int
    draft: Draft


class Revision(AuditInput):
    expected_version: int = Field(ge=1)
    draft: Draft


class Question(AuditInput):
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=300)
    focus: LabCode | None = None

    @model_validator(mode="after")
    def require_text(self) -> "Question":
        if not self.text.strip():
            raise ValueError("请输入问题")
        return self


class KnowledgeCitation(BaseModel):
    source: str
    section: str
    excerpt: str


class Answer(BaseModel):
    version: int
    text: str
    focus: LabCode | None
    records: list[Observation] = Field(default_factory=list)
    knowledge: list[KnowledgeCitation] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
