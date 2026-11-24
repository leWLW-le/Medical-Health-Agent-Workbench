import { ChevronDown, Search } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { KnowledgeRetrievalData } from '../../types';

const sourceLabels: Record<string, string> = {
    medical_knowledge: '健康知识',
    lab_reference: '检验参考资料',
    pharmacy_knowledge: '药学知识',
};

export default function KnowledgeRetrievalCard({ data }: { data: KnowledgeRetrievalData }) {
    const { hits, queries, status } = data;
    const result = status === 'used' ? `${hits.length} 条资料 · 已加入回答上下文`
        : status === 'empty' ? '未找到可用资料' : '检索暂不可用';

    return (
        <details className="group rounded-xl border border-ink-200 bg-white text-sm">
            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-xl px-3 py-2.5 text-ink-600 hover:bg-ink-50 [&::-webkit-details-marker]:hidden">
                <Search size={16} className="shrink-0 text-brand-700" aria-hidden />
                <span className="shrink-0 font-medium text-ink-900">知识库检索</span>
                <span className="ml-auto text-right text-xs">{result}</span>
                <ChevronDown size={15} className="shrink-0 transition-transform group-open:rotate-180 motion-reduce:transition-none" aria-hidden />
            </summary>
            <div className="space-y-3 border-t border-ink-200 px-3 pb-3 pt-2.5">
                <code className="break-all text-xs text-ink-600">search_health_knowledge</code>
                <div>
                    <p className="text-xs font-medium text-ink-700">查询词</p>
                    {queries.length > 0 ? (
                        <ul className="mt-1 space-y-1">
                            {queries.map(query => <li key={query} className="break-words text-ink-800">{query}</li>)}
                        </ul>
                    ) : <p className="mt-1 text-ink-600">报告查询内容已隐藏。</p>}
                </div>
                {hits.length > 0 && (
                    <ol aria-label="知识库原文片段" className="space-y-2">
                        {hits.map((hit, index) => (
                            <li key={index} className="min-w-0 rounded-lg bg-ink-50 p-3">
                                <p className="break-words font-medium text-ink-900">{hit.section || sourceLabels[hit.source] || hit.source}</p>
                                <p className="mt-0.5 break-words text-xs text-ink-600">
                                    {sourceLabels[hit.source] ?? hit.source}{hit.source && ` · ${hit.source}.md`}
                                </p>
                                <div className="mt-2 overflow-x-auto break-words text-ink-800 [&_p+p]:mt-1.5 [&_td]:border [&_td]:border-ink-200 [&_td]:p-1 [&_th]:border [&_th]:border-ink-200 [&_th]:p-1">
                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{hit.excerpt}</ReactMarkdown>
                                </div>
                            </li>
                        ))}
                    </ol>
                )}
            </div>
        </details>
    );
}
