import assert from 'node:assert/strict';
import test from 'node:test';

import { classifyShellUrl, installDesktopShellLinkInterceptor } from '../modules/ui_helpers.js';

const BASE = 'http://127.0.0.1:8765/';

// --- classifier -----------------------------------------------------------

test('classifyShellUrl routes loopback file forms to the bridge (path-only url)', () => {
    assert.deepEqual(
        classifyShellUrl('/api/files/download?path=docs/report.pdf', BASE),
        { kind: 'file', url: '/api/files/download?path=docs/report.pdf' },
    );
    assert.deepEqual(
        classifyShellUrl(`${BASE}api/tasks/t-1/artifacts/chat-media-abc.png`, BASE),
        { kind: 'file', url: '/api/tasks/t-1/artifacts/chat-media-abc.png' },
    );
    assert.deepEqual(
        classifyShellUrl('/api/extensions/skill/asset.png', BASE),
        { kind: 'file', url: '/api/extensions/skill/asset.png' },
    );
});

test('classifyShellUrl routes any other http(s) target to the external opener', () => {
    assert.deepEqual(
        classifyShellUrl('https://example.com/page', BASE),
        { kind: 'external', url: 'https://example.com/page' },
    );
    // Same-origin non-file pages have no tab inside the shell either.
    assert.deepEqual(
        classifyShellUrl('/dashboard', BASE),
        { kind: 'external', url: `${BASE}dashboard` },
    );
    // Different port on the loopback host is NOT the file bridge.
    assert.equal(classifyShellUrl('http://127.0.0.1:9999/api/files/download?path=a', BASE).kind, 'external');
});

test('classifyShellUrl routes data:/blob: payloads to byte saving', () => {
    assert.equal(classifyShellUrl('data:image/png;base64,AAAA', BASE).kind, 'bytes');
    assert.equal(classifyShellUrl(`blob:${BASE}0-1-2`, BASE).kind, 'bytes');
});

test('classifyShellUrl leaves everything else to the default handler', () => {
    assert.equal(classifyShellUrl('', BASE).kind, 'default');
    assert.equal(classifyShellUrl('mailto:owner@example.com', BASE).kind, 'default');
    assert.equal(classifyShellUrl('javascript:void(0)', BASE).kind, 'default');
    assert.equal(classifyShellUrl('#anchor', '').kind, 'default');
});

// --- harness --------------------------------------------------------------

function makeAnchor(attrs) {
    return {
        getAttribute: (name) => (Object.hasOwn(attrs, name) ? attrs[name] : null),
        hasAttribute: (name) => Object.hasOwn(attrs, name),
    };
}

function makeEvent(anchor) {
    return {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
        target: { closest: (selector) => (selector === 'a[href]' ? anchor : null) },
    };
}

function makeHarness({ api, pywebview = true } = {}) {
    const calls = { open: [], copied: [], toasts: [], openFile: [], downloadFile: [] };
    const docListeners = {};
    const winListeners = {};
    const doc = {
        addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
        createElement: () => ({ setAttribute() {}, select() {}, remove() {}, value: '' }),
        body: { appendChild() {} },
        execCommand: () => true,
    };
    const win = {
        location: { href: BASE },
        navigator: { clipboard: { writeText: async (text) => { calls.copied.push(text); } } },
        addEventListener(type, fn) { (winListeners[type] ||= []).push(fn); },
        open(...args) { calls.open.push(args); return 'native-window'; },
    };
    if (pywebview) win.pywebview = { api: api || {} };
    installDesktopShellLinkInterceptor({
        win,
        doc,
        toast: (message, tone) => calls.toasts.push({ message, tone }),
        openFile: async (url, name) => { calls.openFile.push([url, name]); },
        downloadFile: async (url, name) => { calls.downloadFile.push([url, name]); },
    });
    const click = async (anchor) => {
        const event = makeEvent(anchor);
        for (const fn of docListeners.click || []) fn(event);
        await new Promise((resolve) => setTimeout(resolve, 0));
        return event;
    };
    return { win, doc, calls, docListeners, winListeners, click };
}

const FILE_API = { open_file_with_default_app: async () => ({ ok: true }), download_file_to_downloads: async () => ({ ok: true }) };

// --- install gating -------------------------------------------------------

test('browser mode installs nothing: no click listener, native window.open kept', () => {
    const hx = makeHarness({ pywebview: false });
    assert.equal(hx.docListeners.click, undefined, 'no delegated click listener');
    assert.equal(hx.win.open('https://example.com', '_blank'), 'native-window');
    assert.equal(hx.calls.open.length, 1, 'window.open is the untouched native one');
    assert.equal((hx.winListeners.pywebviewready || []).length, 1, 'armed only for a late bridge');
});

test('a late pywebviewready announcement installs the interceptor', async () => {
    const hx = makeHarness({ pywebview: false });
    const external = [];
    hx.win.pywebview = { api: { open_external_url: async (url) => { external.push(url); return { ok: true }; } } };
    for (const fn of hx.winListeners.pywebviewready) fn();
    assert.equal(hx.docListeners.click.length, 1, 'delegated listener installed on the ready event');
    const event = await hx.click(makeAnchor({ href: 'https://example.com/x', target: '_blank' }));
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(external, ['https://example.com/x']);
});

// --- click routing --------------------------------------------------------

