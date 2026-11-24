"""
Skill: lab_interpreter

Interprets common lab test values against reference ranges and produces
a structured abnormality report with clinical significance notes.

Supports:
  血常规 (CBC):  WBC, RBC, HGB, PLT, HCT, MCV
  血生化:        GLU, HbA1c, CREA, BUN, ALT, AST, TC, TG, HDL, LDL, UA
  甲功:          TSH, FT3, FT4
  凝血:          PT, APTT, INR

Used by: report_agent
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field, FiniteFloat

from skills.base import BaseSkill, SkillOutput


# ─── Reference Ranges ─────────────────────────────────────────────────────────
# Format: (low, high, unit, full_name_zh, clinical_note)
_REFERENCE_RANGES: dict[str, tuple] = {
    # 血常规
    "WBC":   (4.0,   10.0,  "×10⁹/L", "白细胞计数",   "升高提示感染/炎症；降低见于病毒感染、骨髓抑制"),
    "RBC":   (3.5,   5.5,   "×10¹²/L","红细胞计数",   "降低提示贫血；升高见于红细胞增多症或脱水"),
    "HGB":   (110,   160,   "g/L",     "血红蛋白",     "低于正常为贫血；需结合MCV判断贫血类型"),
    "PLT":   (100,   300,   "×10⁹/L", "血小板计数",   "降低见于特发性血小板减少性紫癜、DIC；升高见于感染、脾切除后"),
    "HCT":   (0.37,  0.50,  "L/L",     "红细胞压积",   "与HGB平行，辅助判断贫血和血液浓缩"),
    "MCV":   (80,    100,   "fL",      "平均红细胞体积","小细胞性贫血MCV<80，大细胞性贫血MCV>100"),
    # 血糖
    "GLU":   (3.9,   6.1,   "mmol/L",  "空腹血糖",     "≥7.0为糖尿病诊断标准；6.1-7.0为糖尿病前期"),
    "HBA1C": (4.0,   6.0,   "%",       "糖化血红蛋白",  "≥6.5%提示糖尿病；7.0%为糖尿病控制目标"),
    # 肾功能
    "CREA":  (44,    133,   "μmol/L",  "血肌酐",       "升高提示肾功能下降；女性正常上限约106μmol/L"),
    "BUN":   (2.9,   8.2,   "mmol/L",  "尿素氮",       "升高见于肾功能不全、脱水、高蛋白饮食"),
    "UA":    (149,   417,   "μmol/L",  "尿酸",         "男性>420提示高尿酸；女性>360；痛风风险增加"),
    # 肝功能
    "ALT":   (0,     40,    "U/L",     "丙氨酸氨基转移酶","升高提示肝细胞损伤；>3倍正常上限需重视"),
    "AST":   (0,     40,    "U/L",     "天冬氨酸氨基转移酶","AST/ALT比值辅助判断肝病类型"),
    # 血脂
    "TC":    (0,     5.2,   "mmol/L",  "总胆固醇",     "≥6.2为高胆固醇血症；心血管风险随之升高"),
    "TG":    (0,     1.7,   "mmol/L",  "甘油三酯",     "≥5.65严重升高，胰腺炎风险"),
    "HDL":   (1.0,   99,    "mmol/L",  "高密度脂蛋白",  "低HDL（<1.0男/<1.3女）是冠心病独立危险因素"),
    "LDL":   (0,     3.4,   "mmol/L",  "低密度脂蛋白",  "心血管高危患者目标<1.8；一般人群<3.4"),
    # 甲状腺
    "TSH":   (0.27,  4.2,   "mIU/L",  "促甲状腺激素",  "升高提示甲减；降低提示甲亢"),
    "FT3":   (3.1,   6.8,   "pmol/L",  "游离三碘甲状腺原氨酸","甲亢时升高，甲减时降低"),
    "FT4":   (12,    22,    "pmol/L",  "游离甲状腺素",  "甲亢时升高，甲减时降低"),
    # 凝血
    "PT":    (11,    13,    "s",       "凝血酶原时间",   "延长见于肝病、维生素K缺乏、口服抗凝药"),
    "APTT":  (28,    40,    "s",       "活化部分凝血活酶时间","延长见于血友病、肝素治疗"),
    "INR":   (0.8,   1.2,   "",        "国际标准化比值",  "口服华法林患者目标INR 2.0-3.0"),
}

_LAB_ALIASES = {
    **{ref[3]: code for code, ref in _REFERENCE_RANGES.items()},
    "白细胞": "WBC",
    "红细胞": "RBC",
    "血小板": "PLT",
    "血细胞比容": "HCT",
    "HB": "HGB",
    "谷丙转氨酶": "ALT",
    "谷草转氨酶": "AST",
    "肌酐": "CREA",
    "血清肌酐": "CREA",
    "SCR": "CREA",
    "血尿酸": "UA",
    "LDLC": "LDL",
    "HDLC": "HDL",
}

_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_INTERVAL = re.compile(rf"({_NUMBER})\s*[-–—~〜至到]\s*({_NUMBER})")
_ONE_SIDED = re.compile(rf"(<=|>=|≤|≥|<|>)\s*({_NUMBER})")
Comparison = Literal["HIGH", "LOW", "NORMAL"]


def _normalize_unit(unit: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", unit)).replace("μ", "u").replace("l", "L").replace("^", "")


def compare_report_range(value: float, reference: str, unit: str) -> Comparison | None:
    text = unicodedata.normalize("NFKC", reference).strip().replace("−", "-")
    interval = _INTERVAL.match(text)
    bound = _ONE_SIDED.match(text) if not interval else None
    match = interval or bound
    if match is None:
        return None

    suffix = text[match.end():].strip()
    if suffix and (not unit or _normalize_unit(suffix) != _normalize_unit(unit)):
        return None

    if interval:
        low, high = map(float, interval.groups())
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            return None
        return "LOW" if value < low else "HIGH" if value > high else "NORMAL"

    operator, limit_text = match.groups()
    limit = float(limit_text)
    if not math.isfinite(limit):
        return None
    if operator in ("<", "<=", "≤"):
        return "HIGH" if value > limit or (operator == "<" and value == limit) else "NORMAL"
    return "LOW" if value < limit or (operator == ">" and value == limit) else "NORMAL"


# ─── I/O Schemas ──────────────────────────────────────────────────────────────

class LabItem(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="检验项目名称或缩写，如 WBC、HGB、GLU")
    value: FiniteFloat = Field(description="检验结果数值")
    unit: str | None = Field(default=None, description="单位（可选，用于验证）")
    reference_range: str | None = Field(
        default=None,
        description="原报告同一行明确给出的数值参考范围，如 5–15 或 <20。未提供或看不清时填 null，严禁从知识库推断或代填。",
    )


class LabInterpreterInput(BaseModel):
    items: list[LabItem] = Field(description="需要解读的检验项目列表")
    patient_age: int | None = Field(default=None, description="患者年龄（影响部分参考范围）")
    patient_gender: Literal["male", "female"] | None = Field(default=None, description="患者性别")
    require_report_reference: bool = Field(default=False, description="只按原报告或用户明确提供的参考范围比对")


class LabFinding(BaseModel):
    name: str
    full_name: str
    value: float
    unit: str
    status: Literal["HIGH", "LOW", "NORMAL", "UNKNOWN"]
    reference: str
    reference_source: Literal["report", "builtin"] | None = None
    clinical_note: str


class LabInterpreterOutput(SkillOutput):
    total_items: int
    abnormal_count: int
    unknown_count: int
    findings: list[LabFinding]
    summary: str


# ─── Skill Implementation ─────────────────────────────────────────────────────

class LabInterpreterSkill(BaseSkill):
    name = "lab_interpreter"
    input_schema = LabInterpreterInput
    description = (
        "解读用户提供的化验单数值，逐项对比参考范围，标注偏高/偏低/正常，"
        "并给出每项指标的临床意义说明。支持血常规、血生化、甲功、凝血等常见项目。"
    )
    tags = ["report", "clinic"]

    def run(self, **kwargs) -> LabInterpreterOutput:  # type: ignore[override]
        data = LabInterpreterInput(**kwargs)
        findings: list[LabFinding] = []

        for item in data.items:
            key = item.name.strip().upper().replace("-", "").replace("_", "")
            key = _LAB_ALIASES.get(key, key)
            ref = _REFERENCE_RANGES.get(key)
            unit = (item.unit or "").strip()
            full_name = ref[3] if ref else item.name
            reference = (item.reference_range or "").strip()
            if reference:
                status = compare_report_range(item.value, reference, unit)
                findings.append(LabFinding(
                    name=item.name, full_name=full_name, value=item.value, unit=unit,
                    status=status or "UNKNOWN", reference=reference, reference_source="report",
                    clinical_note=("" if status else "参考范围格式不明确或单位不一致，请核对原报告。"),
                ))
                continue

            if data.require_report_reference or ref is None or (unit and _normalize_unit(unit) != _normalize_unit(ref[2])):
                findings.append(LabFinding(
                    name=item.name, full_name=full_name, value=item.value, unit=unit,
                    status="UNKNOWN", reference="",
                    clinical_note="请核对原报告的参考范围和单位。",
                ))
                continue

            low, high, unit, full_name, note = ref

            # Gender-specific adjustments
            if key == "HGB":
                if data.patient_gender == "female":
                    low, high = 110, 150
                elif data.patient_gender == "male":
                    low, high = 120, 160
            if key == "RBC":
                if data.patient_gender == "female":
                    low, high = 3.5, 5.0
                elif data.patient_gender == "male":
                    low, high = 4.0, 5.5
            if key == "UA":
                if data.patient_gender == "female":
                    low, high = 89, 357

            if item.value > high:
                status = "HIGH"
            elif item.value < low:
                status = "LOW"
            else:
                status = "NORMAL"

            reference = f"{low}–{high} {unit}".strip()
            findings.append(LabFinding(
                name=item.name,
                full_name=full_name,
                value=item.value,
                unit=unit,
                status=status,
                reference=reference,
                reference_source="builtin",
                clinical_note=note,
            ))

        abnormal = [f for f in findings if f.status in ("HIGH", "LOW")]
        unknown = sum(f.status == "UNKNOWN" for f in findings)
        if not findings:
            summary = "未识别出可解读的数值项目。请上传能看清项目、结果、单位和参考范围的完整图片，或粘贴对应行。"
        elif unknown == len(findings):
            summary = f"识别到 {len(findings)} 项结果，均缺少可比对的参考范围。请核对原报告的参考范围列，或粘贴项目、数值、单位和参考范围。"
        elif abnormal:
            parts = [f"{f.full_name}({'偏高' if f.status == 'HIGH' else '偏低'})" for f in abnormal]
            summary = f"{len(abnormal)} 项超出对应参考范围（{'、'.join(parts)}）。"
        else:
            summary = f"{len(findings) - unknown} 项在对应参考范围内。"
        if 0 < unknown < len(findings):
            summary += f"另有 {unknown} 项待核对，请查看原报告的范围和单位。"

        return LabInterpreterOutput(
            skill_name=self.name,
            success=True,
            confidence=0.9,
            disclaimer="通用参考仅供对照，结果以原报告及检验机构说明为准。"
            if any(f.reference_source == "builtin" for f in findings)
            else "数值与参考范围请以原报告为准。",
            total_items=len(findings),
            abnormal_count=len(abnormal),
            unknown_count=unknown,
            findings=findings,
            summary=summary,
        )
