import { useEffect, useState, type ReactNode } from 'react';
import { History, MessageSquare, PanelLeft, PanelLeftClose, SquarePen, X, type LucideIcon } from 'lucide-react';

const iconButton = 'grid h-9 w-9 shrink-0 place-items-center rounded-lg text-ink-600 hover:bg-ink-100 hover:text-brand-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600';

interface ConversationHistory {
    title: string;
    items: { id: string; title: string }[];
    activeId?: string | null;
    loading?: boolean;
    creating?: boolean;
    disabled?: boolean;
    emptyText: string;
    error?: string;
    itemIcon?: LucideIcon;
    onCreate: () => void;
    onOpen: (id: string) => void;
    more?: ReactNode;
}

export default function ConversationFrame({ navigation, history, children }: {
    navigation: ReactNode;
    history?: ConversationHistory;
    children: ReactNode;
}) {
    const [desktopCollapsed, setDesktopCollapsed] = useState(false);
    const [drawerOpen, setDrawerOpen] = useState(false);
    const ItemIcon = history?.itemIcon ?? MessageSquare;

    useEffect(() => {
        if (!drawerOpen) return;
        function dismiss(event: KeyboardEvent) {
            if (event.key === 'Escape') setDrawerOpen(false);
        }
        window.addEventListener('keydown', dismiss);
        return () => window.removeEventListener('keydown', dismiss);
    }, [drawerOpen]);

    return <div className="mx-auto h-full w-full max-w-[1680px] sm:p-3 lg:px-5 lg:py-4">
        <div className="workspace-surface relative flex h-full min-w-0 overflow-hidden bg-white sm:rounded-[22px] sm:border sm:border-ink-200/80">
            {history && drawerOpen && <button type="button" aria-label="关闭历史对话抽屉"
                className="absolute inset-0 z-30 bg-ink-900/30 lg:hidden" onClick={() => setDrawerOpen(false)} />}
            {history && <aside aria-label={history.title} id="consultation-sidebar"
                className={`workspace-rail absolute inset-y-0 left-0 z-40 w-[min(280px,85vw)] shrink-0 flex-col border-r border-ink-200/80 px-3 py-4 shadow-floating lg:static lg:z-auto lg:w-[254px] lg:px-4 lg:shadow-none
                    ${drawerOpen ? 'flex' : 'hidden'} ${desktopCollapsed ? 'lg:hidden' : 'lg:flex'}`}>
                <div className="flex items-center gap-1.5">
                    <button type="button" disabled={history.disabled || history.creating || history.loading}
                        onClick={() => { setDrawerOpen(false); history.onCreate(); }}
                        className="flex min-h-11 flex-1 items-center gap-2 rounded-xl border border-brand-200 bg-white px-3 text-left text-sm font-semibold text-brand-800 shadow-sm transition-colors hover:bg-brand-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600 disabled:opacity-50">
                        <SquarePen size={17} aria-hidden /> {history.creating ? '正在创建…' : '新对话'}
                    </button>
                    <button type="button" title="收起侧栏" aria-label="收起侧栏" onClick={() => setDesktopCollapsed(true)}
                        className={`hidden lg:grid ${iconButton}`}><PanelLeftClose size={17} aria-hidden /></button>
                    <button type="button" aria-label="关闭历史对话" onClick={() => setDrawerOpen(false)}
                        className={`${iconButton} lg:hidden`}><X size={18} aria-hidden /></button>
                </div>
                {history.error && <p className="mt-3 px-1 text-xs text-red-700" role="alert">{history.error}</p>}
                <h2 className="mb-2 mt-7 px-2 text-xs font-medium text-ink-500">{history.title}</h2>
                <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
                    {history.items.length ? <ul className="space-y-0.5">
                        {history.items.map(item => <li key={item.id}>
                            <button type="button" title={item.title} aria-current={item.id === history.activeId ? 'true' : undefined}
                                disabled={history.disabled} onClick={() => { setDrawerOpen(false); history.onOpen(item.id); }}
                                className={`flex min-h-10 w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600 disabled:opacity-60
                                    ${item.id === history.activeId ? 'bg-brand-100 font-medium text-brand-900' : 'text-ink-700 hover:bg-ink-100'}`}>
                                <ItemIcon size={15} className="shrink-0 opacity-75" aria-hidden />
                                <span className="min-w-0 truncate">{item.title}</span>
                            </button>
                        </li>)}
                    </ul> : <div className="mx-auto mt-7 flex flex-col items-center gap-2 text-sm text-ink-500">
                        <History size={25} strokeWidth={1.5} className="text-ink-400" aria-hidden />
                        <p>{history.loading ? '正在载入…' : history.emptyText}</p>
                    </div>}
                    {history.more}
                </div>
            </aside>}
            <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex shrink-0 items-center gap-1 border-b border-ink-200 bg-white px-2 sm:px-4">
                    {history && <>
                        <button type="button" aria-label="展开历史对话" aria-expanded={drawerOpen} aria-controls="consultation-sidebar"
                            onClick={() => setDrawerOpen(true)} className={`${iconButton} lg:hidden`}><PanelLeft size={18} aria-hidden /></button>
                        {desktopCollapsed && <button type="button" aria-label="展开侧栏" onClick={() => setDesktopCollapsed(false)}
                            className={`hidden lg:grid ${iconButton}`}><PanelLeft size={18} aria-hidden /></button>}
                    </>}
                    {navigation}
                </div>
                {children}
            </div>
        </div>
    </div>;
}
