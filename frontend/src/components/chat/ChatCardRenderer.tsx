import type { FC } from 'react';
import {
    Stethoscope, FileText, Pill, Store, Phone, Info, AlertTriangle,
    ArrowUp, ArrowDown,
} from 'lucide-react';
import type { ChatCardPayload } from '../../types';
import KnowledgeRetrievalCard from './KnowledgeRetrievalCard';
import WebSearchCard from './WebSearchCard';
import SensitiveImagePreview from '../common/SensitiveImagePreview';
import { Badge, Note, ResultCard, SectionLabel } from '../common/ResultCard';
import type { Tone } from '../common/ResultCard';
import { useTextScale } from '../../design/textScale';

// ─── shared sub-types ───────────────────────────────────────────────────────
interface DrugInfo {
    generic_name?: string; brand_names?: string[]; category?: string; indications?: string[];
    dosage?: string; contraindications?: string[]; warnings?: string[]; price_range?: string; otc?: boolean;
}
interface PharmacyRecord {
    name: string; distance: string; hours: string; phone: string; type: string;
}
interface LabFinding {
    name: string; full_name?: string; value: number; unit?: string;
    status: 'HIGH' | 'LOW' | 'NORMAL' | 'UNKNOWN'; reference?: string; clinical_note?: string;
    reference_source?: 'report' | 'builtin' | null;
}

// ─── Clinic Recommendation Card ──────────────────────────────────────────
type Urgency = 'emergency' | 'soon' | 'routine';

const URGENCY_META: Record<Urgency, { label: string; tone: Tone }> = {
    emergency: { label: '请立即就医', tone: 'danger' },
    soon: { label: '建议尽快就诊', tone: 'warning' },
    routine: { label: '可择期就诊', tone: 'brand' },
};

const SEVERITY_TO_URGENCY: Record<string, Urgency> = { high: 'emergency', medium: 'soon', low: 'routine' };

const ClinicRecommendationCard: FC<{ data: Record<string, unknown> }> = ({ data }) => {
    const t = useTextScale();
    const summary = (data.summary as string) ?? '根据您的描述，建议您就诊以下科室。';
    const departments = (data.departments as string[]) ?? [];
    const notes = ((data.notes as string[]) ?? []).filter(Boolean);
    const urgency: Urgency = (data.urgency as Urgency) ?? SEVERITY_TO_URGENCY[(data.severity as string) ?? 'low'] ?? 'routine';
    const meta = URGENCY_META[urgency] ?? URGENCY_META.routine;
    const isEmergency = urgency === 'emergency';

    return (
        <ResultCard
            icon={isEmergency ? AlertTriangle : Stethoscope}
            tone={isEmergency ? 'danger' : 'brand'}
            alert={isEmergency}
            title="分诊建议"
            meta={<Badge tone={meta.tone}>{meta.label}</Badge>}
            dataCard="clinic_recommendation"
        >
            <p className={`text-ink-800 ${t.callout}`}>{summary}</p>
            {departments.length > 0 && (
                <div>
                    <SectionLabel>推荐科室</SectionLabel>
                    <div className="flex flex-wrap gap-2">
                        {departments.map(dept => (
                            <span key={dept} className={`rounded-full bg-brand-700 px-3 py-1 font-semibold text-white ${t.body}`}>{dept}</span>
                        ))}
                    </div>
                </div>
            )}
            {notes.length > 0 && (
                <div>
                    <SectionLabel>就诊前注意</SectionLabel>
                    <ul className={`list-disc pl-5 space-y-0.5 text-ink-700 marker:text-ink-400 ${t.body}`}>
                        {notes.map((note, idx) => <li key={idx}>{note}</li>)}
                    </ul>
                </div>
            )}
            <p className={`text-ink-500 ${t.caption}`}>AI 预问诊结果仅供参考，不能代替医生诊断。</p>
        </ResultCard>
    );
};

// ─── Report Analysis Card ────────────────────────────────────────────────
const FINDING_META: Record<LabFinding['status'], { label: string; tone: Tone; icon?: typeof ArrowUp }> = {
    HIGH: { label: '偏高', tone: 'warning', icon: ArrowUp },
    LOW: { label: '偏低', tone: 'warning', icon: ArrowDown },
    NORMAL: { label: '范围内', tone: 'success' },
    UNKNOWN: { label: '待核对', tone: 'neutral' },
};

