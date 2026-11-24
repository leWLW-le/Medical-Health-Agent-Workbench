import asyncio
from decimal import Decimal, localcontext
from functools import lru_cache
import os
from pathlib import Path
import re
from typing import Literal, TypedDict
from unicodedata import normalize

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .evidence import EvidenceContext, EvidenceSource, get_evidence_source, load_encounter_records
from .models import (
    LAB_ALIASES, LAB_NAMES, Answer, AuditResult, Draft, Finding, Issue, KnowledgeCitation, LabCode,
    LabRow, Observation, Question, Snapshot,
)

# Bump this when matching or comparison semantics change.
RULES_VERSION = 2
# Exact factors to a common unit within each analyte. Unlisted units are
# compared as written, without inferring a conversion or rounding tolerance.
UNIT_FACTORS: dict[LabCode, dict[str, int]] = {
    "HGB": {"g/L": 1, "g/dL": 10},
    "WBC": {"10^9/L": 1, "10^3/μL": 1},
    "PLT": {"10^9/L": 1, "10^3/μL": 1},
    "RBC": {"10^12/L": 1, "10^6/μL": 1},
    "HCT": {"%": 1, "L/L": 100},
    "MCV": {"fL": 1, "fl": 1},
    "GLU": {"mmol/L": 1},
    "ALT": {"U/L": 1},
    "AST": {"U/L": 1},
    "CREA": {"μmol/L": 1, "mmol/L": 1000},
}


def normalized_unit(unit: str) -> str:
    unit = re.sub(r"10([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", r"10^\1", unit)
    unit = re.sub(r"^[×*](?=10\^)", "", normalize("NFKC", unit))
    return re.sub(r"(?<=/)uL$", "μL", unit)


def comparable_values(row: LabRow, evidence: Observation) -> tuple[Decimal, Decimal] | None:
    if row.unit == evidence.unit:
        return row.value, evidence.value
    unit, source_unit = normalized_unit(row.unit), normalized_unit(evidence.unit)
    factors = UNIT_FACTORS.get(row.code, {})
    if unit in factors and source_unit in factors:
        # LabValue admits 14 digits and the largest multiplier has four digits.
        with localcontext(prec=18):
            return row.value * factors[unit], evidence.value * factors[source_unit]
    return None


def compare_row(row: LabRow, evidence: Observation | None) -> Finding:
    issues: list[Issue] = []
    if evidence is None:
        issues.append("missing_evidence")
    else:
        if row.collected_on != evidence.collected_on:
            issues.append("date_mismatch")
        values = comparable_values(row, evidence)
        if values is None:
            issues.append("unit_mismatch")
        elif row.collected_on == evidence.collected_on and values[0] != values[1]:
            issues.append("value_mismatch")
    return Finding(row=row, evidence=evidence, issues=issues)


class AuditState(TypedDict, total=False):
    draft: Draft
    previous: Snapshot | None
    evidence: dict[LabCode, list[Observation]]
    result: AuditResult


def retrieve_records(state: AuditState, runtime: Runtime[EvidenceContext]) -> dict:
    source = runtime.context.source if runtime.context else get_evidence_source()
    draft = state["draft"]
    evidence: dict[LabCode, list[Observation]] = {}
    for record in load_encounter_records(source, draft.patient_id, draft.encounter_id):
        evidence.setdefault(record.code, []).append(record)
    return {"evidence": evidence}


def audit_rows(state: AuditState) -> dict:
    previous = state.get("previous")
    old_findings = (
        {item.row.code: item for item in previous.findings}
        if previous and previous.rules_version == RULES_VERSION else {}
    )
    findings: list[Finding] = []
    reused = 0
    for row in state["draft"].rows:
        candidates = state["evidence"].get(row.code, [])
        matches = [item for item in candidates if item.collected_on == row.collected_on] or candidates
        if len(matches) > 1:
            findings.append(Finding(row=row, evidence=None, issues=["ambiguous_evidence"]))
            continue
        evidence = matches[0] if matches else None
        cached = old_findings.get(row.code)
        if cached and cached.row == row and cached.evidence == evidence and "ambiguous_evidence" not in cached.issues:
            findings.append(cached)
            reused += 1
        else:
            findings.append(compare_row(row, evidence))
    return {"result": AuditResult(findings=findings, checked_rows=len(findings) - reused,
                                  reused_rows=reused, rules_version=RULES_VERSION)}


def compile_audit():
    graph = StateGraph(AuditState, context_schema=EvidenceContext)
    graph.add_node("retrieve_records", retrieve_records)
    graph.add_node("audit_rows", audit_rows)
    graph.add_edge(START, "retrieve_records")
    graph.add_edge("retrieve_records", "audit_rows")
    graph.add_edge("audit_rows", END)
    return graph.compile()


audit_graph = compile_audit()

ISSUE_NAMES: dict[Issue, str] = {
    "date_mismatch": "采样日期不一致", "unit_mismatch": "单位不一致",
    "value_mismatch": "数值不一致", "missing_evidence": "缺少原始记录",
    "ambiguous_evidence": "有多条候选记录，需人工核对",
}


