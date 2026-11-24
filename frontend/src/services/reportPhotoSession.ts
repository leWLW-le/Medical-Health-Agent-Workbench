import type { ChatMessage, ThreadId } from '../types';
import { healthRequest, HealthRequestError } from './healthAccount';
import { accountReportPhoto } from './reportAudit';

const key = 'health.report-photo.thread';
const endpoint = '/api/report-photo/session';

export function readReportPhotoThread(username?: string): ThreadId | null {
    try {
        const id = sessionStorage.getItem(username ? `${key}:${username}` : key);
        return id && /^[0-9a-f]{32}$/.test(id) ? id : null;
    } catch {
        return null;
    }
}

export function rememberReportPhotoThread(id: ThreadId | null, username?: string) {
    try {
        const storageKey = username ? `${key}:${username}` : key;
        if (id) sessionStorage.setItem(storageKey, id);
        else sessionStorage.removeItem(storageKey);
    } catch {
        // The live conversation can continue if browser storage is disabled.
    }
}

export async function readReportPhotoHistory(id: ThreadId, signal: AbortSignal): Promise<ChatMessage[] | null> {
    const options = { headers: { 'X-Report-Thread': id }, cache: 'no-store', signal } as const;
    let response: Response;
    try {
        response = await (accountReportPhoto ? healthRequest('/report-photo/session', options) : fetch(endpoint, options));
    } catch (error) {
        if (error instanceof HealthRequestError && error.status === 404) return null;
        throw error;
    }
    if (response.status === 404) return null;
    if (!response.ok) throw new Error('无法读取报告解读记录。');
    const payload: { messages: ChatMessage[] } = await response.json();
    return payload.messages;
}

export async function deleteReportPhotoHistory(id: ThreadId, signal: AbortSignal) {
    const options = { method: 'DELETE', headers: { 'X-Report-Thread': id }, signal };
    const response = await (accountReportPhoto ? healthRequest('/report-photo/session', options) : fetch(endpoint, options));
    if (!response.ok) throw new Error('无法清空报告解读记录，请重试。');
}
