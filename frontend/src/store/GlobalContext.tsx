import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { ChatMode, ChatMessage, ThreadId } from '../types';
import { conversationModes, defaultConversationId, emptyConversations, fromHealthHistory, loadConversations, loadWorkspace, newConversation, saveChangedConversations, saveSelectedConversation, saveWorkspace, selectedConversation } from './chatHistory';
import type { Conversation, ConversationMode } from './chatHistory';
import { conversationPath, createHealthConversation, getHealthHistory, healthRequest, listHealthConversations, managedHealthChat, type HealthConversationSummary } from '../services/healthAccount';

type HistoryState = 'loading' | 'ready' | 'busy' | 'error';
const initialSelection = { general: defaultConversationId, clinic: defaultConversationId, pharmacy: defaultConversationId };
const conversationKey = (mode: ConversationMode, id: string) => mode + '/' + id;

interface GlobalState {
    isElderMode: boolean;
    setIsElderMode: (val: boolean | ((prev: boolean) => boolean)) => void;
    chatMode: ConversationMode;
    enterChatMode: (mode: ConversationMode) => void;
    clearConversation: () => Promise<void>;
    activeConversationId: string;
    history: HealthConversationSummary[];
    startConversation: () => Promise<void>;
    openConversation: (id: string) => Promise<void>;
    refreshConversationList: () => Promise<void>;
    historyState: HistoryState;
    reloadHistory: () => Promise<void>;
    agentMode: ChatMode;
    setAgentMode: (mode: ChatMode) => void;
    messages: ChatMessage[];
    setMessages: (msgs: ChatMessage[] | ((prev: ChatMessage[]) => ChatMessage[])) => void;
    threadId: ThreadId | null;
    setThreadId: (id: ThreadId | null) => void;
}

const GlobalContext = createContext<GlobalState | undefined>(undefined);

