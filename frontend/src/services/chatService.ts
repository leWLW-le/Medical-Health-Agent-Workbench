import type { ChatMessage, AgentStep, ChatMode, ChatCardPayload, ReportItemInput, ThreadId } from '../types';
import { consumeStreamEvents, dispatchStreamEvent } from './streamEvents';
import { conversationPath, healthRequest, HealthRequestError } from './healthAccount';
import { accountReportPhoto } from './reportAudit';
import type { ConversationMode } from '../store/chatHistory';

export interface ChatServiceOptions {
    /** Replay recent text when the server no longer has the browser's thread. */
    replayHistory?: boolean;
    reportItems?: ReportItemInput[];
    webSearch?: boolean;
    onChunk: (text: string) => void;
    onStep: (step: AgentStep) => void;
    onStepFinish: (nodeOrTool: string) => void;
    /** Called when LangGraph enters a specialized agent node, or advisor_node (general) to exit a mode */
    onModeChange?: (mode: ChatMode) => void;
    /** Called when a backend tool yields a structured UI card payload */
    onCard?: (card: ChatCardPayload) => void;
    /** Called with the server-side thread id so it can be reused on the next turn */
    onSession?: (threadId: ThreadId) => void;
    /**
     * Called when the graph suspended on an `interrupt()` (a clinic follow-up
     * question). The question itself already arrived as `text` chunks, so the
     * UI only needs this to know the turn ended with a pending question.
     */
    onInterrupt?: (question: string) => void;
    onDone: () => void;
    onError: (error: Error) => void;
}

export interface UserInfoPayload {
    name?: string;
    age?: number;
    medical_history?: string;
    elder_mode?: boolean;
    region?: string;
}

export type VisionScanType = 'report' | 'drug_box' | 'trace_code';
export const visionImageTypes = ['image/png', 'image/jpeg', 'image/webp'];
export const maxVisionImageSize = 8 * 1024 * 1024;

export function isVisionImage(file: File): boolean {
    return visionImageTypes.includes(file.type) && file.size > 0 && file.size <= maxVisionImageSize;
}

type ChatFailure = 'connection' | 'configuration' | 'model' | 'vision';

class ChatServiceError extends Error {
    readonly reason: ChatFailure;

    constructor(reason: ChatFailure) {
        super(reason);
        this.reason = reason;
    }
}

export function describeChatError(error: Error, fallback: string): string {
    if (error instanceof HealthRequestError) return error.message;
    if (!(error instanceof ChatServiceError)) return fallback;
    if (error.reason === 'connection') {
        return '无法连接健康助手服务。请确认本地 Python 后端已启动。也可以打开「报告内容解析」查看报告。';
    }
    if (error.reason === 'configuration') {
        return '健康助手服务已连接，但模型未配置或配置无效。请配置聊天模型。拍照解读还需要可用的视觉模型。';
    }
    if (error.reason === 'vision') return '图片识别服务暂不可用，请稍后重新上传。';
    return '模型服务暂不可用。请检查模型连接。没有外网时，可使用本地模型，或在「报告内容解析」查看参考范围比对。';
}

async function requestChat(path: '/api/chat' | '/api/vision-chat', init: RequestInit, reportPhoto: boolean): Promise<Response> {
    if (reportPhoto && accountReportPhoto) {
        return healthRequest('/report-photo' + path.slice('/api'.length), init);
    }
    const standaloneOrigin = (import.meta.env.VITE_HEALTH_API_URL as string | undefined)?.replace(/\/$/, '')
        || 'http://127.0.0.1:8000';
    const origin = reportPhoto ? '' : standaloneOrigin;
    let response: Response;
    try {
        response = await fetch(origin + path, init);
    } catch (error) {
        if (init.signal?.aborted) throw error;
        throw new ChatServiceError('connection');
    }
    if (!response.ok) {
        const body = await response.json().catch(() => null);
        if (typeof body?.detail === 'string' && body.detail.startsWith('大模型配置错误')) {
            throw new ChatServiceError('configuration');
        }
        if (reportPhoto && response.status >= 500) throw new ChatServiceError('connection');
        throw new Error(`HTTP error ${response.status}`);
    }
    return response;
}

