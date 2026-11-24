import type { AgentStep, ChatCardPayload, ChatMode, ThreadId } from '../types';
import type { ChatServiceOptions } from './chatService';

/** Maps LangGraph node names to ChatMode strings */
const NODE_TO_CHAT_MODE: Record<string, ChatMode> = {
    clinic_node: 'clinic',
    report_node: 'report',
    pharmacy_node: 'pharmacy',
    // The active agent can change without moving the conversation to another workspace.
    advisor_node: 'general',
};

let _stepCounter = 0;
const genStepId = () => `step-${++_stepCounter}-${Date.now()}`;

/** One decoded SSE event. */
export interface StreamEvent {
    type: string;
    code?: string;
    content?: string;
    node?: string;
    tool?: string;
    thread_id?: string;
    payload?: unknown;
}

/** Consume the single-line JSON SSE frames used by the two Agent gateways. */
export async function consumeStreamEvents(response: Response, onEvent: (event: StreamEvent) => boolean | void) {
    if (!response.body) throw new Error('服务未返回流式内容。');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) throw new Error('答复尚未完成，请稍后重试。');
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() ?? '';
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                let event: StreamEvent;
                try {
                    event = JSON.parse(line.slice(6));
                } catch {
                    continue;
                }
                if (event && typeof event.type === 'string' && onEvent(event)) return;
            }
        }
    } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
    }
}

/** The callbacks used by `dispatchStreamEvent`. */
export type StreamEventHandlers = Omit<ChatServiceOptions, 'onDone' | 'onError'>;

/**
 * Handle events shared by the chat and report SSE endpoints.
 * Returns false for any other type so the caller can handle it.
 */
export const dispatchStreamEvent = (data: StreamEvent, options: StreamEventHandlers): boolean => {
    switch (data.type) {
        case 'session':
            if (options.onSession && data.thread_id) {
                options.onSession(data.thread_id as ThreadId);
            }
            return true;

        case 'interrupt':
            options.onInterrupt?.(data.content ?? '');
            return true;

        case 'text':
            options.onChunk(data.content ?? '');
            return true;

        case 'node_start': {
            const step: AgentStep = {
                id: genStepId(),
                type: 'node_start',
                node: data.node,
                content: data.content ?? `进入节点：${data.node}`,
                isFinished: false,
            };
            options.onStep(step);
            // Automatically switch chatMode based on which agent node started
            console.log('[chatService] node_start:', data.node, '→ mode:', (data.node && NODE_TO_CHAT_MODE[data.node]) ?? '(no mapping)');
            if (data.node && NODE_TO_CHAT_MODE[data.node] && options.onModeChange) {
                console.log('[chatService] calling onModeChange:', NODE_TO_CHAT_MODE[data.node]);
                options.onModeChange(NODE_TO_CHAT_MODE[data.node]);
            }
            return true;
        }

        case 'node_end':
            options.onStepFinish(data.node ?? '');
            return true;

        case 'tool_start': {
            const step: AgentStep = {
                id: genStepId(),
                type: 'tool_start',
                tool: data.tool,
                content: data.content ?? `调用工具：${data.tool}`,
                isFinished: false,
            };
            options.onStep(step);
            return true;
        }

        case 'tool_end':
            options.onStepFinish(data.tool ?? '');
            return true;

        case 'card':
            if (options.onCard && data.payload) {
                options.onCard(data.payload as ChatCardPayload);
            }
            return true;

        default:
            return false;
    }
};
