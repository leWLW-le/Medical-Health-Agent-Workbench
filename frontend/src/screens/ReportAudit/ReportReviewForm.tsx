import { useState } from 'react';
import type { ReportItemInput } from '../../types';

type Draft = Record<keyof ReportItemInput, string> & { id: number };

const columns = [
    ['name', '项目'],
    ['value', '结果'],
    ['unit', '单位'],
    ['reference_range', '参考范围'],
] as const;

const fieldClass = 'w-full min-w-0 rounded-md border border-ink-200 bg-white px-2 py-1.5 text-sm';
const emptyRow = (id: number): Draft => ({ id, name: '', value: '', unit: '', reference_range: '' });

export default function ReportReviewForm({ items, busy, onCompare }: {
    items: ReportItemInput[];
    busy: boolean;
    onCompare: (rows: ReportItemInput[]) => void;
}) {
    const [rows, setRows] = useState<Draft[]>(() => items.length ? items.map((item, id) => ({
        id, name: item.name, value: String(item.value), unit: item.unit ?? '', reference_range: item.reference_range ?? '',
    })) : [emptyRow(0)]);
    const [nextId, setNextId] = useState(Math.max(items.length, 1));

    return (
        <form aria-label="核对提取结果" className="border-t border-ink-200 p-4" onSubmit={event => {
            event.preventDefault();
            if (busy || !rows.length || rows.some(row => !row.name.trim() || !row.value.trim() || !Number.isFinite(Number(row.value)))) return;
            onCompare(rows.map(row => ({
                name: row.name.trim(), value: Number(row.value),
                unit: row.unit.trim() || null, reference_range: row.reference_range.trim() || null,
            })));
        }}>
            <h2 className="font-semibold text-ink-900">核对提取结果</h2>
            <p className="mt-1 text-sm text-ink-600">请逐行对照原图并修正。有参考范围就照原文填写，没有就留空。</p>
            <div className="mt-3 max-h-[45dvh] space-y-2 overflow-y-auto">
                {rows.map((row, index) => (
                    <fieldset key={row.id} disabled={busy} className="grid grid-cols-2 gap-2 rounded-lg border border-ink-200 p-2 sm:grid-cols-4">
                        <legend className="sr-only">第 {index + 1} 项</legend>
                        {columns.map(([key, label]) => (
                            <label key={key} className="min-w-0 text-xs text-ink-700">
                                {label}
                                <input value={row[key]} required={key === 'name' || key === 'value'}
                                    type={key === 'value' ? 'number' : 'text'} step={key === 'value' ? 'any' : undefined}
                                    maxLength={key === 'name' ? 120 : undefined}
                                    className={fieldClass} onChange={event => {
                                        const value = event.target.value;
                                        setRows(previous => previous.map(item => item.id === row.id ? { ...item, [key]: value } : item));
                                    }} />
                            </label>
                        ))}
                        <button type="button" className="col-span-2 justify-self-start px-1 text-xs text-ink-600 underline sm:col-span-4"
                            onClick={() => setRows(previous => previous.filter(item => item.id !== row.id))}>移除本项</button>
                    </fieldset>
                ))}
            </div>
            <div className="mt-3 flex flex-wrap justify-between gap-2">
                <button type="button" disabled={busy || rows.length >= 200} className="rounded-lg border border-ink-200 px-3 py-2 text-sm disabled:opacity-50"
                    onClick={() => { setRows(previous => [...previous, emptyRow(nextId)]); setNextId(nextId + 1); }}>添加一项</button>
                <button type="submit" disabled={busy || !rows.length} className="rounded-lg bg-brand-700 px-3 py-2 text-sm font-medium text-white disabled:opacity-50">
                    已核对，开始比对
                </button>
            </div>
        </form>
    );
}
