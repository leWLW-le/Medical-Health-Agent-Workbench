import type { ReadingAnswer, ReadingCitation, ReadingEvidenceCheck, ReadingSource } from '../../services/reportReading';
import ReadingWorkDetails from './ReadingWorkDetails';

const resultLabels = {
    HIGH: '高于参考范围', LOW: '低于参考范围', NORMAL: '在参考范围内', REPORTED: '定性结果', REVIEW: '待确认',
};
const sourceLink = 'inline-flex min-h-8 items-center text-left text-brand-700 underline underline-offset-2';

type OpenSource = (row: number, button: HTMLButtonElement) => void;

function CitationLinks({ rows, citations, openSource }: { rows: number[]; citations: ReadingCitation[]; openSource: OpenSource }) {
    return <div className="flex flex-wrap gap-x-3">
        {rows.map(row => {
            const citation = citations.find(item => item.row === row);
            return citation && <button type="button" key={row} className={`${sourceLink} text-xs`}
                onClick={event => openSource(row, event.currentTarget)}>
                {citation.name} · 第 {citation.page} 页
            </button>;
        })}
    </div>;
}

function Sources({ sources }: { sources: ReadingSource[] }) {
    return <ul className="flex flex-wrap gap-x-3">
        {sources.map(source => <li key={source.url}>
            <a className={`${sourceLink} text-xs`} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>
        </li>)}
    </ul>;
}

