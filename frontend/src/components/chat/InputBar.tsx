import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, Barcode, FileImage, PackageSearch, Paperclip, X } from 'lucide-react';
import { useGlobalStore } from '../../store/GlobalContext';
import { isVisionImage, visionImageTypes, type VisionScanType } from '../../services/chatService';
import { managedHealthChat } from '../../services/healthAccount';
import WebSearchButton from './WebSearchButton';

type AttachmentType = Exclude<VisionScanType, 'report'>;
const attachments = [
    { id: 'drug_box', label: '拍药盒', icon: PackageSearch },
    { id: 'trace_code', label: '扫追溯码', icon: Barcode },
] as const;
const iconButton = 'grid h-9 w-9 place-items-center rounded-lg text-ink-600 hover:bg-ink-100 hover:text-brand-700 disabled:opacity-50';

interface InputBarProps {
    inputValue: string;
    setInputValue: (value: string) => void;
    onSend: () => void;
    onVisionUpload?: (file: File, scanType: AttachmentType) => void;
    attachment?: { accept: string; label: string; onPick: (file: File) => void };
    placeholder?: string;
    inputLabel?: string;
    inputDisabled?: boolean;
    maxLength?: number;
    sendDisabled?: boolean;
    isBusy?: boolean;
    webSearch?: { enabled: boolean; onToggle: () => void };
}

export default function InputBar({ inputValue, setInputValue, onSend, onVisionUpload, attachment, isBusy = false,
    webSearch, placeholder, inputLabel = '咨询消息', inputDisabled, maxLength, sendDisabled }: InputBarProps) {
    const { isElderMode, chatMode } = useGlobalStore();
    const [attachmentOpen, setAttachmentOpen] = useState(false);
    const [uploadError, setUploadError] = useState('');
    const fileInput = useRef<HTMLInputElement>(null);
    const textarea = useRef<HTMLTextAreaElement>(null);
    const scanType = useRef<AttachmentType>('drug_box');
    const canSend = !isBusy && !inputDisabled && !sendDisabled && Boolean(inputValue.trim());
    const defaultPlaceholder = chatMode === 'clinic' ? '描述症状、持续时间和严重程度…'
        : chatMode === 'pharmacy' ? '输入药品名称或用药问题…' : '输入健康问题…';

    useLayoutEffect(() => {
        const field = textarea.current;
        if (!field) return;
        field.style.height = '0px';
        field.style.height = Math.min(field.scrollHeight, 144) + 'px';
    }, [inputValue, isElderMode]);

    return (
        <div className="rounded-[18px] border border-ink-200 bg-[#f9faff] px-3.5 pb-2 pt-3 shadow-sm focus-within:border-brand-300 focus-within:ring-2 focus-within:ring-brand-100 sm:px-4">
            <textarea ref={textarea} id="consultation-message" rows={2} aria-label={inputLabel} disabled={inputDisabled}
                maxLength={maxLength ?? (managedHealthChat ? 2000 : undefined)} value={inputValue} placeholder={placeholder ?? defaultPlaceholder}
                onChange={event => setInputValue(event.target.value)}
                onKeyDown={event => {
                    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229
                        || window.matchMedia('(pointer: coarse)').matches) return;
                    event.preventDefault();
                    if (canSend) onSend();
                }}
                className={`block min-h-[48px] w-full resize-none overflow-y-auto bg-transparent p-0 leading-relaxed text-ink-900 placeholder:text-ink-500 focus:outline-none
                    ${isElderMode ? 'text-title' : 'text-base'}`} />
            <div className="mt-1.5 flex min-h-9 items-center gap-1.5">
                {attachment ? <label className={`flex h-9 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-xs font-medium text-ink-700 hover:bg-brand-50 hover:text-brand-800 focus-within:ring-2 focus-within:ring-brand-300 ${isBusy ? 'cursor-default opacity-50' : ''}`}>
                    <Paperclip size={17} aria-hidden /> {attachment.label}
                    <input id="consultation-attachment" type="file" className="sr-only" aria-label={attachment.label} accept={attachment.accept} disabled={isBusy}
                        onChange={event => {
                            const file = event.target.files?.[0];
                            event.target.value = '';
                            if (file) attachment.onPick(file);
                        }} />
                </label> : onVisionUpload && <div className="relative flex">
                    <input ref={fileInput} type="file" accept={visionImageTypes.join(',')} aria-label="选择药品照片" hidden
                        onChange={event => {
                            const file = event.target.files?.[0];
                            event.target.value = '';
                            if (!file) return;
                            if (!isVisionImage(file)) {
                                setUploadError('请选择 8 MB 以内的 JPG、PNG 或 WebP 图片。');
                                return;
                            }
                            setUploadError('');
                            onVisionUpload(file, scanType.current);
                        }} />
                    <button type="button" aria-label="打开图片上传菜单" aria-expanded={attachmentOpen} title="上传图片"
                        className="flex h-9 items-center gap-1.5 rounded-lg px-2 text-xs font-medium text-ink-700 hover:bg-brand-50 hover:text-brand-800"
                        onClick={() => { setUploadError(''); setAttachmentOpen(open => !open); }}>
                        <Paperclip size={17} aria-hidden /> 上传图片
                    </button>
                    {attachmentOpen && (
                        <div className="absolute bottom-full left-0 z-20 mb-2 w-44 rounded-xl border border-ink-200 bg-white p-1.5 shadow-floating">
                            <div className="flex items-center justify-between px-2 text-xs text-ink-600">图片上传
                                <button type="button" aria-label="关闭附件菜单" onClick={() => setAttachmentOpen(false)} className={iconButton}><X size={15} aria-hidden /></button>
                            </div>
                            {attachments.map(({ id, label, icon: Icon }) => (
                                <button key={id} type="button" disabled={isBusy}
                                    className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2 text-sm hover:bg-brand-50 disabled:opacity-50"
                                    onClick={() => {
                                        scanType.current = id;
                                        setAttachmentOpen(false);
                                        fileInput.current?.click();
                                    }}>
                                    <Icon size={17} className="text-brand-700" aria-hidden /> {label}
                                </button>
                            ))}
                            <a href="#/report-photo" onClick={() => setAttachmentOpen(false)}
                                className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2 text-sm hover:bg-brand-50">
                                <FileImage size={17} className="text-brand-700" aria-hidden /> 报告照片解读
                            </a>
                        </div>
                    )}
                </div>}
                {webSearch && <WebSearchButton enabled={webSearch.enabled} onToggle={webSearch.onToggle} disabled={isBusy} />}
                <span className="hidden text-xs text-ink-500 sm:[@media(pointer:fine)]:inline">Shift + Enter 换行</span>
                <div className="ml-auto flex items-center gap-1">
                    <button type="button" onClick={onSend} disabled={!canSend} aria-label="发送" title="发送"
                        className="grid h-9 w-9 place-items-center rounded-full bg-brand-700 text-white hover:bg-brand-800 disabled:bg-brand-100 disabled:text-brand-700">
                        <ArrowUp size={19} strokeWidth={2.3} aria-hidden />
                    </button>
                </div>
            </div>
            {uploadError && <p role="alert" className="mt-1 px-1 text-xs text-red-700">{uploadError}</p>}
        </div>
    );
}
