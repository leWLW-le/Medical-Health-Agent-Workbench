"""
Clinic Agent — a compiled LangGraph subgraph mounted as ``clinic_node``.

The clinic subgraph collects symptoms, pauses for follow-up answers, and
produces a structured recommendation. Each reply re-enters the emergency gate.

Node responsibilities:
  emergency_gate    Pure rule matching (no LLM). Calls ``EmergencyTriageSkill``
                    directly so the red-flag gate can never be skipped by an
                    LLM that decides not to call the tool. CRITICAL short-
                    circuits the whole subgraph with the verbatim safety text.
                    It runs on entry AND on every resumed answer, so a red
                    flag mentioned while answering a follow-up is caught too.
  extract_symptoms  ONE structured LLM call (``InterviewStep``): the symptom
                    facts, merged into ``ClinicState['collected']``, and a
                    drafted next question (empty when nothing is missing).
  check_sufficiency Deterministic check for the required fields.
  ask_followup      No LLM: asks the drafted question (or a template when the
                    draft is empty / too long) — exactly one question.
  await_answer      ``interrupt({"kind": "followup", ...})`` — suspends the
                    whole graph until the next request on the same
                    ``thread_id`` resumes it with the user's answer, which
                    loops back through the emergency gate.
  conclude          ReAct agent (clinic skills + RAG) writes the triage text,
                    then a structured recommendation is streamed to the client
                    as a ``clinic_recommendation`` card.

Contract with the parent graph: the subgraph returns ``{"messages": [...]}``
containing ONLY the messages produced during this turn (see ``_replace``).
"""
from __future__ import annotations

import operator
import re
from typing import Annotated, Any, Literal, Sequence, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from agents.handoff import apply_handoff, get_handoff_tools, handoff_prompt_section
from agents.llm import get_chat_llm
from agents.router import reported_abdominal_complaint, reported_symptom_quality
from agents.streaming import emit_custom
from agents.knowledge_search import knowledge_search_tool
from rag.knowledge_base import get_knowledge_base
from skills import get_agent_tools, load_skill
from skills.emergency_triage.skill import EmergencyTriageSkill

AGENT_ID = "clinic_agent"

CLINIC_SYSTEM_PROMPT = """你是医疗健康 Agent 工作台中的"AI预问诊助手"，态度温和、专业。

你的任务是通过友好的对话帮助用户描述清楚症状，然后给出合理的分诊建议。

【对话要求】
1. **禁止下绝对诊断**：不允许说"您得了XX病"，只能说"从症状来看，可能需要就诊XX科"。
2. **安全第一**：如果用户提到剧烈胸痛、呼吸困难、突发意识丧失等急症，必须立刻提示"请立即拨打 120！"
3. **信息已收集完毕**：症状要点已由系统结构化整理，请直接据此给出结论，不要再反复追问。
4. **输出分诊建议**：以清晰的格式给出建议，包括：可能的科室、就诊优先级（紧急/较快/可择期）、初步注意事项。
5. **语气**：温和、关怀、专业，像一个有经验的家庭医生。

【可用技能（Skills）调用规则】
- **symptom_scorer**：根据已收集的症状信息调用，获取严重程度分数（0-100）和分诊颜色，用于辅助分诊建议。
- **health_calculator**：用户询问BMI、体重、热量时调用，需提供身高、体重、年龄、性别。
- **risk_assessor**：用户有多项慢性病风险因素时调用，评估心血管和糖尿病10年风险。
- **medication_calculator**：用户询问按体重/年龄计算药物剂量时调用。
- **lab_interpreter**：用户同时提供化验数值时调用，对指标进行结构化判读。

急症安全评估已由系统在进入本节点前完成，无需再调用 emergency_triage。
请直接开始回答，不要重复系统提示内容。"""

