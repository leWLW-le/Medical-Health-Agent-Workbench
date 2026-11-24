import { ChevronDown, Globe } from 'lucide-react';
import type { WebSearchData } from '../../types';

export default function WebSearchCard({ data }: { data: WebSearchData }) {
    const status = data.status === 'found' ? `${data.sources.length} 条来源`
        : data.status === 'empty' ? '未找到资料' : '搜索暂不可用';
    return (
        <details className="group rounded-xl border border-ink-200 bg-white text-sm">
            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl px-3 py-2.5 text-ink-600 hover:bg-ink-50 [&::-webkit-details-marker]:hidden">
                <Globe size={16} className="shrink-0 text-brand-700" aria-hidden />
                <span className="font-medium text-ink-900">联网搜索结果</span>
                <span className="ml-auto text-right text-xs">{status}</span>
                <ChevronDown size={15} className="shrink-0 group-open:rotate-180" aria-hidden />
            </summary>
            <div className="space-y-3 border-t border-ink-200 p-3">
                <p className="break-words text-xs text-ink-600">{data.provider || '公开医学资料'}{data.query && ` · ${data.query}`}</p>
                {data.sources.length > 0 && (
                    <ol className="space-y-2">
                        {data.sources.map(source => (
                            <li key={source.url} className="rounded-lg bg-ink-50 p-3">
                                <a href={source.url} target="_blank" rel="noreferrer"
                                    className="break-words font-medium text-brand-800 underline underline-offset-2">{source.title}</a>
                                <p className="mt-1 text-xs text-ink-600">{[source.journal, source.year].filter(Boolean).join(' · ')}</p>
                                {source.excerpt && <p className="mt-1 break-words text-ink-700">{source.excerpt}</p>}
                            </li>
                        ))}
                    </ol>
                )}
            </div>
        </details>
    );
}
