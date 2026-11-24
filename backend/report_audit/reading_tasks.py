"""Resume one report question across patient replies without replaying earlier turns."""

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import NotRequired, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from agents.report_memory import REPORT_PREVIEW_THREAD_PREFIX, REPORT_READING_THREAD_PREFIX
from agents.streaming import emit_custom

from .reading import (
    ReadingAnswer, ReadingPrompt, ReadingState, _gather, _generate, _model_skip_reason, _relevant_context,
    _route, _with_work_record,
)
from .reading_context import (
    FINISH_REPORT_INTERVIEW, MAX_REPORT_FOLLOW_UPS, ReadingTaskProgress, infection_correction, is_context_reply_detail,
    report_context_form,
)

_GAPS = {
    "recent_infection": "抽血前后是否感染尚未明确，暂时无法判断这条线索是否相关。",
    "previous_low_lymphocytes": "缺少可对照的既往淋巴细胞结果，暂时无法区分一次偏低和持续偏低。",
    "immune_medicine": "缺少用药情况，暂时无法判断这类药物是否值得作为线索。",
    "ongoing_fever": "目前是否持续或反复发热尚未明确。",
    "unusual_bleeding": "目前是否反复出现不明原因的瘀青或出血尚未明确。",
    "liver_medicine": "近期是否新用或调整药物、保健品尚未明确。",
    "liver_medicine_name": "药物或保健品的名称尚未明确。",
    "liver_medicine_since": "用药与抽血的先后尚未明确，暂时无法判断时间上是否相关。",
    "urinary_symptoms": "排尿症状尚未明确，需要结合症状理解尿检结果。",
    "fasting": "抽血前的空腹情况尚未明确。",
    "kidney_medicine": "近期用药或补充剂情况尚未明确。",
}


class TaskState(TypedDict):
    findings_revision: int
    answered: int
    pending_field: NotRequired[str | None]
    pending_question: NotRequired[str]
    verified: NotRequired[dict]


@dataclass
class ReportRun:
    request: ReadingPrompt
    streaming: bool
    resumed: bool
    reply: str
    reading: ReadingState | None = None
    answer: ReadingAnswer | None = None
    answered_field: str | None = None

    @property
    def finished_by_user(self) -> bool:
        return self.reply.strip().rstrip("。！! ") == FINISH_REPORT_INTERVIEW


def _check_evidence(state: TaskState, runtime: Runtime[ReportRun]) -> dict:
    run = runtime.context
    request = run.request
    replies = request.context_replies
    run.answered_field = request.reply_field
    if request.reply_field and (correction := infection_correction(run.reply)):
        replies = {**replies, "recent_infection": correction}
        if request.reply_field != "recent_infection":
            replies.pop(request.reply_field, None)
        run.answered_field = "recent_infection"
    request = _relevant_context(request.model_copy(update={"context_replies": replies, "reply_field": run.answered_field}))
    if run.streaming:
        emit_custom({"type": "node_start", "node": "report_evidence", "content": "正在核对报告字段和已有回复…"})
    reading: ReadingState = {"request": request, "streaming": run.streaming}
    reading.update(_route(reading))
    reading.update(_gather(reading))
    answer = reading["answer"]
    if state.get("verified") and answer.topic and _model_skip_reason(request, answer) is None:
        # All of these artifacts came from the same report revision and original question.
        prior = ReadingAnswer(text="", **state["verified"])
        citations = {citation.row: citation for citation in (*prior.citations, *answer.citations)}
        checks = [*answer.checks, *(check for check in prior.checks if check not in answer.checks)]
        reading["answer"] = answer.model_copy(update={"citations": list(citations.values()), "checks": checks})
    run.reading = reading
    return {}


async def _explain_evidence(state: TaskState, runtime: Runtime[ReportRun]) -> dict:
    run = runtime.context
    assert run.reading is not None
    reading = run.reading
    request = reading["request"]
    task = request.task
    answer = reading["answer"]
    answered = state["answered"]
    follow_up = answer.context_follow_up
    waiting = task is not None and follow_up is not None and not run.finished_by_user and answered < MAX_REPORT_FOLLOW_UPS
    question = answer.context_question if waiting else ""
    if task is not None and not waiting:
        reading["answer"] = answer = answer.model_copy(update={"context_question": "", "context_follow_up": None})
    if _model_skip_reason(request, answer) is None:
        answer = (await _generate(reading))["answer"]
    answer = _with_work_record(request, answer)
    if task is None:
        run.answer = answer
        return {"pending_field": None, "pending_question": ""}

    if answer.guidance and answer.guidance.care_level == "emergency":
        status, reason = "urgent", "urgent"
    elif answer.direct_answer:
        status, reason = "complete", "evidence_ready"
    elif waiting:
        status, reason = "awaiting_reply", "awaiting_patient"
    elif run.finished_by_user:
        status, reason = "limited", "user_finished"
    elif follow_up is not None:
        status, reason = "limited", "question_limit"
    elif answer.scope == "unresolved" or not answer.citations:
        status, reason = "limited", "evidence_missing"
    elif answer.mode == "fallback":
        status, reason = "limited", "provider_fallback"
    else:
        status, reason = "complete", "evidence_ready"

    gaps: list[str] = []
    if status != "urgent":
        if follow_up is not None and not waiting and follow_up.field in _GAPS:
            gaps.append(_GAPS[follow_up.field])
        relevant = report_context_form([request.findings[item.row] for item in answer.citations]).allowed_fields(request.patient_context)
        for field, text in request.context_replies.items():
            if field in relevant and field in _GAPS and not is_context_reply_detail(text) and _GAPS[field] not in gaps:
                gaps.append(_GAPS[field])
        if status == "complete" and not answer.direct_answer and (gaps or any(check.status != "confirmed" for check in answer.evidence_checks)):
            status, reason = "limited", "evidence_missing"
    run.answer = answer.model_copy(update={"task": ReadingTaskProgress(
        id=task.id, findings_revision=task.findings_revision, answered=answered, status=status, stop_reason=reason,
        answered_field=run.answered_field, resumed=run.resumed, gaps=gaps,
    )})
    return {
        "pending_field": follow_up.field if waiting else None,
        "pending_question": question,
        "verified": answer.model_dump(mode="json", include={"citations", "checks"}),
    }


