import { useEffect, useEffectEvent, useRef, useState, type ReactNode } from 'react';
import { useGlobalStore } from '../../store/GlobalContext';
import { streamChat, streamManagedChat, streamManagedVisionChat, streamVisionChat, type VisionScanType } from '../../services/chatService';
import { managedHealthChat } from '../../services/healthAccount';
import { appendChatTurn, createUserMessage } from '../../services/chatTurn';
import InputBar from '../chat/InputBar';

const ChatComposer = () => {
    const { isElderMode, chatMode, agentMode, setAgentMode, messages, setMessages, threadId, setThreadId,
        activeConversationId, historyState, refreshConversationList } = useGlobalStore();
    const [inputValue, setInputValue] = useState('');
    const [webSearch, setWebSearch] = useState(false);

    const requestInFlight = useRef(false);
    const isBusy = historyState !== 'ready' || messages.some(message => message.isGenerating);
    const canSearch = chatMode !== 'clinic' && agentMode !== 'clinic';
    const sessionHandlers = { onModeChange: setAgentMode, onSession: setThreadId };

    const handleSend = async (text = inputValue) => {
        const content = text.trim();
        if (!content || isBusy || requestInFlight.current) return;
        requestInFlight.current = true;
        setInputValue('');

        try {
            const userMessage = createUserMessage(content);
            const options = appendChatTurn(setMessages, userMessage, '这次对话未能完成，请重试。', sessionHandlers);
            options.webSearch = canSearch && webSearch;
            setWebSearch(false);
            if (managedHealthChat) {
                await streamManagedChat(content, chatMode, activeConversationId, isElderMode, options);
                await refreshConversationList();
            } else {
                await streamChat([...messages, userMessage], { ...options, replayHistory: true },
                    { elder_mode: isElderMode }, agentMode, threadId);
            }
        } finally {
            requestInFlight.current = false;
        }
    };

    const handleVisionUpload = async (file: File, scanType: Exclude<VisionScanType, 'report'>) => {
        if (isBusy || requestInFlight.current) return;
        requestInFlight.current = true;
        const userMessage = createUserMessage(scanType === 'trace_code'
            ? '上传了一张药品追溯码图片，请帮我查看用药信息。'
            : '上传了一张药盒图片，请帮我看看这个药。');
        const options = appendChatTurn(setMessages, userMessage, '图片识别未能完成，请重试。', sessionHandlers);
        try {
            if (managedHealthChat) {
                await streamManagedVisionChat(file, scanType, chatMode, activeConversationId, isElderMode, options);
                await refreshConversationList();
            } else {
                await streamVisionChat(file, scanType, [...messages, userMessage], { ...options, replayHistory: true },
                    { elder_mode: isElderMode }, threadId);
            }
        } finally {
            requestInFlight.current = false;
        }
    };

    const sendStarter = useEffectEvent((text: string) => {
        void handleSend(text);
    });

    useEffect(() => {
        const onChatSend = (event: Event) => {
            const text = (event as CustomEvent<string>).detail;
            if (text) sendStarter(text);
        };
        window.addEventListener('chat:send', onChatSend);
        return () => window.removeEventListener('chat:send', onChatSend);
    }, []);

    return (
        <ComposerDock>
            <InputBar
                inputValue={inputValue}
                setInputValue={setInputValue}
                onSend={() => handleSend()}
                onVisionUpload={handleVisionUpload}
                isBusy={isBusy}
                webSearch={canSearch ? { enabled: webSearch, onToggle: () => setWebSearch(enabled => !enabled) } : undefined}
            />
        </ComposerDock>
    );
};

export function ComposerDock({ children }: { children: ReactNode }) {
    return <footer className="relative z-10 shrink-0 bg-white px-3 pt-2 pb-[max(env(safe-area-inset-bottom),0.65rem)] sm:px-7 sm:pt-3">
        <div className="mx-auto w-full max-w-[760px]">
            {children}
            <p className="mt-2 text-center text-xs text-ink-500">AI 生成内容仅供参考</p>
        </div>
    </footer>;
}

export default ChatComposer;
