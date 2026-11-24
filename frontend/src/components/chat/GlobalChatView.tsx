import { useEffect, useRef, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useGlobalStore } from '../../store/GlobalContext';
import type { ChatMessage } from '../../types';
import AgentStatusBubble from './AgentStatusBubble';
import ChatCardRenderer from './ChatCardRenderer';

type MessageRow = Omit<ChatMessage, 'id' | 'timestamp'>;

export function ChatMessageRow({ message, children }: { message: MessageRow; children?: ReactNode }) {
    const { isElderMode } = useGlobalStore();
    const { role, text, isGenerating, awaitingReply, steps, cards } = message;
    return <div className="flex w-full flex-col space-y-2 animate-rise motion-reduce:animate-none">
        {role === 'assistant' && (isGenerating || awaitingReply || Boolean(steps?.length)) && (
            <div className="flex justify-start">
                <div className="max-w-[85%]">
                    <AgentStatusBubble steps={steps ?? []} isGenerating={isGenerating ?? false} awaitingReply={awaitingReply ?? false} />
                </div>
            </div>
        )}
        {cards && cards.length > 0 && <div className="flex w-full flex-col items-center space-y-3 pb-2">
            {cards.map((card, index) => (
                <div key={index} className={card.type === 'knowledge_retrieval' || card.type === 'web_search' ? 'w-full max-w-xl self-start' : 'w-full max-w-sm'}>
                    <ChatCardRenderer payload={card} />
                </div>
            ))}
        </div>}
        {(children || text || isGenerating) && (
            <div className={`flex ${role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`overflow-hidden break-words rounded-2xl py-2.5 ${role === 'user'
                    ? 'max-w-[85%] bg-brand-50 px-4 text-ink-900' : 'max-w-full text-ink-800'
                    } ${isElderMode ? 'text-headline' : 'text-callout'}`}>
                    {children ?? (text && role === 'user' ? <p className="whitespace-pre-wrap">{text}</p> : text ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
                            p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
                            ul: ({ children }) => <ul className="mb-2 list-disc pl-5">{children}</ul>,
                            ol: ({ children }) => <ol className="mb-2 list-decimal pl-5">{children}</ol>,
                            li: ({ children }) => <li className="mb-1">{children}</li>,
                            strong: ({ children }) => <strong className="font-semibold text-ink-900">{children}</strong>,
                            h3: ({ children }) => <h3 className="mb-1 mt-3 text-title font-semibold">{children}</h3>,
                            a: ({ href, children }) => <a href={href} className="text-brand-700 underline underline-offset-2">{children}</a>,
                        }}>{text}</ReactMarkdown>
                    ) : (
                        <span className="inline-flex h-6 items-center gap-1" aria-label="正在输入">
                            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-300 motion-reduce:animate-none" />
                            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-300 motion-reduce:animate-none [animation-delay:150ms]" />
                            <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-300 motion-reduce:animate-none [animation-delay:300ms]" />
                        </span>
                    ))}
                </div>
            </div>
        )}
    </div>;
}

export function ChatTranscript({ messages }: { messages: ChatMessage[] }) {
    const chatEndRef = useRef<HTMLDivElement>(null);
    useEffect(() => {
        if (messages.length < 2 || !chatEndRef.current?.getClientRects().length) return;
        chatEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, [messages]);
    return <div className="mx-auto w-full max-w-4xl space-y-4">
        {messages.map(message => <ChatMessageRow key={message.id} message={message} />)}
        <div ref={chatEndRef} className="h-px" aria-hidden />
    </div>;
}

export default function GlobalChatView() {
    const { messages } = useGlobalStore();
    return <ChatTranscript messages={messages} />;
}