# One structured call does both halves of an interview step (it used to be
# two serial LLM calls, extract then ask): the facts, and the next question.
INTERVIEW_SYSTEM_PROMPT = """你是预问诊助手的"信息整理 + 追问"环节。请阅读预问诊对话，一次完成两件事。

一、抽取结构化症状要点（facts）：
- 只抽取用户明确说过的内容，没有提到的字段一律留空字符串或空数组，禁止推测或编造。
- duration 用原话表达（如"三天"、"两周"、"今天早上开始"）。
- quality 记录用户描述的不适感（如"针扎一样"、"烧灼感"）。这类短答可以承接上一句提问，并且不能据此推断 severity。
- severity 归一化为"轻微"/"中等"/"严重"之一。
- 用户后来更正的内容以更正为准（例如先说"三天"、后来说"其实是五天"，duration 取"五天"）。
- 不要输出诊断结论。

二、写下一个追问（next_question）：
- 必须收集的信息是：主要不适（主诉）、持续时间、严重程度。三项都已明确时，next_question 留空字符串。
- 否则像一位温和的预问诊护士，针对缺失的信息只问**一个**问题，不超过 40 个字，不要编号，不要罗列多个问题。
- 语气关怀、口语化，可以先用半句话共情再提问。
- 不要给出诊断、用药或就诊建议。
- 不要重复已经问过的问题。"""

RECOMMENDATION_SYSTEM_PROMPT = """请把下面这段预问诊结论压缩成结构化分诊建议。

规则：
- department 给出 1-3 个最贴切的科室名称（如"呼吸内科"）。
- urgency 只能是 emergency（需立即急诊）/ soon（建议尽快就诊）/ routine（可择期就诊）之一。
- summary 用一句话概括建议，不超过 60 个字。
- notes 给出 1-4 条就诊前注意事项。
- 只依据给定文本，不要新增结论。"""

REQUIRED_FIELDS: tuple[str, ...] = ("chief_complaint", "duration", "severity")

# Safety valve: never interrogate the user more than this many times per turn.
MAX_FOLLOWUPS = 3

# A drafted question longer than this is not "one short question": use the template.
MAX_QUESTION_CHARS = 60

_ANSWER_BOUNDARY = " \t　，,。.！!？?；;、~～…·"

_FIELD_LABELS = {
    "chief_complaint": "主要不适（主诉）",
    "quality": "不适的感觉",
    "location": "不适的具体部位",
    "duration": "症状持续了多久",
    "severity": "严重程度（轻微/中等/严重）",
    "associated_symptoms": "是否伴随其他症状",
    "onset": "症状是怎么开始的",
    "triggers": "有没有诱因或加重/缓解因素",
}

_FOLLOWUP_TEMPLATES = {
    "chief_complaint": "您现在最主要的不适是什么？",
    "duration": "这种不适持续了多久？",
    "severity": "您会把现在的不适程度描述为轻微、中等还是严重？",
}
_FOLLOWUP_RETRIES = {
    "chief_complaint": "如果只说一种不适，您最想告诉医生的是什么？",
    "duration": "如果记不清具体时间，大概持续了几小时或几天？",
    "severity": "现在的不适大概影响到您正常活动了吗？",
}
_FOLLOWUP_FIELDS = {
    "chief_complaint": re.compile(r"主诉|主要(?:的)?不适|哪里不舒服|哪里疼|什么(?:样的)?感觉|不舒服[^？?]*感觉|描述[^？?]*感觉|最想告诉医生"),
    "duration": re.compile(r"持续|多久|什么时候开始|几天|几小时|何时开始|多长时间"),
    "severity": re.compile(r"程度|多严重|有多痛|有多疼|几分|轻重|轻微|影响[^？?]*正常活动"),
}
_DURATION_REPLY = re.compile(r"(?:大约|大概|差不多)?[0-9一二两三四五六七八九十半]+(?:个)?(?:小时|分钟|天|周|月)(?:左右|了)?")

_URGENCY_TO_SEVERITY = {
    "emergency": "high",
    "soon": "medium",
    "routine": "low",
}


# ─── Structured schemas ───────────────────────────────────────────────────────

class SymptomFacts(BaseModel):
    """Structured symptom profile extracted from the interview transcript."""

    chief_complaint: str = Field(default="", description="主诉，用户最主要的不适")
    quality: str = Field(default="", description="用户原话描述的不适感，例如针扎一样")
    location: str = Field(default="", description="不适部位")
    duration: str = Field(default="", description="症状持续时间，保留用户原话")
    severity: str = Field(default="", description="严重程度：轻微 / 中等 / 严重")
    associated_symptoms: list[str] = Field(default_factory=list, description="伴随症状列表")
    onset: str = Field(default="", description="起病方式，如突发 / 逐渐加重")
    triggers: str = Field(default="", description="诱因或加重、缓解因素")


