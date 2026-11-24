import { healthSessionExpired, managedHealthChat } from './healthAccount';

export type LabCode = 'HGB' | 'WBC' | 'PLT' | 'GLU' | 'ALT' | 'RBC' | 'HCT' | 'MCV' | 'AST' | 'CREA';

export interface LabRow {
    code: LabCode;
    value: string;
    unit: string;
    collected_on: string;
}

export interface Draft {
    patient_id: string;
    encounter_id: string;
    rows: LabRow[];
}

export interface Observation extends LabRow {
    id: string;
    revision: number;
    patient_id: string;
    encounter_id: string;
}

export type Issue = 'date_mismatch' | 'unit_mismatch' | 'value_mismatch' | 'missing_evidence' | 'ambiguous_evidence';

export interface Finding {
    row: LabRow;
    evidence: Observation | null;
    issues: Issue[];
}

export interface Snapshot {
    case_id: string;
    version: number;
    draft: Draft;
    findings: Finding[];
    checked_rows: number;
    reused_rows: number;
    rules_version?: number;
}

export interface Answer {
    version: number;
    text: string;
    focus: LabCode | null;
    records: Observation[];
    knowledge: { source: string; section: string; excerpt: string }[];
}

export interface SavedReportAnswer {
    answer_id: string;
    question: string;
    answer: Answer;
}

export interface ReportChange {
    key: string;
    code: LabCode | null;
    field: 'summary' | 'presence' | 'value' | 'unit' | 'collected_on' | 'evidence' | 'issues';
    before: unknown;
    after: unknown;
}

export interface ReviewTask {
    answer_id: string;
    from_version: number;
    target_version: number;
    reviewed_at: number | null;
    changes: ReportChange[];
}

export interface ReportActivity {
    answers: SavedReportAnswer[];
    reviews: ReviewTask[];
}

export interface AuditJob {
    job_id: string;
    case_id: string;
    version: number;
}

export interface AuditJobStatus extends AuditJob {
    status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED';
    attempts: number;
    error_code: string | null;
}

export const usesApplication = import.meta.env.VITE_AUDIT_ASYNC !== '0';
export const accountReportPhoto = managedHealthChat || usesApplication;

const base = usesApplication ? '/api/health/report-audit'
    : (import.meta.env.VITE_AUDIT_API_URL as string | undefined) || 'http://127.0.0.1:8000/api/report-audit';

export type ScanStatus = 'HIGH' | 'LOW' | 'NORMAL' | 'REPORTED' | 'REVIEW';
export type ScanReviewReason = 'low_confidence' | 'value_missing' | 'value_format' | 'unit_missing'
    | 'shifted_columns' | 'reference_missing' | 'reference_format' | 'unit_format';

export interface ScanFinding {
    page: number;
    name: string;
    value: string;
    unit: string;
    reference: string;
    status: ScanStatus;
    region?: [number, number, number, number] | null;
    review_reasons?: ScanReviewReason[];
}

export type ScanFields = Pick<ScanFinding, 'name' | 'value' | 'unit' | 'reference'>;

export interface ScanPreview {
    scan_id?: string;
    source_sha256?: string;
    pages: { page: number; lines: { text: string; uncertain: boolean }[] }[];
    analysis?: {
        findings: ScanFinding[];
        notes: { page: number; name: string; text: string }[];
        source_audit: Pick<Snapshot, 'findings' | 'checked_rows' | 'reused_rows'> | null;
    };
}

export type SavedDraft = { report: Draft } | { text: string } | { scan: ScanPreview };

export class AuditRequestError extends Error {
    readonly code: string | null;
    constructor(message: string, code: string | null) {
        super(message);
        this.code = code;
    }
}

async function auditResponse(path: string, init?: RequestInit): Promise<Response> {
    const headers = new Headers(init?.headers);
    if (usesApplication && init?.method && init.method !== 'GET') headers.set('X-Health-Request', '1');
    const response = await fetch(base + path, { ...init, headers, cache: 'no-store' });
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 401 && usesApplication) window.dispatchEvent(new Event(healthSessionExpired));
        throw new AuditRequestError(
            typeof data.detail === 'string' ? data.detail : '输入格式有误，请核对检验数值、单位和日期',
            typeof data.code === 'string' ? data.code : null,
        );
    }
    return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await auditResponse(path, init);
    if (response.status === 204) return undefined as T;
    return response.json().catch(() => ({}));
}

function post<T>(path: string, payload: unknown, key?: string) {
    return request<T>(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
        body: JSON.stringify(payload),
    });
}

function uploadFile<T>(path: string, file: File) {
    const form = new FormData();
    form.append('file', file);
    return request<T>(path, { method: 'POST', body: form });
}

export const auditApi = {
    sample: () => request<{ text: string; lab_names: Record<LabCode, string>; rules_version?: number }>('/demo'),
    parse: (text: string) => post<Draft>('/parse', { text }),
    upload: (file: File, save = false) => uploadFile<Draft>(usesApplication && save ? '/upload?save=true' : '/upload', file),
    scan: (file: File) => uploadFile<ScanPreview>('/scan', file),
    scanPage: async (file: File, page: number, fingerprint: string | undefined, signal: AbortSignal) => {
        const query = new URLSearchParams({ page: String(page) });
        if (fingerprint) query.set('expected_sha256', fingerprint);
        const body = new FormData();
        body.append('file', file);
        const response = await auditResponse(`/scan/page?${query}`, { method: 'POST', body, signal });
        return response.blob();
    },
    compareScanRow: (fields: ScanFields) => post<{ status: ScanStatus }>('/scan/compare', fields),
    draft: () => request<SavedDraft | null>('/draft'),
    saveDraft: (text: string) => request<void>('/draft', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
    }),
    clearDraft: () => request<void>('/draft', { method: 'DELETE' }),
    history: (offset = 0) => request<AuditJobStatus[]>(`/jobs?offset=${offset}`),
    create: (draft: Draft, key?: string) => post<Snapshot | AuditJob[]>('/cases', draft, key),
    batch: (drafts: Draft[], key: string) => post<AuditJob[]>('/batches', { drafts }, key),
    job: (id: string) => request<AuditJobStatus>(`/jobs/${id}`),
    retry: (id: string) => post<AuditJobStatus>(`/jobs/${id}/retry`, {}),
    get: (id: string, version?: number) =>
        request<Snapshot>(`/cases/${id}${version ? `?version=${version}` : ''}`),
    revise: (snapshot: Snapshot, draft: Draft, key?: string) =>
        post<Snapshot | AuditJob[]>(`/cases/${snapshot.case_id}/revisions`, { expected_version: snapshot.version, draft }, key),
    chat: (snapshot: Pick<Snapshot, 'case_id' | 'version'>, text: string, focus: LabCode | null) =>
        post<Answer>(`/cases/${snapshot.case_id}/chat`, { version: snapshot.version, text, focus }),
    activity: (id: string) => request<ReportActivity>(`/cases/${id}/activity`),
    completeReview: (id: string, task: ReviewTask) =>
        post<void>(`/cases/${id}/reviews/${task.answer_id}/${task.target_version}/complete`, {}),
};
