"""Handle old insurance routes without returning demonstration account records."""

from langchain_core.messages import AIMessage

from agents.state import MainAgentState
from agents.streaming import emit_custom


async def insurance_node(_state: MainAgentState) -> dict:
    text = (
        "本项目未接入个人医保账户服务。余额、缴费和消费记录可在国家医保服务平台登录后查询。"
        "\n\n[国家医保服务平台登录指南](https://www.nhsa.gov.cn/art/2025/2/15/art_52_15698.html)"
    )
    emit_custom({"type": "text", "content": text})
    return {"messages": [AIMessage(content=text)], "active_agent": "advisor_agent"}
