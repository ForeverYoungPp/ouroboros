import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { apiClient, updateStrategyForPlan } from '../modules/api_client.js';
import { updatePillText, verifiedUpdatePlan } from '../modules/update_status.js';


test('ordinary update selects clean or assisted merge without recovery replacement or client-side stash', () => {
    assert.equal(updateStrategyForPlan({
        available: true,
        kind: 'clean',
        local_dirty_count: 0,
        code_conflict_paths: [],
        doc_conflict_paths: [],
    }), 'auto_merge');
    assert.equal(updateStrategyForPlan({
        available: true,
        kind: 'conflicting',
        recommended_strategy: 'assisted',
        local_dirty_count: 2,
        code_conflict_paths: ['ouroboros/config.py'],
    }), 'assisted');
    assert.equal(updateStrategyForPlan({
        available: true,
        kind: 'clean',
        recommended_strategy: 'assisted',
        local_dirty_count: 0,
        code_conflict_paths: [],
        doc_conflict_paths: [],
    }), 'auto_merge');
    assert.equal(updateStrategyForPlan({ available: true, kind: 'unknown' }), '');
    assert.equal(updateStrategyForPlan({ available: false, kind: 'clean' }), '');

    const source = readFileSync(new URL('../modules/updates.js', import.meta.url), 'utf8');
    const ordinary = source.slice(
        source.indexOf('async function applyUpdate()'),
        source.indexOf('async function replaceWithOfficial()'),
    );
    assert.doesNotMatch(ordinary, /['"]replace['"]/);
    assert.doesNotMatch(ordinary, /updateApply\(['"]stash['"]/);

    // The pill is a pointer, not a second apply surface (owner decision
    // 2026-08-31): it must never call updateApply or preflight itself.
    const pill = readFileSync(new URL('../modules/update_status.js', import.meta.url), 'utf8');
    assert.doesNotMatch(pill, /updateApply\(/);
    assert.doesNotMatch(pill, /updatePreflight\(/);
    assert.match(pill, /openDashboardTab\?\.\('updates'\)/);
});


test('update apply sends exact preflight pins and recovery confirmation only when asked', async () => {
    const originalFetch = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (url, init) => {
        calls.push({ url, init, body: JSON.parse(init.body) });
        return { ok: true, status: 200, json: async () => ({ status: 'ok' }) };
    };
    try {
        const plan = { base_sha: 'base123', target_sha: 'target456' };
        await apiClient.updateApply('auto_merge', plan);
        await apiClient.updateApply('replace', plan, { confirmRecovery: true });
    } finally {
        globalThis.fetch = originalFetch;
    }

    assert.deepEqual(calls[0].body, {
        strategy: 'auto_merge',
        expected_base_sha: 'base123',
        expected_target_sha: 'target456',
    });
    assert.deepEqual(calls[1].body, {
        strategy: 'replace',
        expected_base_sha: 'base123',
        expected_target_sha: 'target456',
        confirm_recovery: true,
    });
});


test('detailed Updates UI makes destructive replacement an explicit recovery action', () => {
    const source = readFileSync(new URL('../modules/updates.js', import.meta.url), 'utf8');
    assert.match(source, />Save recovery point<\/button>/);
    assert.doesNotMatch(source, />Promote to QA<\/button>/);
    assert.doesNotMatch(source, />Promote to Stable<\/button>/);
    assert.match(source, /does not change the official QA feed/);
    assert.match(source, /<summary>Recovery<\/summary>/);
    assert.match(source, /Replace with Official Version \(Recovery\)/);
    assert.match(source, /apiClient\.updateApply\('replace', plan, \{ confirmRecovery: true \}\)/);
    assert.match(source, /updatePreflight\(\)/);
    assert.match(source, /data\.check_ok === false/);
    assert.match(source, /!data\.from_cache.*official_status_requires_check/);
    assert.match(source, /restart_required/);
    assert.match(source, /Rollback completed:.*Restart Ouroboros to finish/s);
    assert.match(source, /Rollback failed:.*Runtime shutdown was incomplete/s);

    const pillSource = readFileSync(new URL('../modules/update_status.js', import.meta.url), 'utf8');
    assert.match(pillSource, /update_status_ready/);
});


test('same-version QA updates show commit identity in the main pill', () => {
    assert.equal(updatePillText({
        current_version: '6.87.6',
        latest_version: '6.87.6',
        current_sha: 'aaaaaaaa00000000',
        latest_sha: 'bbbbbbbb11111111',
    }), 'Update aaaaaaaa → bbbbbbbb');
});


test('main update dialog never invents facts for an unverified preflight', () => {
    assert.equal(verifiedUpdatePlan(null), null);
    assert.equal(verifiedUpdatePlan({ merge_plan: {
        available: true,
        kind: 'unknown',
        base_sha: 'base123',
        target_sha: 'target456',
        local_dirty_count: 0,
    } }), null);
    assert.deepEqual(verifiedUpdatePlan({ merge_plan: {
        available: true,
        kind: 'clean',
        base_sha: 'base123',
        target_sha: 'target456',
        local_dirty_count: 0,
    } }), {
        plan: {
            available: true,
            kind: 'clean',
            base_sha: 'base123',
            target_sha: 'target456',
            local_dirty_count: 0,
        },
        strategy: 'auto_merge',
    });

    // The verification helper still guards the one apply surface: the panel
    // imports it, and an unverified plan never reaches updateApply.
    const panel = readFileSync(new URL('../modules/updates.js', import.meta.url), 'utf8');
    assert.match(panel, /verifiedUpdatePlan\(preflight\)/);
    assert.match(panel, /could not be verified\. No files were changed\./);
});
