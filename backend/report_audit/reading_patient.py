"""Patient-facing interpretation of confirmed report patterns and optional context."""

from typing import Literal

from pydantic import BaseModel, Field

from .reading_context import PatientContext


class ReadingSource(BaseModel):
    title: str
    url: str


class PatientGuidance(BaseModel):
    summary: str
    possibilities: list[str] = Field(default_factory=list)
    care_level: Literal["routine", "prompt", "emergency"]
    next_step: str
    used_context: bool = False
    sources: list[ReadingSource] = Field(default_factory=list)


_LYMPHOCYTES = ReadingSource(
    title="NHS · 淋巴细胞计数偏低",
    url="https://rms.cornwall.nhs.uk/primary_care_clinical_referral_criteria/rms/primary_care_clinical_referral_criteria/haematology/lymphopenia",
)
_DIFFERENTIAL = ReadingSource(title="MedlinePlus · 白细胞分类",
                              url="https://medlineplus.gov/lab-tests/blood-differential/")
_PLATELETS = ReadingSource(title="MedlinePlus · 平均血小板体积",
                          url="https://medlineplus.gov/lab-tests/mpv-blood-test/")
_BLEEDING = ReadingSource(title="美国国立心肺血液研究所 · 出血时何时就医",
                          url="https://www.nhlbi.nih.gov/health/thrombocytopenia")
_FASTING = ReadingSource(title="MedlinePlus · 抽血前空腹",
                         url="https://medlineplus.gov/lab-tests/fasting-for-a-blood-test/")
_KIDNEY = ReadingSource(title="MedlinePlus · 肌酐检查",
                        url="https://medlineplus.gov/lab-tests/creatinine-test/")
_URINE = ReadingSource(title="MedlinePlus · 尿液中的血",
                       url="https://medlineplus.gov/lab-tests/blood-in-urine/")


