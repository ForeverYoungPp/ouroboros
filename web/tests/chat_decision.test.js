import assert from 'node:assert/strict';
import test from 'node:test';

import { createChatDecision } from '../modules/chat_decision.js';

class Classes {
    constructor() { this.values = new Set(); }
    set(value) { this.values = new Set(String(value || '').split(/\s+/).filter(Boolean)); }
    add(...values) { values.forEach((value) => this.values.add(value)); }
    contains(value) { return this.values.has(value); }
    toggle(value, force) {
        const enabled = force === undefined ? !this.contains(value) : Boolean(force);
        if (enabled) this.add(value); else this.values.delete(value);
        return enabled;
    }
}

class NodeStub {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.dataset = {};
        this.classList = new Classes();
        this.disabled = false;
        this.listeners = new Map();
        this.type = '';
        this._text = '';
    }
    set className(value) { this.classList.set(value); }
    get className() { return [...this.classList.values].join(' '); }
    set textContent(value) { this._text = String(value ?? ''); }
    get textContent() { return this._text; }
    append(...nodes) { nodes.forEach((node) => this.children.push(node)); }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    click() { const handler = this.listeners.get('click'); if (handler) handler(); }
    matchesClass(name) { return this.classList.contains(name); }
    collect(name, out = []) {
        if (this.matchesClass(name)) out.push(this);
        this.children.forEach((child) => child.collect(name, out));
        return out;
    }
    querySelector(selector) { return this.collect(selector.replace(/^\./, ''))[0] || null; }
    querySelectorAll(selector) { return this.collect(selector.replace(/^\./, '')); }
}

function fixture({ fetchImpl, renderMarkdown } = {}) {
    const prior = { document: globalThis.document, crypto: globalThis.crypto };
    globalThis.document = { createElement: (tag) => new NodeStub(tag) };
    if (!globalThis.crypto || !globalThis.crypto.randomUUID) {
        Object.defineProperty(globalThis, 'crypto', {
            configurable: true, value: { randomUUID: () => 'fixed-request-id' },
        });
    }
    const toasts = [];
    const calls = [];
    const decision = createChatDecision({
        apiFetch: async (url, init) => {
            calls.push({ url, init });
            if (fetchImpl) return fetchImpl(url, init);
            return { ok: true, status: 200 };
        },
        frameNode: (_msg, node) => node,
        renderMarkdown,
        enhanceMarkdown: renderMarkdown ? () => {} : null,
        showToast: (text, tone) => toasts.push({ text, tone }),
    });
    return { decision, toasts, calls, restore: () => {
        globalThis.document = prior.document;
        Object.defineProperty(globalThis, 'crypto', { configurable: true, value: prior.crypto });
    } };
}

const WS_MSG = {
    type: 'quiz', role: 'assistant', quiz_id: 'qz-1', task_id: 't-1',
    question: 'Merge now?', stake: 'release timing',
    assumption: 'continuing with the merge', state: 'open',
    options: [{ label: 'Yes' }, { label: 'No', detail: 'wait for CI' }],
    ts: '2026-08-31T10:00:00Z',
};

test('quiz card renders full anatomy from a WS frame', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        assert.ok(card);
        assert.equal(card.dataset.state, 'open');
        assert.equal(card.querySelector('.chat-quiz-question').textContent, 'Merge now?');
        assert.match(card.querySelector('.chat-quiz-stake').textContent, /At stake: release timing/);
        assert.match(card.querySelector('.chat-quiz-assumption').textContent, /Continuing meanwhile: continuing with the merge/);
        assert.equal(card.querySelector('.chat-quiz-status-text').textContent, 'Awaiting answer');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.equal(buttons.length, 2);
        assert.equal(buttons[1].querySelector('.chat-quiz-option-detail').textContent, 'wait for CI');
        assert.ok(buttons.every((btn) => !btn.disabled));
    } finally { fx.restore(); }
});

