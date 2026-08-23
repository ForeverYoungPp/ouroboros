import assert from 'node:assert/strict';
import test from 'node:test';

import { mainThreadAccepts } from '../modules/chat_activity.js';

const known = new Set([60193030]);

test('main adopts its own, legacy and transport-shaped frames as before', () => {
    assert.equal(mainThreadAccepts({ chat_id: 1 }, known), true);
    assert.equal(mainThreadAccepts({}, known), true);               // missing chat_id -> main
    assert.equal(mainThreadAccepts({ chat_id: 0 }, known), true);   // legacy log default
    // External transport (Telegram-shaped big id) is never a registry member
    // and is never stamped: Main keeps adopting it.
    assert.equal(mainThreadAccepts({ chat_id: 197422551 }, known), true);
    assert.equal(mainThreadAccepts(null, known), true);
});

test('main rejects frames of a project it already knows', () => {
    assert.equal(mainThreadAccepts({ chat_id: 60193030 }, known), false);
});

test('server stamp rejects a project frame the client has not learned yet', () => {
    // The race: fresh project, projectChatIds still empty (or stale).
    assert.equal(mainThreadAccepts({ chat_id: 77777777, project_thread: true }, new Set()), false);
    assert.equal(mainThreadAccepts({ chat_id: 77777777, project_thread: true }, null), false);
    assert.equal(mainThreadAccepts({ chat_id: 77777777 }, new Set()), true); // unstamped unknown id: unchanged behaviour
});

test('stamp is authoritative regardless of chat_id shape', () => {
    assert.equal(mainThreadAccepts({ chat_id: 1, project_thread: true }, known), false);
    assert.equal(mainThreadAccepts({ project_thread: true }, known), false);
});
