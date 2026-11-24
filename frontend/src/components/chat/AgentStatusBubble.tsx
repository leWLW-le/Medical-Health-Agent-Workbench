import { Check, ChevronDown, Circle, MessageCircle } from 'lucide-react';
import type { AgentStep } from '../../types';
import { useGlobalStore } from '../../store/GlobalContext';

interface AgentStatusBubbleProps {
    steps: AgentStep[];
    isGenerating: boolean;
    awaitingReply: boolean;
}

const Spinner = ({ size }: { size: string }) => (
    <span className={`inline-block ${size} shrink-0 animate-spin rounded-full border-2 border-accent-400 border-t-transparent motion-reduce:animate-none`} />
);

export default function AgentStatusBubble({ steps, isGenerating, awaitingReply }: AgentStatusBubbleProps) {
    const { isElderMode } = useGlobalStore();
    if (steps.length === 0 && !isGenerating && !awaitingReply) return null;

    const unfinished = steps.filter(step => !step.isFinished);
    const activeStep = unfinished.at(-1);
    const textSize = isElderMode ? 'text-body' : 'text-caption';
    const status = isGenerating
        ? activeStep?.content ?? '正在生成回复…'
        : awaitingReply
            ? '等待补充信息'
            : unfinished.length ? '本轮已结束' : '已完成';

    return (
        <details open={isGenerating} className="group">
            <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden">
                <span className={`inline-flex items-center gap-2 rounded-full bg-white/90 px-3 py-1.5 shadow-raised ${textSize} ${isGenerating ? 'text-accent-700' : 'text-ink-600'}`}>
                    {isGenerating ? <Spinner size="h-3 w-3" /> : awaitingReply ? <MessageCircle size={14} /> :
                        unfinished.length ? <Circle size={14} /> : <Check size={14} className="text-success-600" />}
                    <span>{status}</span>
                    {steps.length > 0 && (
                        <ChevronDown size={14} className="text-ink-400 group-open:rotate-180 transition-transform duration-base" />
                    )}
                </span>
            </summary>

            {steps.length > 0 && (
                <ol className="mt-2 ml-3 pl-3 border-l border-ink-200 space-y-1">
                    {steps.map(step => (
                        <li key={step.id} className={`flex items-center gap-2 ${textSize}`}>
                            {step.isFinished ? <Check size={12} className="shrink-0 text-success-600" /> :
                                isGenerating ? <Spinner size="h-2.5 w-2.5" /> : <Circle size={12} className="shrink-0 text-ink-400" />}
                            <span className={step.isFinished ? 'text-ink-500' : 'text-ink-800'}>{step.content}</span>
                        </li>
                    ))}
                </ol>
            )}
        </details>
    );
}
