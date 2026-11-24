const assert = require('node:assert/strict');
const { basename } = require('node:path');
const { chromium } = require('playwright');

const api = '/api/health/report-reading';
const base = process.env.AUDIT_WEB_URL || 'http://127.0.0.1:5173';

async function savedReport(page, id) {
    const response = await page.request.get(`${base}${api}/${id}`, { maxRetries: 2 });
    assert.equal(response.status(), 200);
    return response.json();
}

async function openSavedReport(page, title) {
    const history = page.getByRole('complementary', { name: '报告历史', exact: true });
    const toggle = page.getByRole('button', { name: '展开历史对话', exact: true });
    if ((page.viewportSize()?.width ?? 1280) < 1024 && !(await history.isVisible())) await toggle.click();
    await history.getByRole('button', { name: title, exact: true }).click();
}

async function register(page, suffix) {
    await page.goto(base);
    await page.getByRole('button', { name: '创建体验账号', exact: true }).click();
    await page.getByLabel('用户名').fill(`reading_${Date.now().toString(36)}_${suffix}`);
    await page.getByLabel('密码').fill('synthetic-report-reading-password');
    await page.getByRole('button', { name: '注册并登录', exact: true }).click();
    const modes = page.getByRole('navigation', { name: '咨询模式' });
    await modes.getByRole('link', { name: '报告内容解析', exact: true }).click();
    await page.getByRole('heading', { name: '报告内容解析', exact: true }).waitFor();
}

async function upload(page, file) {
    const input = page.getByLabel('上传报告', { exact: true });
    await input.and(page.locator('input:enabled')).waitFor({ state: 'attached' });
    const [response] = await Promise.all([
        page.waitForResponse(item => item.url().endsWith(api) && item.request().method() === 'POST'),
        input.setInputFiles(file),
    ]);
    const report = await response.json();
    assert.equal(response.status(), 201, report.detail);
    return report;
}

