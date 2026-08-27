// Close/dismiss lifecycle of the login card controller — split from
// harness_login_cards.test.js per that suite's recorded size-ratchet
// commitment ("split when the next face lands"); the instant-acknowledgement
// face is that landing. Local helper copies follow the house test pattern
// (each suite owns its fakes).

import assert from 'node:assert/strict';
import test from 'node:test';

import { createLoginCardController } from '../modules/harness_login_cards.js';

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

const flush = async () => { for (let i = 0; i < 40; i += 1) await Promise.resolve(); };

function interactiveHost() {
    const listeners = new Map();
    return {
        innerHTML: '',
        contains: () => false,
        querySelector(selector) {
            const marker = selector.match(/\[([^\]]+)\]/)?.[1] || '';
            if (!marker || !this.innerHTML.includes(marker)) return null;
            return {
                open: false,
                addEventListener(type, callback) { listeners.set(`${selector}:${type}`, callback); },
            };
        },
        querySelectorAll: () => [],
        click(selector) { return listeners.get(`${selector}:click`)?.({ preventDefault() {} }); },
    };
}


test('Close answers instantly while the create transition is still installing the runtime', async () => {
    // The click's acknowledgement must not wait for the queued close: during a
    // first install the create POST holds the transition chain for minutes,
    // and a silent queued click reads as a dead button.
    let releaseCreate = () => {};
    const createGate = new Promise((resolve) => { releaseCreate = resolve; });
    let deletes = 0;
    const host = interactiveHost();
    const ctl = createLoginCardController({
        host,
        store: null,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                await createGate;
                return json(200, { job_id: 'job-slow', job: { state: 'running' }, attach_command: '' });
            }
            if (init.method === 'DELETE') {
                deletes += 1;
                return json(200, { job: { state: 'cancelled', outcome: { reason: 'user_cancelled' } } });
            }
            return json(200, { job: { state: 'cancelled' } });
        },
    });

    const starting = ctl.start('codex', '');
    await flush();
    assert.ok(host.innerHTML.includes('Checking Claudexor…'),
        'the preparing line is live while the create POST holds the chain');
    assert.ok(host.innerHTML.includes('>Close<'), 'Close renders enabled before the press');

    host.click('[data-login-dismiss]');
    assert.ok(host.innerHTML.includes('Closing…'),
        'the press acknowledges SYNCHRONOUSLY — before any transition settles');
    assert.ok(host.innerHTML.includes('data-login-dismiss disabled'),
        'the acknowledged button cannot be pressed twice');
    assert.equal(deletes, 0, 'nothing to cancel yet: the close is queued, not lost');

    releaseCreate();
    await starting;
    await flush();
    assert.equal(deletes, 1, 'the queued close cancelled the job the create produced');
    assert.equal(host.innerHTML, '', 'the settled close cleared the card');
});


test('detach releases the phase-follow subscription like every other resource', () => {
    // detach() permanently fences the controller outside the transition queue
    // (pagehide, second recovery-face Close); a subscription surviving it
    // would keep re-rendering a dead card on every status snapshot.
    let subscribed = 0;
    const store = {
        subscribe: () => { subscribed += 1; return () => { subscribed -= 1; }; },
        snapshot: null,
    };
    const ctl = createLoginCardController({ host: interactiveHost(), store, fetchImpl: async () => json(404, {}) });
    assert.equal(subscribed, 1, 'the controller follows phases from construction');
    ctl.detach();
    assert.equal(subscribed, 0, 'a detached controller stops following the store');
    ctl.dispose();
    assert.equal(subscribed, 0, 'a later dispose releases nothing twice');
});

