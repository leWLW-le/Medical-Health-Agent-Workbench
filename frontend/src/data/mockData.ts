import type { ConversationMode } from '../store/chatHistory';
import { MessageCircle, Stethoscope, Pill } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export const USER_NAME = "王*虎";

// Map modules with their specific chat mode target
export const MODULES: { id: ConversationMode; name: string; icon: LucideIcon }[] = [
    { id: 'general', name: '健康对话', icon: MessageCircle },
    { id: 'clinic', name: 'AI诊室', icon: Stethoscope },
    { id: 'pharmacy', name: '药管家', icon: Pill },
];