export const GlobalProvider = ({ children, serverAccount }: { children: ReactNode; serverAccount?: string }) => {
    const [isElderMode, setIsElderMode] = useState(false);
    const [workspace, selectWorkspace] = useState(loadWorkspace);
    const enterChatMode = useCallback((mode: ConversationMode) => {
        selectWorkspace(mode);
        saveWorkspace(mode);
    }, []);
    const [conversations, setConversations] = useState(managedHealthChat ? emptyConversations : loadConversations);
    const [selection, setSelection] = useState(() => {
        if (managedHealthChat) return initialSelection;
        return Object.fromEntries(conversationModes.map(mode => {
            const stored = selectedConversation(mode) ?? defaultConversationId;
            return [mode, conversations[mode][stored] ? stored : defaultConversationId];
        })) as Record<ConversationMode, string>;
    });
    const [historyStates, setHistoryStates] = useState<Record<string, HistoryState>>({});
    const [serverLists, setServerLists] = useState<Record<ConversationMode, HealthConversationSummary[]>>({ general: [], clinic: [], pharmacy: [] });
    const previousConversations = useRef(conversations);

    useEffect(() => {
        if (managedHealthChat) return;
        saveChangedConversations(conversations, previousConversations.current);
        previousConversations.current = conversations;
    }, [conversations]);

    const activeConversationId = selection[workspace];
    const key = conversationKey(workspace, activeConversationId);
    const { agentMode, messages, threadId } = conversations[workspace][activeConversationId] ?? newConversation(workspace);
    const updateConversation = useCallback((mode: ConversationMode, id: string, update: (current: Conversation) => Conversation) => {
        setConversations(previous => ({
            ...previous,
            [mode]: { ...previous[mode], [id]: update(previous[mode][id] ?? newConversation(mode)) },
        }));
    }, []);
    const selectConversation = useCallback((mode: ConversationMode, id: string) => {
        setSelection(previous => ({ ...previous, [mode]: id }));
        saveSelectedConversation(mode, id, serverAccount);
    }, [serverAccount]);
    const setHistoryState = useCallback((mode: ConversationMode, id: string, state: HistoryState) => {
        setHistoryStates(previous => ({ ...previous, [conversationKey(mode, id)]: state }));
    }, []);
    const fetchHistory = useCallback(async (mode: ConversationMode, id: string, signal?: AbortSignal) => {
        try {
            const history = await getHealthHistory(mode, id, signal);
            if (signal?.aborted) return;
            updateConversation(mode, id, previous => previous.messages.some(message => message.isGenerating)
                ? previous : fromHealthHistory(mode, history));
            setHistoryState(mode, id, history.busy ? 'busy' : 'ready');
        } catch {
            if (!signal?.aborted) setHistoryState(mode, id, 'error');
        }
    }, [setHistoryState, updateConversation]);

    const refreshList = useCallback(async (mode: ConversationMode, signal?: AbortSignal) => {
        const list = await listHealthConversations(mode, signal);
        if (!signal?.aborted) setServerLists(previous => ({ ...previous, [mode]: list }));
        return list;
    }, []);

    useEffect(() => {
        if (!serverAccount) return;
        const controller = new AbortController();
        void Promise.all(conversationModes.map(async mode => {
            try {
                const list = await refreshList(mode, controller.signal);
                if (controller.signal.aborted) return;
                const saved = selectedConversation(mode, serverAccount);
                const id = saved && list.some(chat => chat.id === saved) ? saved : list[0]?.id ?? defaultConversationId;
                selectConversation(mode, id);
                await fetchHistory(mode, id, controller.signal);
            } catch {
                if (!controller.signal.aborted) setHistoryState(mode, defaultConversationId, 'error');
            }
        }));
        return () => controller.abort();
    }, [fetchHistory, refreshList, selectConversation, serverAccount, setHistoryState]);

    const history = serverAccount ? serverLists[workspace] : Object.entries(conversations[workspace])
        .flatMap(([id, chat]) => {
            const firstQuestion = chat.messages.find(message => message.role === 'user');
            if (!firstQuestion) return [];
            return [{ id, title: firstQuestion.text.slice(0, 80), updatedAt: chat.messages.at(-1)?.timestamp ?? firstQuestion.timestamp }];
        }).sort((a, b) => b.updatedAt - a.updatedAt);

    const openConversation = async (id: string) => {
        selectConversation(workspace, id);
        if (serverAccount && !conversations[workspace][id]?.messages.some(message => message.isGenerating)) {
            setHistoryState(workspace, id, 'loading');
            await fetchHistory(workspace, id);
        }
    };

    const startConversation = async () => {
        if (!messages.some(message => message.role === 'user') && historyStates[key] !== 'error') return;
        const id = serverAccount ? await createHealthConversation(workspace) : crypto.randomUUID();
        updateConversation(workspace, id, () => newConversation(workspace));
        setHistoryState(workspace, id, 'ready');
        selectConversation(workspace, id);
    };

    const reloadHistory = async () => {
        if (!serverAccount || messages.some(message => message.isGenerating)) return;
        setHistoryState(workspace, activeConversationId, 'loading');
        await Promise.all([fetchHistory(workspace, activeConversationId), refreshList(workspace).catch(() => {})]);
    };

    // A stream stays bound to its original conversation while the user navigates.
    const setMessages = useCallback<GlobalState['setMessages']>(update => {
        updateConversation(workspace, activeConversationId, current => ({
            ...current, messages: typeof update === 'function' ? update(current.messages) : update,
        }));
    }, [workspace, activeConversationId, updateConversation]);
    const setThreadId = useCallback((id: ThreadId | null) => {
        updateConversation(workspace, activeConversationId, current => ({ ...current, threadId: id }));
    }, [workspace, activeConversationId, updateConversation]);
    const setAgentMode = useCallback((mode: ChatMode) => {
        updateConversation(workspace, activeConversationId, current => ({ ...current, agentMode: mode }));
    }, [workspace, activeConversationId, updateConversation]);
    const clearConversation = async () => {
        if (serverAccount) {
            setHistoryState(workspace, activeConversationId, 'loading');
            try {
                await healthRequest(conversationPath(workspace, activeConversationId), { method: 'DELETE' });
            } finally {
                setHistoryState(workspace, activeConversationId, 'ready');
            }
            setServerLists(previous => ({ ...previous, [workspace]: previous[workspace].filter(chat => chat.id !== activeConversationId) }));
        }
        updateConversation(workspace, activeConversationId, () => newConversation(workspace));
        if (activeConversationId !== defaultConversationId && serverAccount) {
            const next = history.find(chat => chat.id !== activeConversationId);
            await openConversation(next?.id ?? defaultConversationId);
        }
    };

    return (
        <GlobalContext.Provider value={{
            isElderMode, setIsElderMode, chatMode: workspace, enterChatMode, clearConversation,
            activeConversationId, history, startConversation, openConversation,
            historyState: serverAccount ? historyStates[key] ?? 'loading' : 'ready', reloadHistory,
            refreshConversationList: async () => { if (serverAccount) await refreshList(workspace).catch(() => {}); },
            agentMode, setAgentMode, messages, setMessages, threadId, setThreadId,
        }}>
            {children}
        </GlobalContext.Provider>
    );
};

// eslint-disable-next-line react-refresh/only-export-components
export const useGlobalStore = () => {
    const context = useContext(GlobalContext);
    if (context === undefined) {
        throw new Error('useGlobalStore must be used within a GlobalProvider');
    }
    return context;
};
