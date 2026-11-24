import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ClipboardCheck, FileUp, GitCompareArrows, MessageSquareText } from 'lucide-react';
import { AuditRequestError, auditApi, usesApplication } from '../../services/reportAudit';
import type { Answer, AuditJob, AuditJobStatus, Draft, Issue, LabCode, LabRow, Observation, ReportActivity, ReviewTask, SavedReportAnswer, ScanPreview, Snapshot } from '../../services/reportAudit';
import ReportImpactPanel from './ReportImpactPanel';
import ScanReportResult from './ScanReportResult';

const issueLabels: Record<Issue, string> = {
    date_mismatch: '采样日期不一致',
    unit_mismatch: '单位不一致',
    value_mismatch: '数值不一致',
    missing_evidence: '缺少原始记录',
    ambiguous_evidence: '有多条候选记录，需人工核对',
};

const panel = 'rounded-card border border-ink-200 bg-white p-4';
const button = 'rounded-xl border border-ink-200 bg-white px-3 py-2 text-sm font-medium text-ink-800 hover:bg-ink-50 disabled:opacity-50';
const primary = 'rounded-xl bg-brand-700 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-800 disabled:opacity-50';
const input = 'w-full min-w-0 rounded-lg border border-ink-200 bg-white px-2.5 py-2 text-sm text-ink-900';
const caseKey = 'report-audit-case-id';
const historyPageSize = 50;
const jobLabels = { QUEUED: '排队中', RUNNING: '审核中', SUCCEEDED: '已完成', FAILED: '失败' };
const imageUploadError = '所选文件包含图片。检验核对支持 Excel、PDF 和 TXT。';

const questionStarters = ['概览核对结果', '血红蛋白的原始记录是什么？', '上次的记录呢？', '血红蛋白是什么？'];

function formatDraft(draft: Draft) {
    return [
        `PATIENT ${draft.patient_id}`, `ENCOUNTER ${draft.encounter_id}`,
        ...draft.rows.map(row => `${row.code} ${row.value} ${row.unit} ${row.collected_on}`),
    ].join('\n');
}

function Evidence({ record }: { record: Observation }) {
    return (
        <div className="rounded-lg bg-ink-50 px-3 py-2 text-xs leading-relaxed text-ink-700">
            <div><strong>{record.id} · v{record.revision}</strong> · {record.encounter_id}</div>
            <div>{record.collected_on} · {record.value} {record.unit}</div>
        </div>
    );
}

