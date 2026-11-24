"""
Router Agent Node.

Responsible for classifying the user's intent and dispatching to the
correct specialized agent. This is the entry point of the main graph.
"""
import re

from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from agents.state import MainAgentState
from agents.llm import get_chat_llm


ROUTER_SYSTEM_PROMPT = """你是一个大健康 App 中的"智能意图路由器"。根据最新一句用户消息意图分类。如果它是在补充症状或回答上一句追问，需要结合最近的对话判断。此前的话题已经切换时，以新话题为准。

请你严格回复以下指定字符串选项之一，不要回答多余的废话，只回复分类 ID 字符串：
1. "clinic_agent" - 用户描述了具体的身体不适、症状，寻求如何就医、看什么科或者询问可能得了什么病。
2. "insurance_agent" - 用户询问个人医保余额、报销、消费明细等医保事务，提供官方查询指引。
3. "report_agent" - 用户让你帮忙看看化验单、检查报告，或者解读检查指标。
4. "pharmacy_agent" - 用户询问药品信息（用法用量、副作用、说明书）、药物相互作用、找附近药店、或者根据症状推荐非处方药。
5. "advisor_agent" - 其它通用健康科普、饮食作息建议或者打招呼问候。

只回复分类 ID 字符串，不要任何其他内容。"""

# Phrases that always take the user back to the general advisor, whatever the
# current active_agent is. Also used by main.py to abandon a clinic follow-up
# that is waiting on an interrupt.
EXIT_PHRASES = frozenset(
    {
        "退出",
        "结束",
        "结束问诊",
        "退出问诊",
        "不看了",
        "不用了",
        "我要退出",
        "取消",
        "退出诊室",
        "退出模式",
        "退出功能",
    }
)

# Characters that may sit around an exit phrase without changing its meaning.
# They double as the "word boundary" a phrase must be followed by, which is
# what keeps 取消 inside 我想取消明天的预约 from ending the conversation.
_EXIT_BOUNDARY = " \t　，,。.！!？?；;、~～…·"

_ABDOMINAL_COMPLAINT = re.compile(
    r"(?:我(?:的|现在|最近)?)?(?P<symptom>(?:肠胃|胃|肚子|腹部)(?:有点)?(?:不舒服|难受|疼|痛))"
    r"(?:，|,|\s)*(?:该?怎么办|怎么处理)?"
)
_SYMPTOM_QUALITY = re.compile(
    r"(?:有点|感觉)?(?:(?:像(?:被)?)?针扎(?:一样|似的|感)?(?:的疼|的痛)?|刺痛|烧灼感|钝痛|闷痛|绞痛|胀痛)"
)


def reported_abdominal_complaint(text: str) -> str:
    """Return a directly reported complaint, excluding general symptom questions."""
    match = _ABDOMINAL_COMPLAINT.fullmatch(text.strip(_EXIT_BOUNDARY))
    return match["symptom"] if match else ""


def reported_symptom_quality(text: str) -> str:
    """Recognize a standalone description without inferring its severity."""
    reply = text.strip(_EXIT_BOUNDARY)
    return reply if _SYMPTOM_QUALITY.fullmatch(reply) else ""

def is_exit_request(text: str) -> bool:
    """
    True when the user asked to leave the current specialized mode.

    Matching is deliberately anchored rather than a free substring search:

    1. Whitespace and surrounding punctuation are stripped.
    2. The result must either **be** an exit phrase exactly …
    3. … or **start** with one that is immediately followed by punctuation or
       whitespace (``不用了，谢谢``).

    Anything else is ordinary prose, so ``我想取消明天的预约``、``这个药不用了吗``
    and ``结束以后要复查吗`` stay inside the current specialist.
    """
    normalized = text.strip().strip(_EXIT_BOUNDARY)
    if not normalized:
        return False
    if normalized in EXIT_PHRASES:
        return True
    for phrase in EXIT_PHRASES:
        if not normalized.startswith(phrase):
            continue
        remainder = normalized[len(phrase):]
        if remainder and remainder[0] in _EXIT_BOUNDARY:
            return True
    return False



