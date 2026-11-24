import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ArrowLeft, FileText } from 'lucide-react';
import { ChatMessageRow } from '../../components/chat/GlobalChatView';
import InputBar from '../../components/chat/InputBar';
import { ComposerDock } from '../../components/layout/ChatComposer';
import ConversationFrame from '../../components/layout/ConversationFrame';
import ConversationPane, { ConversationIntro } from '../../components/layout/ConversationPane';
import { readingApi, type ReadingAnswer, type ReadingReport, type ReadingStreamHandlers, type ReadingSummary } from '../../services/reportReading';
import type { ScanFields } from '../../services/reportAudit';
import ScanReportResult, { type ScanReportSource } from './ScanReportResult';
import ReadingAnswerView from './ReadingAnswerView';
import PatientReportSheet from './PatientReportSheet';

const uploadTypes = '.pdf,application/pdf,image/jpeg,image/png,image/webp';
const patientQuestion = '请用通俗的话解读整份报告，就医时该和医生讨论什么？';
const textAction = 'inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-sm font-medium text-brand-800 hover:bg-brand-50 disabled:opacity-50';
const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' });
const message = (error: unknown) => error instanceof Error ? error.message : '处理失败，请重试。';
type ReportView = 'sheet' | 'details' | 'chat';

function ReadingPreview({ status, text }: { status: string; text: string }) {
    return <div aria-label="正在流式解读" role="status">
        <ChatMessageRow message={{ role: 'assistant', text, isGenerating: true,
            steps: [{ id: 'active', type: 'node_start', content: status, isFinished: false }] }} />
    </div>;
}

