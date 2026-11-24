import { healthRequest } from './healthAccount';
import { consumeStreamEvents, dispatchStreamEvent, type StreamEventHandlers } from './streamEvents';
import type { ScanFields, ScanFinding } from './reportAudit';

export interface ReadingSummary {
    id: string;
    title: string;
    source_kind: 'pdf' | 'photo';
    created_at: number;
}

export interface ReadingCitation {
    row: number;
    page: number;
    name: string;
    value: string;
    unit: string;
    reference: string;
    status: ScanFinding['status'];
    related: boolean;
}

export interface ReadingSource {
    title: string;
    url: string;
}

export interface ReadingObservation {
    text: string;
    rows: number[];
    sources: ReadingSource[];
}

export interface ReadingCheck {
    title: string;
    calculation: string;
    conclusion: string;
    rows: number[];
    source_title: string;
    source_url: string;
}

export interface ReadingFollowUp {
    label: string;
    question: string | null;
    row: number | null;
}

export interface ReadingContextFollowUp {
    field: string;
    options: string[];
    max_length: number;
}

export interface ReadingTaskProgress {
    id: string;
    findings_revision: number;
    answered: number;
    status: 'awaiting_reply' | 'complete' | 'limited' | 'urgent';
    stop_reason: 'evidence_ready' | 'awaiting_patient' | 'user_finished' | 'question_limit' | 'evidence_missing'
        | 'provider_fallback' | 'urgent';
    resumed: boolean;
    gaps: string[];
}

export interface ReadingGuidance {
    summary: string;
    possibilities: string[];
    care_level: 'routine' | 'prompt' | 'emergency';
    next_step: string;
    used_context: boolean;
    sources: ReadingSource[];
}

export interface ReadingModelCall {
    phase: 'initial' | 'after_tools' | 'rewrite' | 'timeout_retry' | 'coverage_repair';
    status: 'returned' | 'failed' | 'timeout';
    elapsed_ms: number;
    http_status: number | null;
    rejection: 'invalid_format' | 'unverified_output' | 'missing_groups' | 'missing_medication_evidence' | null;
    missing_groups?: number[][];
}

export interface ReadingToolExecution {
    name: 'lookup_report' | 'search_lab_knowledge' | 'check_lab_relationships' | 'unknown';
    status: 'completed' | 'empty' | 'invalid_request' | 'failed';
    result: string;
}

export interface ReadingWorkRecord {
    request_id: string;
    model_call_count: number;
    model_calls: ReadingModelCall[];
    tool_rounds: ReadingToolExecution[][];
    fallback_reason: 'provider_error' | 'provider_timeout' | 'model_call_timeout' | 'agent_timeout' | 'output_rejected'
        | 'invalid_tool_requests' | 'tool_budget_exhausted' | 'tool_failed' | 'configuration_error' | 'internal_error' | null;
    skip_reason: 'emergency' | 'pending_confirmation' | 'no_evidence' | 'evidence_purpose' | null;
}

export interface ReadingEvidenceCheck {
    indicator: string;
    reason: string;
    action: 'reuse_report_evidence' | 'lookup_report';
    status: 'confirmed' | 'missing' | 'pending_confirmation' | 'ambiguous';
    rows: number[];
    detail: string;
}

export interface ReadingAnswer {
    text: string;
    direct_answer?: string;
    row: number | null;
    page: number | null;
    scope?: 'report' | 'indicator' | 'unresolved';
    mode?: 'evidence' | 'model' | 'fallback';
    model_is_primary?: boolean;
    interpretation?: string[];
    observations?: ReadingObservation[];
    checks?: ReadingCheck[];
    follow_ups?: ReadingFollowUp[];
    guidance?: ReadingGuidance | null;
    overview?: string;
    context_question?: string;
    context_follow_up?: ReadingContextFollowUp | null;
    topic?: { scope: 'report' | 'indicator'; rows: number[] } | null;
    limitation?: string;
    citations?: ReadingCitation[];
    sources?: ReadingSource[];
    work_record?: ReadingWorkRecord | null;
    evidence_checks?: ReadingEvidenceCheck[];
    task?: ReadingTaskProgress | null;
}

export interface ReadingReport extends ReadingSummary {
    revision: number;
    findings_revision?: number;
    page_count: number;
    source_sha256: string;
    findings: ScanFinding[];
    corrections: Record<string, ScanFinding>;
    turns: { question: string; answer: ReadingAnswer; context_field?: string; focus_row?: number }[];
}

const path = '/report-reading';

async function json<T>(suffix: string, options?: RequestInit): Promise<T> {
    return (await healthRequest(path + suffix, options)).json();
}

export type ReadingStreamHandlers = Pick<StreamEventHandlers, 'onChunk' | 'onStep' | 'onStepFinish'>;

async function stream<T>(suffix: string, options: RequestInit, handlers: ReadingStreamHandlers): Promise<T> {
    const response = await healthRequest(path + suffix, { ...options, headers: { Accept: 'text/event-stream' } });
    let result: T | undefined;
    await consumeStreamEvents(response, event => {
        if (event.type === 'answer') result = event.payload as T;
        else if (event.type === 'error') throw new Error(event.content || '报告解读失败，请稍后重试。');
        else if (event.type === 'finish') return true;
        else dispatchStreamEvent(event, handlers);
    });
    if (!result) throw new Error('没有收到完整的报告解读，请稍后重试。');
    return result;
}

function attachment(file: File) {
    const data = new FormData();
    data.append('file', file);
    return data;
}

export const readingApi = {
    list: (offset: number, signal?: AbortSignal) => json<ReadingSummary[]>(`?offset=${offset}`, { signal }),
    get: (id: string) => json<ReadingReport>(`/${id}`),
    upload: (file: File) => json<ReadingReport>('', { method: 'POST', body: attachment(file) }),
    retryOcr: (report: ReadingReport, file: File) => {
        const data = attachment(file);
        data.append('retry', new Blob([JSON.stringify({ revision: report.revision })], { type: 'application/json' }));
        return json<ReadingReport>(`/${report.id}/retry-ocr`, { method: 'POST', body: data });
    },
    page: async (id: string, file: File, page: number, signal: AbortSignal) =>
        (await healthRequest(`${path}/${id}/page?page=${page}`, { method: 'POST', body: attachment(file), signal })).blob(),
    correct: (report: ReadingReport, index: number, fields: ScanFields) =>
        json<ReadingReport>(`/${report.id}/rows/${index}`, {
            method: 'PUT', body: JSON.stringify({ revision: report.revision, fields }),
        }),
    explain: (id: string, index: number, signal: AbortSignal, handlers: ReadingStreamHandlers) =>
        stream<ReadingAnswer>(`/${id}/rows/${index}/explanation/stream`, { signal }, handlers),
    ask: (report: ReadingReport, text: string, row: number | null, replyToContext: boolean,
        signal: AbortSignal, handlers: ReadingStreamHandlers) => stream<ReadingReport>(`/${report.id}/questions/stream`, {
        method: 'POST', body: JSON.stringify({ revision: report.revision, row, text, replyToContext }), signal,
    }, handlers),
};
