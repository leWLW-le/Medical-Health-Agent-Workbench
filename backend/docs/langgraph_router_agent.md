# LangGraph 路由

`agents/graph.py` 将 `router` 注册为主图入口。结构化报告请求携带 `ReportRun`，直接进入报告证据检查。其余请求由 `agents/router.py` 处理。

## 分派顺序

1. 识别退出语句。明确结束当前模式时转到健康顾问。
2. 已有有效的专科角色时继续该角色，便于处理补充信息和追问。
3. 对明确的症状模式可直接进入预问诊。其余请求将最新问题和近期上下文交给路由模型。
4. 路由器把模型返回的角色 ID 映射到节点。无法识别的 ID 回退到健康顾问。每轮分派时重置转接次数。

| 角色 ID | 节点 |
| --- | --- |
| `advisor_agent` | `advisor_node` |
| `clinic_agent` | `clinic_node` |
| `pharmacy_agent` | `pharmacy_node` |
| `report_agent` | `report_node` |

`CHAT_MODE_TO_AGENT` 将 API 传入的 `general`、`clinic`、`pharmacy` 或 `report` 映射到初始角色，HTTP 接口和评测共用这张映射表。`agents/handoff.py` 为四个专科生成彼此的转接工具。转接更新 `active_agent` 和 `next_agent`，通过 `Command` 在同一轮执行目标节点。

[路由器源码](../agents/router.py) · [构图入口](../agents/graph.py) · [图、检查点和转接示例](../../docs/langgraph-runtime.md)
