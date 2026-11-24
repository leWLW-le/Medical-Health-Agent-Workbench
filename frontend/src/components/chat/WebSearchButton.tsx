import { Globe } from 'lucide-react';

export default function WebSearchButton({ enabled, onToggle, disabled }: {
    enabled: boolean;
    onToggle: () => void;
    disabled?: boolean;
}) {
    return (
        <button type="button" aria-pressed={enabled} disabled={disabled} onClick={onToggle}
            title="为下一条消息检索公开医学资料"
            className={`inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-lg px-2 text-xs font-medium disabled:opacity-50
                ${enabled ? 'bg-brand-100 text-brand-800 ring-1 ring-inset ring-brand-300' : 'text-ink-700 hover:bg-brand-50'}`}>
            <Globe size={16} aria-hidden /> 联网搜索
        </button>
    );
}
