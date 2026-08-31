import test from 'node:test';
import assert from 'node:assert/strict';

import { lifecycleFor } from '../modules/marketplace.js';

// #335: a preflight-failed install must offer Repair in the marketplace
// lifecycle, not a Re-review that deterministically fails the same way.

function installedWithPreflightFail() {
    return {
        name: 'minecraft-widget',
        review_status: 'pending',
        review_stale: false,
        review_gate: { executable_review: false, preflight_failed: true },
        executable_review: false,
        review_findings: [{
            item: 'skill_preflight', verdict: 'FAIL', severity: 'critical',
            reason: JSON.stringify({
                manifest: [{ item: 'manifest_entry_exists', ok: false, detail: 'missing or escaping entry: plugin.py' }],
                files: [], ok: false,
            }),
        }],
        grants: { all_granted: true },
    };
}

test('preflight-failed install offers Repair with the human diagnosis', () => {
    const lifecycle = lifecycleFor({}, installedWithPreflightFail(), null);
    assert.equal(lifecycle.action, 'fix');
    assert.equal(lifecycle.button, 'Repair');
    assert.equal(lifecycle.tone, 'danger');
    assert.equal(lifecycle.label, 'Preflight failed');
    assert.match(lifecycle.hint, /missing or escaping entry: plugin\.py/);
});

test('plain pending install keeps the Review action', () => {
    const installed = installedWithPreflightFail();
    installed.review_gate = { executable_review: false };
    installed.review_findings = [];
    const lifecycle = lifecycleFor({}, installed, null);
    assert.equal(lifecycle.action, 'review');
    assert.equal(lifecycle.button, 'Review');
});
