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
