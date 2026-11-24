import type { ChatMessage, ChatMode, ThreadId } from '../types';
import { USER_NAME } from '../data/mockData';
import { managedHealthChat, type HealthHistory } from '../services/healthAccount';

export type ConversationMode = 'general' | 'clinic' | 'pharmacy';

export interface Conversation {
    messages: ChatMessage[];
    threadId: ThreadId | null;
    agentMode: ChatMode;
}

export const conversationModes: ConversationMode[] = ['general', 'clinic', 'pharmacy'];
export const defaultConversationId = 'default';
export type Conversations = Record<ConversationMode, Record<string, Conversation>>;
const prefix = 'health-assistant.conversation.v1.';
const welcome = {
    general: managedHealthChat ? '你好，今天有什么我可以帮你的吗？' : `你好，${USER_NAME}！今天有什么我可以帮你的吗？`,
    clinic: '请描述您主要哪里不舒服、持续了多久，以及严重程度。',
    pharmacy: managedHealthChat ? '请告诉我药品名称。可以问我说明书、注意事项和药物相互作用。'
        : '请告诉我药品名称或上传药盒照片。可以问我说明书、注意事项和药物相互作用。',
};

export function loadWorkspace(): ConversationMode {
    try {
        const saved = sessionStorage.getItem(prefix + 'workspace');
        if (saved === 'clinic' || saved === 'pharmacy') return saved;
    } catch {
        // Browser storage is optional.
    }
    return 'general';
}

export function saveWorkspace(mode: ConversationMode): void {
    try {
        sessionStorage.setItem(prefix + 'workspace', mode);
    } catch {
        // Navigation still works when storage is blocked.
    }
}

export function newConversation(mode: ConversationMode): Conversation {
    return {
        messages: [{ id: `${mode}-welcome`, role: 'assistant', text: welcome[mode], timestamp: Date.now() }],
        threadId: null,
        agentMode: mode,
    };
}

function isTextMessage(value: unknown): value is ChatMessage {
    if (typeof value !== 'object' || value === null) return false;
    const message = value as Partial<ChatMessage>;
    return ['user', 'assistant'].includes(message.role ?? '') && typeof message.id === 'string'
        && typeof message.timestamp === 'number' && typeof message.text === 'string';
}

function storageKey(mode: ConversationMode, id: string): string {
    return prefix + mode + (id === defaultConversationId ? '' : '.' + id);
}

export function selectedConversation(mode: ConversationMode, account?: string): string | null {
    try {
        return sessionStorage.getItem(prefix + 'selected.' + (account ? 'user.' + account : 'local') + '.' + mode);
    } catch {
        return null;
    }
}

export function saveSelectedConversation(mode: ConversationMode, id: string, account?: string): void {
    try {
        sessionStorage.setItem(prefix + 'selected.' + (account ? 'user.' + account : 'local') + '.' + mode, id);
    } catch {
        // The selected conversation still works in this tab.
    }
}

function loadConversation(mode: ConversationMode, id: string): Conversation {
    try {
        const stored = JSON.parse(localStorage.getItem(storageKey(mode, id)) ?? 'null');
        if (stored && Array.isArray(stored.messages) && stored.messages.length > 0) {
            const messages = stored.messages.filter(isTextMessage);
            if (messages.length) {
                const agentMode = ['general', 'clinic', 'pharmacy', 'report'].includes(stored.agentMode)
                    ? stored.agentMode as ChatMode : mode;
                return {
                    messages,
                    agentMode,
                    threadId: typeof stored.threadId === 'string' ? stored.threadId : null,
                };
            }
        }
    } catch {
        // A blocked or unreadable browser store still allows an in-memory conversation.
    }
    return newConversation(mode);
}

export function loadConversations(): Conversations {
    const chats = emptyConversations();
    for (const mode of conversationModes) chats[mode][defaultConversationId] = loadConversation(mode, defaultConversationId);
    try {
        for (const key of Object.keys(localStorage)) {
            for (const mode of conversationModes) {
                const start = prefix + mode + '.';
                if (!key.startsWith(start)) continue;
                const id = key.slice(start.length);
                if (/^[0-9a-f-]{36}$/.test(id)) chats[mode][id] = loadConversation(mode, id);
            }
        }
    } catch {
        // A browser without storage starts with empty conversations.
    }
    return chats;
}

export function emptyConversations(): Conversations {
    return {
        general: { [defaultConversationId]: newConversation('general') },
        clinic: { [defaultConversationId]: newConversation('clinic') },
        pharmacy: { [defaultConversationId]: newConversation('pharmacy') },
    };
}

export function fromHealthHistory(mode: ConversationMode, history: HealthHistory): Conversation {
    const initial = newConversation(mode);
    for (const turn of history.turns) {
        initial.messages.push({ id: turn.id + '-user', role: 'user', timestamp: turn.createdAt, text: turn.question });
        if (turn.answer) {
            initial.messages.push({
                id: turn.id + '-assistant', role: 'assistant', timestamp: turn.createdAt,
                text: turn.answer, awaitingReply: turn.awaitingReply,
            });
        }
    }
    return initial;
}

export function saveChangedConversations(
    current: Conversations,
    previous: Conversations,
): void {
    for (const mode of conversationModes) {
        if (current[mode] === previous[mode]) continue;
        const ids = new Set([...Object.keys(current[mode]), ...Object.keys(previous[mode])]);
        for (const id of ids) {
            const conversation = current[mode][id];
            if (conversation === previous[mode][id] || conversation?.messages.some(message => message.isGenerating)) continue;
            persistConversation(mode, id, conversation);
        }
    }
}

function persistConversation(mode: ConversationMode, id: string, conversation?: Conversation): void {
    if (conversation?.messages.length && conversation.messages.some(message => message.id !== `${mode}-welcome`)) {
        const { messages, threadId, agentMode } = conversation;
        const history = messages.filter(message => message.text.trim() && !message.isGenerating)
            .map(({ id, role, text, timestamp, awaitingReply }) => ({ id, role, text, timestamp, awaitingReply }));
        try {
            localStorage.setItem(storageKey(mode, id), JSON.stringify({ messages: history, threadId, agentMode }));
        } catch {
            // A full or blocked browser store must not interrupt the active conversation.
        }
        return;
    }
    try {
        localStorage.removeItem(storageKey(mode, id));
    } catch {
        // An unavailable browser store does not affect the current session.
    }
}
