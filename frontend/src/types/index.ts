// 00-core-architecture

/** The active conversational context - determines the UI state of the chat */
export type ChatMode = 'general' | 'clinic' | 'insurance' | 'pharmacy' | 'report';

/** Server-side LangGraph conversation id, issued by the backend `session` SSE event */
export type ThreadId = string;

/** Payload the clinic subgraph streams as a `clinic_recommendation` card */
export type ClinicRecommendationData = {
    summary?: string;
    departments?: string[];
    /** UI severity, derived from `urgency` by the backend */
    severity?: 'low' | 'medium' | 'high';
    urgency?: 'emergency' | 'soon' | 'routine';
    notes?: string[];
};

/** One collected fact in the summary card, already rendered to a string by the backend */
export interface ClinicSummaryFact {
    field: string;
    label: string;
    value: string;
}

/**
 * Payload used by saved consultation summaries.
 */
export type ClinicSummaryData = {
    /** `incomplete`: the call ended before a triage conclusion */
    status: 'completed' | 'incomplete';
    consultant?: string;
    /** ISO timestamps */
    started_at?: string;
    ended_at?: string;
    duration_seconds?: number;
    chief_complaint?: string;
    /** Only the filled fields, in display order */
    facts?: ClinicSummaryFact[];
    departments?: string[];
    urgency?: 'emergency' | 'soon' | 'routine' | null;
    summary?: string;
    notes?: string[];
    /** Red flags the emergency gate matched during the call */
    emergency_flags?: string[];
    disclaimer?: string;
};

export interface KnowledgeRetrievalData {
    status: 'used' | 'empty' | 'unavailable';
    queries: string[];
    hits: { source: string; section: string; excerpt: string }[];
}

export interface ReportItemInput {
    name: string;
    value: number;
    unit: string | null;
    reference_range: string | null;
}

export interface WebSearchData {
    status: 'found' | 'empty' | 'unavailable';
    query: string;
    provider: string;
    sources: { title: string; url: string; journal: string; year: string; excerpt: string }[];
}

/** A structured UI card payload that can be embedded in a chat message */
export type ChatCardPayload =
    | { type: 'knowledge_retrieval'; data: KnowledgeRetrievalData }
    | { type: 'web_search'; data: WebSearchData }
    | { type: 'report_ocr_review'; data: { items: ReportItemInput[] } }
    | { type: 'insurance_balance'; data: Record<string, unknown> }
    | { type: 'insurance_expenses'; data: Record<string, unknown> }
    | { type: 'insurance_payments'; data: Record<string, unknown> }
    | { type: 'insurance_cross_region'; data: Record<string, unknown> }
    | { type: 'clinic_recommendation'; data: ClinicRecommendationData }
    | { type: 'clinic_summary'; data: ClinicSummaryData }
    | { type: 'report_analysis'; data: Record<string, unknown> }
    | { type: 'medication_task'; data: Record<string, unknown> }
    | { type: 'hospital_list'; data: Record<string, unknown> }
    | { type: 'sensitive_image_preview'; data: { imageUrl: string; label: string; hint?: string } }
    /** Context transition cards — injected when entering or exiting a ChatMode */
    | { type: 'mode_welcome'; mode: ChatMode; title: string; description: string }
    | { type: 'mode_exit'; mode: ChatMode; title: string; summary?: string };

// 01-ai-chat-root
export type MessageRole = 'user' | 'assistant' | 'system';

/** Represents a single intermediate agent action step during generation */
export interface AgentStep {
    id: string;
    type: 'node_start' | 'node_end' | 'tool_start' | 'tool_end';
    node?: string;
    tool?: string;
    content: string;
    isFinished: boolean;
}

export interface ChatMessage {
    id: string;
    role: MessageRole;
    text: string;
    timestamp: number;
    /** Agent intermediate steps collected during streaming */
    steps?: AgentStep[];
    /** Whether the assistant message is still being generated */
    isGenerating?: boolean;
    /** The agent paused this turn to ask a follow-up */
    awaitingReply?: boolean;
    /** Structured UI cards to render below the text bubble */
    cards?: ChatCardPayload[];
}

// 02-ai-clinic

// 03-insurance

// 04-pharmacy

// 06-medical-services

// 07-report-folder
