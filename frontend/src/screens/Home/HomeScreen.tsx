import { Ellipsis } from 'lucide-react';
import { useMemo, useState } from 'react';
import { ChatTranscript } from '../../components/chat/GlobalChatView';
import ConversationPane, { ConversationIntro } from '../../components/layout/ConversationPane';
import { MODULES } from '../../data/mockData';
import { managedHealthChat } from '../../services/healthAccount';
import { useGlobalStore } from '../../store/GlobalContext';
import type { ConversationMode } from '../../store/chatHistory';

const starters: Record<ConversationMode, { heading: string; description: string; questions: string[] }> = {
    general: {
        heading: '你好，今天想聊些什么？',
        description: '整理日常健康记录，了解常见健康问题。',
        questions: ['高血压日常管理有哪些建议？', '就诊前可以准备哪些资料？', '体检报告里常见的缩写怎么看？'],
    },
    clinic: {
        heading: '我们来整理一下你的症状',
        description: '按症状、持续时间和变化整理就诊信息。',
        questions: ['如何描述症状出现的时间和变化？', '帮我列一份就诊前要告诉医生的信息'],
    },
    pharmacy: {
        heading: '想了解哪种药品？',
        description: '查询药品说明书，了解用法和注意事项。',
        questions: ['怎么看药品说明书里的注意事项？', '药品的商品名和通用名有什么区别？', '如何整理一份正在服用的药品清单？'],
    },
};

export default function HomeScreen() {
    const { chatMode: mode, messages, clearConversation, historyState, reloadHistory } = useGlobalStore();
    const [clearing, setClearing] = useState(false);
    const [clearError, setClearError] = useState('');
    const workspace = MODULES.find(module => module.id === mode);
    const intro = starters[mode];
    const conversation = useMemo(() => messages.filter(message => message.id !== `${mode}-welcome`), [messages, mode]);
    const generating = conversation.some(message => message.isGenerating);
    const ready = historyState === 'ready' && !clearing;

    async function clear() {
        if (!window.confirm('删除当前对话？')) return;
        setClearing(true);
        setClearError('');
        try {
            await clearConversation();
        } catch (error) {
            setClearError(error instanceof Error ? error.message : '删除失败，请重试。');
        } finally {
            setClearing(false);
        }
    }

    return (
        <ConversationPane title={workspace?.name} actions={(managedHealthChat || conversation.length > 0) && (
            <details className="relative z-20">
                <summary className="flex min-h-8 cursor-pointer list-none items-center gap-1.5 rounded-lg px-2 text-xs text-ink-600 hover:bg-ink-50 [&::-webkit-details-marker]:hidden">
                    <Ellipsis size={16} aria-hidden /> 对话记录
                </summary>
                <div className="absolute right-0 z-20 mt-1 min-w-40 rounded-xl border border-ink-200 bg-white p-1.5 text-sm shadow-floating">
                    <p className="px-2 py-1.5 text-xs text-ink-600">{managedHealthChat ? '文字记录保存在账号' : '文字记录保存在本浏览器'}</p>
                    {managedHealthChat && <button type="button" disabled={clearing || generating || historyState === 'loading'}
                        className="block w-full rounded-lg px-2 py-2 text-left hover:bg-ink-50 disabled:opacity-40"
                        onClick={() => void reloadHistory()}>刷新记录</button>}
                    {conversation.length > 0 && <button type="button" disabled={!ready || generating}
                        className="block w-full rounded-lg px-2 py-2 text-left text-red-700 hover:bg-red-50 disabled:opacity-40"
                        onClick={() => void clear()}>{managedHealthChat ? '删除对话记录' : '清除本机记录'}</button>}
                </div>
            </details>
        )} status={(historyState !== 'ready' || clearError) && (
                <div role="status" className="flex shrink-0 items-center gap-2 border-b border-ink-200 px-4 py-2 text-sm text-ink-700 sm:px-6">
                    <span>{clearError || (historyState === 'loading' ? '正在同步记录…' : historyState === 'busy'
                        ? '此模块已有对话正在生成。' : '读取历史记录失败。')}</span>
                    {(historyState === 'busy' || historyState === 'error') && <button type="button"
                        className="font-medium text-brand-800 underline" onClick={() => void reloadHistory()}>刷新</button>}
                </div>
            )}>
            {conversation.length > 0 ? (
                <div className="mx-auto max-w-[816px] px-4 py-4 sm:px-7 sm:py-6"><ChatTranscript messages={conversation} /></div>
            ) : ready && (
                <ConversationIntro {...intro} onQuestion={question => window.dispatchEvent(new CustomEvent('chat:send', { detail: question }))} />
            )}
        </ConversationPane>
    );
}
