const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const asyncAudit = process.env.AUDIT_TEST_ASYNC !== '0';
const managedHealthChat = process.env.AUDIT_TEST_MANAGED !== '0';
const accountReports = asyncAudit || managedHealthChat;
const photoApi = accountReports ? '/api/health/report-photo' : '/api';
const photoSessionApi = accountReports ? '/api/health/report-photo/session' : '/api/report-photo/session';

async function registerReportAccount(page, suffix) {
    if (!accountReports) return;
    await page.getByRole('heading', { name: '登录医疗健康 Agent 工作台' }).waitFor();
    await page.getByRole('button', { name: '创建体验账号', exact: true }).click();
    await page.getByLabel('用户名').fill('audit_ui_' + suffix + '_' + Date.now().toString(36));
    await page.getByLabel('密码').fill('synthetic-audit-ui-password');
    await page.getByRole('button', { name: '注册并登录', exact: true }).click();
}

async function run() {
    const browser = await chromium.launch({
        headless: true,
        executablePath: process.env.AUDIT_CHROME_PATH || undefined,
    });

    try {
        const page = await browser.newPage({
            viewport: { width: 390, height: 844 },
            deviceScaleFactor: 1,
            isMobile: true,
            hasTouch: true,
        });
        const pageErrors = [];
        page.on('pageerror', error => pageErrors.push(error.message));
        page.setDefaultTimeout(10_000);
        await page.goto(process.env.AUDIT_WEB_URL || 'http://127.0.0.1:5173');
        if (accountReports) await page.evaluate(demo => { location.hash = demo ? '/report-audit-demo' : '/report-audit'; }, asyncAudit);
        else await page.getByRole('link', { name: '报告内容解析', exact: true }).click();
        await registerReportAccount(page, 'mobile');
        await page.getByRole('heading', { name: '检验报告核对' }).waitFor();
        await page.getByRole('button', { name: '填入示例报告' }).click();
        await page.getByRole('button', { name: '开始核对' }).click();
        if (asyncAudit) {
            await page.getByRole('button', { name: '查看结果' }).first().click();
        }
        await page.getByText('数值不一致', { exact: true }).waitFor();
        const stats = page.getByLabel('核对统计');
        assert.match(await stats.innerText(), /1\s*待核对\s*3\s*本次复核\s*0\s*沿用结果/);

        await page.getByRole('button', { name: '血红蛋白的原始记录是什么？' }).click();
        await page.getByRole('article').getByText('OBS-101 · v2', { exact: true }).waitFor();
        await page.getByRole('button', { name: '上次的记录呢？' }).click();
        await page.getByRole('article').getByText('OBS-081 · v1', { exact: true }).waitFor();
        await page.getByRole('button', { name: '血红蛋白是什么？', exact: true }).click();
        await page.getByRole('article').getByText(/^(模拟检验项目说明 HGB|lab_reference 血常规参考范围)$/).waitFor({ timeout: 24_000 });
        if (asyncAudit) {
            await page.getByLabel('问题', { exact: true }).fill('白细胞的原始记录是什么？');
            await page.getByRole('button', { name: '发送', exact: true }).click();
            await page.getByRole('article').getByText('白细胞的原始记录是什么？', { exact: true }).waitFor();
        }
        if (process.env.AUDIT_SMOKE_SCREENSHOT) {
            await page.screenshot({ path: process.env.AUDIT_SMOKE_SCREENSHOT, fullPage: true });
        }

        await page.getByRole('button', { name: '更正报告', exact: true }).click();
        await page.getByRole('button', { name: '采用原始记录' }).click();
        await page.getByRole('button', { name: '提交更正并复核' }).click();
        if (asyncAudit) {
            await page.getByRole('listitem').filter({ hasText: 'v2 · 已完成' })
                .getByRole('button', { name: '查看结果' }).click();
        }
        await page.getByText('v2（最新）', { exact: true }).waitFor({ state: 'attached' });
        assert.match(await stats.innerText(), /0\s*待核对\s*1\s*本次复核\s*2\s*沿用结果/);
        if (asyncAudit) {
            const details = page.getByRole('region', { name: '报告问答与复核' });
            const review = details.getByRole('article');
            await review.getByText('待复核', { exact: true }).waitFor();
            assert.equal(await review.count(), 1);
            const reviewText = await review.innerText();
            assert.match(reviewText, /更正前\s+123/);
            assert.match(reviewText, /更正后\s+132/);
            assert.match(reviewText, /血红蛋白的原始记录是什么/);
            if (process.env.AUDIT_IMPACT_SCREENSHOT) {
                await review.scrollIntoViewIfNeeded();
                await page.screenshot({ path: process.env.AUDIT_IMPACT_SCREENSHOT, fullPage: true });
            }
            await review.getByRole('button', { name: '按更正版本重新提问' }).click();
            await details.getByText(/报告值为 132 g\/L/).waitFor();
            const unaffected = details.getByRole('article').filter({ hasText: '白细胞的原始记录是什么？' });
            await unaffected.getByText('v1 · 未受影响', { exact: true }).waitFor();
            await details.getByRole('button', { name: /更正影响/ }).click();
            await details.getByRole('button', { name: '标记已复核' }).click();
            await review.getByText('已复核', { exact: true }).waitFor();
        } else {
            assert.equal(await page.getByRole('article').count(), 0);
        }
        await page.getByLabel('报告版本').selectOption('1');
        await page.getByText('数值不一致', { exact: true }).waitFor();
        await page.reload();
        await page.getByText('v2（最新）', { exact: true }).waitFor({ state: 'attached' });
        assert.equal(await page.getByLabel('报告版本').inputValue(), '2');
        if (asyncAudit) {
            await page.getByRole('button', { name: /更正影响/ }).click();
            await page.getByRole('article').getByText('已复核', { exact: true }).waitFor();
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
        if (asyncAudit) {
            await page.getByRole('button', { name: '返回导入' }).click();
            await page.getByRole('button', { name: '填入示例报告' }).click();
            await page.waitForFunction(() => document.getElementById('audit-text')?.value.includes('PATIENT'));
            const report = await page.getByLabel('报告文本').inputValue();
            const files = ['report-a.txt', 'report-b.txt'].map(name => ({
                name, mimeType: 'text/plain', buffer: Buffer.from(report, 'utf8'),
            }));
            const jobs = page.getByRole('region', { name: '核对历史' });
            const before = await jobs.getByRole('listitem').count();
            const accepted = page.waitForResponse(response => response.url().endsWith('/batches') && response.status() === 202);
            await page.getByLabel('上传报告', { exact: true }).setInputFiles(files);
            const tickets = await (await accepted).json();
            assert.equal(tickets.length, 2);
            await jobs.getByRole('listitem').nth(before + 1).waitFor();
            await jobs.getByRole('button', { name: '查看结果' }).nth(before + 1).waitFor();
        }
        await page.setViewportSize({ width: 1280, height: 800 });
        if (asyncAudit) {
            await page.getByRole('button', { name: '查看结果' }).first().click();
        }
        const resultPanel = await page.getByRole('region', { name: '核对结果' }).boundingBox();
        const evidencePanel = await page.getByRole('region', { name: '报告问答与复核' }).boundingBox();
        assert.ok(resultPanel.x + resultPanel.width < evidencePanel.x);
        assert.equal(resultPanel.y, evidencePanel.y);
        assert.deepEqual(pageErrors, []);
        const desktop = await browser.newPage({ viewport: { width: 1280, height: 800 } });
        await desktop.goto(process.env.AUDIT_WEB_URL || 'http://127.0.0.1:5173');
        const homeFrame = await desktop.getByRole('main').boundingBox();
        if (accountReports) await desktop.evaluate(demo => { location.hash = demo ? '/report-audit-demo' : '/report-audit'; }, asyncAudit);
        else await desktop.getByRole('link', { name: '报告内容解析', exact: true }).click();
        await registerReportAccount(desktop, 'desktop');
        await desktop.getByRole('heading', { name: '检验报告核对' }).waitFor();
        const auditFrame = await desktop.getByRole('main').boundingBox();
        assert.deepEqual(auditFrame, homeFrame);
        assert.equal(await desktop.getByRole('navigation', { name: '主导航' })
            .getByRole('link').count(), 1);
        assert.equal(await desktop.getByRole('navigation', { name: '咨询模式' })
            .getByRole('link', { name: '报告内容解析', exact: true }).getAttribute('aria-current'), 'page');
        assert.equal(await desktop.getByRole('link', { name: '检验核对', exact: true }).getAttribute('aria-current'), 'page');
        await desktop.getByRole('button', { name: '填入示例报告' }).click();
        await desktop.waitForFunction(() => document.getElementById('audit-text')?.value.includes('PATIENT'));
        const draft = await desktop.getByLabel('报告文本').inputValue();

        const cors = { 'access-control-allow-origin': '*' };
        function events(...messages) {
            return messages.map(message => 'data: ' + JSON.stringify(message) + '\n\n').join('');
        }
        const photoThread = '1'.repeat(32);
        await desktop.route('**' + photoApi + '/vision-chat', route => {
            const request = route.request();
            assert.equal(request.method(), 'POST');
            assert.equal(new URL(request.url()).origin, new URL(desktop.url()).origin);
            assert.match(request.postData(), /name="scan_type"\r\n\r\nreport/);
            return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                { type: 'session', thread_id: photoThread },
                { type: 'node_start', node: 'report_node' },
                { type: 'card', payload: { type: 'knowledge_retrieval', data: {
                    queries: [],
                    status: 'used',
                    hits: [{ source: 'lab_reference', section: '血常规参考范围', excerpt: '合成检验资料片段。' }],
                } } },
                { type: 'card', payload: { type: 'report_ocr_review', data: {
                    items: [{ name: '例一', value: 2, unit: 'U/L', reference_range: '1–3' }],
                } } },
                { type: 'text', content: '示例报告照片解读。' },
                { type: 'finish' },
            ) });
        });
        await desktop.route('**' + photoApi + '/chat', route => {
            if (route.request().method() === 'OPTIONS') {
                return route.fulfill({ status: 204, headers: { ...cors, 'access-control-allow-headers': 'content-type' } });
            }
            const payload = route.request().postDataJSON();
            assert.equal(new URL(route.request().url()).origin, new URL(desktop.url()).origin);
            assert.equal(payload.chat_mode, 'report');
            assert.equal(payload.thread_id, photoThread);
            if (payload.report_items) {
                assert.equal(payload.messages.at(-1).content, '按已核对的项目比对。');
                assert.deepEqual(payload.report_items, [{ name: '例一', value: 2, unit: 'U/L', reference_range: null }]);
                return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                    { type: 'card', payload: { type: 'report_analysis', data: {
                        status: '结果正常',
                        summary: '所有解读项目均在参考范围内',
                        findings: [{ name: '例一', value: 2, status: 'UNKNOWN', reference: '暂无参考范围' }],
                    } } },
                    { type: 'text', content: '1 项待核对。' },
                    { type: 'finish' },
                ) });
            }
            assert.equal(payload.messages.length, 1);
            return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                { type: 'text', content: '示例报告追问答复。' },
                { type: 'finish' },
            ) });
        });
        await desktop.getByRole('link', { name: '拍照解读', exact: true }).click();
        await desktop.getByRole('heading', { name: '拍照解读', exact: true }).waitFor();
        await desktop.getByLabel('上传报告照片').setInputFiles({
            name: 'report.png', mimeType: 'image/png',
            buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/0ioAAAAASUVORK5CYII=', 'base64'),
        });
        await desktop.getByText('示例报告照片解读。', { exact: true }).waitFor();
        const retrieval = desktop.locator('details').filter({ has: desktop.locator('summary').getByText('知识库检索', { exact: true }) });
        await retrieval.locator('summary').click();
        await retrieval.getByText('search_health_knowledge', { exact: true }).waitFor();
        await retrieval.getByText('报告查询内容已隐藏。', { exact: true }).waitFor();
        await retrieval.getByText('合成检验资料片段。', { exact: true }).waitFor();
        const analysis = desktop.locator('[data-card="report-analysis"]');
        assert.equal(await analysis.count(), 0);
        const review = desktop.getByRole('form', { name: '核对提取结果' });
        await review.getByLabel('参考范围').fill('');
        await review.getByRole('button', { name: '已核对，开始比对' }).click();
        await analysis.getByText('缺少可比对的参考范围', { exact: true }).waitFor();
        assert.equal(await analysis.getByText('结果正常', { exact: true }).count(), 0);
        assert.equal(await analysis.getByText('所有解读项目均在参考范围内', { exact: true }).count(), 0);
        await desktop.getByLabel('解读问题').fill('可以解释第一项吗？');
        await desktop.getByRole('button', { name: '发送', exact: true }).click();
        await desktop.getByText('示例报告追问答复。', { exact: true }).waitFor();
        await desktop.getByRole('link', { name: '检验核对', exact: true }).click();
        assert.equal(await desktop.getByLabel('报告文本').inputValue(), draft);
        await desktop.goBack();
        await desktop.getByText('示例报告追问答复。', { exact: true }).waitFor();
        await desktop.route('**' + photoSessionApi, route => {
            assert.equal(route.request().headers()['x-report-thread'], photoThread);
            return route.fulfill({ json: { messages: [
                { id: 'restored-photo-answer', role: 'assistant', text: '示例报告追问答复。', timestamp: 0 },
            ] } });
        });
        await desktop.reload();
        await desktop.getByText('示例报告追问答复。', { exact: true }).waitFor();
        await desktop.getByRole('link', { name: '返回健康助手' }).click();
        if (managedHealthChat) {
            const modes = desktop.getByRole('navigation', { name: '咨询模式' });
            await modes.waitFor();
            assert.equal(await modes.getByRole('button').count(), 4);
            await modes.getByRole('button', { name: '药管家', exact: true }).click();
            const pharmacy = desktop.getByRole('region', { name: '药管家' });
            await pharmacy.getByRole('button', { name: '怎么看药品说明书里的注意事项？', exact: true }).waitFor();
            const navBounds = await modes.boundingBox();
            const pharmacyBounds = await pharmacy.boundingBox();
            assert.ok(navBounds.y + navBounds.height <= pharmacyBounds.y + 1);
            assert.equal(await modes.getByRole('button', { name: '药管家', exact: true }).getAttribute('aria-pressed'), 'true');
            if (process.env.AUDIT_HOME_SCREENSHOT) {
                await desktop.screenshot({ path: process.env.AUDIT_HOME_SCREENSHOT, fullPage: true, animations: 'disabled' });
            }
            await pharmacy.getByText('对话记录', { exact: true }).click();
            await pharmacy.getByRole('button', { name: '刷新记录' }).waitFor();
            await desktop.route('**/api/health/chat/pharmacy/image?*', route => {
                const request = route.request();
                assert.match(request.headers()['content-type'], /^multipart\/form-data; boundary=/);
                assert.match(request.postData(), /name="scanType"\r\n\r\ntrace_code/);
                return route.fulfill({
                    contentType: 'text/event-stream',
                    body: events({ type: 'text', content: '合成药品图片识别结果。' }, { type: 'finish' }),
                });
            });
            await desktop.getByRole('button', { name: '打开图片上传菜单' }).click();
            await desktop.getByRole('link', { name: '报告照片解读', exact: true }).waitFor();
            const [picker] = await Promise.all([
                desktop.waitForEvent('filechooser'), desktop.getByRole('button', { name: '扫追溯码' }).click(),
            ]);
            await picker.setFiles({ name: 'synthetic-code.png', mimeType: 'image/png', buffer: Buffer.from('synthetic-picture') });
            await pharmacy.getByText('合成药品图片识别结果。', { exact: true }).waitFor();
            await desktop.getByRole('button', { name: '打开图片上传菜单' }).click();
            await desktop.getByRole('link', { name: '报告照片解读', exact: true }).click();
            await desktop.getByRole('heading', { name: asyncAudit ? '报告内容解析' : '拍照解读', exact: true }).waitFor();
            await desktop.getByRole('link', { name: '返回健康助手' }).click();
            await modes.getByRole('button', { name: '健康对话', exact: true }).click();
            const sidebar = desktop.getByRole('complementary', { name: '历史对话栏' });
            const composer = desktop.getByRole('textbox', { name: '咨询消息' });
            await composer.fill('合成界面测试 A');
            await composer.press('Shift+Enter');
            assert.match(await composer.inputValue(), /\n$/);
            await composer.fill('合成界面测试 A\n请查询我个人账户的医保余额。');
            await desktop.getByRole('button', { name: '发送', exact: true }).click();
            const firstChat = sidebar.getByRole('button', { name: /合成界面测试 A/ });
            await firstChat.waitFor({ timeout: 45_000 });
            await sidebar.getByRole('button', { name: '新对话', exact: true }).click();
            await desktop.getByRole('heading', { name: '你好，今天想聊些什么？' }).waitFor();
            await composer.fill('合成界面测试 B，请查询我个人账户的医保余额。');
            await desktop.getByRole('button', { name: '发送', exact: true }).click();
            const secondChat = sidebar.getByRole('button', { name: /合成界面测试 B/ });
            await secondChat.waitFor({ timeout: 45_000 });
            assert.equal(await secondChat.getAttribute('aria-current'), 'true');
            await firstChat.click();
            const transcript = desktop.getByRole('region', { name: '健康对话' });
            await transcript.getByRole('paragraph').filter({ hasText: /合成界面测试 A/ }).waitFor();
            assert.equal(await transcript.getByRole('paragraph').filter({ hasText: /合成界面测试 B/ }).count(), 0);
            await desktop.reload();
            await transcript.getByRole('paragraph').filter({ hasText: /合成界面测试 A/ }).waitFor();
            await sidebar.getByRole('button', { name: '收起侧栏' }).click();
            await desktop.getByRole('button', { name: '展开侧栏' }).click();
            await firstChat.waitFor();
            await desktop.setViewportSize({ width: 390, height: 844 });
            await desktop.getByRole('button', { name: '展开历史对话' }).click();
            await secondChat.click();
            await transcript.getByRole('paragraph').filter({ hasText: /合成界面测试 B/ }).waitFor();
            assert.equal(await desktop.getByRole('button', { name: '展开历史对话' }).getAttribute('aria-expanded'), 'false');
            assert.equal(await desktop.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
            assert.equal(await modes.getByRole('button', { name: '健康小目标', exact: true }).count(), 0);
            console.log('Passed report review, multiple sessions, history restore, multiline input and responsive sidebar.');
            return;
        }
        await desktop.getByRole('heading', { name: '健康对话' }).waitFor();
        const shortcuts = await desktop.getByRole('navigation', { name: '咨询模式' }).boundingBox();
        assert.equal(await desktop.getByRole('button', { name: '拍报告', exact: true }).count(), 0);
        await desktop.getByRole('button', { name: '打开图片上传菜单' }).click();
        await desktop.getByRole('button', { name: /拍药盒/ }).waitFor();
        assert.equal(await desktop.getByRole('button', { name: /拍报告/ }).count(), 0);
        await desktop.getByRole('button', { name: '关闭附件菜单' }).click();
        const conversation = await desktop.getByRole('region', { name: '健康对话' }).boundingBox();
        assert.ok(homeFrame.width >= 1000);
        assert.ok(shortcuts.y + shortcuts.height <= conversation.y + 1);
        assert.equal(await desktop.getByRole('button', { name: '健康小目标', exact: true }).count(), 0);
        await desktop.getByRole('button', { name: 'AI诊室', exact: true }).click();
        await desktop.getByRole('button', { name: 'AI诊室', exact: true, pressed: true }).waitFor();
        const chatInput = desktop.getByRole('textbox');
        const send = desktop.getByRole('button', { name: '发送', exact: true });
        await desktop.unroute('**/api/chat');
        await desktop.route('**/api/chat', route => route.abort('connectionrefused'));
        await chatInput.fill('合成测试：连接失败');
        await send.click();
        await desktop.getByText(/无法连接健康助手服务/).waitFor();

        await desktop.unroute('**/api/chat');
        await desktop.route('**/api/chat', route => {
            if (route.request().method() === 'OPTIONS') {
                return route.fulfill({ status: 204, headers: { ...cors, 'access-control-allow-headers': 'content-type' } });
            }
            if (route.request().postDataJSON().messages.at(-1).content.includes('配置缺失')) {
                return route.fulfill({ status: 500, headers: cors, json: { detail: '大模型配置错误：Missing model API key' } });
            }
            return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                { type: 'error', code: 'MODEL_UNAVAILABLE', content: '模型服务暂不可用' },
            ) });
        });
        await chatInput.fill('合成测试：配置缺失');
        await send.click();
        await desktop.getByText(/健康助手服务已连接，但模型未配置/).waitFor();

        await chatInput.fill('合成测试：模型离线');
        await send.click();
        await desktop.getByText(/模型服务暂不可用。请检查模型连接/).waitFor();
        await desktop.unroute('**/api/chat');
        await desktop.route('**/api/chat', route => {
            if (route.request().method() === 'OPTIONS') {
                return route.fulfill({ status: 204, headers: { ...cors, 'access-control-allow-headers': 'content-type' } });
            }
            return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                { type: 'node_start', node: 'clinic_node', content: '进入预问诊模块' },
                { type: 'text', content: '这种不适持续多久了？' },
                { type: 'interrupt', content: '这种不适持续多久了？' },
                { type: 'finish' },
            ) });
        });
        await chatInput.fill('合成测试：预问诊追问');
        await send.click();
        const paused = desktop.getByText('等待补充信息', { exact: true }).last();
        await paused.waitFor();
        assert.equal(await paused.locator('..').locator('.animate-spin').count(), 0);
        assert.equal(await desktop.getByRole('button', { name: '查医保', exact: true }).count(), 0);

        await desktop.unroute('**/api/chat');
        await desktop.route('**/api/chat', route => {
            if (route.request().method() === 'OPTIONS') {
                return route.fulfill({ status: 204, headers: { ...cors, 'access-control-allow-headers': 'content-type' } });
            }
            const payload = route.request().postDataJSON();
            const text = payload.messages.at(-1).content;
            if (text === '合成诊室记录') assert.equal(payload.chat_mode, 'clinic');
            if (text === '合成用药记录') {
                assert.equal(payload.chat_mode, 'pharmacy');
                assert.equal(payload.thread_id, null);
                assert.ok(!payload.messages.some(message => message.content === '合成诊室记录'));
            }
            const id = payload.chat_mode === 'clinic' ? 'clinic-history-thread' : 'pharmacy-history-thread';
            if (text === '合成诊室续接') {
                assert.equal(payload.thread_id, 'clinic-history-thread');
                assert.ok(payload.messages.some(message => message.content === '合成诊室记录'));
            }
            return route.fulfill({ contentType: 'text/event-stream', headers: cors, body: events(
                { type: 'session', thread_id: id },
                { type: 'text', content: '收到：' + text },
                { type: 'finish' },
            ) });
        });
        await chatInput.fill('合成诊室记录');
        await send.click();
        await desktop.getByText('收到：合成诊室记录', { exact: true }).waitFor();
        await desktop.getByRole('button', { name: '药管家', exact: true }).click();
        await desktop.getByRole('heading', { name: '药管家' }).waitFor();
        assert.equal(await desktop.getByText('收到：合成诊室记录', { exact: true }).count(), 0);
        await chatInput.fill('合成用药记录');
        await send.click();
        await desktop.getByText('收到：合成用药记录', { exact: true }).waitFor();
        await desktop.reload();
        await desktop.getByText('收到：合成用药记录', { exact: true }).waitFor();
        await desktop.getByRole('button', { name: 'AI诊室', exact: true }).click();
        await desktop.getByText('收到：合成诊室记录', { exact: true }).waitFor();
        assert.equal(await desktop.getByText('收到：合成用药记录', { exact: true }).count(), 0);
        await chatInput.fill('合成诊室续接');
        await send.click();
        await desktop.getByText('收到：合成诊室续接', { exact: true }).waitFor();
        await desktop.getByText('对话记录', { exact: true }).click();
        desktop.once('dialog', dialog => dialog.accept());
        await desktop.getByRole('button', { name: '清除本机记录' }).click();
        await desktop.reload();
        assert.equal(await desktop.getByText('收到：合成诊室记录', { exact: true }).count(), 0);
        await desktop.getByRole('button', { name: '药管家', exact: true }).click();
        await desktop.getByText('收到：合成用药记录', { exact: true }).waitFor();
        console.log('Passed report center, paused clinic status, and independent browser histories.');
    } finally {
        await browser.close();
    }
}

run().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
