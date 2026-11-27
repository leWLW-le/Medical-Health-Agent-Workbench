# Agent SSE 与前端状态

FastAPI 用 SSE 发送单行 JSON。账号网关转发进度、文本和卡片，保存回复后发送完成事件。前端在 `services/streamEvents.ts` 解码流，根据 `types/index.ts` 中的类型更新消息。

```text
data: {"type":"node_start","node":"pharmacy_node","content":"进入用药咨询模块"}

data: {"type":"tool_start","tool":"search_drug_info","content":"正在查询药品信息…"}

data: {"type":"tool_end","tool":"search_drug_info"}

data: {"type":"text","content":"请查看药品信息。"}

data: {"type":"finish"}
```

| 事件 | 界面处理 |
| --- | --- |
| `session` | 保存服务端 `thread_id`，下轮继续同一会话。 |
| `node_start`、`node_end` | 更新节点进度。由节点名派发活跃角色。 |
| `tool_start`、`tool_end` | 更新工具执行状态。 |
| `text` | 将回复片段追加到消息。 |
| `card` | 依据 `payload.type` 展示分诊、药品、报告或检索结果。 |
| `interrupt` | 标记当前会话需要补答。 |
| `finish`、`error` | 完成本轮或显示错误。 |

账号报告使用专门的解读流。通过校验的段落可以渐进展示，最终的 `answer` 事件携带完整证据、工作记录和任务状态。

[服务端封装](../agents/streaming.py) · [前端事件解析](../../frontend/src/services/streamEvents.ts) · [报告任务](../report_audit/reading_tasks.py)
