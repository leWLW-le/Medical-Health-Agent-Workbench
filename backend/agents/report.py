"""
Report Agent Node.

Handles medical lab report interpretation.
Business Flow:
  1. Receives user message containing report data (pasted text or image description)
  2. Compares numeric findings against the applicable reference range
  3. Uses the knowledge base for background when answering text questions
"""
import json

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command
from agents.handoff import apply_handoff, get_handoff_tools, handoff_prompt_section
from agents.knowledge_search import knowledge_search_tool
from agents.report_memory import confirmed_report_prompt, followup_context, report_summary_message
from agents.report_summary import explain_report
from agents.state import MainAgentState
from agents.llm import get_chat_llm
from agents.streaming import emit_card, emit_text
from rag.knowledge_base import get_knowledge_base
from skills import get_agent_tools, get_registry, load_skill
from skills.lab_interpreter.skill import LabInterpreterOutput


AGENT_ID = "report_agent"

# Skill whose structured output is rendered as the `report_analysis` card. The
# skill itself is shared with the clinic line, so the card is emitted here at
# node level instead of from inside the generic registry tool.
_CARD_SKILL = "lab_interpreter"
_CARD_TYPE = "report_analysis"


REPORT_SYSTEM_PROMPT = """你是一位专业的检验报告解读助手。用户会向你描述或粘贴化验单数据，你需要：

【解读流程】
1. **指标识别**：先找出用户提到的所有检验指标和数值。
2. **结构化解读**：调用 lab_interpreter 比对数值。逐行传入原报告的数值、单位和参考范围（reference_range）。
   解读个人报告时将 require_report_reference 设为 true。参考范围缺失或看不清时留空，不得从背景资料补写。
3. **通俗解释**：用老百姓能听懂的语言解释偏高/偏低代表什么临床意义。
4. **下一步**：遇到待核对项目请用户补充原报告的参考范围和单位。按工具的结果解释，避免给出用药或停药指令。
5. **免责声明**：最后必须注明"本解读仅供参考，具体诊疗请面诊专业医生"。

【格式要求】
- 使用清晰的列表格式
- 对正常指标简要说明，重点详细解释异常项
- 仅当所有指标实际完成比较且均在范围内时，才可概括为「已比较项目均在范围内」。
- UNKNOWN 表示无法判断。无可比对项目时，不得使用「结果正常」「均在参考范围内」等措辞。

【可用技能工具】
- lab_interpreter：将用户的化验数值列表结构化解读，输出 HIGH/LOW/NORMAL/UNKNOWN 分类
- load_skill：按名称加载任意注册技能

【注意】
- 严禁凭报告数据直接下诊断，只陈述指标含义和建议
- 保持客观严谨，不夸大也不轻描淡写"""


def _emit_report_cards(new_messages: list[BaseMessage]) -> None:
    """Replay the lab_interpreter results of this turn as frontend cards."""
    for message in new_messages:
        if not isinstance(message, ToolMessage):
            continue
        if getattr(message, "name", None) != _CARD_SKILL:
            continue
        content = message.content
        if not isinstance(content, str):
            continue
        try:
            emit_card(_CARD_TYPE, json.loads(content))
        except json.JSONDecodeError as exc:
            print(f"--- [Report] Unparsable {_CARD_SKILL} output: {exc} ---", flush=True)


async def report_node(state: MainAgentState) -> Command | dict:
    """
    Report agent: interprets lab reports using the lab_interpreter skill for
    structured abnormality detection, plus RAG for clinical context. Returns a
    ``Command`` when the sub-agent transferred the turn to another specialist.
    """
    report_items = state.get("report_items")
    if report_items is not None:
        interpretation = get_registry().get_tool(_CARD_SKILL)
        result = LabInterpreterOutput.model_validate_json(await interpretation.ainvoke({
            "items": report_items, "require_report_reference": True,
        }))
        confirmed_report = result.model_dump()
        emit_card(_CARD_TYPE, confirmed_report)
        text = explain_report(result)
        emit_text(text)
        return {"messages": [report_summary_message(text)], "confirmed_report": confirmed_report, "report_items": None}

    llm = get_chat_llm("precise")
    user_info = state.get("user_info", {})
    age = user_info.get("age", "")
    elder_mode = user_info.get("elder_mode", False)

    extra = ""
    if age:
        extra += f"\n用户年龄：{age}，请结合年龄特点解读。"
    if elder_mode:
        extra += "\n请使用简单易懂的语言，避免复杂的医学术语。"

    system = REPORT_SYSTEM_PROMPT + extra + handoff_prompt_section(AGENT_ID) + state.get("web_context", "")
    confirmed_report = state.get("confirmed_report")
    if confirmed_report:
        system += confirmed_report_prompt(confirmed_report)

    model_messages, earlier_questions = followup_context(state["messages"])
    if earlier_questions:
        system += "\n\n以下是较早轮次的问句片段，仅供理解话题延续。\n" + earlier_questions
    latest = model_messages[-1] if model_messages else None
    last_user_msg = latest.content if isinstance(latest, HumanMessage) else ""
    if isinstance(last_user_msg, str) and last_user_msg.strip():
        search = knowledge_search_tool(get_knowledge_base(), source="lab_reference", hide_queries=True)
        rag_context = await search.ainvoke({"queries": [last_user_msg]})
        if rag_context:
            system += (
                "\n\n## 检验背景资料\n"
                "仅用于解释指标含义，不得作为个人报告的判定阈值、诊断或用药建议。\n\n"
                f"{rag_context}"
            )

    skill_tools = get_agent_tools(tags=["report"])

    agent = create_react_agent(
        llm,
        tools=[load_skill, *skill_tools, *get_handoff_tools(AGENT_ID)],
        prompt=SystemMessage(content=system),
    )
    sub_result = await agent.ainvoke({"messages": model_messages})
    new_messages = list(sub_result["messages"][len(model_messages):])
    _emit_report_cards(new_messages)
    return apply_handoff(state, new_messages)
