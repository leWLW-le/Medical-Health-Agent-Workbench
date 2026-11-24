import type { ReactNode } from 'react';
import { ArrowUpRight, Bot } from 'lucide-react';
import { useGlobalStore } from '../../store/GlobalContext';

export default function ConversationPane({ title, actions, status, children }: {
    title?: string;
    actions?: ReactNode;
    status?: ReactNode;
    children: ReactNode;
}) {
    const { isElderMode } = useGlobalStore();
    return <section aria-labelledby="chat-heading" className="flex min-h-0 flex-1 flex-col">
        <header className="flex min-h-10 shrink-0 items-center justify-between gap-2 px-4 pt-1.5 sm:px-6">
            <h1 id="chat-heading" className={`font-medium text-ink-600 ${isElderMode ? 'text-title' : 'text-sm'}`}>{title}</h1>
            {actions}
        </header>
        {status}
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-y-contain">{children}</div>
    </section>;
}

export function ConversationIntro({ heading, description, questions = [], onQuestion }: {
    heading: string;
    description: string;
    questions?: string[];
    onQuestion?: (question: string) => void;
}) {
    const { isElderMode } = useGlobalStore();
    return <div className="mx-auto flex min-h-full w-full max-w-[816px] flex-col justify-center px-4 py-7 sm:px-7 sm:pb-14">
        <div className="conversation-intro-surface rounded-[24px] border border-ink-200/75 p-4 sm:p-7">
            <div className="flex items-start gap-3 sm:gap-4">
                <span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-brand-100 text-brand-700 sm:h-14 sm:w-14">
                    <Bot size={27} strokeWidth={1.7} aria-hidden />
                </span>
                <div>
                    <h2 className={`font-semibold tracking-tight text-ink-900 ${isElderMode ? 'text-2xl sm:text-3xl' : 'text-xl sm:text-[27px] sm:leading-tight'}`}>{heading}</h2>
                    <p className={`mt-1 text-ink-600 ${isElderMode ? 'text-title' : 'text-sm sm:text-base'}`}>{description}</p>
                </div>
            </div>
            {questions.length > 0 && <ul className="mt-4 flex flex-wrap gap-2 sm:ml-[72px] sm:mt-5">
                {questions.map(question => <li key={question}>
                    <button type="button" onClick={() => onQuestion?.(question)}
                        className="flex min-h-9 items-center gap-2 rounded-xl border border-ink-200 bg-white px-3 py-1.5 text-left text-xs text-ink-700 transition-colors hover:border-brand-300 hover:bg-brand-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600 sm:text-sm">
                        {question}<ArrowUpRight size={13} className="shrink-0 text-ink-500" aria-hidden />
                    </button>
                </li>)}
            </ul>}
        </div>
    </div>;
}
