"""
Shared State definitions for the LangGraph multi-agent system.
All state types used across agents are defined here.
"""
from typing import TypedDict, Annotated, NotRequired, Sequence
import operator
from langchain_core.messages import BaseMessage


CHAT_MODE_TO_AGENT = {
    "general": "advisor_agent",
    "clinic": "clinic_agent",
    "pharmacy": "pharmacy_agent",
    "report": "report_agent",
}


class MainAgentState(TypedDict):
    """
    Global state shared between the Router and all Specialized Agents.
    This is the 'single source of truth' for the current conversation turn.
    """
    # Accumulated conversation history (append-only via operator.add)
    messages: Annotated[Sequence[BaseMessage], operator.add]

    # User profile info injected by the API layer (name, age, medical history, etc.)
    user_info: dict

    # The agent the router selected for this turn.
    next_agent: str
    
    # Persistent tracking of the current focused agent to handle multi-turn conversations
    # If set, the router should bypass standard classification and route back here.
    active_agent: str

    # How many Command-based handoffs have already been honoured this turn.
    # Reset to 0 by router_node and capped by handoff.MAX_HANDOFFS_PER_TURN so
    # two specialists cannot transfer the same message back and forth forever.
    handoff_count: int

    # Numeric rows explicitly submitted for comparison this turn.
    report_items: NotRequired[list[dict] | None]

    # Last user-confirmed report, independent of the dialogue memory window.
    confirmed_report: NotRequired[dict]

    web_context: NotRequired[str]