async function run() {
    const pdf = process.env.AUDIT_SCAN_TEST_PDF;
    const photo = process.env.AUDIT_READING_TEST_PHOTO;
    const bloodCount = process.env.AUDIT_READING_TEST_CBC;
    const sideBySide = process.env.AUDIT_READING_TEST_SIDE_BY_SIDE;
    assert.ok(pdf && photo, 'Set AUDIT_SCAN_TEST_PDF and AUDIT_READING_TEST_PHOTO to synthetic table fixtures');
    const browser = await chromium.launch({ headless: true, executablePath: process.env.AUDIT_CHROME_PATH });
    try {
        const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
        page.setDefaultTimeout(40_000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await register(page, 'owner');
        const mainNav = page.getByRole('navigation', { name: '主导航' });
        const modes = page.getByRole('navigation', { name: '咨询模式' });
        const reportTab = modes.getByRole('link', { name: '报告内容解析', exact: true });
        assert.equal(await mainNav.getByRole('link').count(), 1);
        assert.deepEqual((await modes.getByRole('button').allTextContents()).map(label => label.trim()), ['健康对话', 'AI诊室', '药管家']);
        assert.equal(await reportTab.getAttribute('aria-current'), 'page');
        assert.equal(await page.getByRole('textbox', { name: '咨询消息' }).count(), 0);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        assert.equal(await page.getByRole('checkbox', { name: /使用模型解读/ }).count(), 0);
        const report = await upload(page, pdf);
        assert.equal(report.source_kind, 'pdf');
        const patientSheet = page.getByRole('article', { name: '本次体检解读单', exact: true });
        const openSheet = page.getByRole('button', { name: '体检解读单', exact: true });
        const editor = page.getByRole('dialog', { name: '核对字段', exact: true });
        await patientSheet.getByText('有 1 项需要对照原件', { exact: true }).click();
        await patientSheet.getByRole('button', { name: '核对 血红蛋白（HGB） · 第 1 页', exact: true }).click();
        await editor.getByRole('img', { name: '原 PDF 第 1 页', exact: true }).waitFor();
        await editor.getByRole('button', { name: '暂时跳过', exact: true }).click();
        assert.equal(await patientSheet.getByRole('button', { name: '查看 血红蛋白（HGB） 原报告第 1 页', exact: true }).count(), 0);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        const showReport = page.getByRole('button', { name: '查看报告', exact: true });
        await showReport.click();
        const hgb = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /HGB/ }) });
        await page.getByRole('button', { name: '待确认 1', exact: true }).click();
        await hgb.getByText('单位疑似错列', { exact: true }).waitFor();
        await hgb.getByRole('button', { name: '解读此项', exact: true }).click();
        await page.getByText('这一项的识别结果待确认。请先对照原页核对字段，再查看数值解读。', { exact: true }).waitFor();

        const nextSteps = page.getByRole('list', { name: '核对原报告' });
        await nextSteps.getByRole('button', { name: /核对HGB · 第 1 页/ }).click();
        await editor.getByRole('img', { name: '原 PDF 第 1 页', exact: true }).waitFor();
        await editor.getByRole('button', { name: '暂时跳过', exact: true }).click();
        await showReport.click();
        await hgb.getByText('单位疑似错列', { exact: true }).waitFor();
        await hgb.getByRole('button', { name: '核对 HGB 第 1 页', exact: true }).click();
        await editor.getByLabel('单位', { exact: true }).fill('g/L');
        await editor.getByLabel('报告参考值', { exact: true }).fill('110-160');
        await editor.getByRole('button', { name: '保存校正并比对', exact: true }).click();
        await page.getByRole('button', { name: '超出报告参考范围 2', exact: true }).click();
        await hgb.getByText('已手动校正', { exact: true }).waitFor();
        await openSheet.click();
        await patientSheet.getByRole('button', { name: '查看 血红蛋白（HGB） 原报告第 1 页', exact: true }).waitFor();
        await patientSheet.getByText(/170 g\/L · 报告参考范围 110-160/).waitFor();
        assert.equal(await patientSheet.getByText('有 1 项需要对照原件', { exact: true }).count(), 0);
        await showReport.click();
        await hgb.getByRole('button', { name: '解读此项', exact: true }).click();
        await page.getByText('查看报告依据与计算', { exact: true }).click();
        await page.getByText(/负责携带氧气的蛋白质.*170 g\/L/).waitFor();
        const question = '这项结果是多少？';
        await page.getByLabel('追问报告', { exact: true }).fill(question);
        await page.getByRole('button', { name: '发送', exact: true }).click();
        const thread = page.getByRole('list', { name: '报告追问' });
        await thread.getByText(question, { exact: true }).waitFor();

        const wholeQuestion = '异常指标说明身体有什么问题？';
        await page.getByLabel('追问报告', { exact: true }).fill(wholeQuestion);
        await page.getByRole('button', { name: '发送', exact: true }).click();
        const whole = thread.getByRole('listitem').filter({ hasText: wholeQuestion });
        await whole.getByText('查看报告依据与计算', { exact: true }).click();
        await whole.getByText(/2 项超出报告参考范围，0 项待确认/).waitFor();
        await whole.getByText('证据解读', { exact: true }).waitFor();
        await whole.getByText('查看工作记录', { exact: true }).click();
        await whole.getByText(/模型调用 \d+ 次 · 工具执行 \d+ 轮/).waitFor();
        const workId = await whole.getByText(/^请求 ID · /).innerText();
        await whole.getByRole('button', { name: /HGB · 原报告第 1 页/ }).click();
        await editor.getByRole('img', { name: '原 PDF 第 1 页', exact: true }).waitFor();
        await page.keyboard.press('Escape');

        const history = page.getByRole('complementary', { name: '报告历史', exact: true });
        await page.setViewportSize({ width: 1280, height: 800 });
        await history.getByRole('button', { name: '新对话', exact: true }).waitFor();
        const reportComposerClass = await page.getByLabel('追问报告', { exact: true }).getAttribute('class');
        for (const name of ['健康对话', 'AI诊室', '药管家']) {
            await modes.getByRole('button', { name, exact: true }).click();
            const composer = page.getByRole('textbox', { name: '咨询消息' });
            await composer.waitFor();
            assert.equal(await composer.getAttribute('class'), reportComposerClass);
            await page.getByRole('complementary', { name: '历史对话', exact: true }).getByRole('button', { name: '新对话', exact: true }).waitFor();
            assert.equal(await reportTab.getAttribute('aria-current'), null);
        }
        await reportTab.click();
        await page.getByRole('heading', { name: '报告内容解析', exact: true }).waitFor();
        assert.equal(await reportTab.getAttribute('aria-current'), 'page');
        await page.setViewportSize({ width: 390, height: 844 });
        await page.reload();
        await openSavedReport(page, basename(pdf));
        await showReport.click();
        await page.getByRole('button', { name: '全部项目 2', exact: true }).click();
        await hgb.getByText('已手动校正', { exact: true }).waitFor();
        await page.getByRole('button', { name: '返回对话', exact: true }).click();
        await thread.getByText(question, { exact: true }).waitFor();
        await whole.getByText('查看工作记录', { exact: true }).click();
        await whole.getByText(workId, { exact: true }).waitFor();
        await whole.getByText('查看报告依据与计算', { exact: true }).click();
        await whole.getByRole('button', { name: /HGB · 原报告第 1 页/ }).click();
        await editor.getByLabel('选择原 PDF 查看页码').setInputFiles(pdf);
        await editor.getByRole('button', { name: '查看原 PDF 第 1 页', exact: true }).click();
        await editor.getByRole('img', { name: '原 PDF 第 1 页', exact: true }).waitFor();
        await page.keyboard.press('Escape');

        const imageReport = await upload(page, photo);
        assert.equal(imageReport.source_kind, 'photo');
        await patientSheet.getByText(basename(photo), { exact: true }).waitFor();
        await showReport.click();
        await hgb.getByRole('button', { name: '原报告第 1 页', exact: true }).click();
        await editor.getByRole('img', { name: '原照片 第 1 页', exact: true }).waitFor();
        await page.keyboard.press('Escape');
        await openSheet.click();
        const [patientRequest] = await Promise.all([
            page.waitForResponse(response => response.url().endsWith('/questions/stream')),
            patientSheet.getByRole('button', { name: '结合我的情况解读', exact: true }).click(),
        ]);
        assert.equal(patientRequest.status(), 200);
        assert.match(patientRequest.request().postDataJSON().text, /整份报告.*就医/);
        await thread.getByText('请用通俗的话解读整份报告，就医时该和医生讨论什么？', { exact: true }).waitFor();
        await thread.getByLabel('正在流式解读').waitFor({ state: 'hidden' });

        if (bloodCount) {
            const cbc = await upload(page, bloodCount);
            const composer = page.getByLabel('追问报告', { exact: true });
            await composer.fill('这些异常怎么一起理解？');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            await thread.getByText(/如果现在没有明显不适/).waitFor();
            assert.equal(await thread.getByText('下一步', { exact: true }).count(), 0);
            await thread.getByText('查看报告依据与计算', { exact: true }).click();
            const checks = thread.getByRole('list', { name: '指标间的数值核验' });
            await checks.getByText(/≈ 0\.0948%/).waitFor();
            await checks.getByText(/反推的白细胞总数/).waitFor();
            await thread.getByText('查看工作记录', { exact: true }).click();
            await thread.getByRole('list', { name: '关键证据查证记录' }).getByText(/BAS#/).first().waitFor();
            const basGapText = '已查找本报告的识别结果，未找到 BAS#。';
            const basGapNotice = thread.getByText(basGapText, { exact: true });
            await basGapNotice.waitFor();
            await checks.getByRole('button', { name: 'MPV · 第 1 页', exact: true }).click();
            await editor.getByRole('img', { name: '原 PDF 第 1 页', exact: true }).waitFor();
            await page.keyboard.press('Escape');
            const replyOptions = thread.getByRole('group', { name: '回复助手的补问' });
            await thread.getByText('抽血前后有没有感冒、发烧或其他感染？', { exact: true }).waitFor();
            await replyOptions.getByRole('button', { name: '有', exact: true }).waitFor();
            await openSheet.click();
            await patientSheet.getByText(/如果现在没有明显不适/).waitFor();
            await patientSheet.getByRole('button', { name: '继续回答助手', exact: true }).click();
            await replyOptions.getByRole('button', { name: '有', exact: true }).waitFor();
            assert.equal(await page.getByText('补充基本情况（选填）', { exact: true }).count(), 0);
            assert.equal(await thread.getByRole('button', { name: /有同次的白细胞总数/ }).count(), 0);
            await composer.fill('上周感冒了');
            const [reply] = await Promise.all([
                page.waitForResponse(response => response.url().endsWith('/questions/stream')),
                page.getByRole('button', { name: '发送', exact: true }).click(),
            ]);
            assert.equal(reply.request().postDataJSON().replyToContext, true);
            await thread.getByText(/你提到近期有过感染/).waitFor();
            await thread.getByText(/以前查血时，医生也提过淋巴细胞/).waitFor();
            const continued = await savedReport(page, cbc.id);
            assert.equal(continued.turns[1].answer.task.id, continued.turns[0].answer.task.id);
            assert.equal(continued.turns[1].answer.task.resumed, true);
            assert.equal(continued.turns[1].answer.task.status, 'awaiting_reply');
            assert.ok(continued.turns[1].answer.evidence_checks.some(check => check.indicator === 'BAS#' && check.status === 'missing'));
            assert.equal(await basGapNotice.count(), 1);
            const cbcTitle = basename(bloodCount);
            await page.reload();
            await openSavedReport(page, cbcTitle);
            await basGapNotice.waitFor();
            assert.equal(await basGapNotice.count(), 1);
            await replyOptions.getByRole('button', { name: '先给结论', exact: true }).click();
            await replyOptions.waitFor({ state: 'hidden' });
            const saved = await savedReport(page, cbc.id);
            assert.equal(saved.turns.at(-1).question, '先给结论');
            assert.equal(saved.turns.at(-1).context_field, 'previous_low_lymphocytes');
            assert.equal(saved.turns.at(-1).answer.task.resumed, true);
            assert.equal(saved.turns.at(-1).answer.task.stop_reason, 'user_finished');
            await thread.getByText(/缺少可对照的既往淋巴细胞结果/).waitFor();
            await openSheet.click();
            await patientSheet.getByRole('heading', { name: '这些结果怎么理解？' }).waitFor();
            await patientSheet.getByRole('button', { name: '回到对话', exact: true }).click();
            await composer.fill('我现在正在大量出血');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            await page.getByRole('alert').filter({ hasText: '请立即拨打当地急救电话或前往急诊' }).waitFor();
            await openSheet.click();
            await patientSheet.getByRole('alert').getByText(/请立即拨打当地急救电话或前往急诊/).waitFor();
            assert.equal(await patientSheet.getByRole('heading', { name: '就诊时可以怎么准备？' }).count(), 0);
            await patientSheet.getByRole('button', { name: '回到对话', exact: true }).click();

            await composer.fill('淋巴细胞计数偏低说明什么？');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            await thread.getByText('淋巴细胞计数偏低说明什么？', { exact: true }).waitFor();
            await thread.getByRole('listitem').filter({ hasText: '淋巴细胞计数偏低说明什么？' })
                .getByText(/你提到近期有过感染/).waitFor();
            await page.reload();
            await openSavedReport(page, cbcTitle);
            await composer.fill('那这个是不是一直这样？');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            const followUp = thread.getByRole('listitem').filter({ hasText: '那这个是不是一直这样？' });
            await followUp.getByText(/对照以前同项的结果/).waitFor();
            await followUp.getByText('查看报告依据与计算', { exact: true }).click();
            await followUp.getByRole('button', { name: 'LYM# · 原报告第 1 页', exact: true }).waitFor();

            if (process.env.AUDIT_EXPECT_COVERAGE_REPAIR === '1') {
                const causeQuestion = '这些异常是什么原因造成？';
                await page.getByLabel('追问报告', { exact: true }).fill(causeQuestion);
                await page.getByRole('button', { name: '发送', exact: true }).click();
                const repaired = thread.getByRole('listitem').filter({ hasText: causeQuestion });
                if (process.env.AUDIT_EXPECT_STREAM_PREVIEW === '1') {
                    await repaired.getByText(/嗜碱性粒细胞的占比高于报告参考范围/).waitFor();
                    assert.equal(await repaired.getByText('查看工作记录', { exact: true }).count(), 0);
                }
                await repaired.getByText('淋巴细胞计数偏低，需要结合以往结果判断是否持续。', { exact: true }).waitFor();
                assert.equal(await repaired.getByText('本次按报告依据和规则解读。', { exact: true }).count(), 0);
                await repaired.getByText('查看工作记录', { exact: true }).click();
                await repaired.getByText('未覆盖 · LYM#', { exact: true }).waitFor();
                await repaired.getByText(/按缺项补写 · 已返回/).waitFor();
            }

            const before = await savedReport(page, cbc.id);
            await showReport.click();
            const lymphocytes = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /^LYM#/ }) });
            await lymphocytes.getByRole('button', { name: '核对 LYM# 第 1 页', exact: true }).click();
            await editor.getByLabel('结果', { exact: true }).fill('1.03');
            const [rechecked] = await Promise.all([
                page.waitForResponse(response => response.url().endsWith('/questions/stream')),
                editor.getByRole('button', { name: '保存校正并比对', exact: true }).click(),
            ]);
            assert.equal(rechecked.status(), 200);
            await thread.getByLabel('正在流式解读').waitFor({ state: 'hidden' });
            const after = await savedReport(page, cbc.id);
            assert.equal(after.findings_revision, before.findings_revision + 1);
            const updatedAnswer = after.turns.at(-1).answer;
            assert.equal(updatedAnswer.task.findings_revision, after.findings_revision);
            assert.notEqual(updatedAnswer.task.id, before.turns.at(-1).answer.task.id);
            assert.ok(updatedAnswer.citations.some(row => row.name === 'LYM#' && row.value === '1.03'));

            await composer.fill('为什么要查找BAS#？');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            const rationale = thread.getByRole('listitem').filter({ hasText: '为什么要查找BAS#？' });
            await rationale.getByText(/比例偏高时，绝对计数仍可能在参考范围内/).waitFor();
            await rationale.getByRole('link', { name: 'MedlinePlus · 白细胞分类', exact: true }).waitFor();
            assert.equal(await rationale.getByText(basGapText, { exact: true }).count(), 0);
            await rationale.getByText('查看工作记录', { exact: true }).click();
            await rationale.getByText('未调用模型 · 按已审核资料解释查证目的', { exact: true }).waitFor();
            await rationale.getByRole('list', { name: '关键证据查证记录' }).getByText(/未找到 BAS#/).waitFor();
        }

        if (sideBySide) {
            const table = await upload(page, sideBySide);
            assert.ok(table.findings.length > 0);
            assert.ok(table.findings.some(row => row.unit && row.region));
            await showReport.click();
            await page.getByRole('button', { name: new RegExp(`全部项目 ${table.findings.length}`) }).waitFor();
            await page.getByRole('button', { name: '返回对话', exact: true }).click();
            assert.equal(await page.getByText('补充基本情况（选填）', { exact: true }).count(), 0);
            const sendButton = page.getByRole('button', { name: '发送', exact: true });
            const composer = page.getByLabel('追问报告', { exact: true });
            const capture = click => Promise.all([
                page.waitForResponse(response => response.url().endsWith('/questions/stream') && response.request().method() === 'POST'),
                click(),
            ]);
            const savedTurn = async (response, turnCount) => {
                assert.equal(response.status(), 200);
                assert.match(response.headers()['content-type'], /^text\/event-stream/);
                await thread.getByLabel('正在流式解读').waitFor({ state: 'hidden' });
                const report = await savedReport(page, table.id);
                assert.equal(report.turns.length, turnCount);
                return report.turns.at(-1);
            };
            await composer.fill('解读整份报告');
            const [initialResponse] = await capture(() => sendButton.click());
            assert.equal((await savedTurn(initialResponse, 1)).answer.context_follow_up.field, 'liver_medicine');
            await thread.getByText(/抽血前有没有新用或调整过药物/).waitFor();
            assert.equal(await thread.getByText(/抽血前后有没有感冒/).count(), 0);
            const [affirmative] = await capture(() => thread.getByRole('button', { name: '有', exact: true }).click());
            assert.equal((await savedTurn(affirmative, 2)).answer.context_follow_up.field, 'liver_medicine_name');
            await thread.getByText(/叫什么名字/).waitFor();
            await page.getByRole('button', { name: '另问报告' }).click();
            await page.getByRole('button', { name: '回答上一个问题' }).click();
            await composer.fill('合成保健品甲');
            const [missingResponse] = await capture(() => sendButton.click());
            assert.equal(missingResponse.request().postDataJSON().replyToContext, true);
            const missingTurn = await savedTurn(missingResponse, 3);
            assert.equal(missingTurn.context_field, 'liver_medicine_name');
            const answer = missingTurn.answer;
            assert.equal(answer.context_follow_up.field, 'liver_medicine_since');
            const modelTurn = thread.getByRole('listitem').filter({ has: page.getByText('合成保健品甲', { exact: true }) });
            if (process.env.AUDIT_EXPECT_MEDICATION_MODEL === '1') assert.equal(answer.model_is_primary, true);
            if (process.env.AUDIT_EXPECT_TIMEOUT_RETRY === '1') {
                assert.equal(answer.mode, 'model');
                assert.equal(answer.work_record.fallback_reason, null);
                assert.deepEqual(answer.work_record.model_calls.map(call => [call.phase, call.status]),
                    [['initial', 'timeout'], ['timeout_retry', 'returned']]);
                await modelTurn.getByText('查看工作记录', { exact: true }).click();
                await modelTurn.getByText(/超时后用现有证据重试 · 已返回/).waitFor();
            }
            if (process.env.AUDIT_EXPECT_TOOL_RECORD === '1') {
                assert.equal(answer.work_record.model_call_count, 3);
                assert.deepEqual(answer.work_record.tool_rounds.map(round => round.map(tool => tool.name)),
                    [['lookup_report'], ['search_lab_knowledge']]);
                await modelTurn.getByText('查看工作记录', { exact: true }).click();
                await modelTurn.getByText('模型调用 3 次 · 工具执行 2 轮', { exact: true }).waitFor();
                const tools = modelTurn.getByRole('list', { name: '工具执行记录', exact: true });
                await tools.getByText('lookup_report', { exact: true }).waitFor();
                await tools.getByText('search_lab_knowledge', { exact: true }).waitFor();
                await tools.getByText(/找到 1 条报告记录（AST）/).waitFor();
            }
            if (answer.model_is_primary) {
                await modelTurn.getByText(answer.observations[0].text, { exact: true }).waitFor();
                assert.equal(await modelTurn.getByRole('list', { name: '可能相关的情况' }).count(), 0);
            }
            await modelTurn.getByText(/之前还是之后/).waitFor();
            await composer.fill('抽血前一周');
            const [completeResponse] = await capture(() => sendButton.click());
            const complete = await savedTurn(completeResponse, 4);
            assert.equal(complete.context_field, 'liver_medicine_since');
            assert.equal(complete.question, '抽血前一周');
            assert.equal(complete.answer.context_question, '');
            assert.equal(complete.answer.task.answered, 3);
            assert.equal(complete.answer.task.resumed, true);
        }
        await page.evaluate(() => sessionStorage.setItem('health-assistant.conversation.v1.workspace', 'dashboard'));
        await page.goto(base);
        await modes.getByRole('button', { name: '健康对话', exact: true, pressed: true }).waitFor();
        await page.getByRole('textbox', { name: '咨询消息', exact: true }).waitFor();

        const otherPage = await browser.newPage();
        await register(otherPage, 'other');
        const unauthorized = await otherPage.request.get(base + api + '/' + report.id);
        assert.equal(unauthorized.status(), 404);
        assert.deepEqual(errors, []);
        console.log('Account report reading passed: resume after reload, revised evidence, final answer, tool trace and account isolation.');
    } finally {
        await browser.close();
    }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