export default function ReadingAnswerView({ answer, openSource, inConversation = false, onContextReply, inlineEvidenceGaps,
    busy = false, outdated = false }: {
    answer: ReadingAnswer;
    openSource: OpenSource;
    inConversation?: boolean;
    onContextReply?: (text: string) => void;
    inlineEvidenceGaps?: ReadingEvidenceCheck[];
    busy?: boolean;
    outdated?: boolean;
}) {
    const citations = answer.citations ?? [];
    const guidance = answer.guidance;
    const emergency = guidance?.care_level === 'emergency';
    const modelIsPrimary = answer.model_is_primary && !emergency;
    const gaps = outdated || answer.direct_answer ? []
        : inlineEvidenceGaps ?? answer.evidence_checks?.filter(check => check.status !== 'confirmed') ?? [];
    return (
        <div className="space-y-2 break-words leading-relaxed text-ink-700">
            {outdated && <p className="text-xs text-ink-600">依据校正前的报告字段</p>}
            {emergency && <p role="alert" className="font-semibold text-red-800">{guidance.next_step}</p>}
            {answer.direct_answer && !emergency && <>
                <p className="whitespace-pre-line">{answer.direct_answer}</p>
                <Sources sources={answer.sources ?? []} />
            </>}
            {!modelIsPrimary && !emergency && answer.overview && <p className="font-semibold text-ink-900">{answer.overview}</p>}
            {answer.mode === 'fallback' && <p role="status" className="text-xs text-ink-600">本次按报告依据和规则解读。</p>}
            {answer.topic && !emergency && answer.observations?.map((item, index) => (
                <div key={index}>
                    <p>{item.text}</p>
                    <CitationLinks rows={item.rows} citations={citations} openSource={openSource} />
                    <Sources sources={item.sources} />
                </div>
            ))}
            {guidance && <div className="space-y-2">
                {!modelIsPrimary && !emergency && <>
                    <p className="font-semibold text-ink-900">{guidance.summary}</p>
                    {guidance.possibilities.length > 0 && <ul aria-label="可能相关的情况" className="list-disc space-y-1 pl-5">
                        {guidance.possibilities.map(item => <li key={item}>{item}</li>)}
                    </ul>}
                </>}
                {!emergency && <p>{guidance.next_step}</p>}
                <Sources sources={guidance.sources} />
            </div>}
            {answer.task?.status === 'limited' && answer.task.gaps.map(text => <p key={text}>{text}</p>)}
            {!emergency && gaps.map(check => <div key={check.indicator}>
                <p>{check.detail}</p>
                <div className="flex flex-wrap gap-x-3">
                    {check.rows.slice(0, 3).map(row => <button key={row} type="button" disabled={busy}
                        className={`${sourceLink} text-sm disabled:opacity-50`}
                        onClick={event => openSource(row, event.currentTarget)}>
                        核对 {check.indicator} · 第 {row + 1} 项
                    </button>)}
                </div>
            </div>)}
            {inConversation && answer.context_follow_up && !emergency && !outdated && <>
                <p>{answer.context_question}</p>
                {onContextReply && <div role="group" aria-label="回复助手的补问" className="flex flex-wrap gap-2">
                    {[...answer.context_follow_up.options, '跳过', ...(answer.task ? ['先给结论'] : [])].map(option => (
                        <button key={option} type="button" disabled={busy} onClick={() => onContextReply(option)}
                            className="min-h-10 rounded-lg border border-brand-200 bg-white px-3 text-brand-800 hover:bg-brand-50 disabled:opacity-50">
                            {option}
                        </button>
                    ))}
                </div>}
            </>}
            {(!answer.direct_answer || citations.length > 0) && <details className="mt-2 rounded-lg border border-ink-200 p-3"
                open={guidance || answer.direct_answer ? undefined : true}>
                <summary className="cursor-pointer font-medium text-brand-800">查看报告依据与计算</summary>
                <div className="mt-2 space-y-2">
                    {answer.text && <p>{answer.text}</p>}
                    {modelIsPrimary && answer.overview && <p>{answer.overview}</p>}
                    {Boolean(answer.checks?.length) && <ul aria-label="指标间的数值核验" className="space-y-2">
                        {answer.checks?.map(check => <li key={check.title} className="rounded-lg border border-ink-200 bg-white p-3">
                            <p className="font-semibold text-ink-900">{check.title}</p>
                            <p className="mt-1 tabular-nums">{check.calculation}</p>
                            <p className="mt-1">{check.conclusion}</p>
                            <CitationLinks rows={check.rows} citations={citations} openSource={openSource} />
                            <Sources sources={[{ title: check.source_title, url: check.source_url }]} />
                        </li>)}
                    </ul>}
                    {Boolean(answer.interpretation?.length) && <div className="space-y-2">
                        <p className="font-semibold text-ink-900">证据解读</p>
                        {answer.interpretation?.map(text => <p key={text}>{text}</p>)}
                    </div>}
                    {!answer.topic && answer.observations?.map((item, index) => <div key={index}>
                        <p>{item.text}</p>
                        <CitationLinks rows={item.rows} citations={citations} openSource={openSource} />
                        <Sources sources={item.sources} />
                    </div>)}
                    {answer.limitation && <p>{answer.limitation}</p>}
                    {citations.length > 0 ? <div className="rounded-lg border border-ink-200 bg-white p-2.5 sm:p-3">
                        <p className="font-semibold text-ink-900">报告依据</p>
                        <ul className="mt-1 divide-y divide-ink-100">
                            {citations.map(citation => <li key={citation.row} className="flex flex-wrap items-center gap-x-2 py-1">
                                <button className={sourceLink} type="button" onClick={event => openSource(citation.row, event.currentTarget)}>
                                    {citation.name} · 原报告第 {citation.page} 页
                                </button>
                                {answer.scope !== 'indicator' || citations.length > 1 ? <span className="text-xs">
                                    {citation.value} {citation.unit}{citation.reference && ` · 参考 ${citation.reference}`} · {resultLabels[citation.status]}
                                </span> : null}
                                {citation.related && <span className="text-xs text-ink-600">关联项</span>}
                            </li>)}
                        </ul>
                    </div> : answer.row !== null && answer.page !== null && (
                        <button type="button" className={sourceLink} onClick={event => openSource(answer.row!, event.currentTarget)}>
                            原报告第 {answer.page} 页
                        </button>
                    )}
                    {!answer.direct_answer && answer.sources && answer.sources.length > 0 && <Sources sources={answer.sources} />}
                    {Boolean(answer.follow_ups?.some(item => item.row !== null)) && (
                        <div className="border-t border-ink-200 pt-3">
                            <p className="font-semibold text-ink-900">核对原报告</p>
                            <ul aria-label="核对原报告" className="mt-2 flex flex-wrap gap-2">
                                {answer.follow_ups?.filter(item => item.row !== null).map(({ label, row }) => (
                                    <li key={label}>
                                        <button type="button" disabled={busy}
                                            className="min-h-10 rounded-lg border border-brand-200 bg-white px-3 py-1.5 text-left text-sm font-medium text-brand-800 hover:bg-brand-50 disabled:opacity-50"
                                            onClick={event => { if (row !== null) openSource(row, event.currentTarget); }}>
                                            {label}
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>
            </details>}
            {answer.work_record && <ReadingWorkDetails record={answer.work_record} citations={citations}
                task={answer.task} evidenceChecks={answer.evidence_checks} />}
        </div>
    );
}