class InterviewStep(BaseModel):
    """One interview step: the facts so far and the next question (if any)."""

    facts: SymptomFacts = Field(default_factory=SymptomFacts, description="结构化症状要点")
    next_question: str = Field(
        default="",
        description="下一个追问（一个问题，不超过40字）；主诉、持续时间、严重程度都已明确时留空",
    )


class TriageRecommendation(BaseModel):
    """Structured triage result rendered by the frontend clinic card."""

    department: list[str] = Field(default_factory=list, description="推荐就诊科室")
    urgency: Literal["emergency", "soon", "routine"] = Field(
        default="routine", description="就诊优先级"
    )
    summary: str = Field(default="", description="一句话分诊建议")
    notes: list[str] = Field(default_factory=list, description="就诊前注意事项")


# ─── Subgraph state ───────────────────────────────────────────────────────────

def _replace(_current: Any, incoming: Any) -> Any:
    """Last-write-wins reducer."""
    return incoming


def _merge_facts(current: dict | None, incoming: dict | None) -> dict:
    """Merge newly extracted facts, never overwriting a known value with a blank."""
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        if value in ("", [], None):
            continue
        merged[key] = value
    return merged


class ClinicState(TypedDict, total=False):
    """
    Private state of the clinic subgraph.

    ``messages`` is deliberately a last-write-wins channel: on entry it holds
    the conversation history handed down by the master graph, and terminal
    nodes overwrite it with ONLY the messages produced during this turn. The
    parent's ``operator.add`` reducer then appends exactly those, which keeps
    the ``{"messages": new_messages}`` contract the rest of the app expects.
    """

    messages: Annotated[Sequence[BaseMessage], _replace]
    user_info: dict
    web_context: str
    # Mirrored from MainAgentState so conclude() can respect the per-turn
    # handoff budget before escaping to the parent graph.
    handoff_count: int
    # Messages produced inside this turn (follow-up Q&A + final triage answer).
    turn_messages: Annotated[list[BaseMessage], operator.add]
    # Symptom facts merged across extraction rounds.
    collected: Annotated[dict, _merge_facts]
    emergency_level: str
    missing_fields: list[str]
    is_sufficient: bool
    followup_count: int
    pending_question: str
    # Question drafted by extract_symptoms in the same call as the facts;
    # used by ask_followup only if check_sufficiency finds something missing.
    draft_question: str
    recommendation: dict


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Alias kept for readability inside this module; the implementation is shared
# with the other agents in agents/streaming.py.
_emit = emit_custom


def _transcript(state: ClinicState) -> list[BaseMessage]:
    """Inbound history plus everything produced during this turn, in order."""
    return [*state.get("messages", []), *state.get("turn_messages", [])]


def _message_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def _latest_user_text(state: ClinicState) -> str:
    for message in reversed(_transcript(state)):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _format_transcript(state: ClinicState) -> str:
    lines = []
    for message in _transcript(state):
        if isinstance(message, HumanMessage):
            lines.append(f"用户：{_message_text(message)}")
        elif isinstance(message, AIMessage):
            text = _message_text(message)
            if text:
                lines.append(f"助手：{text}")
    return "\n".join(lines)


def _user_age(state: ClinicState) -> int | None:
    age = state.get("user_info", {}).get("age")
    return age if isinstance(age, int) else None


def _format_collected(collected: dict) -> str:
    lines = []
    for key, label in _FIELD_LABELS.items():
        value = collected.get(key)
        if not value:
            continue
        rendered = "、".join(value) if isinstance(value, list) else str(value)
        lines.append(f"- {label}：{rendered}")
    return "\n".join(lines) or "- （暂无结构化要点）"


def _missing_fields(collected: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not collected.get(field)]


def _short_reply(state: ClinicState) -> str:
    return _latest_user_text(state).strip(_ANSWER_BOUNDARY)


