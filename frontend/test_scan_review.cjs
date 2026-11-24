const assert = require('node:assert/strict');
const { chromium } = require('playwright');

async function run() {
    const file = process.env.AUDIT_SCAN_TEST_PDF;
    assert.ok(file, 'Set AUDIT_SCAN_TEST_PDF to the exported misplaced-unit-and-reference.pdf');
    const browser = await chromium.launch({ headless: true, executablePath: process.env.AUDIT_CHROME_PATH });
    try {
        const page = await browser.newPage({
            viewport: { width: 390, height: 844 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true,
        });
        page.setDefaultTimeout(12_000);
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        const url = new URL(process.env.AUDIT_WEB_URL || 'http://127.0.0.1:5173');
        const accountMode = process.env.AUDIT_SCAN_TEST_ACCOUNT === '1';
        url.hash = accountMode ? '/report-audit-demo' : '/report-audit';
        await page.goto(url.href);
        if (accountMode) {
            await page.getByRole('button', { name: '创建体验账号', exact: true }).click();
            await page.getByLabel('用户名').fill('audit_scan_' + Date.now().toString(36));
            await page.getByLabel('密码').fill('synthetic-scan-test-password');
            await page.getByRole('button', { name: '注册并登录', exact: true }).click();
        }
        const [scanResponse] = await Promise.all([
            page.waitForResponse(response => response.url().endsWith('/report-audit/scan') && response.request().method() === 'POST'),
            page.getByLabel('扫描识别 PDF').setInputFiles(file),
        ]);
        assert.ok(scanResponse.ok(), `Expected the synthetic PDF to scan, got HTTP ${scanResponse.status()}`);

        const row = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: /HGB/ }) });
        await row.getByText('待确认', { exact: true }).waitFor();
        // Simulate a long table while keeping the upload and comparison on the real API.
        await row.evaluate(element => {
            for (let index = 0; index < 18; index++) {
                const extra = document.createElement('tr');
                const cell = extra.insertCell();
                cell.colSpan = 4;
                cell.style.height = '84px';
                cell.textContent = `合成检验项 ${index + 1}`;
                element.before(extra);
            }
        });
        const reviewButton = row.getByRole('button', { name: '核对 HGB 第 1 页' });
        await reviewButton.scrollIntoViewIfNeeded();
        await reviewButton.click();
        const form = page.getByRole('group', { name: '修正扫描字段' });
        await form.waitFor();
        const initialBox = await form.boundingBox();
        assert.ok(initialBox.y >= 0 && initialBox.y + initialBox.height <= 844, 'Review opens inside the viewport from a lower row');
        const dialog = page.getByRole('dialog', { name: '核对字段', exact: true });
        assert.ok(await dialog.evaluate(element => element.contains(document.activeElement)), 'Focus enters the dialog');
        await form.getByRole('button', { name: '查看原 PDF 第 1 页' }).click();
        const image = form.getByRole('img', { name: '原 PDF 第 1 页' });
        await image.waitFor();
        assert.ok(await image.evaluate(async img => {
            await img.decode();
            return img.naturalWidth > 0;
        }));
        await form.getByText('原 PDF · 第 1 页 · 高亮为识别到的原行', { exact: true }).waitFor();
        const formBox = await form.boundingBox();
        assert.ok(formBox.x >= 0 && formBox.x + formBox.width <= 391, 'Review form fits the mobile viewport');
        if (process.env.AUDIT_SCAN_TEST_SCREENSHOT) {
            await form.screenshot({ path: process.env.AUDIT_SCAN_TEST_SCREENSHOT });
        }

        await form.getByLabel('单位', { exact: true }).fill('g/L');
        await form.getByLabel('报告参考值', { exact: true }).fill('110-160');
        const comparison = page.waitForResponse(response => response.url().endsWith('/scan/compare'));
        await form.getByRole('button', { name: '重新比对' }).click();
        assert.deepEqual(await (await comparison).json(), { status: 'HIGH' });
        await row.getByText('高于参考范围', { exact: true }).waitFor();
        await row.getByText('已手动校正', { exact: true }).waitFor();
        await reviewButton.click();
        await form.getByText('OCR 原文 · 第 1 页 · HGB · 170 · 110-160 · g/L', { exact: true }).waitFor();
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        assert.ok(await reviewButton.evaluate(element => element === document.activeElement), 'Escape returns focus to the original row');
        if (accountMode) {
            await page.reload();
            await reviewButton.click();
            await dialog.getByLabel('选择原 PDF 查看页码').setInputFiles(file);
            await dialog.getByRole('button', { name: '查看原 PDF 第 1 页' }).click();
            await dialog.getByRole('img', { name: '原 PDF 第 1 页' }).waitFor();
        }
        assert.deepEqual(errors, []);
        console.log('Mobile scan review passed: visible dialog from a lower row, original page, correction, and focus restoration.');
    } finally {
        await browser.close();
    }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
