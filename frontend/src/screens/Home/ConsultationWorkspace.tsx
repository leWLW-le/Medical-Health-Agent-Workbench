import { Fragment, useState, type ReactNode } from 'react';
import { FileText } from 'lucide-react';
import ChatComposer from '../../components/layout/ChatComposer';
import ConversationFrame from '../../components/layout/ConversationFrame';
import { MODULES } from '../../data/mockData';
import { useGlobalStore } from '../../store/GlobalContext';
import HomeScreen from './HomeScreen';

const workspaceTabs = [
    ...MODULES,
    { id: 'reports' as const, name: '报告内容解析', icon: FileText },
];

export default function ConsultationWorkspace({ report, reportHref }: {
    report?: (navigation: ReactNode) => ReactNode;
    reportHref: string;
}) {
    const { chatMode, enterChatMode, isElderMode, activeConversationId, history, historyState, startConversation, openConversation } = useGlobalStore();
    const [creating, setCreating] = useState(false);
    const [error, setError] = useState('');

    async function createChat() {
        if (creating) return;
        setCreating(true);
        setError('');
        try {
            await startConversation();
            requestAnimationFrame(() => document.getElementById('consultation-message')?.focus());
        } catch (problem) {
            setError(problem instanceof Error ? problem.message : '无法开始新对话，请稍后重试。');
        } finally {
            setCreating(false);
        }
    }

    const navigation = <nav aria-label="咨询模式" className="flex min-w-0 flex-1 gap-1 overflow-x-auto py-2 scrollbar-hide">
        {workspaceTabs.map(({ id, name, icon: Icon }) => {
            const active = id === 'reports' ? Boolean(report) : !report && chatMode === id;
            const tabClass = `flex min-h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border px-2.5 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brand-600 sm:px-3
                ${isElderMode ? 'text-callout' : 'text-sm'}
                ${active ? 'border-brand-100 bg-brand-50 font-semibold text-brand-800' : 'border-transparent text-ink-600 hover:bg-ink-50 hover:text-ink-900'}`;
            const label = <><Icon size={15} className="hidden shrink-0 sm:block" aria-hidden /> {name}</>;
            if (id === 'reports') return <a key={id} href={reportHref} aria-current={active ? 'page' : undefined} className={tabClass}>{label}</a>;
            return <button type="button" key={id} onClick={() => {
                setError('');
                enterChatMode(id);
                if (report) window.location.hash = '';
            }} aria-pressed={active} className={tabClass}>{label}</button>;
        })}
    </nav>;

    if (report) return report(navigation);
    return (
        <ConversationFrame navigation={navigation} history={{
            title: '历史对话', items: history, activeId: activeConversationId, emptyText: '暂无历史对话',
            creating, loading: historyState === 'loading', error, onCreate: () => void createChat(),
            onOpen: id => void openConversation(id),
        }}>
            <Fragment key={chatMode + '/' + activeConversationId}>
                <HomeScreen />
                <ChatComposer />
            </Fragment>
        </ConversationFrame>
    );
}
