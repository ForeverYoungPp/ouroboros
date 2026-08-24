import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
    WIDGET_FRAME_BORDER_RESERVE,
    WIDGET_FRAME_DEFAULT_HEIGHT,
    WIDGET_FRAME_MAX_HEIGHT,
} from '../modules/widgets.js';
import {
    moduleResizeScript,
} from '../modules/widget_frame.js';
import {
    classifyWidgetJobStatus,
    isRetryableWidgetError,
    readWidgetJobStatus,
    withWidgetRequestTimeout,
} from '../modules/widget_job.js';

function resizeHarness({
    floor = WIDGET_FRAME_DEFAULT_HEIGHT,
    maxHeight = 1000,
    borderReserve = WIDGET_FRAME_BORDER_RESERVE,
    initialHeight = 600,
    paddingBottom = 12,
    borderBottom = 4,
} = {}) {
    const state = { height: initialHeight };
    const messages = [];
    const sequence = [];
    const disposeCallbacks = [];
    const listeners = new Map();
    const styleWrites = { append: 0, remove: 0 };
    let observerCallback = null;
    let observerDisconnects = 0;
    const style = {
        textContent: '',
        parentNode: null,
        remove() {
            if (!this.parentNode) return;
            this.parentNode = null;
            styleWrites.remove += 1;
            sequence.push('style:remove');
        },
    };
    const head = {
        appendChild(node) {
            assert.equal(node, style);
            node.parentNode = head;
            styleWrites.append += 1;
            sequence.push('style:append');
        },
    };
    const root = {
        get scrollHeight() { return state.height; },
        getBoundingClientRect: () => ({ height: state.height, bottom: state.height }),
    };
    const body = {
        scrollHeight: 0,
        clientHeight: 0,
        getBoundingClientRect: () => ({ top: 0 }),
    };
    const document = {
        body,
        head,
        createElement(tag) {
            assert.equal(tag, 'style');
            return style;
        },
        getElementById: (id) => (id === 'root' ? root : null),
    };
    const window = {
        innerHeight: 768,
        parent: {
            postMessage(message) {
                messages.push(message);
                sequence.push('message');
            },
        },
        addEventListener(type, listener) { listeners.set(type, listener); },
        removeEventListener(type, listener) {
            if (listeners.get(type) === listener) listeners.delete(type);
        },
        __ouroWidgetOnDispose(callback) { disposeCallbacks.push(callback); },
    };
    class FakeResizeObserver {
        constructor(callback) { observerCallback = callback; }
        observe(target) { assert.equal(target, root); }
        disconnect() { observerDisconnects += 1; }
    }
    const getComputedStyle = (target) => {
        assert.equal(target, body);
        return {
            height: 'auto',
            paddingBottom: `${paddingBottom}px`,
            borderBottomWidth: `${borderBottom}px`,
        };
    };
    Function(
        'document',
        'window',
        'ResizeObserver',
        'getComputedStyle',
        moduleResizeScript('nonce', floor, maxHeight, borderReserve),
    )(document, window, FakeResizeObserver, getComputedStyle);
    return {
        messages,
        sequence,
        style,
        styleWrites,
        listeners,
        observerDisconnects: () => observerDisconnects,
        resize(height) {
            state.height = height;
            observerCallback();
        },
        dispose() { disposeCallbacks.forEach((callback) => callback()); },
    };
}

test('widget frame contract keeps the bounded host geometry', () => {
    assert.equal(WIDGET_FRAME_DEFAULT_HEIGHT, 320);
    assert.equal(WIDGET_FRAME_MAX_HEIGHT, 8192);
    assert.equal(WIDGET_FRAME_BORDER_RESERVE, 2);
});

test('module auto-height owns only vertical overflow across cap transitions', () => {
    const harness = resizeHarness();
    assert.equal(harness.style.textContent, 'html, body { overflow-y: hidden !important; }');
    assert.doesNotMatch(harness.style.textContent, /overflow-x/);
    assert.equal(harness.style.parentNode !== null, true);
    assert.deepEqual(harness.styleWrites, { append: 1, remove: 0 });
    assert.deepEqual(harness.messages.map((item) => item.height), [616]);
    assert.deepEqual(harness.sequence.slice(0, 2), ['style:append', 'message']);

    harness.resize(600);
    assert.deepEqual(harness.styleWrites, { append: 1, remove: 0 });
    assert.equal(harness.messages.length, 1);

    harness.resize(1200);
    assert.equal(harness.style.parentNode, null);
    assert.deepEqual(harness.styleWrites, { append: 1, remove: 1 });
    assert.deepEqual(harness.messages.map((item) => item.height), [616, 1216]);

    harness.resize(1200);
    assert.deepEqual(harness.styleWrites, { append: 1, remove: 1 });
    assert.equal(harness.messages.length, 2);

    harness.resize(500);
    assert.equal(harness.style.parentNode !== null, true);
    assert.deepEqual(harness.styleWrites, { append: 2, remove: 1 });
    assert.deepEqual(harness.messages.map((item) => item.height), [616, 1216, 516]);
});

