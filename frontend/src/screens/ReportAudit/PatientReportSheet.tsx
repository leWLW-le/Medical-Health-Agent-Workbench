import { ArrowRight, FileText, MessageCircle } from 'lucide-react';
import type { ReadingReport } from '../../services/reportReading';

const savedDate = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' });
const linkClass = 'inline-flex min-h-9 items-center text-left text-sm font-medium text-brand-800 underline underline-offset-2 disabled:opacity-50';
const indicatorNames = new Map([
    ['WBC', '白细胞总数'], ['RBC', '红细胞计数'], ['HGB', '血红蛋白'], ['HCT', '红细胞压积'],
    ['LYM#', '淋巴细胞计数'], ['LYM%', '淋巴细胞比例'], ['BAS#', '嗜碱性粒细胞计数'], ['BAS%', '嗜碱性粒细胞比例'],
    ['PLT', '血小板计数'], ['MPV', '平均血小板体积'], ['PDW', '血小板分布宽度'],
    ['ALT', '丙氨酸氨基转移酶'], ['AST', '天冬氨酸氨基转移酶'], ['HBA1C', '糖化血红蛋白'], ['TG', '甘油三酯'],
]);

function displayName(name: string, unit: string) {
    const code = name.trim().toUpperCase();
    const label = code === 'PCT' && unit.trim() === '%' ? '血小板压积' : indicatorNames.get(code);
    return label ? `${label}（${name}）` : name;
}