def _explicit_facts(state: ClinicState) -> dict[str, str]:
    reply = _short_reply(state)
    facts: dict[str, str] = {}
    if _DURATION_REPLY.fullmatch(reply):
        facts["duration"] = reply
    if reply in {"轻微", "中等", "严重", "轻微程度", "中等程度"}:
        facts["severity"] = reply.removesuffix("程度")
    if quality := reported_symptom_quality(reply):
        facts["quality"] = quality
    complaint = reported_abdominal_complaint(reply)
    if not complaint and "quality" in facts and not state.get("collected", {}).get("chief_complaint"):
        complaint = next(
            (detail for message in reversed(_transcript(state)[-8:])
             if isinstance(message, HumanMessage)
             and (detail := reported_abdominal_complaint(_message_text(message)))),
            "",
        )
    if complaint and not state.get("collected", {}).get("chief_complaint"):
        facts["chief_complaint"] = complaint
    return facts


def _question_fields(question: str) -> set[str]:
    return {field for field, pattern in _FOLLOWUP_FIELDS.items() if pattern.search(question)}


def _latest_questions(state: ClinicState) -> list[str]:
    # Include the most recent answer from a prior agent when it hands off and
    # all follow-ups from the current interview.
    previous = next(
        (_message_text(message) for message in reversed(state.get("messages", []))
         if isinstance(message, AIMessage) and _message_text(message)),
        "",
    )
    return [previous, *(
        _message_text(message) for message in state.get("turn_messages", [])
        if isinstance(message, AIMessage)
    )]


def _clinic_card(recommendation: dict) -> dict:
    """Build the ``clinic_recommendation`` card payload the frontend renders."""
    urgency = recommendation.get("urgency", "routine")
    return {
        "type": "card",
        "payload": {
            "type": "clinic_recommendation",
            "data": {
                "summary": recommendation.get("summary", ""),
                "departments": recommendation.get("department", []),
                "severity": _URGENCY_TO_SEVERITY.get(urgency, "low"),
                "urgency": urgency,
                "notes": recommendation.get("notes", []),
            },
        },
    }


# ─── Nodes ────────────────────────────────────────────────────────────────────

async def emergency_gate(state: ClinicState) -> dict:
    """
    Code-level red-flag gate. Runs the ``emergency_triage`` skill directly —
    never through the LLM — so a CRITICAL presentation always short-circuits.
    """
    result = EmergencyTriageSkill().run(
        symptoms_text=_latest_user_text(state),
        age=_user_age(state),
    )
    if result.level != "CRITICAL":
        return {"emergency_level": result.level}

    flags = "、".join(result.triggered_flags)
    safety_text = result.safety_message
    if flags:
        safety_text += f"\n\n识别到的危险信号：{flags}。"
    safety_text += f"\n\n{result.disclaimer}"

    recommendation = {
        "department": ["急诊科"],
        "urgency": "emergency",
        "summary": f"检测到危急征象（{flags or '危急症状'}），请立即拨打120或前往最近急诊。",
        "notes": ["不要自行驾车前往医院", "保持电话畅通并有人陪同", "记录症状开始时间供医生参考"],
    }

    _emit({"type": "text", "content": safety_text})
    _emit(_clinic_card(recommendation))

    answer = AIMessage(content=safety_text)
    return {
        "emergency_level": result.level,
        "turn_messages": [answer],
        # On a resumed answer the follow-up Q&A of this turn must reach the
        # parent history too; on entry turn_messages is still empty.
        "messages": [*state.get("turn_messages", []), answer],
        "recommendation": recommendation,
    }


def route_after_gate(state: ClinicState) -> str:
    """CRITICAL ends the subgraph immediately; anything else (re)enters the interview."""
    return END if state.get("emergency_level") == "CRITICAL" else "extract_symptoms"