const ReportAnalysisCard: FC<{ data: Record<string, unknown> }> = ({ data }) => {
    const t = useTextScale();
    const findings = (data.findings as LabFinding[]) ?? [];
    const abnormalCount = findings.filter(f => f.status === 'HIGH' || f.status === 'LOW').length;
    const normalCount = findings.filter(f => f.status === 'NORMAL').length;
    const unknownCount = findings.length - abnormalCount - normalCount;
    const tone = abnormalCount > 0 ? 'warning' : unknownCount > 0 || !findings.length ? 'neutral' : 'success';
    const statusText = abnormalCount > 0 ? `${abnormalCount} 项超范围`
        : unknownCount > 0 ? `${unknownCount} 项待核对`
        : findings.length > 0 ? '均在范围内' : '未识别项目';
    const counts = [
        normalCount > 0 && `${normalCount} 项在范围内`,
        abnormalCount > 0 && `${abnormalCount} 项超范围`,
        unknownCount > 0 && `${unknownCount} 项待核对`,
    ].filter(Boolean).join(' · ');
    const date = data.date as string | undefined;
    const hospital = data.hospital as string | undefined;
    const disclaimer = data.disclaimer as string | undefined;
    // Keep the report order within each group, with out-of-range rows first.
    const ordered = [...findings].sort((a, b) => Number(b.status === 'HIGH' || b.status === 'LOW') - Number(a.status === 'HIGH' || a.status === 'LOW'));

    return (
        <ResultCard
            icon={FileText}
            tone={tone}
            dataCard="report-analysis"
            title={(data.title as string) ?? '检验报告解读'}
            meta={<Badge tone={tone}>{statusText}</Badge>}
        >
            {(hospital || date) && (
                <div className={`-mt-2 text-ink-600 ${t.caption}`}>{[hospital, date].filter(Boolean).join(' · ')}</div>
            )}
            {counts && <p className={`text-ink-800 ${t.body}`}>{counts}</p>}
            {ordered.length > 0 && (
                <ul className="divide-y divide-ink-100">
                    {ordered.map((f, i) => {
                        const meta = FINDING_META[f.status] ?? FINDING_META.UNKNOWN;
                        const abnormal = f.status === 'HIGH' || f.status === 'LOW';
                        const Arrow = meta.icon;
                        const referenceLabel = f.reference_source === 'report' ? '报告范围'
                            : f.reference_source === 'builtin' ? '通用参考' : '参考范围';
                        return (
                            <li key={`${f.name}-${i}`} className="py-2.5">
                                <div className="flex items-center gap-3">
                                    <div className="flex-1 min-w-0">
                                        <div className={`font-semibold text-ink-900 ${t.body}`}>
                                            {f.full_name || f.name}
                                            {f.full_name && f.full_name !== f.name && <span className="font-normal text-ink-500 whitespace-nowrap"> {f.name}</span>}
                                        </div>
                                        {(f.reference || f.status === 'UNKNOWN') && (
                                            <div className={`break-words text-ink-600 ${t.caption}`}>
                                                {f.reference && f.reference !== '暂无参考范围'
                                                    ? `${referenceLabel} ${f.reference}` : '缺少可比对的参考范围'}
                                            </div>
                                        )}
                                    </div>
                                    <div className={`font-semibold tabular-nums ${abnormal ? 'text-warning-700' : 'text-ink-900'} ${t.body}`}>
                                        {f.value}{f.unit && <span className={`font-normal text-ink-500 ${t.caption}`}> {f.unit}</span>}
                                    </div>
                                    <Badge tone={meta.tone}>{Arrow && <Arrow size={12} />}{meta.label}</Badge>
                                </div>
                                {abnormal && f.clinical_note && <p className={`mt-1 text-ink-700 ${t.caption}`}>{f.clinical_note}</p>}
                                {f.status === 'UNKNOWN' && f.reference_source === 'report' && f.clinical_note &&
                                    <p className={`mt-1 text-ink-700 ${t.caption}`}>{f.clinical_note}</p>}
                            </li>
                        );
                    })}
                </ul>
            )}
            {disclaimer && <p className={`text-ink-500 ${t.caption}`}>{disclaimer}</p>}
        </ResultCard>
    );
};

