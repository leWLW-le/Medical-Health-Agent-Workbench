import { GitCompareArrows } from 'lucide-react';
import type { LabCode, ReportChange, ReviewTask, SavedReportAnswer } from '../../services/reportAudit';

const fieldNames: Record<ReportChange['field'], string> = {
    summary: '核对概览', presence: '报告项目', value: '数值', unit: '单位',
    collected_on: '采样日期', evidence: '原始记录', issues: '核对结论',
};
const issueNames: Record<string, string> = {
    date_mismatch: '采样日期不一致', unit_mismatch: '单位不一致',
    value_mismatch: '数值不一致', missing_evidence: '缺少原始记录',
};

function displayValue(field: ReportChange['field'], value: unknown) {
    if (value === null || value === undefined) return '无记录';
    if (Array.isArray(value)) return value.map(item => issueNames[String(item)] ?? String(item)).join('、') || '字段一致';
    if (typeof value === 'object') {
        const row = value as Record<string, unknown>;
        const observation = field === 'evidence' && typeof row.id === 'string' ? `[${row.id} v${row.revision}] ` : '';
        return `${observation}${row.collected_on} · ${row.value} ${row.unit}`;
    }
    return String(value);
}

interface Props {
    reviews: ReviewTask[];
    answers: SavedReportAnswer[];
    names: Partial<Record<LabCode, string>>;
    pending: boolean;
    onRecheck: (task: ReviewTask, answer: SavedReportAnswer) => void;
    onComplete: (task: ReviewTask) => void;
    onVersion: (version: number) => void;
}

export default function ReportImpactPanel({ reviews, answers, names, pending, onRecheck, onComplete, onVersion }: Props) {
    if (!reviews.length) {
        return <div className="rounded-xl border border-dashed border-ink-200 px-4 py-7 text-center text-sm text-ink-600">
            <GitCompareArrows size={24} className="mx-auto mb-2 text-brand-700" aria-hidden />
            <p className="font-medium text-ink-800">暂无受影响的答复</p>
            <p className="mx-auto mt-1 max-w-xs">发布更正后，引用了变更字段的历史答复会在这里生成复核任务。</p>
        </div>;
    }

    const byId = new Map(answers.map(answer => [answer.answer_id, answer]));
    const ordered = [...reviews].sort((a, b) => Number(!!a.reviewed_at) - Number(!!b.reviewed_at));
    return <div className="space-y-3">
        {ordered.map(task => {
            const saved = byId.get(task.answer_id);
            if (!saved) return null;
            return <article key={`${task.answer_id}-${task.target_version}`}
                className="rounded-xl border border-ink-200 bg-white p-3.5 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-semibold">{saved.question}</p>
                    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${task.reviewed_at ? 'bg-brand-50 text-brand-800' : 'bg-amber-50 text-amber-900'}`}>
                        {task.reviewed_at ? '已复核' : '待复核'}
                    </span>
                </div>
                <p className="mt-2 text-xs text-ink-600">答复依据 v{saved.answer.version} · 更正 v{task.from_version} → v{task.target_version}</p>
                <details className="mt-2 text-xs text-ink-700">
                    <summary className="cursor-pointer font-medium text-brand-800">展开原答复</summary>
                    <p className="mt-1.5 leading-relaxed">{saved.answer.text}</p>
                </details>
                <dl className="mt-3 divide-y divide-ink-200 rounded-lg bg-ink-50 px-2.5">
                    {task.changes.map(change => <div key={change.key} className="py-2 text-xs">
                        <dt className="font-semibold text-ink-800">
                            {change.code && (names[change.code] ?? change.code) + ' · '}{fieldNames[change.field]}
                        </dt>
                        <dd className="mt-1 grid grid-cols-2 gap-2 break-words text-ink-700">
                            <span><span className="text-ink-600">更正前 </span>{displayValue(change.field, change.before)}</span>
                            <span><span className="text-ink-600">更正后 </span>{displayValue(change.field, change.after)}</span>
                        </dd>
                    </div>)}
                </dl>
                <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" className="rounded-lg px-2 py-2 text-xs font-medium text-brand-800 hover:bg-brand-50 disabled:opacity-50"
                        disabled={pending} onClick={() => onVersion(saved.answer.version)}>查看答复依据 v{saved.answer.version}</button>
                    <button type="button" className="rounded-lg px-2 py-2 text-xs font-medium text-brand-800 hover:bg-brand-50 disabled:opacity-50"
                        disabled={pending} onClick={() => onVersion(task.target_version)}>查看 v{task.target_version} 报告</button>
                    {!task.reviewed_at && <>
                        <button type="button" className="rounded-lg border border-ink-200 px-2.5 py-2 text-xs font-medium hover:bg-ink-50 disabled:opacity-50"
                            disabled={pending} onClick={() => onRecheck(task, saved)}>按更正版本重新提问</button>
                        <button type="button" className="rounded-lg bg-brand-700 px-2.5 py-2 text-xs font-medium text-white hover:bg-brand-800 disabled:opacity-50"
                            disabled={pending} onClick={() => onComplete(task)}>标记已复核</button>
                    </>}
                </div>
            </article>;
        })}
    </div>;
}