def _await_patient(state: TaskState) -> dict:
    reply = interrupt({"field": state["pending_field"], "question": state["pending_question"]})
    # A correction to an earlier answer keeps the pending question open.
    corrects_history = state["pending_field"] != "recent_infection" and bool(infection_correction(reply))
    return {"answered": min(MAX_REPORT_FOLLOW_UPS, state["answered"] + (0 if corrects_history else 1))}


def add_reading_task_nodes(workflow: StateGraph) -> None:
    workflow.add_node("check_evidence", _check_evidence)
    workflow.add_node("explain_evidence", _explain_evidence)
    workflow.add_node("await_patient", _await_patient)
    workflow.add_edge("check_evidence", "explain_evidence")
    workflow.add_conditional_edges("explain_evidence", lambda state: "await_patient" if state["pending_field"] else END,
                                   {"await_patient": "await_patient", END: END})
    workflow.add_edge("await_patient", "check_evidence")


def build_reading_task_graph(checkpointer):
    """Compile the former standalone topology for checkpoint compatibility tests."""
    workflow = StateGraph(TaskState, context_schema=ReportRun)
    add_reading_task_nodes(workflow)
    workflow.add_edge(START, "check_evidence")
    return workflow.compile(checkpointer=checkpointer)


async def run_reading_task(graph, request: ReadingPrompt, *, streaming: bool) -> AsyncIterator[dict]:
    task = request.task
    thread_id = f"{REPORT_READING_THREAD_PREFIX}{task.id}" if task else f"{REPORT_PREVIEW_THREAD_PREFIX}{uuid4()}"
    thread = {"thread_id": thread_id}
    reply = request.context_replies.get(request.reply_field, "") if request.reply_field else ""
    resumed = False
    graph_input = {"findings_revision": task.findings_revision if task else 1, "answered": task.answered if task else 0}
    if task and task.checkpoint and request.reply_field:
        config = {"configurable": {**thread, "checkpoint_id": str(task.checkpoint)}}
        saved = await graph.aget_state(config)
        if (saved.next == ("await_patient",) and saved.values.get("pending_field") == request.reply_field
                and saved.values.get("findings_revision") == task.findings_revision):
            resumed = True
            graph_input = Command(resume=reply)
            thread = config["configurable"]
        else:
            # The Java report still holds the original question and replies if a checkpoint has expired.
            replacement = task.model_copy(update={"id": uuid4(), "checkpoint": None})
            request = request.model_copy(update={"task": replacement})
            thread = {"thread_id": f"{REPORT_READING_THREAD_PREFIX}{replacement.id}"}
    if task and not resumed and request.reply_field and not (request.reply_field != "recent_infection" and infection_correction(reply)):
        graph_input["answered"] = min(MAX_REPORT_FOLLOW_UPS, task.answered + 1)
    run = ReportRun(request, streaming, resumed, reply)
    checkpoint = None
    interrupted = False
    modes = ["custom", "checkpoints", "updates"] if streaming else ["checkpoints", "updates"]
    try:
        async with aclosing(graph.astream(graph_input, {"configurable": thread}, context=run, stream_mode=modes,
                                         durability="sync")) as events:
            async for mode, payload in events:
                if mode == "custom":
                    yield payload
                elif mode == "checkpoints" and payload.get("config") is not None:
                    checkpoint = payload["config"]["configurable"]["checkpoint_id"]
                elif mode == "updates" and payload.get("__interrupt__"):
                    interrupted = True
    finally:
        if task is None:
            await graph.checkpointer.adelete_thread(thread_id)

    if run.answer is None or (task is not None and run.answer.task is None):
        raise RuntimeError("Report task did not produce an answer")
    if run.answer.task and run.answer.task.status == "awaiting_reply":
        if checkpoint is None or not interrupted:
            raise RuntimeError("Report task was not checkpointed before pausing")
        task = run.answer.task.model_copy(update={"checkpoint": UUID(checkpoint)})
        run.answer = run.answer.model_copy(update={"task": task})
    yield {"type": "answer", "payload": run.answer.model_dump(mode="json")}
    yield {"type": "finish"}
