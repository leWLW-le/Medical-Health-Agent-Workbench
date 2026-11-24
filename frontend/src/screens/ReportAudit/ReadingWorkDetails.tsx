import type { ReadingCitation, ReadingEvidenceCheck, ReadingTaskProgress, ReadingWorkRecord } from '../../services/reportReading';

const phaseLabels = {
    initial: '首次生成', after_tools: '根据工具结果生成', rewrite: '校验后重写', timeout_retry: '超时后用现有证据重试',
    coverage_repair: '按缺项补写',
};
const callLabels = { returned: '已返回', failed: '调用失败', timeout: '超时' };
const rejectionLabels = {
    invalid_format: '输出格式不符合要求', unverified_output: '输出未通过证据或内容校验',
    missing_groups: '缺少必答异常组', missing_medication_evidence: '缺少有证据支持的用药解读',
};
const fallbackLabels = {
    provider_error: '模型调用失败', provider_timeout: '模型服务连接或响应超时',
    model_call_timeout: '单次模型调用超过时限', agent_timeout: '报告解读超过整轮时限',
    output_rejected: '模型回答未通过校验', invalid_tool_requests: '模型返回了无效的工具调用',
    tool_budget_exhausted: '已达到工具执行轮数上限', tool_failed: '工具执行失败',
    configuration_error: '模型配置错误', internal_error: '报告 Agent 执行异常',
};
const skipLabels = {
    emergency: '按紧急规则直接回答', pending_confirmation: '所问字段待确认', no_evidence: '缺少可供模型解读的报告依据',
    evidence_purpose: '按已审核资料解释查证目的',
};
const toolLabels = { completed: '已执行', empty: '无结果', invalid_request: '请求无效', failed: '执行失败' };
const stopLabels: Record<ReadingTaskProgress['stop_reason'], string> = {
    evidence_ready: '依据已核验，完成本次解读', awaiting_patient: '已保存进度，等待回复', user_finished: '按现有证据收尾',
    question_limit: '追问达到本次上限，按现有证据收尾', evidence_missing: '现有报告或自述缺少部分证据',
    provider_fallback: '使用已核验的报告依据和规则收尾', urgent: '按紧急规则直接回复',
};
const evidenceLabels: Record<ReadingEvidenceCheck['status'], string> = {
    confirmed: '已确认字段', missing: '未在识别结果中找到', pending_confirmation: '待核对', ambiguous: '无法匹配采样',
};

export default function ReadingWorkDetails({ record, citations, task, evidenceChecks = [] }: {
    record: ReadingWorkRecord; citations: ReadingCitation[]; task?: ReadingTaskProgress | null; evidenceChecks?: ReadingEvidenceCheck[];
}) {
    const namesByRow = new Map(citations.map(({ row, name }) => [row, name]));
    return (
        <details className="mt-2 rounded-lg border border-ink-200 p-3">
            <summary className="cursor-pointer font-medium text-brand-800">查看工作记录</summary>
            <div className="mt-2 space-y-2 text-xs">
                <p>模型调用 {record.model_call_count} 次 · 工具执行 {record.tool_rounds.length} 轮</p>
                {task && <p>{task.resumed ? '从已保存进度继续' : '从当前报告开始'} · {stopLabels[task.stop_reason]}</p>}
                {record.fallback_reason && <p>回退原因 · {fallbackLabels[record.fallback_reason]}</p>}
                {record.skip_reason && <p>未调用模型 · {skipLabels[record.skip_reason]}</p>}
                {evidenceChecks.length > 0 && <ul aria-label="关键证据查证记录" className="space-y-2 border-t border-ink-100 pt-2">
                    {evidenceChecks.map(check => <li key={check.indicator}>
                        <p>{check.action === 'lookup_report' ? '自动查找' : '复用报告依据'} · {check.indicator} · {evidenceLabels[check.status]}</p>
                        <p className="text-ink-600">{check.reason}。{check.detail}</p>
                    </li>)}
                </ul>}
                {record.model_calls.length > 0 && <ol aria-label="模型调用记录" className="space-y-1 border-t border-ink-100 pt-2">
                    {record.model_calls.map((call, index) => <li key={index}>
                        <p>第 {index + 1} 次 · {phaseLabels[call.phase]} · {callLabels[call.status]} · {call.elapsed_ms} ms
                            {call.http_status !== null && ` · HTTP ${call.http_status}`}</p>
                        {call.rejection && <p className="pl-2 text-ink-600">{rejectionLabels[call.rejection]}</p>}
                        {Boolean(call.missing_groups?.length) && <p className="pl-2 text-ink-600">
                            未覆盖 · {call.missing_groups?.map(group => group.map(row => namesByRow.get(row) ?? `报告记录 ${row + 1}`).join('、')).join(' / ')}
                        </p>}
                    </li>)}
                </ol>}
                {record.tool_rounds.length > 0 && <ol aria-label="工具执行记录" className="space-y-2 border-t border-ink-100 pt-2">
                    {record.tool_rounds.map((round, index) => <li key={index}>
                        <p className="font-medium">第 {index + 1} 轮工具</p>
                        <ul className="space-y-1 pl-2">
                            {round.map((tool, toolIndex) => <li key={toolIndex}>
                                <p><code>{tool.name === 'unknown' ? '未注册工具' : tool.name}</code> · {toolLabels[tool.status]}</p>
                                <p className="text-ink-600">{tool.result}</p>
                            </li>)}
                        </ul>
                    </li>)}
                </ol>}
                <p className="break-all text-ink-600">请求 ID · {record.request_id}</p>
            </div>
        </details>
    );
}
