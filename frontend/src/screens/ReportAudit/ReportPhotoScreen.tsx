import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Camera, Send, Upload } from 'lucide-react';
import { ChatTranscript } from '../../components/chat/GlobalChatView';
import WebSearchButton from '../../components/chat/WebSearchButton';
import { appendChatTurn, createUserMessage } from '../../services/chatTurn';
import { isVisionImage, streamChat, streamVisionChat, visionImageTypes } from '../../services/chatService';
import { deleteReportPhotoHistory, readReportPhotoHistory, readReportPhotoThread, rememberReportPhotoThread } from '../../services/reportPhotoSession';
import { useGlobalStore } from '../../store/GlobalContext';
import type { ChatMessage, ReportItemInput, ThreadId } from '../../types';
import ReportReviewForm from './ReportReviewForm';

const action = 'inline-flex min-h-10 items-center justify-center gap-2 rounded-lg bg-brand-700 px-4 py-2 text-sm font-medium text-white hover:bg-brand-800 disabled:opacity-50';

export default function ReportPhotoScreen({ username }: { username?: string }) {
    const { isElderMode } = useGlobalStore();
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [threadId, setThreadId] = useState<ThreadId | null>(() => readReportPhotoThread(username));
    const [question, setQuestion] = useState('');
    const [webSearch, setWebSearch] = useState(false);
    const [pending, setPending] = useState(false);
    const [restoring, setRestoring] = useState(() => Boolean(readReportPhotoThread(username)));
    const [restoreAttempt, setRestoreAttempt] = useState(0);
    const [restoreError, setRestoreError] = useState(false);
    const [error, setError] = useState('');
    const [review, setReview] = useState<{ id: string; items: ReportItemInput[] } | null>(null);
    const previews = useRef(new Set<string>());
    const inFlight = useRef<AbortController | null>(null);
    const busy = pending || restoring;

    useEffect(() => {
        const id = readReportPhotoThread(username);
        if (!id) return;
        const controller = new AbortController();
        void readReportPhotoHistory(id, controller.signal).then(history => {
            if (controller.signal.aborted) return;
            if (history) {
                setMessages(history);
            } else {
                rememberReportPhotoThread(null, username);
                setThreadId(null);
                setError('上次报告会话已结束，可以上传报告开始新对话。');
            }
        }).catch(() => {
            if (!controller.signal.aborted) setRestoreError(true);
        }).finally(() => {
            if (!controller.signal.aborted) setRestoring(false);
        });
        return () => controller.abort();
    }, [restoreAttempt, username]);

    useEffect(() => {
        const urls = previews.current;
        return () => {
            inFlight.current?.abort();
            for (const url of urls) URL.revokeObjectURL(url);
            urls.clear();
        };
    }, []);

    async function ask(text: string, file?: File, reportItems?: ReportItemInput[], search = false) {
        if (inFlight.current || restoring || restoreError) return;
        const controller = new AbortController();
        inFlight.current = controller;
        setPending(true);
        setError('');
        const message = createUserMessage(text);
        let imageUrl: string | undefined;
        if (file) {
            imageUrl = URL.createObjectURL(file);
            previews.current.add(imageUrl);
            message.cards = [{ type: 'sensitive_image_preview', data: { imageUrl, label: '报告图片' } }];
        }
        let nextThread = file ? null : threadId;
        let nextReview: ReportItemInput[] | undefined;
        const options = appendChatTurn(setMessages, message,
            file ? '图片解读失败，请确认图片识别服务可用后重试。' : '暂时无法获取解读，请稍后再试。',
            { onSession: id => { nextThread = id; } });
        options.replayHistory = !file && threadId !== readReportPhotoThread(username);
        options.reportItems = reportItems;
        options.webSearch = search;
        const onCard = options.onCard;
        options.onCard = card => {
            if (card.type === 'report_ocr_review') nextReview = card.data.items;
            else onCard?.(card);
        };
        const onDone = options.onDone;
        options.onDone = () => {
            onDone();
            setThreadId(nextThread);
            rememberReportPhotoThread(file ? null : nextThread, username);
            if (file && nextReview) {
                setMessages(previous => previous.slice(-2));
                setReview({ id: message.id, items: nextReview });
                for (const url of previews.current) {
                    if (url === imageUrl) continue;
                    URL.revokeObjectURL(url);
                    previews.current.delete(url);
                }
            } else if (reportItems) setReview(null);
        };
        const history = [...messages, message];
        const userInfo = { elder_mode: isElderMode };
        try {
            if (file) {
                await streamVisionChat(file, 'report', [message], options, userInfo, null, controller.signal);
            } else {
                await streamChat(history, options, userInfo, 'report', threadId, controller.signal);
            }
        } finally {
            if (!controller.signal.aborted) {
                inFlight.current = null;
                setPending(false);
            }
        }
    }

    async function clearHistory() {
        if (inFlight.current || restoring) return;
        const controller = new AbortController();
        inFlight.current = controller;
        setPending(true);
        try {
            if (threadId) await deleteReportPhotoHistory(threadId, controller.signal);
            rememberReportPhotoThread(null, username);
            setThreadId(null);
            setMessages([]);
            setReview(null);
            setQuestion('');
            setWebSearch(false);
            setRestoreError(false);
            setError('');
            for (const url of previews.current) URL.revokeObjectURL(url);
            previews.current.clear();
        } catch {
            if (!controller.signal.aborted) setError('无法清空报告解读记录，请重试。');
        } finally {
            if (!controller.signal.aborted) {
                inFlight.current = null;
                setPending(false);
            }
        }
    }

    function upload(file?: File) {
        if (!file) return;
        if (!isVisionImage(file)) {
            setError('请选择 8 MB 以内的 JPG、PNG 或 WebP 图片。');
            return;
        }
        void ask('请帮我解读这张报告。', file);
    }

    function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (!question.trim() || busy || restoreError) return;
        void ask(question.trim(), undefined, undefined, webSearch);
        setQuestion('');
        setWebSearch(false);
    }

    return (
        <div className="h-full overflow-y-auto overscroll-y-contain">
            <section className="mx-auto w-full max-w-5xl px-4 py-5 lg:px-8 lg:py-7" aria-labelledby="photo-heading">
                <h1 id="photo-heading" className="flex items-center gap-2.5 text-xl font-semibold tracking-tight lg:text-2xl">
                    <Camera size={22} className="shrink-0 text-brand-700" aria-hidden /> 拍照解读
                </h1>
                <p className="mt-1.5 text-sm text-ink-600">上传能看清项目、结果、单位和参考范围的报告照片。也可以粘贴项目明细或继续提问。</p>
                <div className="mt-5 rounded-card border border-ink-200 bg-white">
                    <div className="flex flex-wrap items-center gap-3 border-b border-ink-200 p-4">
                        <label className={`${action} cursor-pointer focus-within:ring-2 focus-within:ring-brand-600 ${busy || restoreError ? 'cursor-default opacity-50' : ''}`}>
                            <Upload size={16} aria-hidden /> 上传报告照片
                            <input type="file" accept={visionImageTypes.join(',')} aria-label="上传报告照片" className="sr-only"
                                disabled={busy || restoreError} onChange={event => {
                                    const file = event.target.files?.[0];
                                    event.target.value = '';
                                    upload(file);
                                }} />
                        </label>
                        <span className="text-xs text-ink-600">JPG、PNG、WebP · 最大 8 MB</span>
                        {(messages.length > 0 || threadId) && <button type="button" disabled={busy}
                            className="ml-auto rounded-lg px-3 py-2 text-sm text-ink-700 hover:bg-ink-50 disabled:opacity-50"
                            onClick={() => void clearHistory()}>清空会话</button>}
                    </div>
                    {error && <p role="alert" className="mx-4 mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800">{error}</p>}
                    {restoring && <p role="status" className="px-4 pt-3 text-sm text-ink-600">正在恢复报告会话…</p>}
                    {restoreError && (
                        <div className="flex items-center gap-2 px-4 pt-3 text-sm text-ink-700">
                            <span role="alert">暂时无法恢复报告解读记录。</span>
                            <button type="button" className="text-brand-700 underline" onClick={() => {
                                setRestoreError(false);
                                setRestoring(true);
                                setRestoreAttempt(attempt => attempt + 1);
                            }}>重试</button>
                        </div>
                    )}
                    {messages.length > 0 && (
                        <div aria-label="解读对话" className="max-h-[55dvh] overflow-y-auto bg-ink-50 px-4 py-4">
                            <ChatTranscript messages={messages} />
                        </div>
                    )}
                    {pending && <p role="status" className="px-4 pt-3 text-sm text-ink-600">正在处理…</p>}
                    {review && <ReportReviewForm key={review.id} items={review.items} busy={pending}
                        onCompare={rows => void ask('按已核对的项目比对。', undefined, rows)} />}
                    <form className="space-y-2 p-4" onSubmit={submit}>
                        <WebSearchButton enabled={webSearch} onToggle={() => setWebSearch(enabled => !enabled)} disabled={busy || restoreError} />
                        <div className="flex gap-2">
                            <input type="text" value={question} onChange={event => setQuestion(event.target.value)}
                                aria-label="解读问题" placeholder="输入检验项目或继续追问"
                                className="min-w-0 flex-1 rounded-lg border border-ink-200 px-3 py-2 text-sm" />
                            <button type="submit" disabled={busy || restoreError || !question.trim()} className={`${action} shrink-0`}>
                                <Send size={15} aria-hidden /> 发送
                            </button>
                        </div>
                    </form>
                </div>
            </section>
        </div>
    );
}