test('external link rides open_external_url and surfaces bridge refusals', async () => {
    const external = [];
    let hx = makeHarness({ api: { open_external_url: async (url) => { external.push(url); return { ok: true }; } } });
    const event = await hx.click(makeAnchor({ href: 'https://example.com/docs', target: '_blank' }));
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(external, ['https://example.com/docs']);
    assert.deepEqual(hx.calls.toasts, [], 'success is silent — the browser opening IS the feedback');

    hx = makeHarness({ api: { open_external_url: async () => ({ ok: false, error: 'refused' }) } });
    await hx.click(makeAnchor({ href: 'https://example.com/x', target: '_blank' }));
    assert.match(hx.calls.toasts[0].message, /Could not open link: refused/);
});

test('old launcher without open_external_url copies the link with an honest toast', async () => {
    const hx = makeHarness({ api: {} });
    const event = await hx.click(makeAnchor({ href: 'https://example.com/release', target: '_blank' }));
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(hx.calls.copied, ['https://example.com/release']);
    assert.deepEqual(hx.calls.toasts, [{ message: 'Link copied — open it in your browser.', tone: 'info' }]);
});

test('base64 data: payload rides save_bytes_to_downloads with a mime-derived name', async () => {
    const saved = [];
    const hx = makeHarness({
        api: { save_bytes_to_downloads: async (name, b64) => { saved.push([name, b64]); return { ok: true, path: '/home/o/Downloads/download.png' }; } },
    });
    const event = await hx.click(makeAnchor({ href: 'data:image/png;base64,AAECAw==', download: '' }));
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(saved, [['download.png', 'AAECAw==']]);
    assert.deepEqual(hx.calls.toasts, [{ message: 'Saved to Downloads: download.png', tone: 'ok' }]);
});

test('a download-named anchor keeps its filename for byte saves', async () => {
    const saved = [];
    const hx = makeHarness({
        api: { save_bytes_to_downloads: async (name, b64) => { saved.push([name, b64]); return { ok: true }; } },
    });
    await hx.click(makeAnchor({ href: 'data:text/plain;base64,aGk=', download: 'notes.txt' }));
    assert.deepEqual(saved, [['notes.txt', 'aGk=']]);
});

test('old launcher without save_bytes_to_downloads toasts that saving is unavailable', async () => {
    const hx = makeHarness({ api: {} });
    const event = await hx.click(makeAnchor({ href: 'data:image/png;base64,AAAA', download: '' }));
    assert.equal(event.defaultPrevented, true);
    assert.deepEqual(hx.calls.toasts, [{ message: "Saving isn't available in the app — open in a browser.", tone: 'warn' }]);
});

test('loopback artifact link opens via the host bridge; download attr downloads instead', async () => {
    const hx = makeHarness({ api: FILE_API });
    const openEvent = await hx.click(makeAnchor({ href: '/api/tasks/t-9/artifacts/chat-media-ff.png', target: '_blank' }));
    assert.equal(openEvent.defaultPrevented, true);
    assert.deepEqual(hx.calls.openFile, [['/api/tasks/t-9/artifacts/chat-media-ff.png', 'chat-media-ff.png']]);

    await hx.click(makeAnchor({ href: '/api/files/download?path=logs/run.txt', download: '' }));
    assert.deepEqual(hx.calls.downloadFile, [['/api/files/download?path=logs/run.txt', 'run.txt']]);
});

test('without ANY file bridge method the file class keeps the native default (no loop)', async () => {
    const hx = makeHarness({ api: {} });
    const event = await hx.click(makeAnchor({ href: '/api/files/download?path=a.txt', target: '_blank' }));
    assert.equal(event.defaultPrevented, false, 'default kept: helpers would fall back into the shim');
    assert.deepEqual(hx.calls.openFile, []);
    assert.deepEqual(hx.calls.downloadFile, []);
});

test('ordinary same-tab anchors and non-anchor clicks stay untouched', async () => {
    const hx = makeHarness({ api: { open_external_url: async () => ({ ok: true }) } });
    const plain = await hx.click(makeAnchor({ href: 'https://example.com/inline' }));
    assert.equal(plain.defaultPrevented, false, 'no target=_blank and no download attribute');
    const event = makeEvent(null);
    for (const fn of hx.docListeners.click) fn(event);
    assert.equal(event.defaultPrevented, false);
});

// --- window.open shim -----------------------------------------------------

test('window.open shim routes external URLs over the bridge and returns null', async () => {
    const external = [];
    const hx = makeHarness({ api: { open_external_url: async (url) => { external.push(url); return { ok: true }; } } });
    const result = hx.win.open('https://example.com/photo', '_blank', 'noopener');
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(result, null);
    assert.deepEqual(external, ['https://example.com/photo']);
    assert.deepEqual(hx.calls.open, [], 'native open was not reached');
});

test('window.open shim opens durable media via the file bridge and passes the rest through', async () => {
    const hx = makeHarness({ api: FILE_API });
    assert.equal(hx.win.open('/api/tasks/t-1/artifacts/chat-media-aa.png', '_blank', 'noopener'), null);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(hx.calls.openFile, [['/api/tasks/t-1/artifacts/chat-media-aa.png', 'chat-media-aa.png']]);
    // Unclassifiable targets keep the native behavior.
    assert.equal(hx.win.open('mailto:o@example.com'), 'native-window');
    assert.equal(hx.calls.open.length, 1);
});

test('window.open shim keeps native default for file URLs on an ancient launcher', async () => {
    const hx = makeHarness({ api: {} });
    assert.equal(hx.win.open('/api/files/download?path=a.txt', '_blank'), 'native-window');
    assert.deepEqual(hx.calls.open, [['/api/files/download?path=a.txt', '_blank', undefined]]);
    assert.deepEqual(hx.calls.openFile, []);
});
