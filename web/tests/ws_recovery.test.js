import assert from 'node:assert/strict';
import test from 'node:test';

import { WS, decide, RECOVERY_HEALTHY_PROBE_LIMIT } from '../modules/ws.js';

// Behavior coverage for the served-SHA reconciliation contract (netres Lane B):
// one SSOT decision drives both the post-open refresh and the socket-down
// recovery probe. These tests run the real WS code under a mocked browser
// environment (same pattern as client_surface.test.js).

class FakeSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSING = 2;
    static CLOSED = 3;
    static instances = [];

    constructor(url) {
        this.url = url;
        this.readyState = FakeSocket.CONNECTING;
        this.sent = [];
        FakeSocket.instances.push(this);
    }

    send(data) { this.sent.push(data); }

    close() {}
}

function installEnv(responses = []) {
    const replaced = [];
    let calls = 0;
    globalThis.WebSocket = FakeSocket;
    FakeSocket.instances = [];
    globalThis.window = {
        location: {
            href: 'http://127.0.0.1:8000/',
            replace: (url) => replaced.push(String(url)),
        },
    };
    globalThis.document = { getElementById: () => null };
    globalThis.fetch = async () => {
        calls += 1;
        // The last scripted response repeats for every later probe.
        const script = responses.length > 1 ? responses.shift() : responses[0];
        if (!script || script.reject) throw new Error('network down');
        return {
            ok: script.ok !== false,
            json: async () => {
                if (script.badJson) throw new Error('malformed body');
                return script.body ?? {};
            },
        };
    };
    return {
        replaced,
        fetchCalls: () => calls,
        setFetch: (fn) => { globalThis.fetch = async (...args) => { calls += 1; return fn(...args); }; },
    };
}

function settle(turns = 4) {
    let p = Promise.resolve();
    for (let i = 0; i < turns; i += 1) {
        p = p.then(() => new Promise((resolve) => setTimeout(resolve, 0)));
    }
    return p;
}

async function waitFor(cond, what, maxTurns = 400) {
    for (let i = 0; i < maxTurns; i += 1) {
        if (cond()) return;
        await settle(1);
    }
    throw new Error(`condition not reached: ${what}`);
}

// Stops any chained delay-0 recovery timers so node:test can exit: an OPEN
// socket makes an in-flight probe bail without re-arming, then clear timers.
async function teardown(ws) {
    ws.ws = { readyState: FakeSocket.OPEN };
    await settle(6);
    ws._clearUiRecoveryTimer();
    ws._clearReconnectTimer();
    ws._clearWatchdogTimer();
    ws.ws = null;
}

// ---------------------------------------------------------------------------
// decide(): the pure SSOT contract.
// ---------------------------------------------------------------------------

test('decide keeps the page on the first-ever connection regardless of SHA state', () => {
    assert.equal(decide(null, 'abc', false), 'keep');
    assert.equal(decide(null, undefined, false), 'keep');
    assert.equal(decide('abc', 'def', false), 'keep');
});

test('decide keeps the page when the served SHA is unchanged', () => {
    assert.equal(decide('abc', 'abc', true), 'keep');
    assert.equal(decide(' abc ', 'abc', true), 'keep');
});

test('decide reloads as changed when the served SHA differs', () => {
    assert.equal(decide('abc', 'def', true), 'reload_changed');
});

test('decide reloads as unknown for missing, empty, or malformed SHAs after a connection', () => {
    assert.equal(decide('abc', undefined, true), 'reload_unknown');
    assert.equal(decide('abc', null, true), 'reload_unknown');
    assert.equal(decide('abc', '', true), 'reload_unknown');
    assert.equal(decide('abc', '   ', true), 'reload_unknown');
    assert.equal(decide('abc', 12345, true), 'reload_unknown');
    assert.equal(decide('abc', { sha: 'abc' }, true), 'reload_unknown');
    // An unproveable PREVIOUS SHA equally forbids the keep claim.
    assert.equal(decide(null, 'abc', true), 'reload_unknown');
    assert.equal(decide('', 'abc', true), 'reload_unknown');
});

// ---------------------------------------------------------------------------
// _refreshStateAfterOpen: post-open reconciliation through the same SSOT.
// ---------------------------------------------------------------------------

test('first open remembers the served SHA without reloading; later opens compare against it', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._refreshStateAfterOpen(false);
        await settle();
        assert.equal(env.replaced.length, 0);

        // Same SHA after a reconnect: keep.
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 0);

        // Changed SHA after a reconnect: reload (proves the first open stored 'abc').
        env.setFetch(async () => ({ ok: true, json: async () => ({ sha: 'def' }) }));
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
    } finally {
        await teardown(ws);
    }
});