export default function ReportReadingScreen({ navigation }: { navigation: ReactNode }) {
    const [history, setHistory] = useState<ReadingSummary[]>([]);
    const [hasMore, setHasMore] = useState(false);
    const [report, setReport] = useState<ReadingReport | null>(null);
    const [original, setOriginal] = useState<File | null>(null);
    const [focus, setFocus] = useState<number | null>(null);
    const [explanation, setExplanation] = useState<ReadingAnswer | null>(null);
    const [pending, setPending] = useState<{ question: string | null; text: string; status: string } | null>(null);
    const [question, setQuestion] = useState('');
    const [view, setView] = useState<ReportView>('chat');
    const [askingNewQuestion, setAskingNewQuestion] = useState(false);
    const sourcePreview = useRef<ScanReportSource>(null);
    const [busy, setBusy] = useState('正在读取报告历史…');
    const [error, setError] = useState('');
    const chatEnd = useRef<HTMLDivElement>(null);
    const reportTop = useRef<HTMLDivElement>(null);
    const turnCount = report?.turns.length ?? 0;
    const latestAnswer = report?.turns.at(-1)?.answer;
    const outdated = latestAnswer?.task && latestAnswer.task.findings_revision !== (report?.findings_revision ?? 1);
    const contextFollowUp = outdated ? null : latestAnswer?.context_follow_up;
    const replying = Boolean(contextFollowUp && !askingNewQuestion);
    const submitAsReply = replying && !/(?:[？?]$|^(?:请问[，, ]*)?(?:为什么|为啥))/.test(question.trim());
    const questionLimit = submitAsReply ? contextFollowUp?.max_length ?? 400 : 400;
    const running = useRef(false);
    const activeStream = useRef<AbortController | null>(null);
    const newEvidenceGaps = useMemo(() => {
        const revision = report?.findings_revision ?? 1;
        const shown = new Set<string>();
        return report?.turns.map(({ answer }) => {
            if (answer.direct_answer || answer.guidance?.care_level === 'emergency'
                || (answer.task?.findings_revision ?? 1) !== revision) return [];
            return (answer.evidence_checks ?? []).filter(check => {
                if (check.status === 'confirmed') return false;
                const key = `${check.indicator}:${check.status}:${check.rows.join(',')}`;
                if (shown.has(key)) return false;
                shown.add(key);
                return true;
            });
        }) ?? [];
    }, [report]);

    useEffect(() => {
        const controller = new AbortController();
        void readingApi.list(0, controller.signal).then(items => {
            if (!controller.signal.aborted) {
                setHistory(items);
                setHasMore(items.length === 50);
            }
        }).catch(problem => {
            if (!controller.signal.aborted) setError(message(problem));
        }).finally(() => {
            if (!controller.signal.aborted) setBusy('');
        });
        return () => controller.abort();
    }, []);

    useEffect(() => {
        if (view === 'chat') chatEnd.current?.scrollIntoView({ block: 'end' });
    }, [report?.id, turnCount, explanation, pending?.text, view]);

    useEffect(() => {
        if (view !== 'chat') reportTop.current?.scrollIntoView({ block: 'start' });
    }, [report?.id, view]);

    useEffect(() => () => activeStream.current?.abort(), []);

    async function run(label: string, action: () => Promise<void>) {
        if (running.current) return;
        running.current = true;
        setBusy(label);
        setError('');
        try {
            await action();
        } catch (problem) {
            setError(message(problem));
        } finally {
            running.current = false;
            setBusy('');
        }
    }

    function readAnswer(text: string | null, action: (signal: AbortSignal, handlers: ReadingStreamHandlers) => Promise<void>) {
        if (running.current) return;
        const controller = new AbortController();
        activeStream.current = controller;
        const status = text ? '正在回答报告问题…' : '正在读取指标说明…';
        setPending({ question: text, text: '', status });
        const handlers: ReadingStreamHandlers = {
            onChunk: chunk => setPending(previous => previous && { ...previous, text: previous.text + chunk, status: '正在核对证据并完成答复…' }),
            onStep: step => setPending(previous => previous && { ...previous, status: step.content || status }),
            onStepFinish: () => setPending(previous => previous && { ...previous, status: '正在核对报告依据…' }),
        };
        void run(status, async () => {
            try {
                await action(controller.signal, handlers);
            } catch (error) {
                if (!controller.signal.aborted) throw error;
            } finally {
                activeStream.current = null;
                setPending(null);
            }
        });
    }

    function select(next: ReadingReport | null, file: File | null) {
        setReport(next);
        setOriginal(file);
        setExplanation(null);
        setFocus(null);
        setQuestion('');
        setAskingNewQuestion(false);
        setView(next && !next.turns.length ? 'sheet' : 'chat');
    }

    function upload(file: File) {
        void run('正在识别报告，完成后保存到账号…', async () => {
            const next = await readingApi.upload(file);
            const { id, title, source_kind, created_at } = next;
            setHistory(previous => [{ id, title, source_kind, created_at }, ...previous]);
            select(next, file);
        });
    }

    function explain(index: number) {
        if (!report || running.current) return;
        setFocus(index);
        setAskingNewQuestion(true);
        setExplanation(null);
        setView('chat');
        readAnswer(null, async (signal, handlers) => setExplanation(await readingApi.explain(report.id, index, signal, handlers)));
    }

    function openSource(row: number, trigger: HTMLButtonElement) {
        sourcePreview.current?.showSource(row, trigger);
    }

    function ask(text: string, row: number | null, replyToContext = false, targetReport = report) {
        const prompt = text.trim();
        if (!targetReport || !prompt || busy) return;
        setView('chat');
        readAnswer(prompt, async (signal, handlers) => {
            const updated = await readingApi.ask(targetReport, prompt, row, replyToContext, signal, handlers);
            setReport(updated);
            setAskingNewQuestion(false);
            const answer = updated.turns[updated.turns.length - 1]?.answer;
            if (answer?.scope === 'report' || (answer?.scope === 'indicator' && answer.row !== focus)) {
                setFocus(answer.row);
                setExplanation(null);
            }
            setQuestion(current => current.trim() === prompt ? '' : current);
        });
    }

    async function correct(index: number, fields: ScanFields) {
        if (!report) return;
        const updated = await readingApi.correct(report, index, fields);
        setReport(updated);
        if (focus === index) setExplanation(null);
        const originalQuestion = updated.turns.filter(turn => !turn.context_field).at(-1);
        if (originalQuestion) ask(originalQuestion.question, originalQuestion.focus_row ?? null, false, updated);
    }

    return (
        <ConversationFrame navigation={navigation} history={{
            title: '报告历史',
            items: history,
            itemIcon: FileText,
            activeId: report?.id,
            emptyText: '暂无已保存报告',
            loading: busy === '正在读取报告历史…',
            disabled: Boolean(busy),
            onCreate: () => {
                select(null, null);
                setError('');
                requestAnimationFrame(() => document.getElementById('consultation-attachment')?.focus());
            },
            onOpen: id => {
                if (report?.id !== id) void run('正在打开报告…', async () => select(await readingApi.get(id), null));
            },
            more: hasMore && <button type="button" className={textAction} disabled={Boolean(busy)}
                onClick={() => void run('正在读取更多报告…', async () => {
                    const more = await readingApi.list(history.length);
                    setHistory(previous => [...previous, ...more.filter(item => !previous.some(saved => saved.id === item.id))]);
                    setHasMore(more.length === 50);
                })}>加载更多</button>,
        }}>
            <ConversationPane title="报告内容解析" actions={report && <div className="flex flex-wrap justify-end gap-x-1">
                {view !== 'sheet' && <button type="button" className={textAction} onClick={() => setView('sheet')}>
                    体检解读单
                </button>}
                {view === 'details' || view === 'sheet' ? <button type="button" className={textAction} onClick={() => setView('chat')}>
                    <ArrowLeft size={16} aria-hidden className="hidden sm:block" /> 返回对话
                </button> : null}
                {view !== 'details' && <button type="button" className={textAction} onClick={() => setView('details')}>
                    <FileText size={16} aria-hidden className="hidden sm:block" /> 查看报告
                </button>}
            </div>} status={<>
                {report && view !== 'sheet' && <h2 title={report.title} className="truncate px-4 pb-1 text-xs text-ink-600 sm:px-6">{report.title}</h2>}
                {busy && !pending && <p role="status" className="px-4 pb-1 text-sm text-ink-600 sm:px-6">{busy}</p>}
                {error && <p role="alert" className="bg-red-50 px-4 py-2 text-sm text-red-800 sm:px-6">{error}</p>}
            </>}>
                {report && view === 'sheet' && <div ref={reportTop}>
                    <PatientReportSheet report={report} busy={Boolean(busy)} openSource={openSource}
                        onDiscuss={() => ask(patientQuestion, null)} onContinue={() => {
                            setView('chat');
                            requestAnimationFrame(() => document.getElementById('consultation-message')?.focus());
                        }} onDetails={() => setView('details')} />
                </div>}
                {report && <div ref={view === 'details' ? reportTop : undefined} hidden={view !== 'details'} className="mx-auto w-full max-w-[1060px] px-4 pb-5 sm:px-7">
                    <p className="mt-2 text-xs text-ink-600">已保存到账号 · {dateFormat.format(report.created_at)}</p>
                    {report.findings.length > 0 && <p className="mt-1.5 text-xs text-ink-600">
                        解读会将相关的已确认字段、提问和补充情况发送给配置的模型服务。
                    </p>}
                    {report.findings.length === 0 && (
                        <label className={`${textAction} mt-2 cursor-pointer focus-within:ring-2 focus-within:ring-brand-600`}>
                            选择原件重新识别
                            <input type="file" className="sr-only" aria-label="选择原件重新识别"
                                accept={report.source_kind === 'pdf' ? '.pdf,application/pdf' : 'image/jpeg,image/png,image/webp'}
                                disabled={Boolean(busy)} onChange={event => {
                                    const file = event.target.files?.[0];
                                    event.target.value = '';
                                    if (!file) return;
                                    void run('正在重新识别这份报告…', async () => {
                                        setReport(await readingApi.retryOcr(report, file));
                                        setOriginal(file);
                                        setExplanation(null);
                                    });
                                }} />
                        </label>
                    )}
                    <ScanReportResult key={report.id} ref={sourcePreview} original={original} disabled={Boolean(busy)}
                        scan={{
                            pages: Array.from({ length: report.page_count }, (_, index) => ({ page: index + 1, lines: [] })),
                            analysis: { findings: report.findings, notes: [], source_audit: null },
                        }}
                        reader={{
                            sourceKind: report.source_kind, corrections: report.corrections, correct, explain,
                            loadPage: (file, page, signal) => readingApi.page(report.id, file, page, signal),
                        }} />
                </div>}
                {view === 'chat' && (report && (turnCount > 0 || pending || explanation) ? (
                    <div className="mx-auto w-full max-w-[816px] px-4 py-4 sm:px-7 sm:py-6">
                        <ol aria-label="报告追问" className="space-y-4">
                            {report.turns.map((turn, index) => (
                                <li key={index} className="space-y-2">
                                    <ChatMessageRow message={{ role: 'user', text: turn.question }} />
                                    <ChatMessageRow message={{ role: 'assistant', text: '',
                                        awaitingReply: index === turnCount - 1 && Boolean(contextFollowUp) && !pending }}>
                                        <ReadingAnswerView answer={turn.answer} openSource={openSource} inConversation busy={Boolean(busy)}
                                            inlineEvidenceGaps={newEvidenceGaps[index]}
                                            outdated={Boolean(turn.answer.task && turn.answer.task.findings_revision !== (report.findings_revision ?? 1))}
                                            onContextReply={index === turnCount - 1 && contextFollowUp ? text => ask(text, null, true) : undefined} />
                                    </ChatMessageRow>
                                </li>
                            ))}
                            {pending?.question && <li className="space-y-2">
                                <ChatMessageRow message={{ role: 'user', text: pending.question }} />
                                <ReadingPreview status={pending.status} text={pending.text} />
                            </li>}
                        </ol>
                        {pending?.question === null ? <ReadingPreview status={pending.status} text={pending.text} />
                            : explanation && <ChatMessageRow message={{ role: 'assistant', text: '' }}>
                                <ReadingAnswerView answer={explanation} openSource={openSource} busy={Boolean(busy)} />
                            </ChatMessageRow>}
                        <div ref={chatEnd} className="h-px" aria-hidden />
                    </div>
                ) : (
                    <ConversationIntro
                        heading={report ? '想了解这份报告的哪些结果？' : '上传一份报告开始解读'}
                        description={report ? '可以询问整份报告，也可以打开报告查看某个指标的原页。'
                            : '支持 5 页以内、1 MB 以内的 PDF，以及 8 MB 以内的 JPG、PNG、WebP。'}
                        questions={report?.findings.length ? ['解读整份报告', '这些异常可能是什么原因？'] : []}
                        onQuestion={text => ask(text, null)}
                    />
                ))}
            </ConversationPane>
            <ComposerDock>
                {contextFollowUp && view === 'chat' && <button type="button" disabled={Boolean(busy)}
                    onClick={() => setAskingNewQuestion(value => !value)} className={textAction}>
                    {replying ? '另问报告' : '回答上一个问题'}
                </button>}
                <InputBar inputValue={question} setInputValue={setQuestion} inputLabel="追问报告" maxLength={400}
                    isBusy={Boolean(busy)} inputDisabled={!report || (Boolean(busy) && !pending)}
                    sendDisabled={question.trim().length > questionLimit}
                    placeholder={replying ? '直接回答助手的问题…' : report ? '输入想问的报告问题…' : '上传报告后可以在这里提问…'}
                    attachment={{ label: '上传报告', accept: uploadTypes, onPick: upload }}
                    onSend={() => ask(question, focus, submitAsReply)} />
                {question.trim().length > questionLimit && <p className="mt-1 text-xs text-ink-600">
                    这条补充请控制在 {questionLimit} 字以内。
                </p>}
            </ComposerDock>
        </ConversationFrame>
    );
}
