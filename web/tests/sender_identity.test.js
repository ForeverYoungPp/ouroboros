// C-scheme sender identity (v6.114.3): the UI renders a producer-stamped
// background identity distinctly; everything else keeps 'Ouroboros'.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { senderLabel } from '../modules/chat_activity.js';
import { buildMessageKey } from '../modules/chat_activity.js';

const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');

test('senderLabel renders a background identity distinctly', () => {
    assert.equal(
        senderLabel('assistant', false, 'proactive_message', { senderIdentity: 'background' }),
        '🧠 Background',
    );
});

test('senderLabel keeps the ordinary assistant label by default', () => {
    assert.equal(senderLabel('assistant'), 'Ouroboros');
    assert.equal(senderLabel('assistant', false, '', { senderIdentity: 'agent' }), 'Ouroboros');
});

test('senderLabel keeps progress and system branches intact', () => {
    assert.equal(senderLabel('assistant', true), '💬 Thought');
    assert.equal(senderLabel('system', false, 'task_summary'), '📋 Task Summary');
});

test('chat.js forwards sender_identity to the renderer', () => {
    // History replay path (addMessage opts) carries the projected field.
    assert.match(chat, /senderIdentity: msg\.sender_identity \|\| ''/);
    // addMessage reads it into the renderer call (no senderLabel shadowing).
    assert.match(chat, /const senderIdentity = opts\.senderIdentity \|\| ''/);
});

test('buildMessageKey separates identities so dedupe cannot swallow a sibling', () => {
    // Same text, same timestamp, different identity: distinct keys.
    const base = {
        role: 'assistant', text: 'hello', timestamp: '2026-09-07T19:00:00Z',
        isProgress: false, source: 'web',
    };
    const agentKey = buildMessageKey('assistant', 'hello', '2026-09-07T19:00:00Z', {
        ...base, senderIdentity: 'agent',
    });
    const bgKey = buildMessageKey('assistant', 'hello', '2026-09-07T19:00:00Z', {
        ...base, senderIdentity: 'background',
    });
    assert.notEqual(agentKey, bgKey);
});

test('the offline sessionStorage snapshot carries sender identity', () => {
    // Regression: the snapshot projection dropped the identity, so an offline
    // bootstrap repainted a BG message as "Ouroboros" with a DIFFERENT key than
    // its live/durable bubble — the same record showed twice (one per label)
    // until a full rebuild. Write side and read side must both carry it.
    assert.match(chat, /senderSessionId,\s*\n\s*senderIdentity,/);
    assert.match(chat, /senderIdentity: msg\.senderIdentity \|\| ''/);
});

test('a live frame and its replay row build the SAME key (parity contract)', () => {
    // The live WS frame carries no `source`, `sender_label` or `sender_session_id`
    // (supervisor/message_bus.py:396-410) while the replay passes them off the
    // durable row (web/modules/chat.js:2977-2990). Hashing on fields one side
    // cannot know made the same row render twice — the owner's "Ouroboros and
    // 🧠 Background at once, refreshing collapses it to one" — so the key must be
    // built only from fields BOTH paths have.
    const text = 'the same durable row';
    const ts = '2026-09-11T19:19:11.675716+00:00';
    const liveOpts = {
        role: 'assistant', text, timestamp: ts, isProgress: false,
        systemType: 'proactive_message', source: '', senderIdentity: 'background',
        senderLabel: '', senderSessionId: '', taskId: '',
    };
    const replayOpts = {
        ...liveOpts, source: 'web', senderLabel: 'GB', senderSessionId: 'sess-1234',
    };

    assert.equal(
        buildMessageKey('assistant', text, ts, liveOpts),
        buildMessageKey('assistant', text, ts, replayOpts),
    );

    // Task-keyed branch (taskId present) must have parity too.
    const liveTask = { ...liveOpts, taskId: 'bg-consciousness' };
    const replayTask = { ...replayOpts, taskId: 'bg-consciousness' };
    assert.equal(
        buildMessageKey('assistant', text, ts, liveTask),
        buildMessageKey('assistant', text, ts, replayTask),
    );

    // …and the key must still DISCRIMINATE, or the parity would dedupe real rows.
    assert.notEqual(
        buildMessageKey('assistant', text, ts, liveOpts),
        buildMessageKey('assistant', `${text} (a different message)`, ts, liveOpts),
    );
    assert.notEqual(
        buildMessageKey('assistant', text, ts, liveOpts),
        buildMessageKey('assistant', text, ts, { ...liveOpts, senderIdentity: 'agent' }),
    );
    assert.notEqual(
        buildMessageKey('assistant', text, ts, liveTask),
        buildMessageKey('assistant', text, ts, { ...liveTask, taskId: 'other-task' }),
    );
});
