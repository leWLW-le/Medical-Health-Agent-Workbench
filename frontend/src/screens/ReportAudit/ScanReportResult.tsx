import { useEffect, useId, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref } from 'react';
import { createPortal } from 'react-dom';
import { auditApi, type Issue, type LabCode, type ScanFields, type ScanFinding, type ScanPreview, type ScanReviewReason, type ScanStatus } from '../../services/reportAudit';

const statusLabels: Record<ScanStatus, string> = {
    HIGH: '高于参考范围',
    LOW: '低于参考范围',
    NORMAL: '在参考范围内',
    REPORTED: '原报告结果',
    REVIEW: '待确认',
};

const sortOrder: Record<ScanStatus, number> = { HIGH: 0, LOW: 0, REVIEW: 1, REPORTED: 2, NORMAL: 3 };
const reasonLabels: Record<ScanReviewReason, string> = {
    low_confidence: '识别置信度较低', value_missing: '结果未读出', value_format: '结果格式无法识别',
    unit_missing: '单位未读出', shifted_columns: '单位疑似错列',
    reference_missing: '参考范围缺失', reference_format: '参考范围格式无法比对',
    unit_format: '单位疑似识别错误',
};
type Filter = 'all' | 'outside' | 'review';
interface ReaderMode {
    sourceKind: 'pdf' | 'photo';
    corrections: Record<string, ScanFinding>;
    loadPage: (file: File, page: number, signal: AbortSignal) => Promise<Blob>;
    correct: (index: number, fields: ScanFields) => Promise<void>;
    explain: (index: number) => void;
}
const editableFields = [
    ['name', '检验项目', 100], ['value', '结果', 100], ['unit', '单位', 100], ['reference', '报告参考值', 240],
] as const;

type PageRequest = { controller: AbortController; url?: string };
type PageResult = { image: string; error: string };

function releasePage(request: PageRequest | null) {
    request?.controller.abort();
    if (request?.url) URL.revokeObjectURL(request.url);
}

function ScanPagePreview({ finding, image, error, sourceLabel }: {
    finding: ScanFinding;
    sourceLabel: string;
} & PageResult) {
    const [zoomed, setZoomed] = useState(false);
    const viewport = useRef<HTMLDivElement>(null);
    const marker = useRef<HTMLDivElement>(null);
    const { page, region } = finding;

    if (error) return <p role="alert" className="text-sm text-red-800">{error}</p>;
    if (!image) return <p role="status" className="text-sm text-ink-600">正在打开{sourceLabel}第 {page} 页…</p>;
    const [left, top, right, bottom] = region ?? [0, 0, 0, 0];
    return (
        <figure className="space-y-1 text-xs text-ink-600">
            <figcaption className="flex flex-wrap items-center justify-between gap-1">
                <span>{sourceLabel} · 第 {page} 页{region && ' · 高亮为识别到的原行'}</span>
                <button type="button" className="text-brand-700 underline" aria-pressed={zoomed}
                    onClick={() => setZoomed(!zoomed)}>{zoomed ? '适应宽度' : '放大原页'}</button>
            </figcaption>
            <div ref={viewport} className="max-h-[55dvh] w-full overflow-auto rounded-lg border border-ink-200 bg-white">
                <div className={zoomed ? 'relative min-w-[48rem]' : 'relative'}>
                    <img className="block w-full" alt={`${sourceLabel} 第 ${page} 页`} src={image} onLoad={() => {
                        if (!viewport.current || !marker.current) return;
                        viewport.current.scrollTop = marker.current.offsetTop - viewport.current.clientHeight / 3;
                        viewport.current.scrollLeft = marker.current.offsetLeft - 12;
                    }} />
                    {region && <div ref={marker} aria-hidden="true" className="pointer-events-none absolute border-2 border-brand-700 bg-brand-300/[.15]"
                        style={{ left: `${left * 100}%`, top: `${top * 100}%`, width: `${(right - left) * 100}%`, height: `${(bottom - top) * 100}%` }} />}
                </div>
            </div>
        </figure>
    );
}

