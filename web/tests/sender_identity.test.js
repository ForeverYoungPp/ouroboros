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