// ─── Medication Card (药品信息) ───────────────────────────────────────────
const MedicationCard: FC<{ data: Record<string, unknown> }> = ({ data }) => {
    const t = useTextScale();
    const query = (data.query as string) ?? '';
    const drug = data.drug as DrugInfo | undefined;

    if (!data.found || !drug) {
        return (
            <ResultCard icon={Pill} tone="neutral" title={query || '药品查询'} meta={<Badge>未收录</Badge>}>
                <Note icon={Info}>药品库里暂未收录「{query}」，回答中已结合医学知识库说明。用药前请咨询药师或医生。</Note>
            </ResultCard>
        );
    }

    const brands = drug.brand_names?.filter(Boolean) ?? [];
    return (
        <ResultCard
            icon={Pill}
            title={drug.generic_name || query}
            meta={<Badge tone={drug.otc ? 'success' : 'warning'}>{drug.otc ? '非处方药' : '处方药'}</Badge>}
        >
            {(brands.length > 0 || drug.category) && (
                <div className={`-mt-2 text-ink-600 ${t.caption}`}>
                    {[drug.category, brands.length > 0 ? `商品名：${brands.join('、')}` : ''].filter(Boolean).join(' · ')}
                </div>
            )}
            {drug.indications && drug.indications.length > 0 && (
                <div>
                    <SectionLabel>适应症</SectionLabel>
                    <div className="flex flex-wrap gap-1.5">
                        {drug.indications.map(item => <Badge key={item} tone="brand">{item}</Badge>)}
                    </div>
                </div>
            )}
            {drug.dosage && (
                <div>
                    <SectionLabel>用法用量</SectionLabel>
                    <p className={`text-ink-800 ${t.body}`}>{drug.dosage}</p>
                </div>
            )}
            {drug.warnings && drug.warnings.length > 0 && (
                <div className="rounded-control bg-warning-50 px-3 py-2.5">
                    <div className={`mb-1 flex items-center gap-1.5 font-semibold text-warning-800 ${t.caption}`}>
                        <AlertTriangle size={14} /> 注意事项
                    </div>
                    <ul className={`list-disc pl-5 space-y-0.5 text-ink-800 marker:text-warning-500 ${t.body}`}>
                        {drug.warnings.map(item => <li key={item}>{item}</li>)}
                    </ul>
                </div>
            )}
            {drug.contraindications && drug.contraindications.length > 0 && (
                <div>
                    <SectionLabel>禁忌</SectionLabel>
                    <ul className={`list-disc pl-5 space-y-0.5 text-ink-700 marker:text-ink-400 ${t.body}`}>
                        {drug.contraindications.map(item => <li key={item}>{item}</li>)}
                    </ul>
                </div>
            )}
            {drug.price_range && <div className={`text-ink-600 ${t.caption}`}>参考价格 {drug.price_range}</div>}
        </ResultCard>
    );
};

// ─── Nearby Pharmacies Card (附近药店) ────────────────────────────────────
const PharmacyListCard: FC<{ data: Record<string, unknown> }> = ({ data }) => {
    const t = useTextScale();
    const pharmacies = (data.pharmacies as PharmacyRecord[]) ?? [];
    const location = (data.location as string) ?? '';

    return (
        <ResultCard icon={Store} title="附近药店" meta={location ? <span className={`text-ink-600 ${t.caption}`}>{location}</span> : undefined}>
            <ul className="divide-y divide-ink-100">
                {pharmacies.map((p, i) => {
                    const dialable = /\d/.test(p.phone);
                    return (
                        <li key={`${p.name}-${i}`} className="flex items-center gap-3 py-2.5">
                            <div className="flex-1 min-w-0">
                                <div className={`font-semibold text-ink-900 truncate ${t.body}`}>{p.name}</div>
                                <div className={`text-ink-600 truncate ${t.caption}`}>{p.type} · {p.hours}</div>
                            </div>
                            <div className={`shrink-0 font-semibold text-ink-900 tabular-nums ${t.body}`}>{p.distance}</div>
                            {dialable && (
                                <a
                                    href={`tel:${p.phone.replace(/[^\d+]/g, '')}`}
                                    aria-label={`拨打${p.name}电话`}
                                    className="shrink-0 w-9 h-9 rounded-full bg-brand-50 text-brand-700 flex items-center justify-center hover:bg-brand-100 active:scale-[0.94] transition duration-fast"
                                >
                                    <Phone size={16} />
                                </a>
                            )}
                        </li>
                    );
                })}
            </ul>
        </ResultCard>
    );
};

// ─── Card Factory ────────────────────────────────────────────────────────────
const ChatCardRenderer: FC<{ payload: ChatCardPayload }> = ({ payload }) => {
    switch (payload.type) {
        case 'knowledge_retrieval': return <KnowledgeRetrievalCard data={payload.data} />;
        case 'web_search': return <WebSearchCard data={payload.data} />;
        case 'clinic_recommendation': return <ClinicRecommendationCard data={payload.data} />;
        case 'report_analysis': return <ReportAnalysisCard data={payload.data} />;
        case 'medication_task': return <MedicationCard data={payload.data} />;
        case 'hospital_list': return <PharmacyListCard data={payload.data} />;
        case 'sensitive_image_preview': return <SensitiveImagePreview {...payload.data} />;
        default: return null;
    }
};

export default ChatCardRenderer;
