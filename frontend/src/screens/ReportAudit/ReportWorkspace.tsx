import { Camera, ClipboardCheck } from 'lucide-react';
import type { ReactNode } from 'react';
import ConversationFrame from '../../components/layout/ConversationFrame';
import ReportAuditScreen from './ReportAuditScreen';
import ReportPhotoScreen from './ReportPhotoScreen';
import ReportReadingScreen from './ReportReadingScreen';
import { usesApplication } from '../../services/reportAudit';

export default function ReportWorkspace({ photo, demo, username, navigation }: {
    photo: boolean; demo: boolean; username?: string; navigation: ReactNode;
}) {
    if (usesApplication && !demo) return <ReportReadingScreen navigation={navigation} />;
    const tab = 'inline-flex min-h-10 shrink-0 items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium';
    const active = 'border-brand-700 text-brand-800';
    const idle = 'border-transparent text-ink-600 hover:text-brand-800';

    return (
        <ConversationFrame navigation={navigation}><div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="shrink-0 border-b border-ink-200 bg-white">
                <nav aria-label="报告操作" className="mx-auto flex w-full max-w-[1440px] gap-2 px-4 lg:px-8">
                    <a href={demo ? '#/report-audit-demo' : '#/report-audit'} aria-current={!photo ? 'page' : undefined} className={`${tab} ${photo ? idle : active}`}>
                        <ClipboardCheck size={17} aria-hidden /> 检验核对
                    </a>
                    <a href={demo ? '#/report-photo-demo' : '#/report-photo'} aria-current={photo ? 'page' : undefined} className={`${tab} ${photo ? active : idle}`}>
                        <Camera size={17} aria-hidden /> 拍照解读
                    </a>
                </nav>
            </div>
            <div hidden={photo} className="min-h-0 flex-1">
                <ReportAuditScreen username={username} />
            </div>
            <div hidden={!photo} className="min-h-0 flex-1">
                <ReportPhotoScreen username={username} />
            </div>
        </div></ConversationFrame>
    );
}