export interface ScanReportSource {
    showSource: (index: number, trigger: HTMLButtonElement) => void;
}

export default function ScanReportResult({ scan, original, names = {}, issueLabels, disabled, reader, ref }: {
    scan: ScanPreview;
    original: File | null;
    names?: Partial<Record<LabCode, string>>;
    issueLabels?: Record<Issue, string>;
    disabled: boolean;
    reader?: ReaderMode;
    ref?: Ref<ScanReportSource>;
}) {
    const [attached, setAttached] = useState<File | null>(null);
    const [corrected, setCorrected] = useState<Record<number, ScanFinding>>({});
    const [filter, setFilter] = useState<Filter>('all');
    const [editing, setEditing] = useState<{ index: number; fields: ScanFields; showPage: boolean } | null>(null);
    const [pageResult, setPageResult] = useState<PageResult>({ image: '', error: '' });
    const pageRequest = useRef<PageRequest | null>(null);
    const reviewDialog = useRef<HTMLDialogElement>(null);
    const reviewHeading = useRef<HTMLHeadingElement>(null);
    const reviewTrigger = useRef<HTMLButtonElement>(null);
    const reviewTitle = useId();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const analysis = scan.analysis;
    const extracted = analysis?.findings ?? [];
    const edits = reader?.corrections ?? corrected;
    const findings = extracted.map((row, index) => edits[index] ?? row);
    const outside = findings.filter(row => row.status === 'HIGH' || row.status === 'LOW').length;
    const needsReview = findings.filter(row => row.status === 'REVIEW').length;
    const within = findings.filter(row => row.status === 'NORMAL').length;
    const visibleRows = findings.map((row, index) => ({ row, index }))
        .filter(({ row }) => !reader || filter === 'all' || (filter === 'review'
            ? row.status === 'REVIEW' : row.status === 'HIGH' || row.status === 'LOW'))
        .sort((a, b) => sortOrder[a.row.status] - sortOrder[b.row.status]);
    const sourceFile = original ?? attached;
    const sourceLabel = reader?.sourceKind === 'photo' ? '原照片' : '原 PDF';
    const sourceTypes = reader?.sourceKind === 'photo' ? 'image/jpeg,image/png,image/webp' : '.pdf,application/pdf';
    const editingIndex = editing?.index;

    useEffect(() => () => releasePage(pageRequest.current), []);

    useLayoutEffect(() => {
        if (editingIndex === undefined) return;
        const dialog = reviewDialog.current;
        if (!dialog) return;
        dialog.showModal();
        reviewHeading.current?.focus();
        return () => dialog.close();
    }, [editingIndex]);

    function clearPage() {
        releasePage(pageRequest.current);
        pageRequest.current = null;
        setPageResult({ image: '', error: '' });
    }

    async function togglePage() {
        if (!editing || !sourceFile) return;
        clearPage();
        setEditing({ ...editing, showPage: !editing.showPage });
        if (editing.showPage) return;
        await requestPage(sourceFile, editing.index);
    }

    async function requestPage(file: File, index: number) {
        const request: PageRequest = { controller: new AbortController() };
        pageRequest.current = request;
        try {
            const page = extracted[index].page;
            const blob = await (reader ? reader.loadPage(file, page, request.controller.signal)
                : auditApi.scanPage(file, page, scan.source_sha256, request.controller.signal));
            if (request.controller.signal.aborted) return;
            request.url = URL.createObjectURL(blob);
            setPageResult({ image: request.url, error: '' });
        } catch (err) {
            if (!request.controller.signal.aborted) {
                setPageResult({ image: '', error: err instanceof Error ? err.message : '无法查看原报告，请重试。' });
            }
        }
    }

    function edit(index: number, trigger: HTMLButtonElement, showSource = false) {
        const { name, value, unit, reference } = findings[index];
        reviewTrigger.current = trigger;
        clearPage();
        setError('');
        setEditing({ index, fields: { name, value, unit, reference }, showPage: Boolean(showSource && sourceFile) });
        if (showSource && sourceFile) void requestPage(sourceFile, index);
    }

    useImperativeHandle(ref, () => ({
        showSource(index, trigger) {
            if (findings[index]) edit(index, trigger, true);
        },
    }));

    function closeEditor(revealResult = false) {
        reviewDialog.current?.close();
        clearPage();
        setEditing(null);
        const trigger = reviewTrigger.current;
        if (revealResult) {
            requestAnimationFrame(() => {
                if (!trigger?.isConnected) return;
                trigger.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                trigger.focus({ preventScroll: true });
            });
        } else {
            trigger?.focus({ preventScroll: true });
        }
    }

    async function compareAgain() {
        if (!editing || busy) return;
        if (!editing.fields.name.trim()) {
            setError('请填写检验项目。');
            return;
        }
        setError('');
        setBusy(true);
        try {
            const { index, fields } = editing;
            const trimmed = { name: fields.name.trim(), value: fields.value.trim(), unit: fields.unit.trim(),
                reference: fields.reference.trim() };
            if (reader) {
                await reader.correct(index, trimmed);
            } else {
                const { status } = await auditApi.compareScanRow(trimmed);
                setCorrected(previous => ({ ...previous, [index]: { ...trimmed, page: extracted[index].page, status } }));
            }
            closeEditor(true);
        } catch (err) {
            setError(err instanceof Error ? err.message : '无法重新比对，请重试。');
        } finally {
            setBusy(false);
        }
    }

    return (
        <section aria-labelledby="scan-title" className="mt-5 space-y-4">
            <div>
                <h3 id="scan-title" className="text-lg font-semibold">{reader ? '检验结果摘要' : '报告核对与分析'}</h3>
                {!analysis ? (
                    <p className="mt-1 text-sm text-ink-600">这份草稿来自旧版扫描，请重新上传原 PDF 生成分析结果。</p>
                ) : (
                    <p className="mt-1 text-sm text-ink-600">
                        {scan.pages.length} 页 · {findings.length} 项检验结果
                        {!reader && outside > 0 && ` · ${outside} 项超出报告参考范围`}
                        {!reader && within > 0 && ` · ${within} 项在参考范围内`}
                        {!reader && needsReview > 0 && ` · ${needsReview} 项待确认`}
                    </p>
                )}
            </div>
            {analysis && (
                <>
                    {!reader && <section aria-labelledby="scan-source-title" className="rounded-lg bg-ink-50 p-3">
                        <h4 id="scan-source-title" className="text-sm font-semibold">原始病历核对</h4>
                        {analysis.source_audit ? (
                            <>
                            <p className="mt-1 text-xs text-ink-600">以下结果基于原始扫描识别。</p>
                            <ul className="mt-1 space-y-1 text-sm text-ink-700">
                                {analysis.source_audit.findings.map(({ row, evidence, issues }) => (
                                    <li key={row.code}>
                                        {names[row.code] ?? row.code} · {issues.length ? issues.map(issue => issueLabels?.[issue] ?? issue).join('、') : '字段一致'}
                                        {evidence && ` · 原始记录 ${evidence.value} ${evidence.unit}（${evidence.id}）`}
                                    </li>
                                ))}
                            </ul>
                            </>
                        ) : (
                            <p className="mt-1 text-sm text-ink-600">未找到对应的原始病历记录。下方按报告所列参考范围比对。</p>
                        )}
                    </section>}
                    {reader && <div className="flex flex-wrap gap-2" role="group" aria-label="筛选检验结果">
                        {([
                            ['outside', '超出报告参考范围', outside],
                            ['review', '待确认', needsReview],
                            ['all', '全部项目', findings.length],
                        ] as const).map(([value, label, count]) => (
                            <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}
                                className={`rounded-lg border px-3 py-2 text-sm ${filter === value
                                    ? 'border-brand-600 bg-brand-50 text-brand-800' : 'border-ink-200 text-ink-700'}`}>
                                {label} <span className="font-semibold tabular-nums">{count}</span>
                            </button>
                        ))}
                    </div>}
                    {reader && findings.length > 0 && visibleRows.length === 0 &&
                        <p role="status" className="text-sm text-ink-600">{filter === 'review' ? '没有待确认的项目。' : '没有识别到超出报告参考范围的项目。'}</p>}
                    {editing && createPortal(
                        <dialog ref={reviewDialog} aria-labelledby={reviewTitle}
                            className="fixed inset-0 m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-xl overflow-y-auto overscroll-contain rounded-xl border border-ink-200 bg-white p-4 text-ink-900 shadow-floating backdrop:bg-ink-900/45 sm:p-5"
                            onCancel={event => {
                                event.preventDefault();
                                if (!busy) closeEditor();
                            }}>
                            <h4 id={reviewTitle} ref={reviewHeading} tabIndex={-1} className="mb-3 text-lg font-semibold">核对字段</h4>
                            <fieldset disabled={disabled || busy} className="w-full min-w-0 space-y-3 rounded-lg border border-ink-200 p-3">
                                <legend className="px-1 text-sm font-semibold">修正扫描字段</legend>
                                <p className="text-sm text-ink-600">
                                    OCR 原文 · 第 {extracted[editing.index].page} 页 · {editableFields.map(([field]) =>
                                        extracted[editing.index][field] || '未读出').join(' · ')}
                                </p>
                                {!original && (
                                    <label className="inline-flex cursor-pointer items-center rounded-lg border border-ink-200 px-3 py-2 text-sm focus-within:ring-2 focus-within:ring-brand-600">
                                        {`选择${sourceLabel} 查看页码`}
                                        <input className="sr-only" type="file" accept={sourceTypes}
                                            onChange={event => {
                                                const file = event.target.files?.[0];
                                                if (file) {
                                                    clearPage();
                                                    setAttached(file);
                                                    setEditing(previous => previous && { ...previous, showPage: false });
                                                }
                                                event.target.value = '';
                                            }} />
                                    </label>
                                )}
                                {sourceFile && <button type="button" className="text-sm font-medium text-brand-700 underline"
                                    aria-expanded={editing.showPage}
                                    onClick={() => void togglePage()}>
                                    {editing.showPage ? '收起原页' : `查看${sourceLabel} 第 ${extracted[editing.index].page} 页`}
                                </button>}
                                {sourceFile && editing.showPage && <ScanPagePreview finding={extracted[editing.index]} sourceLabel={sourceLabel} {...pageResult} />}
                                <div className="grid grid-cols-2 gap-2">
                                    {editableFields.map(([field, label, limit]) => (
                                        <label key={field} className="min-w-0 text-xs text-ink-700">
                                            {label}
                                            <input className="mt-1 w-full rounded-md border border-ink-200 px-2 py-1.5 text-sm"
                                                value={editing.fields[field]} maxLength={limit} onChange={event => {
                                                    const value = event.target.value;
                                                    setEditing(previous => previous && ({
                                                        ...previous, fields: { ...previous.fields, [field]: value },
                                                    }));
                                                }} />
                                        </label>
                                    ))}
                                </div>
                                {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
                                <div className="flex flex-wrap items-center gap-3 text-sm">
                                    <button type="button" className="rounded-lg bg-brand-700 px-3 py-2 text-white disabled:opacity-50"
                                        onClick={() => void compareAgain()}>{busy ? '正在比对…' : reader ? '保存校正并比对' : '重新比对'}</button>
                                    <button type="button" className="text-ink-700 underline" onClick={() => closeEditor()}>
                                        {reader && findings[editing.index].status === 'REVIEW' ? '暂时跳过' : '取消'}
                                    </button>
                                    {!reader && <span className="text-xs text-ink-600">修正用于本次查看的参考范围比对。</span>}
                                </div>
                            </fieldset>
                        </dialog>, document.body
                    )}
                    {visibleRows.length > 0 && (
                        <div className="overflow-x-auto rounded-lg border border-ink-200">
                            <table className="w-full min-w-[30rem] border-collapse text-left text-sm" aria-label="检验项目与参考范围比对">
                                <thead className="bg-ink-50 text-ink-700">
                                    <tr>
                                        <th scope="col" className="px-3 py-2">检验项目</th>
                                        <th scope="col" className="px-3 py-2">结果</th>
                                        <th scope="col" className="px-3 py-2">报告参考值</th>
                                        <th scope="col" className="px-3 py-2">比对结果</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y divide-ink-200">
                                    {visibleRows.map(({ row, index }) => (
                                        <tr key={index}>
                                            <th scope="row" className="px-3 py-2 font-medium">
                                                {row.name}
                                                {reader ? <button type="button" disabled={disabled || busy} aria-haspopup="dialog"
                                                    onClick={event => edit(index, event.currentTarget, true)}
                                                    className="block text-xs font-normal text-brand-700 underline disabled:opacity-50">原报告第 {row.page} 页</button>
                                                    : <span className="block text-xs font-normal text-ink-600">第 {row.page} 页</span>}
                                                <button type="button" disabled={disabled || busy} aria-haspopup="dialog"
                                                    onClick={event => edit(index, event.currentTarget)}
                                                    aria-label={`核对 ${row.name} 第 ${row.page} 页`}
                                                    className="mt-1 text-xs font-normal text-brand-700 underline disabled:opacity-50">核对字段</button>
                                                {reader && <button type="button" disabled={disabled || busy}
                                                    onClick={() => reader.explain(index)}
                                                    className="ml-3 mt-1 text-xs font-normal text-brand-700 underline disabled:opacity-50">解读此项</button>}
                                            </th>
                                            <td className="px-3 py-2">{row.value || '未读出'}{row.unit && ` ${row.unit}`}</td>
                                            <td className="px-3 py-2">{row.reference || '—'}</td>
                                            <td className={`px-3 py-2 font-medium ${row.status === 'HIGH' || row.status === 'LOW'
                                                ? 'text-red-800' : row.status === 'REVIEW' ? 'text-amber-800' : 'text-ink-700'}`}>
                                                <span>{statusLabels[row.status]}</span>
                                                {reader && row.status === 'REVIEW' && row.review_reasons?.map(reason => (
                                                    <span key={reason} className="block whitespace-nowrap text-xs font-normal">{reasonLabels[reason]}</span>
                                                ))}
                                                {edits[index] && <span className="block text-xs font-normal text-ink-600">已手动校正</span>}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                    {analysis.notes.length > 0 && (
                        <section aria-labelledby="scan-conclusions-title">
                            <h4 id="scan-conclusions-title" className="text-sm font-semibold">原报告检查结论</h4>
                            <ul className="mt-2 space-y-2 text-sm text-ink-700">
                                {analysis.notes.map((note, index) => (
                                    <li key={index}><strong>{note.name}</strong> · 第 {note.page} 页 · {note.text}</li>
                                ))}
                            </ul>
                        </section>
                    )}
                    {findings.length === 0 && analysis.notes.length === 0 && !analysis.source_audit && (
                        <p className="text-sm text-ink-600">{reader
                            ? '这份报告还没有识别结果。可以选择原件重新识别。'
                            : '未能定位可比对的检验表。请上传清晰且含项目、结果和参考值的报告。'}</p>
                    )}
                </>
            )}
        </section>
    );
}
