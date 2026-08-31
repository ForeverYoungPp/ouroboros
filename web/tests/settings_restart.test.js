import test from 'node:test';
import assert from 'node:assert/strict';

import { confirmAndSendRestart } from '../modules/settings.js';
import { summarizeChatLiveEvent } from '../modules/log_events.js';

// #285 decision 16=A: the settings "Restart now" flow — confirm dialog, the
// exact /restart command, queue:false (a disconnected page must not queue a
// destructive command for a later reconnect), and honest outcomes.

test('Restart now confirm-and-send sends exactly one non-queueable /restart', async () => {
    const sent = [];
    const seenOptions = [];
    const outcome = await confirmAndSendRestart({
        openConfirmDialog: async (options) => {
            seenOptions.push(options);
            return true;
        },
        ws: { send: (msg, options) => { sent.push([msg, options]); return { status: 'sent' }; } },
    });
    assert.equal(outcome, 'sent');
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0][0], { type: 'command', cmd: '/restart' });
    assert.deepEqual(sent[0][1], { queue: false });
    assert.equal(seenOptions[0].danger, true);
    assert.equal(seenOptions[0].confirmLabel, 'Restart');
    // The dialog must not understate the blast radius: /restart stops ALL
    // running and queued tasks (server owner-restart path), not just one.
    assert.match(seenOptions[0].body, /running and queued tasks stop/);
});

test('Restart now cancel sends nothing', async () => {
    for (const resolution of [false, undefined, null, 0]) {
        const sent = [];
        const outcome = await confirmAndSendRestart({
            openConfirmDialog: async () => resolution,
            ws: { send: (msg) => { sent.push(msg); return { status: 'sent' }; } },
        });
        assert.equal(outcome, 'cancelled');
        assert.deepEqual(sent, []);
    }
});

test('Restart now on a disconnected socket reports not_connected, never queues', async () => {
    const outcome = await confirmAndSendRestart({
        openConfirmDialog: async () => true,
        ws: { send: (_msg, options) => (options?.queue === false ? { status: 'failed' } : { status: 'queued' }) },
    });
    assert.equal(outcome, 'not_connected');
});

// #285 loud disclosure: the reload-failure event must be VISIBLE in the chat
// timeline (the default projection hides unknown types) with the honest story.
test('task_start_settings_reload_failed renders a visible warning row in chat', () => {
    const view = summarizeChatLiveEvent({
        type: 'task_start_settings_reload_failed',
        task_id: 't1',
        error: 'RuntimeError: settings.json unreadable',
    });
    assert.equal(view.visible, true);
    assert.equal(view.phase, 'warn');
    assert.match(view.headline, /Settings reload failed/);
    assert.match(view.body, /previously applied configuration/);
});
