import { readFile, writeFile } from "node:fs/promises";

const target =
  process.argv[2] ||
  "/app/node_modules/puppeteer-stream/dist/PuppeteerStream.js";

function replaceOnce(source, before, after, label) {
  if (source.includes(after)) return source;
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
    throw new Error(`Cannot apply puppeteer-stream patch: ${label}`);
  }
  return source.slice(0, first) + after + source.slice(first + before.length);
}

let source = await readFile(target, "utf8");

source = replaceOnce(
  source,
  '    addToArgs("--auto-accept-this-tab-capture");\n',
  '    addToArgs("--auto-accept-this-tab-capture");\n' +
    '    addToArgs(`--allowlisted-extension-id=${extensionId}`);\n',
  "extension allowlist",
);

source = replaceOnce(
  source,
  `    const old_browser_close = browser.close;
    browser.close = async () => {
        for (const page of await browser.pages()) {
            if (!page.url().startsWith(\`chrome-extension://\${extensionId}/options.html\`)) {
                await page.close();
            }
        }
        const extension = await getExtensionPage(browser);
        await extension.evaluate(async () => {
            return chrome.tabs.query({});
        });
        if (opts.closeDelay) {
            await new Promise((r) => setTimeout(r, opts.closeDelay));
        }
        await old_browser_close.call(browser);
    };
`,
  `    const old_browser_close = browser.close;
    let browserClosePromise;
    browser.close = async () => {
        if (browserClosePromise)
            return browserClosePromise;
        browserClosePromise = (async () => {
            for (const page of await browser.pages().catch(() => [])) {
                if (!page.url().startsWith(\`chrome-extension://\${extensionId}/options.html\`)) {
                    await page.close().catch(() => {});
                }
            }
            const extension = await getExtensionPage(browser).catch(() => null);
            if (extension && !extension.isClosed()) {
                await extension.evaluate(async () => {
                    return chrome.tabs.query({});
                }).catch(() => {});
            }
            if (opts.closeDelay) {
                await new Promise((r) => setTimeout(r, opts.closeDelay));
            }
            if (browser.isConnected())
                await old_browser_close.call(browser);
        })();
        return browserClosePromise;
    };
`,
  "idempotent browser close",
);

source = replaceOnce(
  source,
  `    await lock();
    await page.bringToFront();
    const [tab] = await extension.evaluate(async (x) => {
        // @ts-ignore
        return chrome.tabs.query(x);
    }, opts.tabQuery || {
        active: true,
    });
    unlock();
`,
  `    let tab;
    await lock();
    try {
        await page.bringToFront();
        [tab] = await extension.evaluate(async (x) => {
            // @ts-ignore
            return chrome.tabs.query(x);
        }, opts.tabQuery || {
            active: true,
        });
    }
    finally {
        unlock();
    }
`,
  "tab query mutex",
);

source = replaceOnce(
  source,
  `    const stream = new stream_1.Transform({
        highWaterMark: 1024 * 1024 * highWaterMarkMB,
        transform(chunk, encoding, callback) {
            callback(null, chunk);
        },
    });
`,
  `    const stream = new stream_1.Transform({
        highWaterMark: 1024 * 1024 * highWaterMarkMB,
        transform(chunk, encoding, callback) {
            callback(null, chunk);
        },
    });
    stream.stop = async () => {
        if (!extension.isClosed() && extension.browser().isConnected()) {
            await extension.evaluate((index) => STOP_RECORDING(index), index).catch(() => {});
        }
        if (!stream.destroyed)
            stream.destroy();
    };
`,
  "capture stop API",
);

source = replaceOnce(
  source,
  `        async function close() {
            var _a, _b;
            if (!stream.readableEnded && !stream.writableEnded)
                stream.end();
            if (!extension.isClosed() && extension.browser().isConnected()) {
                // @ts-ignore
                extension.evaluate((index) => STOP_RECORDING(index), index);
            }
`,
  `        let closing = false;
        async function close() {
            var _a, _b;
            if (closing)
                return;
            closing = true;
            if (!stream.destroyed && !stream.readableEnded && !stream.writableEnded)
                stream.end();
            if (!extension.isClosed() && extension.browser().isConnected()) {
                // @ts-ignore
                await extension.evaluate((index) => STOP_RECORDING(index), index).catch(() => {});
            }
`,
  "idempotent capture close",
);

source = replaceOnce(
  source,
  `        ws.on("message", (data) => {
            stream.write(data);
        });
        ws.on("close", close);
        page.on("close", close);
        stream.on("close", close);
`,
  `        ws.on("message", (data) => {
            if (closing || stream.destroyed || stream.writableEnded)
                return;
            stream.write(data);
        });
        ws.on("close", close);
        ws.on("error", close);
        page.on("close", close);
        stream.on("close", close);
`,
  "capture socket shutdown",
);

source = replaceOnce(
  source,
  `    await lock();
    await page.bringToFront();
    await assertExtensionLoaded(extension, retryPolicy);
    // Invoke extension via keyboard command to grant activeTab (Ctrl/Command+Shift+Y)
    const isMac = process.platform === 'darwin';
    await page.keyboard.down(isMac ? 'Meta' : 'Control');
    await page.keyboard.down('Shift');
    await page.keyboard.press('KeyY');
    await page.keyboard.up('Shift');
    await page.keyboard.up(isMac ? 'Meta' : 'Control');
    // Small delay to let Chrome register the invocation
    await new Promise((r) => setTimeout(r, 100));
    await extension.evaluate(
    // @ts-ignore
    (settings) => START_RECORDING(settings), Object.assign(Object.assign({}, opts), { index, tabId: tab.id }));
    unlock();
`,
  `    await lock();
    try {
        await page.bringToFront();
        await assertExtensionLoaded(extension, retryPolicy);
        // Invoke extension via keyboard command to grant activeTab (Ctrl/Command+Shift+Y)
        const isMac = process.platform === 'darwin';
        await page.keyboard.down(isMac ? 'Meta' : 'Control');
        await page.keyboard.down('Shift');
        await page.keyboard.press('KeyY');
        await page.keyboard.up('Shift');
        await page.keyboard.up(isMac ? 'Meta' : 'Control');
        // Small delay to let Chrome register the invocation
        await new Promise((r) => setTimeout(r, 100));
        await extension.evaluate(
        // @ts-ignore
        (settings) => START_RECORDING(settings), Object.assign(Object.assign({}, opts), { index, tabId: tab.id }));
    }
    catch (error) {
        (await exports.wss).off("connection", onConnection);
        if (!stream.destroyed)
            stream.destroy();
        throw error;
    }
    finally {
        unlock();
    }
`,
  "capture startup mutex",
);

for (const marker of [
  "--allowlisted-extension-id=",
  "let browserClosePromise;",
  "stream.stop = async",
  "stream.destroyed || stream.writableEnded",
  "finally {\n        unlock();",
]) {
  if (!source.includes(marker)) {
    throw new Error(`Incomplete puppeteer-stream patch: ${marker}`);
  }
}

await writeFile(target, source, "utf8");
console.log(`Patched puppeteer-stream runtime: ${target}`);
