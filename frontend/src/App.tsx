import { useEffect, useState, useSyncExternalStore } from 'react';
import { HeartPulse, LayoutDashboard, Settings2 } from 'lucide-react';
import { GlobalProvider, useGlobalStore } from './store/GlobalContext';
import ConsultationWorkspace from './screens/Home/ConsultationWorkspace';
import ReportWorkspace from './screens/ReportAudit/ReportWorkspace';
import HealthSignIn from './screens/Home/HealthSignIn';
import { getHealthAccount, healthRequest, healthSessionExpired, managedHealthChat, type HealthAccount } from './services/healthAccount';
import { usesApplication } from './services/reportAudit';

const auditRoute = '#/report-audit';
const photoRoute = '#/report-photo';
const auditDemoRoute = '#/report-audit-demo';
const photoDemoRoute = '#/report-photo-demo';
const reportRoutes = new Set([auditRoute, photoRoute, auditDemoRoute, photoDemoRoute]);

function SiteHeader({ isReports, signOut, username }: { isReports: boolean; signOut?: () => void; username?: string }) {
  const { isElderMode, setIsElderMode } = useGlobalStore();
  const tab = 'inline-flex min-h-9 shrink-0 items-center gap-2 rounded-full border border-brand-100 px-3 text-xs font-semibold transition-colors sm:px-3.5 sm:text-sm';

  return (
    <header className="relative z-30 shrink-0 border-b border-ink-200/80 bg-white/90 backdrop-blur-sm">
      <div className="mx-auto flex w-full max-w-[1680px] items-center gap-x-3 px-3.5 pb-2 pt-[max(env(safe-area-inset-top),0.55rem)] sm:gap-x-5 sm:px-7 sm:py-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span aria-hidden="true" className="brand-mark grid h-9 w-9 shrink-0 place-items-center rounded-xl text-white"><HeartPulse size={19} /></span>
          <span className="truncate text-sm font-bold text-ink-900 sm:text-base">医疗健康 Agent 工作台</span>
        </div>
        <nav aria-label="主导航" className="shrink-0">
          <a href="#" aria-label={isReports ? '返回健康助手' : undefined} aria-current={isReports ? 'location' : 'page'}
            className={`${tab} bg-brand-50/70 text-brand-800 hover:bg-brand-100`}>
            <LayoutDashboard size={16} className="hidden sm:block" aria-hidden /> 健康助手
          </a>
        </nav>
        <details className="relative ml-auto">
          <summary aria-label="账户与显示设置" title="账户与显示设置"
            className="grid h-9 w-9 cursor-pointer list-none place-items-center rounded-full border border-brand-100 bg-brand-50 text-sm font-semibold text-brand-800 transition-colors hover:bg-brand-100 focus-visible:outline-brand-600 [&::-webkit-details-marker]:hidden">
            {username ? username.slice(0, 1).toUpperCase() : <Settings2 size={17} aria-hidden />}
          </summary>
          <div className="absolute right-0 z-40 mt-2 w-44 rounded-xl border border-ink-200 bg-white p-1.5 shadow-floating">
            {username && <div className="truncate border-b border-ink-200 px-2.5 py-2 text-xs text-ink-600">{username}</div>}
            <button type="button" role="switch" aria-checked={isElderMode} onClick={() => setIsElderMode(!isElderMode)}
              className="flex w-full items-center justify-between rounded-lg px-2.5 py-2 text-sm text-ink-800 hover:bg-ink-50">
              长辈模式
              <span aria-hidden="true" className={`relative h-5 w-9 rounded-full ${isElderMode ? 'bg-brand-700' : 'bg-ink-200'}`}>
                <span className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${isElderMode ? 'translate-x-4' : ''}`} />
              </span>
            </button>
            {signOut && <button type="button" onClick={signOut}
              className="w-full rounded-lg px-2.5 py-2 text-left text-sm text-ink-700 hover:bg-ink-50">退出登录</button>}
          </div>
        </details>
      </div>
    </header>
  );
}

function AppContent({ hash, content, signOut, username }: { hash: string; content: React.ReactNode; signOut?: () => void; username?: string }) {
  const isReports = reportRoutes.has(hash);

  return (
    <div className="app-canvas relative flex h-[100dvh] w-full flex-col overflow-hidden font-cn text-ink-900">
      <SiteHeader isReports={isReports} signOut={signOut} username={username} />
      <main className="min-h-0 flex-1 overflow-hidden">
        {content}
      </main>
    </div>
  );
}

function subscribeToHash(callback: () => void) {
  window.addEventListener('hashchange', callback);
  return () => window.removeEventListener('hashchange', callback);
}

export default function App() {
  const hash = useSyncExternalStore(subscribeToHash, () => window.location.hash);
  const hasAccounts = managedHealthChat || usesApplication;
  const [account, setAccount] = useState<HealthAccount | null>(null);
  const [checking, setChecking] = useState(hasAccounts);
  const [authError, setAuthError] = useState('');
  const [checkAttempt, setCheckAttempt] = useState(0);

  useEffect(() => {
    if (!hasAccounts) return;
    const controller = new AbortController();
    const expireSession = () => setAccount(null);
    window.addEventListener(healthSessionExpired, expireSession);
    void getHealthAccount(controller.signal).then(user => {
      if (!controller.signal.aborted) setAccount(user);
    }).catch(error => {
      if (!controller.signal.aborted) setAuthError(error.message);
    }).finally(() => {
      if (!controller.signal.aborted) setChecking(false);
    });
    return () => {
      controller.abort();
      window.removeEventListener(healthSessionExpired, expireSession);
    };
  }, [checkAttempt, hasAccounts]);

  async function signOut() {
    try {
      await healthRequest('/logout', { method: 'POST' });
      setAccount(null);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : '退出登录失败，请重试。');
    }
  }

  const isReports = reportRoutes.has(hash);
  let content = <ConsultationWorkspace reportHref={auditRoute} report={isReports
    ? navigation => <ReportWorkspace navigation={navigation} photo={hash === photoRoute || hash === photoDemoRoute}
        demo={hash === auditDemoRoute || hash === photoDemoRoute} username={account?.username} />
    : undefined} />;
  if ((isReports ? hasAccounts : managedHealthChat) && !account) {
    content = checking
      ? <p className="p-6 text-ink-600" role="status">正在检查登录状态…</p>
      : authError ? <div className="space-y-3 p-6">
          <p role="alert">{authError}</p>
          <button type="button" className="rounded-lg bg-brand-700 px-4 py-2 text-white"
            onClick={() => { setChecking(true); setAuthError(''); setCheckAttempt(value => value + 1); }}>重试</button>
        </div>
        : <HealthSignIn onAccount={setAccount} />;
  }
  return (
    <GlobalProvider key={account ? 'account:' + account.username : 'anonymous'} serverAccount={account?.username}>
      <AppContent hash={hash} content={content} signOut={account ? signOut : undefined} username={account?.username} />
    </GlobalProvider>
  );
}
