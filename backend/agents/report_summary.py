"""Patient-facing explanations based on confirmed lab comparisons."""
from dataclasses import dataclass
import re
import unicodedata

from skills.lab_interpreter.skill import LabFinding, LabInterpreterOutput

_MEDLINE_TESTS = "https://medlineplus.gov/lab-tests/"


@dataclass(frozen=True)
class _TestGroup:
    title: str
    names: str
    explanation: str
    source: str


_GROUPS = (
    _TestGroup(
        "胆红素", "总胆红素 直接胆红素 间接胆红素 结合胆红素 非结合胆红素 TBIL DBIL IBIL TSB",
        "胆红素来自红细胞分解。这类检查用于了解肝脏处理和排出胆红素的情况。",
        _MEDLINE_TESTS + "bilirubin-blood-test/",
    ),
    _TestGroup(
        "肝胆酶", "谷丙转氨酶 丙氨酸氨基转移酶 谷草转氨酶 天冬氨酸氨基转移酶 谷草/谷丙 谷草/谷丙比 "
        "碱性磷酸酶 谷氨酰转移酶 γ谷氨酰转移酶 γ谷氨酰转肽酶 ALT AST AST/ALT ALP GGT",
        "转氨酶等指标帮助观察肝细胞或胆道相关变化，通常需要结合这一组的其他结果理解。",
        _MEDLINE_TESTS + "liver-function-tests/",
    ),
    _TestGroup(
        "血清蛋白", "总蛋白 白蛋白 球蛋白 白球比 白球蛋白比值 白蛋白/球蛋白 TP ALB GLB GLOB A/G",
        "总蛋白是白蛋白和球蛋白的合计。白球比反映这两类蛋白的比例。",
        _MEDLINE_TESTS + "total-protein-and-albumin-globulin-a-g-ratio/",
    ),
    _TestGroup(
        "肾脏滤过相关指标", "尿素 尿素氮 血尿素氮 肌酐 血肌酐 血清肌酐 肾小球滤过率 估算肾小球滤过率 "
        "BUN UREA CREA SCR EGFR",
        "这组血液指标用于评估肾脏清除代谢废物的情况，解读时也会参考既往结果。",
        "https://www.nhs.uk/conditions/kidney-disease/diagnosis/",
    ),
    _TestGroup(
        "尿酸", "尿酸 血尿酸 UA URICACID",
        "尿酸是身体分解嘌呤后产生的物质。这个项目测量样本中的尿酸水平。",
        _MEDLINE_TESTS + "uric-acid-test/",
    ),
    _TestGroup(
        "血常规", "白细胞 白细胞计数 红细胞 红细胞计数 血红蛋白 血小板 血小板计数 红细胞压积 血细胞比容 "
        "平均红细胞体积 WBC RBC HGB HB PLT HCT MCV",
        "血常规从血细胞的数量、大小等方面，帮助了解供氧、免疫和凝血相关情况。",
        _MEDLINE_TESTS + "complete-blood-count-cbc/",
    ),
    _TestGroup(
        "血糖", "血糖 空腹血糖 葡萄糖 糖化血红蛋白 GLU FPG HBA1C",
        "血糖反映采血当时的水平，糖化血红蛋白帮助了解近几个月的平均血糖。",
        _MEDLINE_TESTS + "hemoglobin-a1c-hba1c-test/",
    ),
    _TestGroup(
        "血脂", "总胆固醇 甘油三酯 高密度脂蛋白 高密度脂蛋白胆固醇 低密度脂蛋白 低密度脂蛋白胆固醇 "
        "TC TG HDL HDLC LDL LDLC",
        "血脂检查测量胆固醇和甘油三酯等成分，可和其他因素一起用于评估心血管风险。",
        _MEDLINE_TESTS + "cholesterol-levels/",
    ),
    _TestGroup(
        "甲状腺功能", "促甲状腺激素 促甲状腺素 游离三碘甲状腺原氨酸 游离甲状腺素 TSH FT3 FT4",
        "这组检查通过甲状腺激素及其调节信号来了解甲状腺功能。",
        "https://medlineplus.gov/thyroidtests.html",
    ),
    _TestGroup(
        "凝血", "凝血酶原时间 活化部分凝血活酶时间 国际标准化比值 PT PTT APTT INR",
        "这类检查观察血液形成凝块所需的时间及相关指标。",
        _MEDLINE_TESTS + "prothrombin-time-test-and-inr-pt-inr/",
    ),
)


def _name_key(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).split("(", 1)[0].upper()
    return re.sub(r"[\s_-]", "", name)


_GROUP_BY_NAME = {_name_key(name): group for group in _GROUPS for name in group.names.split()}


def _inline(value: str) -> str:
    """Render report-provided labels and ranges as literal text in Markdown."""
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", " ".join(value.split()))


def _format_result(finding: LabFinding) -> str:
    value = str(finding.value).removesuffix(".0")
    measurement = f"{value} {finding.unit}".strip()
    direction = "高于" if finding.status == "HIGH" else "低于"
    return f"- **{_inline(finding.name)}**（{_inline(measurement)}）{direction}原报告参考范围（{_inline(finding.reference)}）。"


def explain_report(result: LabInterpreterOutput) -> str:
    """Explain the purpose and scope of the tests with conclusions from the comparison only."""
    if not result.findings:
        return result.summary

    groups: dict[_TestGroup, list[str]] = {}
    abnormal = []
    pending = []
    for finding in result.findings:
        group = _GROUP_BY_NAME.get(_name_key(finding.name)) or _GROUP_BY_NAME.get(_name_key(finding.full_name))
        if group:
            groups.setdefault(group, []).append(finding.name)
        if finding.status in ("HIGH", "LOW"):
            abnormal.append(finding)
        elif finding.status == "UNKNOWN":
            pending.append(finding)

    normal = len(result.findings) - len(abnormal) - len(pending)
    counts = [
        text for count, text in (
            (normal, f"{normal} 项在原报告参考范围内"),
            (len(abnormal), f"{len(abnormal)} 项超出范围"),
            (len(pending), f"{len(pending)} 项待核对"),
        ) if count
    ]
    sections = ["### 报告解读", "本次提交的项目中，" + "，".join(counts) + "。"]
    if abnormal:
        sections.append("**超出参考范围的项目**\n\n" + "\n".join(map(_format_result, abnormal)))
    if pending:
        names = "、".join(dict.fromkeys(_inline(finding.name) for finding in pending))
        sections.append(f"**待核对的项目**　{names}。请核对原报告上的参考范围和单位。")

    if groups:
        panel_lines = []
        for group, names in groups.items():
            labels = "、".join(dict.fromkeys(map(_inline, names)))
            panel_lines.append(f"- **{group.title}**（{labels}）。{group.explanation} [资料]({group.source})")
        sections.append("**这些检查在看什么**\n\n" + "\n".join(panel_lines))

    if abnormal:
        sections.append("可以把超范围的项目和既往报告一起带给开单医生，由医生结合检查目的和实际情况解读。")
    elif normal:
        sections.append("范围比对反映这次提交的检查项目。理解结果时也要结合检查目的、症状和既往趋势。")
    if sum(map(len, groups.values())) < len(result.findings):
        sections.append("其他项目可以按名称继续询问，逐项查看它们的含义。")
    return "\n\n".join(sections)
