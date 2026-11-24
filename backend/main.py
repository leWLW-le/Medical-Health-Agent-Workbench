from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import asdict
import asyncio
from hmac import compare_digest
import inspect
import logging
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import json
import os
import re
from typing import Any, Literal
from uuid import uuid4
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from openai import APIError
from agents.checkpointing import DEFAULT_REPORT_CHECKPOINT_DB_PATH, ReportCheckpointRouter, create_checkpointer
from agents.handoff import HANDOFF_TOOL_LABELS
from agents.llm import LLMConfigurationError, resolve_model_settings
from agents.report_memory import PROTECTED_REPORT_THREADS, REPORT_THREAD_PREFIX, serialize_report_history
from agents.streaming import sse_payload as _sse_payload
from agents.web_search import search_medical_literature
from agents.router import is_exit_request
from agents.vision import (
    MAX_IMAGE_BYTES,
    SCAN_TYPE_TO_AGENT,
    VisionInputError,
    compose_agent_message,
    normalize_scan_type,
    parse_report_items,
    recognize_image,
    validate_image_upload,
)
from observability import configure_observability
from report_audit.api import router as report_audit_router
from report_audit.reading import ReadingAnswer, ReadingPrompt
from report_audit.reading_tasks import run_reading_task
from skills.lab_interpreter.skill import LabItem

load_dotenv()

_observability_runtime = configure_observability()
if _observability_runtime.provider != "none":
    print(
        f"--- [Observability] {_observability_runtime.provider} tracing enabled ---",
        flush=True,
    )