export default function PatientReportSheet({ report, busy, openSource, onDiscuss, onContinue, onDetails }: {
    report: ReadingReport;
    busy: boolean;
    openSource: (row: number, trigger: HTMLButtonElement) => void;
    onDiscuss: () => void;
    onContinue: () => void;
    onDetails: () => void;
}) {
    const items = report.findings.map((original, index) => {
        const finding = report.corrections[index] ?? original;
        return { index, finding, name: displayName(finding.name, finding.status === 'REVIEW' ? '' : finding.unit) };
    });
    const outside = items.filter(({ finding }) => finding.status === 'HIGH' || finding.status === 'LOW');
    const pending = items.filter(({ finding }) => finding.status === 'REVIEW');
    const compared = items.filter(({ finding }) => ['HIGH', 'LOW', 'NORMAL'].includes(finding.status)).length;
    const latest = report.turns.at(-1)?.answer;
    const current = latest && (latest.task?.findings_revision ?? 1) === (report.findings_revision ?? 1) ? latest : undefined;
    const emergency = current?.guidance?.care_level === 'emergency' ? current.guidance : undefined;
    const promptCare = current?.guidance?.care_level === 'prompt' ? current.guidance : undefined;
    const answer = current?.scope === 'report' ? current : undefined;
    const guidance = answer?.guidance;
    const awaitingReply = Boolean(current?.context_follow_up);
    const observations = answer?.observations ?? [];
    const explanations = guidance?.possibilities.length ? guidance.possibilities : answer?.interpretation ?? [];
    const hasInterpretation = observations.length > 0 || explanations.length > 0;
    const canDiscuss = !emergency && items.length > pending.length;

    return <article aria-labelledby="patient-report-title" className="mx-auto w-full max-w-[816px] space-y-5 px-4 pb-7 pt-2 sm:px-7 sm:pt-4">
        <header className="conversation-intro-surface rounded-2xl border border-brand-100 px-4 py-5 sm:px-6">
            <div className="flex items-start gap-3">
                <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-brand-100 text-brand-800">
                    <FileText size={22} aria-hidden />
                </span>
                <div className="min-w-0">
                    <h2 id="patient-report-title" className="text-2xl font-semibold tracking-tight text-ink-900">本次体检解读单</h2>
                    <p className="mt-1 break-words text-sm text-ink-700">{report.title}</p>
                    <p className="mt-1 text-xs text-ink-600">上传于 {savedDate.format(report.created_at)} · {report.page_count} 页</p>
                </div>
            </div>
            <dl className="mt-4 grid grid-cols-3 gap-2 border-t border-brand-100 pt-3 text-sm text-ink-700">
                <div className="flex flex-col"><dt className="order-2">识别项目</dt><dd className="text-lg font-semibold tabular-nums text-ink-900">{items.length}</dd></div>
                <div className="flex flex-col"><dt className="order-2">超出范围</dt><dd className="text-lg font-semibold tabular-nums text-ink-900">{compared ? outside.length : '—'}</dd></div>
                <div className="flex flex-col"><dt className="order-2">待核对</dt><dd className="text-lg font-semibold tabular-nums text-ink-900">{pending.length}</dd></div>
            </dl>
            <div className="flex flex-wrap items-center gap-3">
                {canDiscuss && !awaitingReply && !guidance && !hasInterpretation && <button type="button" disabled={busy}
                    className="mt-4 inline-flex min-h-10 items-center gap-2 rounded-xl bg-brand-700 px-3.5 text-sm font-semibold text-white hover:bg-brand-800 disabled:opacity-50"
                    onClick={onDiscuss}><MessageCircle size={17} aria-hidden /> 结合我的情况解读 <ArrowRight size={16} aria-hidden /></button>}
                {(awaitingReply || guidance || hasInterpretation || emergency) && <button type="button" className={`${linkClass} mt-3`} onClick={onContinue}>
                    {awaitingReply ? '继续回答助手' : '回到对话'}
                </button>}
            </div>
        </header>

        {emergency && <p role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 font-semibold leading-relaxed text-red-900">
            {emergency.next_step}
        </p>}
        {promptCare && <p className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 font-medium leading-relaxed text-amber-900">
            {promptCare.next_step}
        </p>}

        <section aria-labelledby="patient-report-focus" className="space-y-2.5">
            <h3 id="patient-report-focus" className="text-lg font-semibold text-ink-900">这次有哪些结果值得留意？</h3>
            {outside.length ? <ul className="divide-y divide-ink-200 rounded-xl border border-ink-200 px-4">
                {outside.map(({ finding, index, name }) => <li key={index} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 py-2">
                    <div className="min-w-0 flex-1 basis-44 break-words">
                        <p className="flex flex-wrap items-center gap-x-2 gap-y-0.5 font-semibold text-ink-900">
                            <span className="min-w-0">{name}</span>
                            <span className="whitespace-nowrap rounded-md bg-amber-50 px-1.5 py-0.5 text-xs font-semibold text-amber-900">
                                {finding.status === 'HIGH' ? '偏高' : '偏低'}
                            </span>
                        </p>
                        <p className="mt-1 text-sm tabular-nums text-ink-700">本次 {finding.value} {finding.unit}
                            {finding.reference && ` · 报告参考范围 ${finding.reference}`}</p>
                    </div>
                    <button type="button" disabled={busy} className={linkClass}
                        aria-label={`查看 ${name} 原报告第 ${finding.page} 页`}
                        onClick={event => openSource(index, event.currentTarget)}>原报告第 {finding.page} 页</button>
                </li>)}
            </ul> : <p className="text-sm leading-relaxed text-ink-700">{compared
                ? '能与报告参考范围比较的结果里，暂未发现偏高或偏低的项目。'
                : items.length ? '本次暂无可与参考范围比较的结果。请查看原件中的结果。'
                    : '还没读出可展示的检验项目。可以打开报告明细，用原件重新识别。'}</p>}
            {pending.length > 0 && <details className="rounded-xl border border-ink-200 bg-ink-50 px-4 py-2.5">
                <summary className="cursor-pointer font-medium text-ink-900">有 {pending.length} 项需要对照原件</summary>
                <ul className="mt-1 divide-y divide-ink-200">
                    {pending.map(({ finding, index, name }) => <li key={index} className="py-1">
                        <button type="button" disabled={busy} className={linkClass}
                            onClick={event => openSource(index, event.currentTarget)}>
                            核对 {name} · 第 {finding.page} 页
                        </button>
                    </li>)}
                </ul>
            </details>}
            <button type="button" className={linkClass} onClick={onDetails}>查看全部检验项目</button>
        </section>

        {!emergency && hasInterpretation && <section aria-labelledby="patient-report-reading" className="space-y-2">
            <h3 id="patient-report-reading" className="text-lg font-semibold text-ink-900">这些结果怎么理解？</h3>
            {awaitingReply && <p className="text-sm text-ink-600">助手还有一个问题，回答后可以结合你的情况继续解读。</p>}
            {answer?.mode === 'fallback' && <p className="text-sm text-ink-600">以下内容根据已核实的报告结果和规则整理。</p>}
            {observations.length > 0 ? <ul className="space-y-2.5 text-sm leading-relaxed text-ink-800">
                {observations.map(({ text, rows }, index) => <li key={index}>
                    <p>{text}</p>
                    <div className="flex flex-wrap gap-x-3">
                        {rows.map(row => {
                            const citation = answer?.citations?.find(item => item.row === row);
                            return citation && <button key={row} type="button" disabled={busy} className={linkClass}
                                onClick={event => openSource(row, event.currentTarget)}>
                                {displayName(citation.name, citation.unit)} · 第 {citation.page} 页
                            </button>;
                        })}
                    </div>
                </li>)}
            </ul> : <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-ink-800">
                {explanations.map((text, index) => <li key={index}>{text}</li>)}
            </ul>}
        </section>}

        {!emergency && <section aria-labelledby="patient-report-visit" className="space-y-2 border-t border-ink-200 pt-4">
            <h3 id="patient-report-visit" className="text-lg font-semibold text-ink-900">就诊时可以怎么准备？</h3>
            {guidance?.care_level === 'routine' && <p className="text-sm leading-relaxed text-ink-800">{guidance.next_step}</p>}
            <p className="text-sm leading-relaxed text-ink-700">
                可以带上原报告、以往同项检查结果和用药清单。如果近期有不适，记下从什么时候开始。就诊时可以问医生，哪些项目需要复查，以及应该什么时候复查。
            </p>
        </section>}
    </article>;
}