test('the preparation line advances with live snapshots and retreats on a dead read', async () => {
    // End-to-end: while the create POST holds the transition chain, the
    // phase-follow subscription re-renders the card as store reads land — and
    // a FAILED read (which retains the prior snapshot) must not keep a
    // positive "Installing…" claim alive.
    const { createClaudexorStatusStore } = await import('../modules/claudexor_status_store.js');
    let runtime = { state: 'installing', target_version: '3.3.14' };
    let daemonState = 'unreachable';
    let fail = false;
    const store = createClaudexorStatusStore({
        fetchImpl: async () => {
            if (fail) throw new Error('status read died');
            return json(200, {
                daemon: { state: daemonState, runtime },
                config_dir: '/home/agent', harnesses: [], profiles: { harnessAccounts: [], profiles: [] }, quota: [],
            });
        },
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    let releaseCreate = () => {};
    const gate = new Promise((resolve) => { releaseCreate = resolve; });
    const host = interactiveHost();
    const ctl = createLoginCardController({
        host,
        store,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                await gate;
                return json(200, { job_id: 'job-x', job: { state: 'cancelled' }, attach_command: '' });
            }
            return json(200, { job: { state: 'cancelled' } });
        },
    });
    try {
        const starting = ctl.start('codex', '');
        await flush();
        assert.ok(host.innerHTML.includes('Checking Claudexor…'), 'no snapshot yet — the generic');

        await store.refresh();
        assert.ok(host.innerHTML.includes('Installing Claudexor 3.3.14…'),
            'a landed installing snapshot advances the line without any job poll');

        runtime = { state: 'ready' };
        daemonState = 'stale';
        await store.refresh();
        assert.ok(host.innerHTML.includes('Starting the Claudexor daemon…'));

        fail = true;
        await store.refresh();
        assert.ok(host.innerHTML.includes('Checking Claudexor…'),
            'a dead read retains the snapshot but is NOT phase evidence');

        releaseCreate();
        await starting;
    } finally {
        ctl.dispose();
        await flush();
        store.dispose();
    }
});

test('a close queued during a create that fails without a job detaches honestly', async () => {
    // The queued close observes the world the create left: a failed create
    // has no job id, so there is nothing a DELETE can address — the close
    // must land on the same honest local detach the pre-queue check applies.
    let releaseCreate = () => {};
    const gate = new Promise((resolve) => { releaseCreate = resolve; });
    let deletes = 0;
    const host = interactiveHost();
    const ctl = createLoginCardController({
        host,
        store: null,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                await gate;
                return json(502, { error: 'engine exploded mid-create' });
            }
            if (init.method === 'DELETE') { deletes += 1; return json(200, { job: {} }); }
            return json(404, {});
        },
    });
    const starting = ctl.start('codex', '');
    await flush();
    const clicked = host.click('[data-login-dismiss]');
    assert.ok(host.innerHTML.includes('Closing…'), 'the press acknowledged before the create settled');
    releaseCreate();
    await starting;
    const settledStatus = await clicked;
    assert.equal(deletes, 0, 'no job id — nothing a DELETE can address');
    assert.equal(host.innerHTML, '', 'the queued close detached the failed create honestly');
    assert.ok(typeof settledStatus === 'string' && settledStatus.length > 0,
        `the close must keep its released|retained|unknown contract, got: ${String(settledStatus)}`);
});

test('a malformed 2xx create with a job id is still cancellable by a queued close', async () => {
    // The id names a job the daemon may be running however malformed the rest
    // of the answer is: the queued close must DELETE it, never take the
    // no-job detach shortcut.
    let releaseCreate = () => {};
    const gate = new Promise((resolve) => { releaseCreate = resolve; });
    let deletes = 0;
    const host = interactiveHost();
    const ctl = createLoginCardController({
        host,
        store: null,
        fetchImpl: async (url, init = {}) => {
            if (url === '/api/claudexor/login' && init.method === 'POST') {
                await gate;
                return json(200, { job_id: 'job-malformed' /* no job body */ });
            }
            if (init.method === 'DELETE') {
                deletes += 1;
                return json(200, { job: { state: 'cancelled', outcome: { reason: 'user_cancelled' } } });
            }
            return json(200, { job: { state: 'cancelled' } });
        },
    });
    const starting = ctl.start('codex', '');
    await flush();
    const clicked = host.click('[data-login-dismiss]');
    releaseCreate();
    await starting;
    const status = await clicked;
    assert.equal(deletes, 1, 'the id was adopted before body validation — the job got its DELETE');
    assert.ok(typeof status === 'string' && status.length > 0);
})