async def _close_checkpointer() -> None:
    """Release the checkpointer's backing connection (sqlite) on shutdown."""
    checkpointer = getattr(_master_app, "checkpointer", None)
    if isinstance(checkpointer, ReportCheckpointRouter):
        checkpointer = checkpointer.chat  # The report connection is owned by the app lifespan.
    connection = getattr(checkpointer, "conn", None)
    close = getattr(connection, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # pragma: no cover - shutdown best effort
        print(f"--- [API] Checkpointer close failed: {exc} ---", flush=True)


async def _warm_knowledge_base() -> None:
    """Load the RAG index at startup so the first clinic conclusion doesn't pay for it."""
    import time

    started = time.perf_counter()
    try:
        from rag.knowledge_base import get_knowledge_base

        await get_knowledge_base().warm()
    except Exception as exc:  # never block serving on RAG
        print(f"--- [RAG] Warm-up failed ({type(exc).__name__}); will retry lazily ---", flush=True)
        return
    print(f"--- [RAG] Knowledge base warmed in {time.perf_counter() - started:.1f}s ---", flush=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _master_app
    async with AsyncExitStack() as resources:
        if os.getenv("HEALTH_AGENT_TOKEN"):
            from agents.graph import build_graph

            path = os.getenv("AUDIT_READING_CHECKPOINT_DB", DEFAULT_REPORT_CHECKPOINT_DB_PATH)
            reports = await resources.enter_async_context(AsyncSqliteSaver.from_conn_string(path))
            await reports.setup()
            _master_app = build_graph(checkpointer=ReportCheckpointRouter(create_checkpointer(), reports))

        # Requests that need RAG before warm-up finishes use the same init lock.
        warmup = asyncio.create_task(_warm_knowledge_base()) if os.getenv("RAG_WARMUP", "1") == "1" else None
        try:
            yield
        finally:
            if warmup is not None:
                warmup.cancel()
            await _close_checkpointer()
            _master_app = None
            _observability_runtime.shutdown()


app = FastAPI(title="大健康 AI 后端", version="0.5.0", lifespan=lifespan)

app.include_router(report_audit_router)

# Vite dev servers and explicitly allowed browser origins.
ALLOWED_ORIGINS = (
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5175",
    # Comma-separated extras, e.g. a LAN address when testing on a phone.
    *(o.strip() for o in os.getenv("EXTRA_ALLOWED_ORIGINS", "").split(",") if o.strip()),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lazy-load the graph to speed up startup
_master_app = None

def get_master_app():
    global _master_app
    if _master_app is None:
        from agents.graph import master_app
        _master_app = master_app
    return _master_app


# --- 工具名称映射 (Tool / Node display names) ---
_NODE_LABELS = {
    "router": "分析您的需求",
    "clinic_node": "进入预问诊模块",
    "insurance_node": "提供医保官方查询指引",
    "report_node": "进入报告解读模块",
    "advisor_node": "进入健康顾问模块",
    "pharmacy_node": "进入用药咨询模块",
    # clinic subgraph internals
    "emergency_gate": "正在进行急症安全筛查…",
    "extract_symptoms": "正在整理症状要点…",
    "check_sufficiency": "正在检查信息是否完整…",
    "ask_followup": "正在准备追问…",
    "conclude": "正在生成分诊建议…",
}

# --- 技能工具 → 前端展示标签 ---
_SKILL_LABELS: dict[str, str] = {
    "search_health_knowledge":  "正在检索知识库…",
    # insurance tools
    # pharmacy tools
    "search_drug_info":         "正在查询药品信息…",
    "check_drug_interaction":   "正在检查药物相互作用…",
    "find_nearby_pharmacy":     "正在查找附近药店…",
    "get_otc_recommendation":   "正在检索用药建议…",
    # skills
    "load_skill":               "正在加载技能模块…",
    "emergency_triage":         "正在进行急症安全评估…",
    "symptom_scorer":           "正在评估症状严重程度…",
    "health_calculator":        "正在计算健康指标…",
    "lab_interpreter":          "正在解读化验指标…",
    "risk_assessor":            "正在评估慢性病风险…",
    "medication_calculator":    "正在计算用药剂量…",
    # agent-to-agent handoffs
    **HANDOFF_TOOL_LABELS,
}

_MODE_TO_AGENT = {
    "clinic": "clinic_agent",
    "insurance": "insurance_agent",
    "report": "report_agent",
    "pharmacy": "pharmacy_agent",
    "general": "advisor_agent",
    "dashboard": "advisor_agent",
}


class ChatMessage(BaseModel):
    role: str
    content: str


class UserInfo(BaseModel):
    name: str = "用户"
    age: int | None = None
    medical_history: str = "无"
    elder_mode: bool = False
    region: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    user_info: UserInfo | None = None
    chat_mode: str = "general"
    # Server-side conversation id. When supplied, the checkpointer holds the
    # history and only the newest user message is sent to the graph.
    thread_id: str | None = None
    report_items: list[LabItem] | None = Field(default=None, min_length=1, max_length=200)
    web_search: bool = False


def _build_lc_messages(messages: list[ChatMessage]) -> list[HumanMessage | AIMessage]:
    return [
        HumanMessage(content=msg.content)
        if msg.role == "user"
        else AIMessage(content=msg.content)
        for msg in messages
        if msg.role in ("user", "assistant") and msg.content.strip()
    ]


def _build_initial_state(
    messages: list[ChatMessage],
    user_info: UserInfo | None,
    active_agent: str,
) -> dict:
    user_info_dict = user_info.model_dump() if user_info else {}
    return {
        "messages": _build_lc_messages(messages)[-10:],
        "user_info": user_info_dict,
        "next_agent": "",
        "active_agent": active_agent,
        "handoff_count": 0,
        "report_items": None,
        "web_context": "",
    }


def _parse_user_info_json(raw: str) -> UserInfo:
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError("user_info must be an object")
        return UserInfo(**data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"user_info 格式错误: {exc}") from exc


def _parse_messages_json(raw: str) -> list[ChatMessage]:
    try:
        data = json.loads(raw) if raw else []
        if not isinstance(data, list):
            raise ValueError("messages must be a list")
        return [ChatMessage(**item) for item in data if isinstance(item, dict)]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"messages 格式错误: {exc}") from exc


# Custom-stream payloads an agent may push straight through to the client via
# agents.streaming. They must reuse the SSE event types the frontend already
# understands — this whitelist is the only place event types are gated.
_STREAMABLE_CUSTOM_TYPES = {"card", "text"}


def _latest_user_text(messages: list[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            return message.content
    return ""


def _split_stream_chunk(chunk: Any) -> tuple[str, Any] | None:
    """
    Decode a root-level `astream` chunk into (stream_mode, payload).

    With `subgraphs=True` the root chunk is `(namespace, mode, payload)`; node
    level `on_chain_stream` events carry plain dicts and are ignored here.
    """
    if not isinstance(chunk, tuple):
        return None
    if len(chunk) == 3:
        return chunk[1], chunk[2]
    if len(chunk) == 2 and isinstance(chunk[0], str):
        return chunk[0], chunk[1]
    return None


def _interrupt_question(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("question", "content", "text"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(value, ensure_ascii=False, default=str)


async def _resolve_graph_input(
    config: dict,
    turn_state: dict,
    full_state: dict,
    user_text: str,
) -> Any:
    """
    Decide what to feed the graph for this request.

    - pending interrupt + exit phrase -> None, meaning "drop this thread"
    - pending interrupt on the thread -> resume it with the user's answer
    - known thread                    -> only the newest message
    - unknown thread                  -> the full history from the request
    """
    master_app = get_master_app()
    try:
        snapshot = await master_app.aget_state(config)
    except Exception as exc:  # pragma: no cover - checkpointer backend dependent
        print(f"--- [API] Unable to read thread state: {exc} ---", flush=True)
        return full_state

    pending = [
        interrupt
        for task in getattr(snapshot, "tasks", ())
        for interrupt in getattr(task, "interrupts", ())
    ]
    if pending:
        if is_exit_request(user_text):
            # Never swallow an exit phrase as an answer to a follow-up question.
            print("--- [API] Exit phrase during an interrupt, dropping thread ---", flush=True)
            return None
        print("--- [API] Resuming a pending interrupt ---", flush=True)
        return Command(resume=user_text)

    if getattr(snapshot, "values", None):
        return turn_state
    return full_state


async def _iter_agent_events(
    graph_input: Any,
    config: dict | None = None,
):
    """Translate a graph run into events for the SSE endpoints."""
    seen_interrupts: set[str] = set()
    try:
        print("--- [API] Starting event stream ---", flush=True)
        master_app = get_master_app()
        async for event in master_app.astream_events(
            graph_input,
            config=config,
            version="v2",
            stream_mode=["updates", "custom"],
            subgraphs=True,
        ):
            kind = event["event"]
            node_name = event.get("name", "")

            if kind not in (
                "on_chat_model_stream",
                "on_chat_model_start",
                "on_chat_model_end",
                "on_chain_stream",
            ):
                print(f"--- [Event] {kind} | Node: {node_name} ---", flush=True)

            # 1. LLM is streaming text tokens
            if kind == "on_chat_model_stream":
                chunk_content = event["data"]["chunk"].content
                if chunk_content:
                    yield {"type": "text", "content": chunk_content}

            # 1b. Root-level stream chunks: custom card/text events pushed by
            #     the agents themselves (agents/streaming.py), plus interrupts.
            elif kind == "on_chain_stream":
                decoded = _split_stream_chunk(event.get("data", {}).get("chunk"))
                if decoded is None:
                    continue
                stream_mode, stream_payload = decoded
                if stream_mode == "custom":
                    if (
                        isinstance(stream_payload, dict)
                        and stream_payload.get("type") in _STREAMABLE_CUSTOM_TYPES
                    ):
                        yield stream_payload
                elif stream_mode == "updates" and isinstance(stream_payload, dict):
                    for item in stream_payload.get("__interrupt__", ()) or ():
                        marker = str(getattr(item, "id", "") or id(item))
                        if marker in seen_interrupts:
                            continue
                        seen_interrupts.add(marker)
                        value = getattr(item, "value", item)
                        question = _interrupt_question(value)
                        yield {"type": "text", "content": question}
                        yield {"type": "interrupt", "content": question}

            # 2. A graph node is starting (shows agent status in UI)
            elif kind == "on_chain_start":
                label = _NODE_LABELS.get(node_name)
                if label:
                    yield {"type": "node_start", "node": node_name, "content": label}

            # 3. A graph node finished
            elif kind == "on_chain_end":
                if node_name in _NODE_LABELS:
                    yield {"type": "node_end", "node": node_name}

            # 4. Tool / skill calls
            elif kind == "on_tool_start":
                tool_name = event.get("name", "tool")
                label = _SKILL_LABELS.get(tool_name, f"正在调用：{tool_name}")
                yield {"type": "tool_start", "tool": tool_name, "content": label}

            elif kind == "on_tool_end":
                tool_name = event.get("name", "tool")
                yield {"type": "tool_end", "tool": tool_name}
                # `card` events are NOT derived here: every card-producing tool
                # (or its agent node) writes the payload itself through
                # agents.streaming, and it arrives on the `custom` stream above.

        print("--- [API] Event stream finished successfully ---", flush=True)
        yield {"type": "finish"}

    except APIError as exc:
        print(f"--- [API] Model request failed ({type(exc).__name__}) ---", flush=True)
        yield {"type": "error", "code": "MODEL_UNAVAILABLE", "content": "模型服务暂不可用"}
    except Exception as exc:
        print(f"--- [API] Event stream error ({type(exc).__name__}) ---", flush=True)
        yield {"type": "error", "content": "对话处理失败，请稍后重试"}


async def _stream_agent_events(graph_input: Any, config: dict | None = None):
    """SSE wrapper around ``_iter_agent_events`` used by the HTTP endpoints."""
    async for event in _iter_agent_events(graph_input, config):
        yield _sse_payload(event)


@app.get("/")
async def root():
    return {"message": "大健康 AI 后端 v0.5 (LangGraph)"}


@app.get("/health")
async def health():
    return {"status": "ok"}


def _text_thread_config(thread_id: str, *, report: bool = False, account: str | None = None) -> dict:
    if report:
        if not re.fullmatch(r"[0-9a-f]{32}", thread_id):
            raise HTTPException(status_code=400, detail="报告会话无效")
        thread_id = REPORT_THREAD_PREFIX + (f"{account}:" if account else "") + thread_id
    elif thread_id.startswith(PROTECTED_REPORT_THREADS):
        raise HTTPException(status_code=400, detail="会话类型不匹配")
    return {"configurable": {"thread_id": thread_id}}


@app.get("/api/report-photo/session")
async def report_photo_history(x_report_thread: str = Header()):
    return await _report_photo_history(x_report_thread)


async def _report_photo_history(thread_id: str, account: str | None = None):
    config = _text_thread_config(thread_id, report=True, account=account)
    snapshot = await get_master_app().aget_state(config)
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="报告会话已失效", headers={"Cache-Control": "no-store"})
    return JSONResponse({"messages": serialize_report_history(snapshot.values)}, headers={"Cache-Control": "no-store"})


@app.delete("/api/report-photo/session")
async def clear_report_photo_history(x_report_thread: str = Header()):
    return await _clear_report_photo_history(x_report_thread)


async def _clear_report_photo_history(thread_id: str, account: str | None = None):
    config = _text_thread_config(thread_id, report=True, account=account)
    await get_master_app().checkpointer.adelete_thread(config["configurable"]["thread_id"])
    return Response(status_code=204)


@app.post("/api/chat")
async def chat(request: ChatRequest):
    return await _chat(request, report_session=request.chat_mode == "report")


async def _chat(request: ChatRequest, *, report_session: bool, account: str | None = None):
    thread_id = (request.thread_id or "").strip() or uuid4().hex
    config = _text_thread_config(thread_id, report=report_session, account=account)
    messages = request.messages
    report_items = None
    if request.report_items is not None:
        if not report_session:
            raise HTTPException(status_code=400, detail="仅报告解读模式支持逐项比对")
        report_items = [item.model_dump() for item in request.report_items]
        if not _latest_user_text(messages):
            messages = [*messages, ChatMessage(role="user", content="按已核对的项目比对。")]
    else:
        try:
            resolve_model_settings()
        except LLMConfigurationError as exc:
            raise HTTPException(status_code=500, detail=f"大模型配置错误：{exc}") from exc

    active_agent = _MODE_TO_AGENT.get(request.chat_mode, "advisor_agent")
    full_state = _build_initial_state(messages, request.user_info, active_agent)
    turn_state = _build_initial_state(messages[-1:], request.user_info, active_agent)
    full_state["report_items"] = turn_state["report_items"] = report_items
    user_text = _latest_user_text(messages)

    async def event_generator():
        active_thread_id = thread_id
        active_config = config
        graph_input = await _resolve_graph_input(config, turn_state, full_state, user_text)
        if graph_input is None:
            # The user left mid-interview: abandon the suspended thread so the
            # router gets to see the exit phrase on a clean conversation.
            active_thread_id = uuid4().hex
            active_config = _text_thread_config(active_thread_id, report=report_session, account=account)
            graph_input = full_state
        yield _sse_payload({"type": "session", "thread_id": active_thread_id})
        if request.web_search and report_items is None and user_text and isinstance(graph_input, dict):
            tool_name = "search_medical_literature"
            yield _sse_payload({"type": "tool_start", "tool": tool_name, "content": "正在搜索公开医学资料…"})
            search = await search_medical_literature(user_text)
            yield _sse_payload({"type": "tool_end", "tool": tool_name})
            yield _sse_payload({"type": "card", "payload": {"type": "web_search", "data": asdict(search)}})
            graph_input["web_context"] = search.answer_context()
        async for payload in _stream_agent_events(graph_input, active_config):
            yield payload

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def require_health_gateway(x_agent_token: str = Header(default="")) -> None:
    token = os.getenv("HEALTH_AGENT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="Health gateway is not configured")
    if not compare_digest(x_agent_token.encode(), token.encode()):
        raise HTTPException(status_code=403, detail="Health gateway credentials required")


report_account_header = Header(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


@app.get("/internal/health/report-photo/session", dependencies=[Depends(require_health_gateway)])
async def gateway_report_photo_history(x_report_thread: str = Header(), x_report_account: str = report_account_header):
    return await _report_photo_history(x_report_thread, x_report_account)


@app.delete("/internal/health/report-photo/session", dependencies=[Depends(require_health_gateway)])
async def gateway_clear_report_photo_history(x_report_thread: str = Header(), x_report_account: str = report_account_header):
    return await _clear_report_photo_history(x_report_thread, x_report_account)


@app.post("/internal/health/report-photo/chat", dependencies=[Depends(require_health_gateway)])
async def gateway_report_photo_chat(request: ChatRequest, x_report_account: str = report_account_header):
    if request.chat_mode != "report":
        raise HTTPException(status_code=400, detail="请选择报告解读模式")
    return await _chat(request, report_session=True, account=x_report_account)


@app.post("/internal/health/chat", dependencies=[Depends(require_health_gateway)])
async def gateway_chat(request: ChatRequest):
    return await _chat(request, report_session=False)


@app.post("/internal/health/report-reading/answer", dependencies=[Depends(require_health_gateway)], response_model=ReadingAnswer)
async def gateway_report_reading(prompt: ReadingPrompt) -> ReadingAnswer:
    async with aclosing(run_reading_task(get_master_app(), prompt, streaming=False)) as events:
        async for event in events:
            if event["type"] == "answer":
                return ReadingAnswer.model_validate(event["payload"])
    raise RuntimeError("Report reading did not return an answer")


@app.post("/internal/health/report-reading/answer/stream", dependencies=[Depends(require_health_gateway)],
          response_class=StreamingResponse)
async def gateway_report_reading_stream(prompt: ReadingPrompt) -> StreamingResponse:
    async def events():
        try:
            async with aclosing(run_reading_task(get_master_app(), prompt, streaming=True)) as stream:
                async for event in stream:
                    yield _sse_payload(event)
        except Exception as error:
            logging.getLogger(__name__).warning("Report reading stream failed (%s)", type(error).__name__)
            yield _sse_payload({"type": "error", "content": "报告解读失败，请稍后重试。"})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.delete("/internal/health/threads/{thread_id}", dependencies=[Depends(require_health_gateway)])
async def gateway_delete_thread(thread_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f-]{36}", thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread ID")
    if _master_app is not None:
        await _master_app.checkpointer.adelete_thread(thread_id)
    return Response(status_code=204)


@app.post("/api/vision-chat")
async def vision_chat(
    file: UploadFile = File(...),
    scan_type: str = Form(...),
    user_info: str = Form("{}"),
    messages: str = Form("[]"),
    thread_id: str = Form(""),
):
    try:
        normalized_scan_type = normalize_scan_type(scan_type)
    except VisionInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    parsed_user_info = _parse_user_info_json(user_info)
    parsed_messages = _parse_messages_json(messages)

    image_bytes = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        validate_image_upload(file.content_type, len(image_bytes))
    except VisionInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        resolve_model_settings(purpose="vision")
        if normalized_scan_type != "report":
            resolve_model_settings()
    except LLMConfigurationError as exc:
        raise HTTPException(status_code=500, detail=f"大模型配置错误：{exc}") from exc

    is_report = normalized_scan_type == "report"
    resolved_thread_id = uuid4().hex if is_report else thread_id.strip() or uuid4().hex
    config = _text_thread_config(resolved_thread_id, report=is_report)

    async def event_generator():
        try:
            yield _sse_payload({"type": "session", "thread_id": resolved_thread_id})
            # An image upload is never an exit phrase, so the thread always holds.
            yield _sse_payload({
                "type": "tool_start",
                "tool": "vision_model",
                "content": "正在识别图片内容…",
            })
            vision_text = await recognize_image(
                image_bytes,
                file.content_type or "",
                normalized_scan_type,
            )
            yield _sse_payload({"type": "tool_end", "tool": "vision_model"})

            if normalized_scan_type == "report":
                report_items = [item.model_dump() for item in parse_report_items(vision_text)]
                yield _sse_payload({
                    "type": "card", "payload": {"type": "report_ocr_review", "data": {"items": report_items}},
                })
                yield _sse_payload({
                    "type": "text",
                    "content": "请对照原报告核对下方提取表，再提交比对。" if report_items
                    else "未能可靠提取数值项目。可以在下方手动补录，或重新上传清晰照片。",
                })
                yield _sse_payload({"type": "finish"})
                return
            injected_message = compose_agent_message(normalized_scan_type, vision_text)
            latest_message = ChatMessage(role="user", content=injected_message)
            vision_messages = [*parsed_messages[-9:], latest_message]
            active_agent = SCAN_TYPE_TO_AGENT[normalized_scan_type]
            full_state = _build_initial_state(vision_messages, parsed_user_info, active_agent)
            turn_state = _build_initial_state([latest_message], parsed_user_info, active_agent)
            graph_input = await _resolve_graph_input(
                config, turn_state, full_state, injected_message
            )
            async for payload in _stream_agent_events(graph_input or full_state, config):
                yield payload

        except VisionInputError as exc:
            yield _sse_payload({"type": "error", "content": str(exc)})
        except APIError as exc:
            print(f"--- [Vision] Model request failed ({type(exc).__name__}) ---", flush=True)
            yield _sse_payload({"type": "error", "code": "MODEL_UNAVAILABLE", "content": "模型服务暂不可用"})
        except Exception as exc:
            print(f"--- [Vision] Event stream error ({type(exc).__name__}) ---", flush=True)
            yield _sse_payload({"type": "error", "content": "图片识别失败，请稍后再试"})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/internal/health/vision-chat", dependencies=[Depends(require_health_gateway)])
async def gateway_vision_chat(
    file: UploadFile = File(...),
    scan_type: Literal["drug_box", "trace_code"] = Form(...),
    user_info: str = Form("{}"),
    messages: str = Form("[]"),
    thread_id: str = Form(""),
):
    return await vision_chat(file, scan_type, user_info, messages, thread_id)


@app.post("/internal/health/report-photo/vision-chat", dependencies=[Depends(require_health_gateway)])
async def gateway_report_photo_vision(file: UploadFile = File(...), user_info: str = Form("{}")):
    return await vision_chat(file, "report", user_info, "[]", "")