test('an OK state response without a SHA after a reconnect reloads (the closed keep-hole)', async () => {
    const env = installEnv([{ body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a malformed state body after a reconnect reloads as unknown', async () => {
    const env = installEnv([{ badJson: true }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a non-OK state response after open never reloads', async () => {
    const env = installEnv([{ ok: false, body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 0);
    } finally {
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// _scheduleUiRecovery: probe decisions, fuse, race bail, queue survival.
// ---------------------------------------------------------------------------

test('healthy same-SHA probes reconnect in place; the fuse reloads exactly on the Nth probe', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload');
        // The fuse fired on the Nth probe — earlier healthy probes kept the page.
        assert.equal(env.fetchCalls(), RECOVERY_HEALTHY_PROBE_LIMIT);
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/);
    } finally {
        await teardown(ws);
    }
});

test('a failed probe resets the consecutive healthy count', async () => {
    const env = installEnv([
        { body: { sha: 'abc' } },
        { body: { sha: 'abc' } },
        { ok: false },
        { body: { sha: 'abc' } },
    ]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload after reset');
        // Two healthy, one failed (reset), then a fresh full run of healthy probes.
        assert.equal(env.fetchCalls(), 3 + RECOVERY_HEALTHY_PROBE_LIMIT);
    } finally {
        await teardown(ws);
    }
});

test('a recovery probe seeing a changed SHA reloads immediately', async () => {
    const env = installEnv([{ body: { sha: 'def' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'sha-change reload');
        assert.equal(env.fetchCalls(), 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
    } finally {
        await teardown(ws);
    }
});

test('a recovery probe with a missing SHA after a connection reloads as unknown', async () => {
    const env = installEnv([{ body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'sha-unknown reload');
        assert.equal(env.fetchCalls(), 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a probe resolving after the socket reopened bails without reloading or re-arming', async () => {
    const env = installEnv([]);
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    env.setFetch(async () => {
        await gate;
        // A changed SHA that would reload were the bail missing.
        return { ok: true, json: async () => ({ sha: 'zzz' }) };
    });
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 1, 'probe dispatched');
        ws.ws = { readyState: FakeSocket.OPEN };
        release();
        await settle();
        assert.equal(env.replaced.length, 0);
        await settle();
        assert.equal(env.fetchCalls(), 1, 'a bailed probe must not re-arm recovery');
    } finally {
        await teardown(ws);
    }
});

test('the fuse fires at most once per disconnect episode and resets on reconnect', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';

        // Episode 1: fuse fires once.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'first fuse');

        // Still down, recovery re-armed by the reconnect cycle: healthy probes
        // continue but the episode fuse stays latched.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        const seen = env.fetchCalls();
        await waitFor(
            () => env.fetchCalls() >= seen + RECOVERY_HEALTHY_PROBE_LIMIT + 2,
            'post-fuse probes',
        );
        assert.equal(env.replaced.length, 1, 'the fuse must not fire twice in one episode');

        // Successful reconnect ends the episode.
        ws.ws = { readyState: FakeSocket.OPEN };
        await settle(6);
        ws.ws = null;
        ws.connect();
        const sock = FakeSocket.instances.at(-1);
        sock.readyState = FakeSocket.OPEN;
        sock.onopen();
        await settle();
        assert.equal(env.replaced.length, 1, 'a same-SHA reconnect must not reload');

        // Episode 2 after a fresh disconnect: the fuse is armed again.
        ws.ws = null;
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 2, 'second-episode fuse');
    } finally {
        await teardown(ws);
    }
});

test('keep-recovery preserves the outbound queue and the reconnect flush delivers it', async () => {
    const env = installEnv([
        { body: { sha: 'abc' } },
        { ok: false },
    ]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        const outbound = [];
        ws.on('outbound_queued', (e) => outbound.push(['queued', e.clientMessageId]));
        ws.on('outbound_sent', (e) => outbound.push(['sent', e.clientMessageId, e.queued]));

        const result = ws.send({ type: 'chat', text: 'hello' });
        assert.equal(result.status, 'queued');
        // send() armed the slow default timers; drive recovery deterministically.
        ws._clearUiRecoveryTimer();
        ws._clearReconnectTimer();
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() >= 2, 'keep probe plus follow-up');
        assert.equal(env.replaced.length, 0, 'a healthy same-SHA probe must not reload');

        const sock = ws.ws;
        sock.readyState = FakeSocket.OPEN;
        sock.onopen();
        await settle();
        assert.equal(env.replaced.length, 0);
        assert.equal(sock.sent.length, 1, 'the queued message must flush on reconnect');
        const frame = JSON.parse(sock.sent[0]);
        assert.equal(frame.type, 'chat');
        assert.equal(frame.text, 'hello');
        assert.deepEqual(outbound[0], ['queued', result.clientMessageId]);
        assert.deepEqual(outbound[1], ['sent', result.clientMessageId, true]);
    } finally {
        await teardown(ws);
    }
});
