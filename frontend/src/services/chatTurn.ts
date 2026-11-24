import type { Dispatch, SetStateAction } from 'react';
import type { ChatMessage } from '../types';
import { describeChatError, type ChatServiceOptions } from './chatService';

let nextMessageId = 0;
const messagePrefix = Date.now().toString(36);

export function createUserMessage(text: string): ChatMessage {
    return { id: `user-turn-${messagePrefix}-${++nextMessageId}`, role: 'user', text, timestamp: Date.now() };
}

export function appendChatTurn(
    setMessages: Dispatch<SetStateAction<ChatMessage[]>>,
    userMessage: ChatMessage,
    errorMessage: string,
    handlers: Pick<ChatServiceOptions, 'onModeChange' | 'onSession'>,
): ChatServiceOptions {
    const assistant: ChatMessage = {
        id: `assistant-turn-${messagePrefix}-${++nextMessageId}`, role: 'assistant', text: '',
        timestamp: Date.now(), steps: [], isGenerating: true,
    };

    setMessages(previous => [
        ...previous.map(message => message.awaitingReply ? { ...message, awaitingReply: false } : message),
        userMessage,
        assistant,
    ]);

    const update = (change: (message: ChatMessage) => ChatMessage) => {
        setMessages(previous => previous.map(message => message.id === assistant.id ? change(message) : message));
    };

    return {
        ...handlers,
        onChunk: text => update(message => ({ ...message, text: message.text + text })),
        onStep: step => update(message => ({ ...message, steps: [...(message.steps ?? []), step] })),
        onStepFinish: id => update(message => ({
            ...message,
            steps: message.steps?.map(step => step.node === id || step.tool === id ? { ...step, isFinished: true } : step),
        })),
        onInterrupt: () => update(message => ({ ...message, awaitingReply: true })),
        onCard: card => update(message => ({ ...message, cards: [...(message.cards ?? []), card] })),
        onDone: () => update(message => ({ ...message, isGenerating: false })),
        onError: error => {
            const detail = describeChatError(error, errorMessage);
            update(message => ({
                ...message, text: message.text ? `${message.text}\n\n${detail}` : detail,
                isGenerating: false, awaitingReply: false,
            }));
        },
    };
}