test('module overflow ownership covers floor equality, fixed-height no-op, and cleanup', () => {
    const floorCap = resizeHarness({ maxHeight: WIDGET_FRAME_DEFAULT_HEIGHT, initialHeight: 100 });
    assert.equal(floorCap.style.parentNode, null);
    assert.deepEqual(floorCap.styleWrites, { append: 1, remove: 1 });

    const harness = resizeHarness();
    harness.dispose();
    assert.equal(harness.style.parentNode, null);
    assert.deepEqual(harness.styleWrites, { append: 1, remove: 1 });
    assert.equal(harness.observerDisconnects(), 1);
    assert.equal(harness.listeners.has('load'), false);

    const widgetsSource = readFileSync(new URL('../modules/widgets.js', import.meta.url), 'utf8');
    assert.match(widgetsSource, /const resizeBridge = autoHeight\s*\? moduleResizeScript\(/);
    assert.match(
        widgetsSource,
        /moduleResizeScript\(\s*nonce,\s*WIDGET_FRAME_DEFAULT_HEIGHT,\s*maxHeight,\s*WIDGET_FRAME_BORDER_RESERVE,/,
    );
    assert.doesNotMatch(widgetsSource, /scrolling="no"|syncModuleFrameScrolling/);
});

test('widget job retry classification distinguishes transport from terminal errors', () => {
    assert.equal(isRetryableWidgetError({ status: 408 }), true);
    assert.equal(isRetryableWidgetError({ status: 429 }), true);
    assert.equal(isRetryableWidgetError({ status: 503 }), true);
    assert.equal(isRetryableWidgetError({ name: 'TypeError' }), true);
    assert.equal(isRetryableWidgetError({ status: 400 }), false);
    assert.equal(isRetryableWidgetError({ status: 404 }), false);
    assert.equal(isRetryableWidgetError({ status: 200, retryable: false }), false);
    assert.equal(isRetryableWidgetError({ name: 'AbortError', retryable: true }), false);
});

test('widget jobs bound unknown status and reject a missing status', () => {
    assert.equal(classifyWidgetJobStatus('queued'), 'pending');
    assert.equal(classifyWidgetJobStatus('running'), 'pending');
    assert.equal(classifyWidgetJobStatus('done'), 'success');
    assert.equal(classifyWidgetJobStatus('failed'), 'failure');
    assert.equal(classifyWidgetJobStatus(''), 'invalid');
    assert.equal(classifyWidgetJobStatus(123), 'invalid');
    assert.equal(classifyWidgetJobStatus({}), 'invalid');
    assert.equal(classifyWidgetJobStatus([]), 'invalid');
    assert.equal(classifyWidgetJobStatus('mystery'), 'pending');
});

test('widget job status selection preserves explicit falsy status values', () => {
    assert.equal(readWidgetJobStatus({ status: 0, state: 'running' }), 0);
    assert.equal(readWidgetJobStatus({ status: false, state: 'running' }), false);
    assert.equal(readWidgetJobStatus({ status: '', state: 'running' }), '');
    assert.equal(readWidgetJobStatus({ state: 'running' }), 'running');
});

test('widget request timeout aborts the request and remains retryable', async () => {
    const controller = new AbortController();
    await assert.rejects(
        withWidgetRequestTimeout(
            (signal) => new Promise((_, reject) => {
                signal.addEventListener('abort', () => {
                    const error = new Error('aborted');
                    error.name = 'AbortError';
                    reject(error);
                }, { once: true });
            }),
            controller,
            5,
        ),
        (error) => error.code === 'WIDGET_REQUEST_TIMEOUT' && error.retryable === true,
    );
    assert.equal(controller.signal.aborted, true);
});

test('widget request timeout stays terminal when the task swallows abort', async () => {
    const controller = new AbortController();
    await assert.rejects(
        withWidgetRequestTimeout(
            () => new Promise((resolve) => setTimeout(() => resolve('late result'), 20)),
            controller,
            5,
        ),
        (error) => error.code === 'WIDGET_REQUEST_TIMEOUT' && error.retryable === true,
    );
    assert.equal(controller.signal.aborted, true);
});