Intent = Literal["summary", "history", "knowledge", "record", "clarify"]


class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    focus: LabCode | None


class ChatState(TypedDict, total=False):
    snapshot: Snapshot
    question: Question
    focus: LabCode | None
    intent: Intent
    answer: Answer


def rules_for_question(question: Question) -> Route:
    text = normalize("NFKC", question.text.strip())
    if re.search(r"糖化血红蛋白|(?<![A-Z0-9])HBA1C(?![A-Z0-9])", text.upper()):
        return Route(focus=None, intent="clarify")
    upper = text.upper()
    codes = [
        code for code, aliases in LAB_ALIASES.items()
        if any(
            re.search(rf"(?<![A-Z0-9]){label.upper()}(?![A-Z0-9])", upper)
            if label.isascii() else label in text
            for label in (code, *aliases)
        )
    ]
    if len(codes) > 1:
        return Route(focus=None, intent="clarify")
    if re.search(r"历史|上次|以前|变化|趋势|前次", text):
        intent = "history"
    elif not codes and not re.search(r"这项|那项", text) and re.search(r"概览|汇总|全部|哪些项目|核对结果|有几项", text):
        return Route(focus=None, intent="summary")
    elif re.search(r"证据|原始记录|来源|版本|差异|不一致", text):
        intent = "record"
    elif re.search(r"是什么|什么意思|定义|含义", text):
        intent = "knowledge"
    elif codes or re.search(r"记录|这项|那项", text):
        intent = "record"
    else:
        return Route(focus=None, intent="clarify")
    return Route(focus=codes[0] if codes else question.focus, intent=intent)


async def classify_with_model(question: Question) -> Route:
    from agents.llm import get_chat_llm

    classifier = get_chat_llm("precise", streaming=False).with_structured_output(Route, method="function_calling")
    result = await classifier.ainvoke([
        ("system",
         "只做意图分类。summary=核对概览，history=同一项目的以往结果，knowledge=术语释义，"
         "record=当前报告与原始记录，clarify=不支持或不明确。最多返回一个项目代码。"
         "询问其他患者、诊断或治疗时选择 clarify。禁止回答问题本身。"),
        ("human", f"上一轮项目：{question.focus or '无'}\n当前问题：{question.text}"),
    ])
    return Route.model_validate(result)


async def resolve_question(state: ChatState) -> dict:
    question = state["question"]
    if re.search(r"别的患者|其他患者|另一个患者|DEMO-[0-9]|VISIT-[0-9]", question.text.upper()):
        return Route(focus=None, intent="clarify").model_dump()
    route = rules_for_question(question)
    if route.intent == "clarify" and os.getenv("REPORT_AUDIT_LLM_ROUTER", "0") == "1":
        try:
            route = await asyncio.wait_for(classify_with_model(question), timeout=5)
        except Exception:
            pass  # The rule result remains available on timeout or invalid model output.
    if route.intent == "clarify":
        route = Route(focus=None, intent="clarify")
    return route.model_dump()


async def retrieve_hybrid(query: str) -> list[Document]:
    from rag.knowledge_base import get_knowledge_base

    return await get_knowledge_base().aretrieve(query, k=3)


@lru_cache(maxsize=1)
def local_glossary() -> dict[str, str]:
    path = Path(__file__).with_name("lab_glossary.md")
    entries = re.split(r"^## ", path.read_text(encoding="utf-8-sig"), flags=re.MULTILINE)
    glossary: dict[str, str] = {}
    for entry in entries[1:]:
        heading, text = entry.split("\n", 1)
        glossary[heading.strip()] = text.strip()
    return glossary


async def search_knowledge(code: LabCode) -> list[KnowledgeCitation]:
    if os.getenv("REPORT_AUDIT_KNOWLEDGE", "0") == "1":
        panel = "血常规" if code in {"HGB", "WBC", "PLT", "RBC", "HCT", "MCV"} else "血生化"
        query = f"{LAB_NAMES[code]} {panel} 参考范围"
        section = f"{panel}参考范围"
        entry_pattern = re.compile(rf"^\s*-?\s*{re.escape(LAB_NAMES[code])}(?:[（(:：\s]|$)")
        try:
            docs = await asyncio.wait_for(retrieve_hybrid(query), timeout=10)
            citations = [
                KnowledgeCitation(source="lab_reference", section=section, excerpt=line.strip())
                for doc in docs
                if doc.metadata.get("source") == "lab_reference" and doc.metadata.get("section") == section
                for line in doc.page_content.splitlines()
                if entry_pattern.match(line)
            ]
            if citations:
                return citations[:3]
        except Exception:
            pass
    return [KnowledgeCitation(source="模拟检验项目说明", section=code, excerpt=local_glossary()[code])]