test('quiz card renders the replay shape and settled states disable buttons', () => {
    const fx = fixture();
    try {
        const replayMsg = {
            msg_type: 'quiz', role: 'assistant', task_id: 't-1',
            text: 'Merge now?', ts: 'x',
            quiz: {
                quiz_id: 'qz-2', state: 'expired_terminal',
                options: [{ label: 'Yes' }, { label: 'No' }],
                stake: '', assumption: 'merging meanwhile',
            },
        };
        const card = fx.decision.buildQuizCard(replayMsg);
        assert.ok(card);
        assert.equal(card.dataset.state, 'expired_terminal');
        assert.match(card.querySelector('.chat-quiz-status-text').textContent, /question expired/);
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => btn.disabled));
        // The assumption line survives settlement: it is the record of the path taken.
        assert.match(card.querySelector('.chat-quiz-assumption').textContent, /merging meanwhile/);
    } finally { fx.restore(); }
});

test('an accepted answer marks the chosen option; degenerate cards refuse to render', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        assert.match(fx.calls[0].url, /\/api\/tasks\/t-1\/decision$/);
        const body = JSON.parse(fx.calls[0].init.body);
        assert.equal(body.decision_id, 'quiz:t-1:qz-1');
        assert.equal(body.option_index, 1);
        assert.ok(body.request_id);
        assert.equal(card.dataset.state, 'answered');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.ok(buttons[1].classList.contains('chosen'));
        assert.ok(buttons.every((btn) => btn.disabled));

        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, options: [{ label: 'only' }] }), null);
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: '' }), null);
        // An anonymous quiz has no answer address: refuse to render buttons.
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, task_id: '' }), null);
    } finally { fx.restore(); }
});

test('one corrupt option refuses that card only, preserving index integrity', () => {
    const fx = fixture();
    try {
        // Filtering would shift option_index against the producer's original
        // list and submit a silently WRONG answer once the ingress exists.
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, options: [null, null] }), null);
        assert.equal(fx.decision.buildQuizCard({
            ...WS_MSG, options: [null, 'Plain', { label: 'Real' }, { detail: 'no label' }],
        }), null);
        // String options remain a legal producer shorthand.
        const card = fx.decision.buildQuizCard({ ...WS_MSG, options: ['Plain', { label: 'Real' }] });
        assert.ok(card);
        assert.equal(card.querySelectorAll('.chat-quiz-option').length, 2);
    } finally { fx.restore(); }
});

test('question and stake go through the injected markdown pipeline', () => {
    const fx = fixture({ renderMarkdown: (text) => `<md>${text}</md>` });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        assert.equal(card.querySelector('.chat-quiz-question').innerHTML, '<md>Merge now?</md>');
        assert.equal(card.querySelector('.chat-quiz-stake').innerHTML, '<md>At stake: release timing</md>');
    } finally { fx.restore(); }
});

test('a second click while the first answer is in flight is ignored', async () => {
    let resolveFetch;
    const fx = fixture({ fetchImpl: () => new Promise((resolve) => { resolveFetch = resolve; }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const buttons = card.querySelectorAll('.chat-quiz-option');
        buttons[0].click();
        buttons[1].click();
        resolveFetch({ ok: true, status: 200 });
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        assert.equal(card.dataset.state, 'answered');
    } finally { fx.restore(); }
});

test('a non-409 failure keeps the card open with an honest toast', async () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: false, status: 404 }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'open');
        assert.match(fx.toasts[0].text, /Could not record the answer \(404\)/);
        // The pending latch is released: a later click retries.
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 2);
    } finally { fx.restore(); }
});

test('a 409 settles the card as expired and says so', async () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: false, status: 409 }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'expired_terminal');
        assert.equal(fx.toasts.length, 1);
        assert.match(fx.toasts[0].text, /no longer open/);
    } finally { fx.restore(); }
});
