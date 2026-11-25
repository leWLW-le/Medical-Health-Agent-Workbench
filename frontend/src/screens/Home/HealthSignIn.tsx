import { useState, type FormEvent } from 'react';
import { FileText, HeartPulse, LockKeyhole, MessageCircle, Pill, Stethoscope } from 'lucide-react';
import { signInHealthAccount, type HealthAccount } from '../../services/healthAccount';

const features = [
    { label: '健康对话', description: '了解日常健康问题', icon: MessageCircle },
    { label: 'AI 诊室', description: '梳理症状与就诊信息', icon: Stethoscope },
    { label: '药管家', description: '查询药品说明与注意事项', icon: Pill },
    { label: '报告内容解析', description: '识别检验结果并定位原页', icon: FileText },
];

const inputClass = 'mt-1.5 block w-full rounded-xl border border-ink-200 bg-ink-50/65 px-3.5 py-3 text-base text-ink-900 transition-colors focus:border-brand-400 focus:bg-white focus:outline-none focus:ring-2 focus:ring-brand-100 sm:text-sm';

export default function HealthSignIn({ onAccount }: { onAccount: (account: HealthAccount) => void }) {
    const [register, setRegister] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');

    async function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault();
        if (busy) return;
        setBusy(true);
        setError('');
        try {
            onAccount(await signInHealthAccount(register, username.trim(), password));
        } catch (problem) {
            setError(problem instanceof Error ? problem.message : '请求失败，请重试。');
        } finally {
            setBusy(false);
        }
    }

    return (
        <div className="h-full overflow-y-auto">
            <div className="mx-auto grid min-h-full max-w-[1088px] place-items-center px-4 py-7 sm:px-8 lg:py-10">
                <div className="workspace-surface grid w-full max-w-md overflow-hidden rounded-[28px] border border-ink-200/80 bg-white lg:max-w-none lg:grid-cols-[1.12fr_1fr]">
                    <section aria-labelledby="signin-product-title" className="auth-story relative hidden min-w-0 flex-col justify-center gap-8 overflow-hidden px-9 py-12 text-white lg:flex xl:px-11">
                        <div className="relative">
                            <span className="mb-5 grid h-12 w-12 place-items-center rounded-2xl border border-white/25 bg-white/10" aria-hidden><HeartPulse size={25} /></span>
                            <h2 id="signin-product-title" className="text-[2rem] font-bold leading-snug">一个工作区，<br />查看健康问题与报告</h2>
                        </div>
                        <ul className="relative grid grid-cols-2 gap-3">
                            {features.map(({ label, description, icon: Icon }) => <li key={label} className="rounded-2xl border border-white/15 bg-white/[0.08] px-3.5 py-3">
                                <Icon size={19} className="mb-2 text-brand-200" aria-hidden />
                                <p className="text-sm font-semibold">{label}</p>
                                <p className="mt-1 text-xs leading-relaxed text-blue-100">{description}</p>
                            </li>)}
                        </ul>
                    </section>
                    <form onSubmit={submit} className="flex flex-col justify-center gap-5 p-6 sm:p-9 lg:px-10 lg:py-12">
                        <div>
                            <span className="mb-4 grid h-11 w-11 place-items-center rounded-2xl border border-brand-100 bg-brand-50 text-brand-700" aria-hidden><LockKeyhole size={20} /></span>
                            <h1 className="text-[1.6rem] font-bold leading-snug text-ink-900">{register ? '创建体验账号' : '登录医疗健康 Agent 工作台'}</h1>
                            <p className="mt-2 rounded-xl bg-brand-50/60 px-3 py-2.5 text-sm leading-relaxed text-ink-700">本体验环境仅用于模拟数据，请勿填写真实病历或个人健康信息。</p>
                        </div>
                        <label className="block text-sm font-semibold text-ink-700">
                            用户名
                            <input name="username" value={username} onChange={e => setUsername(e.target.value)}
                                autoComplete="username" required minLength={3} maxLength={32} pattern="[A-Za-z0-9_]+"
                                className={inputClass} />
                        </label>
                        <label className="block text-sm font-semibold text-ink-700">
                            密码
                            <input name="password" type="password" value={password} onChange={e => setPassword(e.target.value)}
                                autoComplete={register ? 'new-password' : 'current-password'} required minLength={12} maxLength={128}
                                className={inputClass} />
                        </label>
                        {error && <p role="alert" className="rounded-xl border border-danger-200 bg-danger-50 px-3 py-2.5 text-sm text-danger-800">{error}</p>}
                        <div className="space-y-2">
                            <button type="submit" disabled={busy}
                                className="w-full rounded-xl bg-brand-700 px-4 py-3 text-sm font-semibold text-white shadow-raised transition-colors hover:bg-brand-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 disabled:opacity-50">
                                {busy ? '处理中…' : register ? '注册并登录' : '登录'}
                            </button>
                            <button type="button" disabled={busy} onClick={() => { setRegister(!register); setError(''); setPassword(''); }}
                                className="w-full rounded-xl border border-transparent px-4 py-2.5 text-sm font-semibold text-brand-800 transition-colors hover:border-brand-100 hover:bg-brand-50 focus-visible:outline-brand-600">
                                {register ? '已有账号，返回登录' : '创建体验账号'}
                            </button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    );
}