async def knowledge_specialist(state: ChatState) -> dict:
    snapshot, code = state["snapshot"], state["focus"]
    if code is None:
        return answer_question(state)
    endpoint = os.getenv("AUDIT_MCP_URL", "")
    citations = []
    if endpoint:
        try:
            from .mcp_client import get_references

            citations = await asyncio.wait_for(get_references(endpoint, code), timeout=13)
        except Exception:
            citations = []
        if not citations:
            citations = [KnowledgeCitation(source="模拟检验项目说明", section=code, excerpt=local_glossary()[code])]
    else:
        citations = await search_knowledge(code)
    return {"answer": Answer(version=snapshot.version, focus=code, knowledge=citations,
                              text=f"找到涉及{LAB_NAMES[code]}的资料，见下方摘录。")}


def record_text(record: Observation) -> str:
    return f"{record.collected_on.isoformat()} 记录为 {record.value} {record.unit} [{record.id} v{record.revision}]"


def answer_question(state: ChatState, source: EvidenceSource | None = None) -> dict:
    snapshot, intent, code = state["snapshot"], state["intent"], state["focus"]
    response = Answer(version=snapshot.version, focus=code, text="")
    if intent == "summary":
        problems = [LAB_NAMES[item.row.code] for item in snapshot.findings if item.issues]
        response.text = (
            f"当前报告共 {len(snapshot.findings)} 项，{len(problems)} 项待核对。"
            + (f"涉及{'、'.join(problems)}。" if problems else "")
        )
        response.records = [item.evidence for item in snapshot.findings if item.issues and item.evidence]
    elif code is None:
        response.text = "请指定一个报告项目，例如血红蛋白，或询问核对结果。"
    else:
        finding = next((item for item in snapshot.findings if item.row.code == code), None)
        if finding is None:
            response.text = f"当前报告中没有{LAB_NAMES[code]}项目。"
        elif intent == "history":
            cutoff = finding.evidence.collected_on if finding.evidence else finding.row.collected_on
            provider = source if source is not None else get_evidence_source()
            draft = snapshot.draft
            earlier = sorted(
                (
                    record for record in provider.prior_records(
                        draft.patient_id, code, before=cutoff, exclude_encounter=draft.encounter_id,
                    )
                    if record.patient_id == draft.patient_id and record.encounter_id != draft.encounter_id
                    and record.code == code and record.collected_on < cutoff
                ),
                key=lambda record: (record.collected_on, record.id),
                reverse=True,
            )
            response.records = ([finding.evidence] if finding.evidence else []) + earlier
            response.text = (
                "；".join(record_text(record) for record in response.records) + "。"
                if earlier else f"原始病历中没有更早的{LAB_NAMES[code]}记录。"
            )
        elif "ambiguous_evidence" in finding.issues:
            response.text = f"{LAB_NAMES[code]}有多条候选原始记录，请人工确认本次采样记录。"
        elif finding.evidence is None:
            response.text = f"本次就诊没有可核对的{LAB_NAMES[code]}原始记录。"
        else:
            response.records = [finding.evidence]
            status = "、".join(ISSUE_NAMES[issue] for issue in finding.issues) or "字段一致"
            row = finding.row
            response.text = (
                f"{LAB_NAMES[code]}报告值为 {row.value} {row.unit}，采样日期 {row.collected_on.isoformat()}。"
                f"原始病历中 {record_text(finding.evidence)}。核对结果为{status}。"
            )
    return {"answer": response}


def trace_dependencies(state: ChatState) -> dict:
    """Record the report fields the selected answer actually read."""
    answer, intent = state["answer"], state["intent"]
    code = answer.focus
    keys: list[str] = []
    if intent == "summary":
        keys.append("summary")
    elif code is not None and intent not in {"knowledge", "clarify"}:
        keys.append(f"{code}.presence")
        finding = next((item for item in state["snapshot"].findings if item.row.code == code), None)
        if finding is not None:
            keys.append(f"{code}.evidence")
            if intent == "record" and finding.evidence is not None:
                keys.extend(f"{code}.{field}" for field in ("value", "unit", "collected_on", "issues"))
            elif intent == "record":
                keys.append(f"{code}.issues")
            elif finding.evidence is None:
                keys.append(f"{code}.collected_on")
    return {"answer": answer.model_copy(update={"dependencies": keys})}


async def record_specialist(state: ChatState, runtime: Runtime[EvidenceContext]) -> dict:
    source = runtime.context.source if runtime.context else None
    return await run_in_threadpool(answer_question, state, source)


def compile_chat():
    graph = StateGraph(ChatState, context_schema=EvidenceContext)
    graph.add_node("resolve_question", resolve_question)
    graph.add_node("record_specialist", record_specialist)
    graph.add_node("knowledge_specialist", knowledge_specialist)
    graph.add_node("trace_dependencies", trace_dependencies)
    graph.add_edge(START, "resolve_question")
    graph.add_conditional_edges(
        "resolve_question",
        lambda state: "knowledge_specialist" if state["intent"] == "knowledge" else "record_specialist",
        ["knowledge_specialist", "record_specialist"],
    )
    graph.add_edge("record_specialist", "trace_dependencies")
    graph.add_edge("knowledge_specialist", "trace_dependencies")
    graph.add_edge("trace_dependencies", END)
    return graph.compile()


chat_graph = compile_chat()