export default function ReportAuditScreen({ username }: { username?: string }) {
    const selectionKey = usesApplication ? `${caseKey}:${username}` : caseKey;
    const [jobs, setJobs] = useState<AuditJobStatus[]>([]);
    const [nextHistoryOffset, setNextHistoryOffset] = useState<number | null>(null);
    const submitKey = useRef<{ payload: string; key: string } | null>(null);

    function submissionKey(scope: string, payload: unknown) {
        const serialized = JSON.stringify([scope, payload]);
        if (submitKey.current?.payload !== serialized) {
            submitKey.current = { payload: serialized, key: crypto.randomUUID() };
        }
        return submitKey.current.key;
    }

    function track(tickets: AuditJob[]) {
        const tracked: AuditJobStatus[] = tickets.map(ticket => ({
            ...ticket, status: 'QUEUED', attempts: 0, error_code: null,
        }));
        setJobs(previous => [...tracked, ...previous.filter(job => !tracked.some(item => item.job_id === job.job_id))]);
        setNextHistoryOffset(offset => offset === null ? null : offset + tracked.length);
        submitKey.current = null;
    }

    const [sample, setSample] = useState('');
    const [rulesVersion, setRulesVersion] = useState<number | null>(null);
    const [names, setNames] = useState<Partial<Record<LabCode, string>>>({});
    const [text, setText] = useState('');
    const [savedText, setSavedText] = useState('');
    const [scan, setScan] = useState<ScanPreview | null>(null);
    const [scanFile, setScanFile] = useState<File | null>(null);
    const [scanning, setScanning] = useState(false);
    const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
    const [latestVersion, setLatestVersion] = useState(0);
    const [rows, setRows] = useState<LabRow[]>([]);
    const [editing, setEditing] = useState(false);
    const [pending, setPending] = useState(true);
    const [error, setError] = useState('');
    const [question, setQuestion] = useState('');
    const [turns, setTurns] = useState<{ question: string; answer: Answer }[]>([]);
    const [detailTab, setDetailTab] = useState<'qa' | 'impact'>('qa');
    const [activity, setActivity] = useState<{ caseId: string; data?: ReportActivity; error?: string } | null>(null);
    const activityRequest = useRef(0);
    const loadActivity = useCallback(async (caseId: string) => {
        const request = ++activityRequest.current;
        try {
            const data = await auditApi.activity(caseId);
            if (request === activityRequest.current) setActivity({ caseId, data });
        } catch (err) {
            if (request === activityRequest.current) {
                setActivity({ caseId, error: err instanceof Error ? err.message : '无法读取报告活动' });
            }
        }
    }, []);

    function showSnapshot(result: Snapshot) {
        setSnapshot(result);
        setRows(result.draft.rows);
        setEditing(false);
        setQuestion('');
        if (!usesApplication) setTurns([]);
    }

    useEffect(() => {
        let active = true;
        async function initialize() {
            try {
                const demo = await auditApi.sample().catch(() => null);
                if (!active) return;
                if (demo) {
                    setSample(demo.text);
                    setNames(demo.lab_names);
                    setRulesVersion(demo.rules_version ?? null);
                } else {
                    setError('示例报告暂不可用，已完成的审核结果仍可查看。');
                }
                if (usesApplication) {
                    const [draft, history] = await Promise.allSettled([auditApi.draft(), auditApi.history()]);
                    if (!active) return;
                    if (history.status === 'fulfilled') {
                        setJobs(history.value);
                        setNextHistoryOffset(history.value.length === historyPageSize ? historyPageSize : null);
                    }
                    if (draft.status === 'fulfilled' && draft.value) {
                        if ('scan' in draft.value) setScan(draft.value.scan);
                        else {
                            const restoredText = 'report' in draft.value ? formatDraft(draft.value.report) : draft.value.text;
                            setText(restoredText);
                            setSavedText(restoredText);
                        }
                    }
                    if (draft.status === 'rejected' || history.status === 'rejected') {
                        setError('部分报告记录暂时无法加载，请刷新后重试。');
                    }
                }
                const saved = sessionStorage.getItem(selectionKey);
                if (!saved) return;
                const restored = await auditApi.get(saved).catch(() => null);
                if (active) {
                    if (!restored) { sessionStorage.removeItem(selectionKey); return; }
                    setLatestVersion(restored.version);
                    showSnapshot(restored);
                }
            } catch {
                if (active) setError('无法加载报告。请检查后端服务后重试。');
            } finally {
                if (active) setPending(false);
            }
        }
        void initialize();
        return () => { active = false; };
    }, [selectionKey]);

    const openJobIds = jobs.filter(job => job.status === 'QUEUED' || job.status === 'RUNNING')
        .map(job => job.job_id).join(',');
    const caseId = snapshot?.case_id;
    const currentVersion = snapshot?.version;
    const latestPublished = Math.max(0, ...jobs.filter(job => job.case_id === caseId && job.status === 'SUCCEEDED')
        .map(job => job.version));
    const reportActivity = activity && activity.caseId === caseId ? activity : null;
    const reviews = reportActivity?.data?.reviews ?? [];
    const savedAnswers = reportActivity?.data?.answers ?? [];
    const displayedTurns = usesApplication
        ? savedAnswers.map(turn => ({ ...turn, id: turn.answer_id }))
        : turns.map((turn, index) => ({ ...turn, id: String(index) }));
    const pendingReviews = reviews.filter(review => !review.reviewed_at).length;

    useEffect(() => {
        if (usesApplication && caseId) void loadActivity(caseId);
    }, [caseId, currentVersion, latestPublished, loadActivity]);

    useEffect(() => {
        if (!openJobIds) return;
        let active = true;
        let timer: ReturnType<typeof setTimeout>;
        async function refreshJobs() {
            const states = await Promise.allSettled(openJobIds.split(',').map(id => auditApi.job(id)));
            if (!active) return;
            const updated = new Map(states.flatMap(state => state.status === 'fulfilled' ? [[state.value.job_id, state.value]] : []));
            setJobs(previous => previous.map(job => updated.get(job.job_id) ?? job));
            timer = setTimeout(refreshJobs, 1000);
        }
        timer = setTimeout(refreshJobs, 300);
        return () => { active = false; clearTimeout(timer); };
    }, [openJobIds]);

    async function run(action: () => Promise<void>) {
        setError('');
        setPending(true);
        try {
            await action();
        } catch (err) {
            setError(err instanceof Error ? err.message : '请求失败，请重试');
        } finally {
            setPending(false);
        }
    }

    function startAudit(event: FormEvent) {
        event.preventDefault();
        if (scan) return;
        void run(async () => {
            const draft = await auditApi.parse(text);
            const result = await auditApi.create(draft, usesApplication ? submissionKey('create', draft) : undefined);
            if (Array.isArray(result)) {
                track(result);
                return;
            }
            sessionStorage.setItem(selectionKey, result.case_id);
            setLatestVersion(result.version);
            showSnapshot(result);
        });
    }

    async function scanReport(file: File) {
        setScanning(true);
        try {
            setScan(await auditApi.scan(file));
            setScanFile(file);
            setText('');
            setSavedText('');
        } finally {
            setScanning(false);
        }
    }

    function upload(file?: File, scanDirectly = false) {
        if (!file) return;
        void run(async () => {
            if (scanDirectly) {
                await scanReport(file);
                return;
            }
            try {
                const imported = formatDraft(await auditApi.upload(file, true));
                setText(imported);
                setSavedText(imported);
                setScan(null);
                setScanFile(null);
            } catch (err) {
                if (!(err instanceof AuditRequestError) || err.code !== 'PDF_OCR_REQUIRED') throw err;
                await scanReport(file);
            }
        });
    }

    function uploadBatch(files: File[]) {
        if (!files.length) return;
        void run(async () => {
            if (files.length > 25) throw new Error('每批最多 25 份报告');
            const drafts = [];
            for (const file of files) drafts.push(await auditApi.upload(file));
            track(await auditApi.batch(drafts, submissionKey('batch', drafts)));
        });
    }

    function viewJob(job: AuditJobStatus) {
        void run(async () => {
            const latest = await auditApi.get(job.case_id);
            const selected = job.version === latest.version ? latest : await auditApi.get(job.case_id, job.version);
            sessionStorage.setItem(selectionKey, selected.case_id);
            setLatestVersion(latest.version);
            showSnapshot(selected);
        });
    }

    function retryJob(job: AuditJobStatus) {
        void run(async () => {
            const updated = await auditApi.retry(job.job_id);
            setJobs(previous => previous.map(item => item.job_id === updated.job_id ? updated : item));
        });
    }

    function reset() {
        sessionStorage.removeItem(selectionKey);
        setSnapshot(null);
        setLatestVersion(0);
        if (!usesApplication) {
            setText('');
            setScan(null);
            setScanFile(null);
        }
        setError('');
        setDetailTab('qa');
    }

    function saveDraft(value: string) {
        void run(async () => {
            if (usesApplication) await auditApi.saveDraft(value);
            setScan(null);
            setScanFile(null);
            setText(value);
            setSavedText(value);
        });
    }

    function clearDraft(clearText: boolean) {
        void run(async () => {
            if (usesApplication) await auditApi.clearDraft();
            setScan(null);
            setScanFile(null);
            if (clearText) setText('');
            setSavedText('');
        });
    }

    function loadMoreHistory() {
        if (nextHistoryOffset === null) return;
        void run(async () => {
            const extra = await auditApi.history(nextHistoryOffset);
            setJobs(previous => [...previous, ...extra.filter(job => !previous.some(item => item.job_id === job.job_id))]);
            setNextHistoryOffset(extra.length === historyPageSize ? nextHistoryOffset + historyPageSize : null);
        });
    }

    function changeRow(code: LabCode, patch: Partial<LabRow>) {
        setRows(previous => previous.map(row => row.code === code ? { ...row, ...patch } : row));
    }

    function reviseReport(revisedRows: LabRow[]) {
        if (!snapshot) return;
        void run(async () => {
            const draft = { ...snapshot.draft, rows: revisedRows };
            const key = usesApplication ? submissionKey(snapshot.case_id, { version: snapshot.version, draft }) : undefined;
            const result = await auditApi.revise(snapshot, draft, key);
            if (Array.isArray(result)) {
                track(result);
                setEditing(false);
                setDetailTab('impact');
                return;
            }
            setLatestVersion(result.version);
            showSnapshot(result);
        });
    }

    function loadVersion(version?: number) {
        if (!snapshot) return;
        void run(async () => {
            const result = await auditApi.get(snapshot.case_id, version);
            if (!version) setLatestVersion(result.version);
            showSnapshot(result);
        });
    }

    function ask(value: string, version?: number, previousFocus?: LabCode | null) {
        if (!snapshot || !value.trim()) return;
        const message = value.trim();
        void run(async () => {
            const basis = version ?? snapshot.version;
            const focus = previousFocus === undefined
                ? displayedTurns.filter(turn => turn.answer.version === basis).at(-1)?.answer.focus ?? null
                : previousFocus;
            const answer = await auditApi.chat({ case_id: snapshot.case_id, version: basis }, message, focus);
            if (usesApplication) {
                if (basis !== snapshot.version) {
                    showSnapshot(await auditApi.get(snapshot.case_id, basis));
                    setLatestVersion(previous => Math.max(previous, basis));
                }
                await loadActivity(snapshot.case_id);
            } else {
                setTurns(previous => [...previous, { question: message, answer }]);
            }
            setQuestion('');
            setDetailTab('qa');
        });
    }

    function completeReview(task: ReviewTask) {
        if (!caseId) return;
        void run(async () => {
            await auditApi.completeReview(caseId, task);
            await loadActivity(caseId);
        });
    }

    function recheck(task: ReviewTask, answer: SavedReportAnswer) {
        ask(answer.question, task.target_version, answer.answer.focus);
    }

    const differences = snapshot?.findings.filter(finding => finding.issues.length).length ?? 0;
    const hasPendingRevision = snapshot && jobs.some(job => job.case_id === snapshot.case_id && job.version > snapshot.version);
    const isLatest = snapshot?.version === latestVersion && !hasPendingRevision;
    const changed = snapshot && rows.some((row, index) => (
        row.value !== snapshot.draft.rows[index].value
        || row.unit !== snapshot.draft.rows[index].unit
        || row.collected_on !== snapshot.draft.rows[index].collected_on
    ));

    return (
        <div className="h-full overflow-y-auto overscroll-y-contain">
            <div className="mx-auto w-full max-w-[1440px] px-4 pt-5 pb-[max(env(safe-area-inset-bottom),1rem)] lg:px-8 lg:py-7">
                <header className="mb-5 flex flex-wrap items-center justify-between gap-3 lg:mb-6">
                    <div>
                        <h1 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight lg:text-2xl">
                            <ClipboardCheck size={22} className="shrink-0 text-brand-700" aria-hidden /> 检验报告核对
                        </h1>
                        <p className="mt-1.5 text-sm text-ink-600">导入模拟报告，核对原始记录并追溯更正。</p>
                    </div>
                    {snapshot && <button type="button" className={button} onClick={reset} disabled={pending}>{usesApplication ? '返回导入' : '导入新报告'}</button>}
                </header>
                {error && (
                    <p role="alert" className="mb-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-800">
                        {error}
                        {error === imageUploadError && (
                            <a href="#/report-photo" className="ml-1 font-medium underline underline-offset-2">去拍照解读</a>
                        )}
                    </p>
                )}
                {pending && <p role="status" className="mb-3 text-sm text-ink-600">{scanning ? '正在分析报告…' : '正在处理…'}</p>}

                <div className={`grid gap-4 lg:gap-6 ${usesApplication && jobs.length > 0
                    ? 'lg:grid-cols-[15rem_minmax(0,1fr)]' : 'grid-cols-1'}`}>
                    {usesApplication && jobs.length > 0 && (
                        <section className={`${panel} min-w-0 self-start lg:sticky lg:top-0`} aria-labelledby="audit-jobs-title">
                            <h2 id="audit-jobs-title" className="text-lg font-semibold">核对历史</h2>
                            <ul className="mt-2 max-h-56 divide-y divide-ink-200 overflow-y-auto overscroll-y-contain lg:max-h-[calc(100dvh-15rem)]">
                                {jobs.map(job => (
                                    <li key={job.job_id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm lg:items-start">
                                        <span>报告 {job.case_id.slice(0, 8)} · v{job.version} · {jobLabels[job.status]}
                                            {job.attempts > 1 && ` · 第 ${job.attempts} 次执行`}
                                            {job.status === 'FAILED' && ` · ${job.error_code || '审核失败'}`}
                                        </span>
                                        {job.status === 'SUCCEEDED' && <button type="button" className={button} disabled={pending}
                                            onClick={() => viewJob(job)}>查看结果</button>}
                                        {job.status === 'FAILED' && job.error_code !== 'INVALID_INPUT' && (
                                            <button type="button" className={button} disabled={pending}
                                                onClick={() => retryJob(job)}>重试任务</button>
                                        )}
                                    </li>
                                ))}
                            </ul>
                            {nextHistoryOffset !== null && (
                                <button type="button" className={`${button} mt-2`} disabled={pending}
                                    onClick={loadMoreHistory}>加载更多历史</button>
                            )}
                        </section>
                    )}

                    {!snapshot ? (
                        <form onSubmit={startAudit} className={`${panel} w-full max-w-4xl`}>
                            {!scan && <>
                            <h2 className="text-lg font-semibold">导入报告</h2>
                            <p className="mt-2 text-sm text-ink-600">
                                支持 Excel（.xlsx / .xls）、PDF 和 UTF-8 TXT，每份最大 1 MB，PDF 最多 5 页。
                                {usesApplication && '结构化报告可批量核对。'}
                            </p>
                            <p className="mt-1 text-sm text-ink-600">
                                扫描 PDF 请单独上传，系统会自动生成分析结果。
                            </p>
                            </>}
                            <div className={`${scan ? '' : 'mt-4 '}flex flex-wrap gap-2`}>
                                {!scan && <button type="button" className={button} disabled={pending || !sample}
                                    onClick={() => saveDraft(sample)}>填入示例报告</button>}
                                <label className={`${button} inline-flex cursor-pointer items-center gap-1.5 focus-within:ring-2 focus-within:ring-brand-600`}>
                                    <FileUp size={15} aria-hidden /> {scan ? '重新上传报告' : '上传报告'}
                                    <input type="file" aria-label="上传报告" className="sr-only"
                                        accept=".xlsx,.xls,.pdf,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,application/pdf,text/plain"
                                        multiple={usesApplication} disabled={pending} onChange={event => {
                                            const files = Array.from(event.target.files ?? []);
                                            event.target.value = '';
                                            if (files.some(file => file.type.startsWith('image/') || /\.(png|jpe?g|webp|heic)$/i.test(file.name))) {
                                                setError(imageUploadError);
                                                return;
                                            }
                                            if (files.length > 1) uploadBatch(files);
                                            else upload(files[0]);
                                        }} />
                                </label>
                                <label className={`${button} cursor-pointer focus-within:ring-2 focus-within:ring-brand-600`}>
                                    扫描识别 PDF
                                    <input type="file" aria-label="扫描识别 PDF" className="sr-only"
                                        accept=".pdf,application/pdf" disabled={pending} onChange={event => {
                                            const file = event.target.files?.[0];
                                            event.target.value = '';
                                            upload(file, true);
                                        }} />
                                </label>
                                {scan && <button type="button" className={button} disabled={pending}
                                    onClick={() => clearDraft(true)}>清除分析结果</button>}
                            </div>
                            {scan ? <ScanReportResult key={scan.scan_id ?? 'saved-scan'} scan={scan} original={scanFile}
                                names={names} issueLabels={issueLabels} disabled={pending} /> : <>
                            <label htmlFor="audit-text" className="mt-4 block text-sm font-medium">报告文本</label>
                            <textarea id="audit-text" value={text} onChange={event => setText(event.target.value)} rows={8}
                                placeholder="PATIENT DEMO-001" disabled={pending} maxLength={10_000} className={`${input} mt-2 font-mono leading-relaxed`} />
                            <button type="submit" className={`${primary} mt-3`} disabled={pending || !text.trim()}>开始核对</button>
                            {usesApplication && (
                                <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-ink-600">
                                    <button type="button" className={button} disabled={pending || !text.trim() || text === savedText}
                                        onClick={() => saveDraft(text)}>保存草稿</button>
                                    {savedText && <button type="button" className={button} disabled={pending}
                                        onClick={() => clearDraft(true)}>清除草稿</button>}
                                    <span role="status">
                                        {text !== savedText ? '文本修改尚未保存' : savedText && '草稿已保存到账号'}
                                    </span>
                                </div>
                            )}
                            </>}
                        </form>
                    ) : (
                        <div className="grid min-w-0 items-start gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)] xl:gap-6">
                            <section aria-labelledby="findings-title" className={panel}>
                                <div className="flex flex-wrap items-start justify-between gap-3">
                                    <div>
                                        <h2 id="findings-title" className="text-lg font-semibold">核对结果</h2>
                                        <p className="mt-1 text-xs text-ink-600">
                                            {snapshot.draft.patient_id} · {snapshot.draft.encounter_id} · 规则 v{snapshot.rules_version ?? 1}
                                        </p>
                                    </div>
                                    <label className="flex shrink-0 items-center gap-2 whitespace-nowrap text-sm text-ink-700">
                                        报告版本
                                        <select className={`${input} !w-auto`} value={snapshot.version} disabled={pending}
                                            onChange={event => loadVersion(Number(event.target.value))}>
                                            {Array.from({ length: latestVersion }, (_, i) => (
                                                <option key={i + 1} value={i + 1}>v{i + 1}{i + 1 === latestVersion ? '（最新）' : ''}</option>
                                            ))}
                                        </select>
                                    </label>
                                </div>
                                <div className="my-4 grid grid-cols-3 gap-2" aria-label="核对统计">
                                    {[[differences, '待核对'], [snapshot.checked_rows, '本次复核'], [snapshot.reused_rows, '沿用结果']].map(([count, label]) => (
                                        <div key={label} className="rounded-xl bg-brand-50 p-2.5">
                                            <div className="text-lg font-semibold tabular-nums text-brand-800">{count}</div>
                                            <div className="text-xs text-ink-700">{label}</div>
                                        </div>
                                    ))}
                                </div>
                                <form onSubmit={event => {
                                    event.preventDefault();
                                    if (editing && changed && !pending) reviseReport(rows);
                                }}>
                                    <ul className="divide-y divide-ink-200 border-y border-ink-200">
                                        {snapshot.findings.map(({ row, evidence, issues }, index) => (
                                            <li key={row.code} className="py-3">
                                                <div className="flex flex-wrap items-center justify-between gap-1.5">
                                                    <h3 className="text-sm font-semibold">{names[row.code] || row.code} <span className="font-normal text-ink-600">{row.code}</span></h3>
                                                    <span className={`rounded-full px-2 py-0.5 text-xs ${issues.length ? 'bg-amber-50 text-amber-900' : 'bg-green-50 text-green-800'}`}>
                                                        {issues.map(issue => issueLabels[issue]).join('、') || '字段一致'}
                                                    </span>
                                                </div>
                                                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                                                    <div className="rounded-lg bg-white py-1 text-xs text-ink-700">
                                                        <strong>报告</strong>
                                                        <div>{row.collected_on} · {row.value} {row.unit}</div>
                                                    </div>
                                                    {evidence ? <Evidence record={evidence} /> : <span className="py-2 text-xs text-ink-600">无匹配病历记录</span>}
                                                </div>
                                                {editing && (
                                                    <div className="mt-2 grid grid-cols-2 gap-2">
                                                        <label className="text-xs text-ink-700">数值<input required disabled={pending} className={input} inputMode="decimal" value={rows[index].value} onChange={e => changeRow(row.code, { value: e.target.value })} /></label>
                                                        <label className="text-xs text-ink-700">单位<input required disabled={pending} className={input} value={rows[index].unit} onChange={e => changeRow(row.code, { unit: e.target.value })} /></label>
                                                        <label className="col-span-2 text-xs text-ink-700">采样日期<input required disabled={pending} className={input} type="date" value={rows[index].collected_on} onChange={e => changeRow(row.code, { collected_on: e.target.value })} /></label>
                                                        {evidence && issues.length > 0 && (
                                                            <button className={`${button} col-span-2 justify-self-start`} type="button" disabled={pending}
                                                                onClick={() => changeRow(row.code, { value: evidence.value, unit: evidence.unit, collected_on: evidence.collected_on })}>
                                                                采用原始记录
                                                            </button>
                                                        )}
                                                    </div>
                                                )}
                                            </li>
                                        ))}
                                    </ul>
                                    <div className="mt-4 flex flex-wrap gap-2">
                                        {editing ? (
                                            <>
                                                <button className={primary} type="submit" disabled={pending || !changed}>提交更正并复核</button>
                                                <button className={button} type="button" disabled={pending} onClick={() => { setRows(snapshot.draft.rows); setEditing(false); }}>取消</button>
                                            </>
                                        ) : isLatest && <button className={button} type="button" disabled={pending} onClick={() => setEditing(true)}>更正报告</button>}
                                        {!editing && isLatest && rulesVersion !== null && (snapshot.rules_version ?? 1) !== rulesVersion && (
                                            <button className={primary} type="button" disabled={pending}
                                                onClick={() => reviseReport(snapshot.draft.rows)}>按规则 v{rulesVersion} 重新核对</button>
                                        )}
                                        <button className={button} type="button" disabled={pending} onClick={() => loadVersion()}>载入最新版本</button>
                                    </div>
                                </form>
                            </section>

                            <section aria-label="报告问答与复核" className={panel}>
                                {usesApplication ? (
                                    <div className="mb-4 flex flex-wrap gap-1 rounded-xl bg-ink-50 p-1" aria-label="报告活动">
                                        <button type="button" aria-pressed={detailTab === 'qa'} onClick={() => setDetailTab('qa')}
                                            className={`flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium ${detailTab === 'qa' ? 'bg-white text-brand-800 shadow-raised' : 'text-ink-600'}`}>
                                            <MessageSquareText size={16} aria-hidden /> 证据问答
                                        </button>
                                        <button type="button" aria-pressed={detailTab === 'impact'} onClick={() => setDetailTab('impact')}
                                            className={`flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium ${detailTab === 'impact' ? 'bg-white text-brand-800 shadow-raised' : 'text-ink-600'}`}>
                                            <GitCompareArrows size={16} aria-hidden /> 更正影响
                                            {pendingReviews > 0 && <span aria-label={`${pendingReviews} 项待复核`}
                                                className="rounded-full bg-amber-100 px-1.5 text-xs text-amber-900">{pendingReviews}</span>}
                                        </button>
                                    </div>
                                ) : <h2 className="text-lg font-semibold">证据问答</h2>}
                                {usesApplication && (!reportActivity || reportActivity.error) && (
                                    <div className="mb-3 flex items-center gap-2 text-xs text-ink-600" role="status">
                                        {reportActivity?.error ? '读取报告活动失败' : '正在读取问答和复核记录…'}
                                        {reportActivity?.error && <button type="button" className={button}
                                            onClick={() => void loadActivity(snapshot.case_id)}>重试</button>}
                                    </div>
                                )}
                                {detailTab === 'impact' ? (
                                    <ReportImpactPanel reviews={reviews} answers={savedAnswers} names={names} pending={pending}
                                        onRecheck={recheck} onComplete={completeReview} onVersion={loadVersion} />
                                ) : <>
                                <p className="mt-1 text-xs text-ink-600">新提问基于报告 v{snapshot.version}，历史答复标注来源版本。</p>
                                <div className="mt-4 flex flex-wrap gap-1.5">
                                    {questionStarters.map(starter => (
                                        <button key={starter} type="button" className={`${button} text-xs`} disabled={pending} onClick={() => ask(starter)}>{starter}</button>
                                    ))}
                                </div>
                                <div aria-live="polite" className="mt-4 space-y-3 break-words">
                                    {displayedTurns.map(turn => {
                                        const flags = reviews.filter(review => review.answer_id === turn.id);
                                        const needsReview = flags.some(review => !review.reviewed_at);
                                        return (
                                        <article key={turn.id} className="rounded-xl border border-ink-200 p-3 text-sm leading-relaxed">
                                            <div className="flex flex-wrap items-baseline justify-between gap-1.5">
                                                <p className="font-semibold">{turn.question}</p>
                                                <span className={`rounded-full px-2 py-0.5 text-xs ${needsReview ? 'bg-amber-50 text-amber-900' : 'bg-ink-50 text-ink-700'}`}>
                                                    v{turn.answer.version}{needsReview ? ' · 待复核' : flags.length > 0 ? ' · 已复核'
                                                        : turn.answer.version < Math.max(latestVersion, latestPublished) ? ' · 未受影响' : ''}
                                                </span>
                                            </div>
                                            <p className="my-1.5 text-ink-700">{turn.answer.text}</p>
                                            <div className="space-y-1.5">
                                                {turn.answer.records.map(record => <Evidence key={record.id} record={record} />)}
                                                {turn.answer.knowledge.map((item, i) => (
                                                    <blockquote className="rounded-lg bg-brand-50 p-2 text-xs" key={i}>
                                                        <p>{item.excerpt}</p><cite className="mt-1 block">{item.source} {item.section}</cite>
                                                    </blockquote>
                                                ))}
                                            </div>
                                        </article>
                                    ); })}
                                </div>
                                <form className="mt-4 flex gap-2" onSubmit={event => { event.preventDefault(); ask(question); }}>
                                    <input className={input} aria-label="问题" placeholder="询问项目、原始记录或上次结果" maxLength={300} disabled={pending} value={question}
                                        onChange={event => setQuestion(event.target.value)} />
                                    <button type="submit" className={`${primary} shrink-0`} disabled={pending || !question.trim()}>发送</button>
                                </form>
                                </>}
                            </section>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