async def extract_symptoms(state: ClinicState) -> dict:
    """
    One structured LLM call: merge the transcript's symptom facts and draft
    the next follow-up question. Whether the question is used is still decided
    deterministically by ``check_sufficiency``.
    """
    transcript = _format_transcript(state)
    if not transcript:
        return {}
    explicit = _explicit_facts(state)

    try:
        llm = get_chat_llm("precise", streaming=False)
        # The interview runs on OpenAI-compatible providers that can call tools.
        # GLM's text response does not reliably satisfy the default JSON schema mode.
        step = await llm.with_structured_output(InterviewStep, method="function_calling").ainvoke(
            [
                SystemMessage(content=INTERVIEW_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"已收集到的信息：\n{_format_collected(state.get('collected', {}))}\n\n"
                        f"预问诊对话：\n{transcript}"
                    )
                ),
            ]
        )
        if isinstance(step, dict):
            step = InterviewStep.model_validate(step)
    except Exception as exc:
        print(f"--- [Clinic] interview step failed: {type(exc).__name__} ---", flush=True)
        return {"collected": explicit, "draft_question": ""} if explicit else {"draft_question": ""}

    if not isinstance(step, InterviewStep):
        return {"collected": explicit, "draft_question": ""} if explicit else {"draft_question": ""}
    facts = step.facts.model_dump()
    # Preserve exact short answers if the model drops them. A known sensation
    # never fills the severity field.
    facts.update({field: value for field, value in explicit.items() if field != "chief_complaint" or not facts[field]})
    if "quality" in explicit:
        facts["severity"] = ""
    return {
        "collected": facts,
        "draft_question": step.next_question.strip(),
    }


async def check_sufficiency(state: ClinicState) -> dict:
    """Check for required facts or an exhausted follow-up budget."""
    missing = _missing_fields(state.get("collected", {}))
    exhausted = state.get("followup_count", 0) >= MAX_FOLLOWUPS
    return {"missing_fields": missing, "is_sufficient": not missing or exhausted}


def route_after_sufficiency(state: ClinicState) -> str:
    return "conclude" if state.get("is_sufficient") else "ask_followup"


async def ask_followup(state: ClinicState) -> dict:
    """
    Ask exactly one gentle follow-up question about the missing facts.

    Accept the draft only if it addresses one missing fact and moves the
    interview forward. Otherwise ask about one missing fact from a template.
    """
    missing = state.get("missing_fields") or list(REQUIRED_FIELDS)
    previous = _latest_questions(state)
    already_asked = set().union(*(_question_fields(question) for question in previous))
    fresh = [field for field in missing if field not in already_asked]
    question = (state.get("draft_question") or "").strip()
    draft_fields = _question_fields(question)
    normalized = question.strip(_ANSWER_BOUNDARY)
    asked_text = {item.strip(_ANSWER_BOUNDARY) for item in previous}
    if (not normalized or len(question) > MAX_QUESTION_CHARS or question.count("？") + question.count("?") > 1
            or len(draft_fields) != 1
            or not draft_fields.issubset(fresh) or normalized in asked_text):
        field = (fresh or missing)[0]
        question = _FOLLOWUP_TEMPLATES[field]
        if question.strip(_ANSWER_BOUNDARY) in asked_text:
            question = _FOLLOWUP_RETRIES[field]
    quality = reported_symptom_quality(_short_reply(state))
    if quality and quality == state.get("collected", {}).get("quality"):
        question = f"明白，您刚才说的是「{quality}」。{question}"

    return {
        "pending_question": question,
        "draft_question": "",
        "turn_messages": [AIMessage(content=question)],
    }


async def await_answer(state: ClinicState) -> dict:
    """
    Suspend the whole graph until the user answers.

    ``interrupt()`` persists the pending question in the checkpoint; the next
    request on the same ``thread_id`` resumes with ``Command(resume=<answer>)``
    and the answer re-enters the interview through ``emergency_gate``, so a
    red flag in the answer is caught before anything else runs.
    The interrupt value is ``{"kind": "followup", "question": …}``;
    ``main._interrupt_question`` turns it back into the plain SSE text.
    """
    question = state.get("pending_question", "")
    answer = interrupt({"kind": "followup", "question": question})
    return {
        "turn_messages": [HumanMessage(content=str(answer))],
        "followup_count": state.get("followup_count", 0) + 1,
        "pending_question": "",
    }


async def _build_system_prompt(state: ClinicState) -> str:
    user_info = state.get("user_info", {})
    name = user_info.get("name", "您")
    age = user_info.get("age", "")
    history = user_info.get("medical_history", "无")
    lang_note = "请使用极度通俗易懂的语言。" if user_info.get("elder_mode") else ""

    system = f"""{CLINIC_SYSTEM_PROMPT}

【当前用户】姓名：{name}  年龄：{age}  既往病史：{history}
{lang_note}

【系统已结构化的症状要点】
{_format_collected(state.get("collected", {}))}
【系统急症安全评估】{state.get("emergency_level", "NON_URGENT")}""" + state.get("web_context", "")

    query = state.get("collected", {}).get("chief_complaint") or _latest_user_text(state)
    if query:
        search = knowledge_search_tool(get_knowledge_base())
        rag_context = await search.ainvoke({
            "queries": [query, f"{query} 疾病 症状 诊断", f"{query} 就诊科室 治疗"],
            "limit": 4,
        })
        if rag_context:
            system += (
                "\n\n## 参考临床知识\n"
                "以下为相关疾病知识，用于辅助分诊判断（不要直接引用，结合症状灵活使用）：\n\n"
                f"{rag_context}"
            )
    return system