async def router_node(state: MainAgentState) -> dict:
    """
    Classifies the user's latest message and sets next_agent in state.
    Respects 'active_agent' short-circuiting to persist multi-turn context;
    a specialist that receives an off-topic question escapes the lock by
    calling one of the handoff tools (see agents/handoff.py) rather than by
    waiting for the user to type an exit phrase.

    Every return path resets 'handoff_count', which is what makes the handoff
    budget per-turn instead of per-conversation.
    """
    print("--- [Router] Entering router_node ---", flush=True)

    # Get the last human message
    last_message = state["messages"][-1]
    user_text = last_message.content if hasattr(last_message, "content") else str(last_message)

    # 1. Check for explicit system intents (Frontend forced mode switches)
    # Note: SYSTEM_INTENT strings are reserved for future use if the frontend
    # ever needs to inject text-based intents into the message body.
    # Currently, routing context is passed via the 'active_agent' state field.

    # 2. Context Awareness: Short-circuit if already in a specialized mode
    # active_agent uses the same "*_agent" key format that _AGENT_MAP in graph.py expects.
    active_agent = state.get("active_agent", "")

    # Check if the user is explicitly asking to exit the current mode.
    # These phrases take highest priority over any active_agent lock.
    if is_exit_request(user_text):
        print("--- [Router] User requested exit, routing back to advisor_agent ---", flush=True)
        return {
            "next_agent": "advisor_agent",
            "active_agent": "advisor_agent",
            "handoff_count": 0,
        }

    if active_agent and active_agent not in ("advisor_agent", ""):
        print(f"--- [Router] Short-circuit: already in {active_agent}, bypassing LLM classification ---", flush=True)
        return {"next_agent": active_agent, "handoff_count": 0}

    recent = [
        message for message in state["messages"]
        if isinstance(message, (HumanMessage, AIMessage)) and isinstance(message.content, str)
        and message.content.strip() and not getattr(message, "tool_calls", None)
    ][-5:]
    previous_user = next((message.content for message in reversed(recent[:-1]) if isinstance(message, HumanMessage)), "")
    if isinstance(user_text, str) and (
        reported_abdominal_complaint(user_text)
        or (reported_symptom_quality(user_text) and reported_abdominal_complaint(previous_user))
    ):
        return {"next_agent": "clinic_agent", "active_agent": "clinic_agent", "handoff_count": 0}

    # 3. Classify with recent user/assistant turns so brief replies retain context.
    llm = get_chat_llm("router", streaming=False)
    try:
        print("--- [Router] Invoking LLM... ---", flush=True)
        response = await llm.ainvoke([
            SystemMessage(content=ROUTER_SYSTEM_PROMPT),
            *recent[:-1],
            HumanMessage(content=user_text),
        ])
        raw_content_lower = response.content.lower()
        # Remove all whitespace (spaces, newlines, tabs) to handle LLM formatting quirks (e.g. 'clin\nic_agent')
        raw_clean = "".join(raw_content_lower.split())

        # Robust substring matching
        if "clinic_agent" in raw_clean:
            next_agent = "clinic_agent"
        elif "insurance_agent" in raw_clean:
            next_agent = "insurance_agent"
        elif "report_agent" in raw_clean:
            next_agent = "report_agent"
        elif "pharmacy_agent" in raw_clean:
            next_agent = "pharmacy_agent"
        else:
            next_agent = "advisor_agent"

        print(f"--- [Router] Matched next_agent: '{next_agent}' ---", flush=True)

    except Exception as e:
        import traceback
        print(f"--- [Router] LLM error: {e} ---", flush=True)
        print(f"--- [Router] Traceback:\n{traceback.format_exc()} ---", flush=True)
        next_agent = "advisor_agent"

    print(f"--- [Router] Dispatching to: {next_agent} ---", flush=True)

    # Store the decision in active_agent so subsequent turns persist
    return {
        "next_agent": next_agent,
        "active_agent": next_agent,
        "handoff_count": 0,
    }
