import type { ConversationMode } from '../store/chatHistory';

export const managedHealthChat = import.meta.env.VITE_MANAGED_HEALTH_CHAT !== '0';
export const healthSessionExpired = 'health:session-expired';

export interface HealthAccount {
    username: string;
}

interface HealthTurn {
    id: string;
    question: string;
    answer: string | null;
    awaitingReply: boolean;
    createdAt: number;
}

export interface HealthHistory {
    turns: HealthTurn[];
    busy: boolean;
}

export interface HealthConversationSummary {
    id: string;
    title: string;
    updatedAt: number;
}

export class HealthRequestError extends Error {
    readonly status: number;
    constructor(status: number, message: string) {
        super(message);
        this.status = status;
    }
}

export async function healthRequest(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (init.method && init.method !== 'GET') headers.set('X-Health-Request', '1');
    if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
        headers.set('Content-Type', 'application/json');
    }
    let response: Response;
    try {
        response = await fetch('/api/health' + path, { ...init, headers, cache: 'no-store' });
    } catch (error) {
        if (init.signal?.aborted) throw error;
        throw new HealthRequestError(0, '无法连接账号服务，请稍后重试。');
    }
    if (response.ok) return response;
    if (response.status === 401 && path !== '/login' && path !== '/register' && path !== '/me') {
        window.dispatchEvent(new Event(healthSessionExpired));
    }
    const problem = await response.json().catch(() => null);
    const message = response.status >= 500 && !problem?.code
        ? '账号或对话服务暂时不可用，请稍后重试。'
        : problem?.detail || '请求失败，请稍后重试。';
    throw new HealthRequestError(response.status, message);
}

export async function getHealthAccount(signal?: AbortSignal): Promise<HealthAccount | null> {
    try {
        const response = await healthRequest('/me', { signal });
        return response.json();
    } catch (error) {
        if (error instanceof HealthRequestError && error.status === 401) return null;
        throw error;
    }
}

export async function signInHealthAccount(register: boolean, username: string, password: string): Promise<HealthAccount> {
    const response = await healthRequest(register ? '/register' : '/login', {
        method: 'POST', body: JSON.stringify({ username, password }),
    });
    return response.json();
}

export function conversationPath(mode: ConversationMode, id: string, kind: 'text' | 'image' = 'text'): string {
    return '/chat/' + mode + (kind === 'image' ? '/image' : '') + '?conversation=' + encodeURIComponent(id);
}

export async function getHealthHistory(mode: ConversationMode, id: string, signal?: AbortSignal): Promise<HealthHistory> {
    const response = await healthRequest(conversationPath(mode, id), { signal });
    return response.json();
}

export async function listHealthConversations(mode: ConversationMode, signal?: AbortSignal): Promise<HealthConversationSummary[]> {
    return (await healthRequest('/conversations/' + mode, { signal })).json();
}

export async function createHealthConversation(mode: ConversationMode): Promise<string> {
    const data: { id: string } = await (await healthRequest('/conversations/' + mode, { method: 'POST' })).json();
    return data.id;
}