def guidance_for_patient(
    abnormal: dict[str, set[str]], evidence: dict[str, set[str]], context: PatientContext,
    *, liver_context: str = "",
) -> PatientGuidance:
    findings: list[str] = []
    possibilities: list[str] = []
    sources: list[ReadingSource] = []
    low_lymphocytes = "LOW" in abnormal.get("LYM#", ())
    if low_lymphocytes:
        findings.append("一种参与免疫的细胞（淋巴细胞）的实际数量低于本次参考范围")
        sources.append(_LYMPHOCYTES)
        if context.recent_infection and context.age_group != "under_18":
            possibilities.append("你提到近期有过感染。感染期间或之后，淋巴细胞计数可能暂时偏低。"
                                 "这个线索值得和医生核对，单次化验无法确定原因。")
        if context.immune_medicine and context.age_group != "under_18":
            possibilities.append("你填了在用激素或免疫抑制药。部分这类药会影响淋巴细胞计数。就诊时带上药名和剂量。")
        if context.previous_low_lymphocytes:
            possibilities.append("你填了以前也出现过淋巴细胞计数偏低。带上历次报告，医生可以看低值是否持续以及是否还有其他指标变化。")
        if context.age_group == "under_18":
            possibilities.append("儿童的血常规需要按年龄解读。就诊时可说明近期有无感染、正在用的药，并带上以前的血常规。")
        elif not possibilities:
            possibilities.append("近期感染和部分用药都可能影响淋巴细胞计数。还要看以往结果是否也偏低。")
    if "HIGH" in abnormal.get("BAS%", ()):
        if evidence.get("BAS#") == {"NORMAL"}:
            possibilities.append("嗜碱性粒细胞占比偏高，已识别的绝对计数在报告范围内。单靠这项变化无法判断是否过敏。")
        else:
            possibilities.append("嗜碱性粒细胞占比偏高。判断具体原因还要结合这类细胞的实际数量和症状，单看占比无法判断是否过敏。")
        sources.append(_DIFFERENTIAL)
    if set(abnormal).intersection({"PLT", "MPV", "PCT", "PDW"}):
        findings.append("血小板相关指标超出报告范围")
        if evidence.get("PLT") == {"NORMAL"}:
            possibilities.append("血小板的个数在本报告范围内，超范围的是反映体积或大小差异的指标。"
                                 "这些变化单独不能确定有出血性疾病。")
        else:
            possibilities.append("血小板的数量与大小需要一起看。医生会结合原报告数值及有无出血、瘀青判断这些变化的意义。")
        sources.append(_PLATELETS)

    if context.fasting and evidence.keys() & {"GLU", "TG"}:
        if context.fasting == "yes":
            possibilities.append("你填写了按要求空腹。血糖或甘油三酯仍需按报告注明的采样条件和参考范围解读。")
        else:
            possibilities.append("这次没有按要求空腹，或你不确定。请核对报告项目是否要求空腹，再解释此次血糖或甘油三酯。")
        sources.append(_FASTING)
    if liver_context:
        possibilities.append(liver_context)
    if context.kidney_medicine and evidence.keys() & {"CREA", "KIDNEY"}:
        possibilities.append("你提到近期有用药或补充剂。部分药物和补充剂可影响肌酐，解读时可带上清单与以往结果。")
        sources.append(_KIDNEY)
    if context.urinary_symptoms and evidence.keys() & {"URBC", "UWBC", "URINE", "URINE_BLOOD"}:
        possibilities.append("你提到排尿不适。解读尿检时需要结合症状及采样情况，可以把症状出现的时间和报告一起告诉医生。")
        sources.append(_URINE)
    if context.menstrual_collection and evidence.keys() & {"URBC", "URINE_BLOOD"}:
        possibilities.append("你填写了采尿时处于月经期。经血混入样本可能影响尿液中的红细胞或隐血结果，请告诉开单医生。")
        if _URINE not in sources:
            sources.append(_URINE)

    summary = "这份报告主要看到" + "，以及".join(findings) + "。" if findings else "这份报告单独无法确定具体病症。"
    if context.severe_bleeding:
        return PatientGuidance(
            summary=summary, possibilities=possibilities, care_level="emergency", used_context=True,
            next_step="你勾选了大量出血或按压后仍止不住。请立即拨打当地急救电话或前往急诊。",
            sources=[*sources, _BLEEDING],
        )

    age_advice = "未满 18 岁的参考范围随年龄变化，请由儿科医生按年龄解读。" if context.age_group == "under_18" else ""
    if context.ongoing_fever or context.unusual_bleeding:
        symptoms = "持续或反复发热" if context.ongoing_fever else "反复不明原因的瘀青或出血"
        if context.unusual_bleeding and context.ongoing_fever:
            symptoms += "以及反复不明原因的瘀青或出血"
        if context.ongoing_fever and _DIFFERENTIAL not in sources:
            sources.append(_DIFFERENTIAL)
        if context.unusual_bleeding:
            sources.append(_BLEEDING)
        next_step = f"你勾选了{symptoms}。建议尽快联系医生，带上原报告并说明症状从什么时候开始。"
        level = "prompt"
    elif low_lymphocytes and context.previous_low_lymphocytes:
        next_step = "建议预约开单医生或全科门诊，带上前后几次血常规和用药清单，讨论是否需要复查。"
        level = "routine"
    else:
        next_step = "如果现在没有明显不适，可以带原报告和以往同项结果咨询开单医生，由医生判断是否需要复查。"
        level = "routine"
        if evidence.keys() & {"WBC", "LYM#", "NEUT#", "BAS%", "PLT", "MPV", "PCT", "PDW"}:
            next_step += "如果出现持续发热或反复不明原因的瘀青、出血，请提前联系医生。"
            if _DIFFERENTIAL not in sources:
                sources.append(_DIFFERENTIAL)
            sources.append(_BLEEDING)
    return PatientGuidance(
        summary=summary, possibilities=possibilities, care_level=level, next_step=next_step + age_advice,
        used_context=bool(context.model_dump(exclude_defaults=True)), sources=sources,
    )