async def _summarize_recommendation(triage_text: str, state: ClinicState) -> dict:
    """Derive the structured triage card from the ReAct agent's answer."""
    try:
        llm = get_chat_llm("precise", streaming=False)
        result = await llm.with_structured_output(TriageRecommendation, method="function_calling").ainvoke(
            [
                SystemMessage(content=RECOMMENDATION_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"症状要点：\n{_format_collected(state.get('collected', {}))}\n\n"
                        f"预问诊结论：\n{triage_text}"
                    )
                ),
            ]
        )
    except Exception as exc:  # pragma: no cover - provider/network dependent
        print(f"--- [Clinic] recommendation summary failed: {type(exc).__name__} ---", flush=True)
        return {}

    if isinstance(result, TriageRecommendation):
        recommendation = result.model_dump()
    elif isinstance(result, dict):
        recommendation = dict(result)
    else:
        return {}
    return recommendation


async def conclude(state: ClinicState) -> Command | dict:
    """
    ReAct triage answer + structured ``clinic_recommendation`` card.

    Clinic is a mounted subgraph, so a handoff here leaves through
    ``Command(graph=Command.PARENT, ...)`` rather than a plain node ``Command``.
    """
    system = await _build_system_prompt(state) + handoff_prompt_section(AGENT_ID)
    skill_tools = get_agent_tools(tags=["clinic"])
    agent = create_react_agent(
        get_chat_llm("balanced"),
        tools=[load_skill, *skill_tools, *get_handoff_tools(AGENT_ID)],
        prompt=SystemMessage(content=system),
    )

    transcript = _transcript(state)
    sub_result = await agent.ainvoke({"messages": transcript})
    produced = list(sub_result["messages"][len(transcript):])

    handoff = apply_handoff(
        state,
        produced,
        parent=True,
        prefix_messages=state.get("turn_messages", []),
    )
    if isinstance(handoff, Command):
        return handoff

    triage_text = next(
        (
            _message_text(message)
            for message in reversed(produced)
            if isinstance(message, AIMessage) and _message_text(message).strip()
        ),
        "",
    )

    update: dict = {
        "turn_messages": produced,
        "messages": [*state.get("turn_messages", []), *produced],
    }
    if triage_text:
        recommendation = await _summarize_recommendation(triage_text, state)
        if recommendation:
            update["recommendation"] = recommendation
            _emit(_clinic_card(recommendation))
    return update


# ─── Subgraph assembly ────────────────────────────────────────────────────────

def build_clinic_graph() -> StateGraph:
    workflow = StateGraph(ClinicState)

    workflow.add_node("emergency_gate", emergency_gate)
    workflow.add_node("extract_symptoms", extract_symptoms)
    workflow.add_node("check_sufficiency", check_sufficiency)
    workflow.add_node("ask_followup", ask_followup)
    workflow.add_node("await_answer", await_answer)
    workflow.add_node("conclude", conclude)

    workflow.add_edge(START, "emergency_gate")
    workflow.add_conditional_edges(
        "emergency_gate",
        route_after_gate,
        {"extract_symptoms": "extract_symptoms", END: END},
    )
    workflow.add_edge("extract_symptoms", "check_sufficiency")
    workflow.add_conditional_edges(
        "check_sufficiency",
        route_after_sufficiency,
        {
            "ask_followup": "ask_followup",
            "conclude": "conclude",
        },
    )
    workflow.add_edge("ask_followup", "await_answer")
    # Every answer goes back through the rule gate, not straight to extraction.
    workflow.add_edge("await_answer", "emergency_gate")
    workflow.add_edge("conclude", END)

    return workflow.compile()


# Compiled subgraph mounted as `clinic_node` in the master graph.
clinic_node = build_clinic_graph()