const consumeSseResponse = (response: Response, options: ChatServiceOptions) => consumeStreamEvents(response, event => {
    if (event.type === 'finish') return true;
    if (event.type === 'error') {
        if (event.code === 'MODEL_UNAVAILABLE') throw new ChatServiceError('model');
        if (event.code === 'VISION_UNAVAILABLE') throw new ChatServiceError('vision');
        throw new Error('Chat stream failed.');
    }
    dispatchStreamEvent(event, options);
});

export const streamChat = async (
    messages: ChatMessage[],
    options: ChatServiceOptions,
    userInfo?: UserInfoPayload,
    chatMode: ChatMode = 'general',
    threadId?: ThreadId | null,
    signal?: AbortSignal
) => {
    try {
        // With a thread_id the backend checkpointer owns the history and only
        // needs the newest user message; without one it replays everything.
        const outbound = threadId && !options.replayHistory
            ? messages.slice(-1) : messages.filter(message => message.text.trim() && !message.isGenerating).slice(-10);
        const payload = {
            messages: outbound.map(msg => ({
                role: msg.role,
                content: msg.text
            })),
            user_info: userInfo ?? {},
            chat_mode: chatMode,
            thread_id: threadId ?? null,
            report_items: options.reportItems,
            web_search: options.webSearch,
        };

        const response = await requestChat('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
            signal
        }, chatMode === 'report');

        await consumeSseResponse(response, options);

        options.onDone();

    } catch (err) {
        if (!signal?.aborted) options.onError(err instanceof Error ? err : new Error(String(err)));
    }
};

export async function streamManagedChat(
    text: string, mode: ConversationMode, session: string, elderMode: boolean, options: ChatServiceOptions,
) {
    try {
        const response = await healthRequest(conversationPath(mode, session), {
            method: 'POST',
            body: JSON.stringify({ requestId: crypto.randomUUID(), text, elderMode, webSearch: options.webSearch }),
        });
        await consumeSseResponse(response, options);
        options.onDone();
    } catch (error) {
        if (error instanceof ChatServiceError && error.reason === 'model') {
            options.onError(new HealthRequestError(503, '智能助手暂时无法回复，已保存你的提问，请稍后重试。'));
            return;
        }
        options.onError(error instanceof Error ? error : new Error(String(error)));
    }
}

export async function streamManagedVisionChat(
    file: File, scanType: Exclude<VisionScanType, 'report'>,
    mode: ConversationMode, session: string, elderMode: boolean, options: ChatServiceOptions,
) {
    const form = new FormData();
    form.append('file', file);
    form.append('scanType', scanType);
    form.append('requestId', crypto.randomUUID());
    form.append('elderMode', String(elderMode));
    try {
        const response = await healthRequest(conversationPath(mode, session, 'image'), { method: 'POST', body: form });
        await consumeSseResponse(response, options);
        options.onDone();
    } catch (error) {
        options.onError(error instanceof Error ? error : new Error(String(error)));
    }
}

export const streamVisionChat = async (
    file: File,
    scanType: VisionScanType,
    messages: ChatMessage[],
    options: ChatServiceOptions,
    userInfo?: UserInfoPayload,
    threadId?: ThreadId | null,
    signal?: AbortSignal
) => {
    try {
        const outbound = threadId && !options.replayHistory
            ? messages.slice(-1) : messages.filter(message => message.text.trim() && !message.isGenerating).slice(-10);
        const formData = new FormData();
        if (scanType === 'report' && accountReportPhoto) formData.append('elderMode', String(userInfo?.elder_mode ?? false));
        formData.append('file', file);
        formData.append('scan_type', scanType);
        formData.append('user_info', JSON.stringify(userInfo ?? {}));
        formData.append('thread_id', threadId ?? '');
        formData.append('messages', JSON.stringify(outbound.map(msg => ({
            role: msg.role,
            content: msg.text
        }))));

        const response = await requestChat('/api/vision-chat', {
            method: 'POST',
            body: formData,
            signal
        }, scanType === 'report');

        await consumeSseResponse(response, options);
        options.onDone();

    } catch (err) {
        if (!signal?.aborted) options.onError(err instanceof Error ? err : new Error(String(err)));
    }
};
