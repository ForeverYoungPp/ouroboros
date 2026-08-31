import assert from 'node:assert/strict';
import test from 'node:test';

import { renderInstalledSkillCard } from '../modules/skill_card_renderer.js';
import { preflightFailed, preflightFindingText, topReviewFinding } from '../modules/utils.js';

// #335: a deterministic preflight FAIL persists as review_status=pending.
// Re-review would deterministically fail again — the card must offer Repair.

const PREFLIGHT_REASON = JSON.stringify({
    skill: 'minecraft-widget',
    manifest: [
        { item: 'manifest_present', ok: true },
        { item: 'manifest_entry_exists', ok: false, detail: 'missing or escaping entry: plugin.py' },
    ],
    files: [],
    ok: false,
});

function preflightSkill(overrides = {}) {
    return {
        name: 'minecraft-widget',
        type: 'extension',
        version: '1.0.0',
        enabled: false,
        source: 'external',
        payload_root: 'skills/external/minecraft-widget',
        review_status: 'pending',
        review_stale: false,
        review_gate: { executable_review: false, preflight_failed: true, blocking_reason: 'review_pending' },
        executable_review: false,
        review_findings: [{
            item: 'skill_preflight', verdict: 'FAIL', severity: 'critical',
            reason: PREFLIGHT_REASON, model: 'deterministic_preflight',
        }],
        grants: { all_granted: true, missing_keys: [], missing_permissions: [] },
        permissions: [],
        ...overrides,
    };
}

test('preflight-failed external skill offers Repair as the primary action', () => {
    const html = renderInstalledSkillCard(preflightSkill());
    assert.match(html, /data-skill-action="repair"[^>]*>Repair</);
    assert.doesNotMatch(html, />Re-review</);
});

test('preflight-failed self-authored skill offers Repair too', () => {
    const html = renderInstalledSkillCard(preflightSkill({
        source: 'self_authored',
        is_self_authored: true,
    }));
    assert.match(html, /data-skill-action="repair"[^>]*>Repair</);
});

test('plain pending without a preflight failure keeps the Review CTA', () => {
    const html = renderInstalledSkillCard(preflightSkill({
        review_gate: { executable_review: false, blocking_reason: 'review_pending' },
        review_findings: [],
    }));
    assert.match(html, />Review</);
    assert.doesNotMatch(html, /data-skill-action="repair"[^>]*>Repair</);
});

test('a gate without the preflight key is never treated as a failure', () => {
    // Absence propagation: older backends do not send preflight_failed.
    assert.equal(preflightFailed({ review_gate: { executable_review: false } }), false);
    assert.equal(preflightFailed({}), false);
    assert.equal(preflightFailed({ review_gate: { preflight_failed: true } }), true);
});

test('Skip review is hidden when the preflight failed (it would 409)', () => {
    const html = renderInstalledSkillCard(preflightSkill({
        source: 'self_authored',
        is_self_authored: true,
    }));
    assert.doesNotMatch(html, /Skip review/);
    // ...and stays offered for the same skill without the preflight failure.
    const clean = renderInstalledSkillCard(preflightSkill({
        source: 'self_authored',
        is_self_authored: true,
        review_gate: { executable_review: false, blocking_reason: 'review_pending' },
        review_findings: [],
    }));
    assert.match(clean, /Skip review/);
});

test('the preflight finding renders a human-readable diagnosis, not raw JSON', () => {
    const html = renderInstalledSkillCard(preflightSkill());
    assert.match(html, /missing or escaping entry: plugin\.py/);
    assert.doesNotMatch(html, /manifest_present/); // passing rows are not noise

    const text = preflightFindingText({
        item: 'skill_preflight', verdict: 'FAIL', reason: PREFLIGHT_REASON,
    });
    assert.match(text, /^Preflight failed — manifest_entry_exists: missing or escaping entry: plugin\.py$/);

    // topReviewFinding (the marketplace hint) uses the same diagnosis.
    assert.match(topReviewFinding(preflightSkill()), /missing or escaping entry/);
});

test('an unparseable preflight reason falls back to the raw rendering', () => {
    assert.equal(preflightFindingText({ item: 'skill_preflight', reason: 'not json' }), '');
    assert.equal(preflightFindingText({ item: 'other', reason: PREFLIGHT_REASON }), '');
    const html = renderInstalledSkillCard(preflightSkill({
        review_findings: [{ item: 'skill_preflight', verdict: 'FAIL', reason: 'exploded' }],
    }));
    assert.match(html, /exploded/);
});

test('non-repairable native source keeps the Review CTA on a preflight failure', () => {
    const html = renderInstalledSkillCard(preflightSkill({
        source: 'native',
        payload_root: '',
    }));
    assert.match(html, />Review</);
    assert.doesNotMatch(html, /data-skill-action="repair"[^>]*>Repair</);
});

test('the status chip names the preflight failure instead of generic Needs review', () => {
    const html = renderInstalledSkillCard(preflightSkill());
    assert.match(html, />Preflight failed</);
    assert.doesNotMatch(html, />Needs review</);
});

test('the diagnosis covers widget, permission, and presence failures too', () => {
    const reason = JSON.stringify({
        manifest: [{ item: 'manifest_present', ok: true }],
        widgets: [{ item: 'widget_schema', ok: false, detail: 'bad schema: x' }],
        permissions: [{ item: 'permission_static', ok: false, detail: 'register_tool without tool permission' }],
        presence: [{ item: 'presence_profile', ok: false, detail: 'unknown slot' }],
        files: [],
        ok: false,
    });
    const text = preflightFindingText({ item: 'skill_preflight', verdict: 'FAIL', reason });
    assert.match(text, /widget_schema: bad schema: x/);
    assert.match(text, /permission_static: register_tool without tool permission/);
    // Three parts shown; the cap keeps the line readable.
    assert.match(text, /^Preflight failed — /);
});

test('a stale review falls back to Re-review with Skip review available', () => {
    // The backend gate reports preflight_failed=false for a stale state (the
    // persisted failure belongs to the previous payload bytes) — the card
    // must offer the cheap Re-review and re-surface owner attestation.
    const html = renderInstalledSkillCard(preflightSkill({
        source: 'self_authored',
        is_self_authored: true,
        review_stale: true,
        review_gate: { executable_review: false, preflight_failed: false, stale: true },
    }));
    assert.match(html, />Re-review</);
    assert.doesNotMatch(html, /data-skill-action="repair"[^>]*>Repair</);
    assert.match(html, /Skip review/);
});

test('self-authored instruction skill offers Make runnable in the menu', () => {
    const html = renderInstalledSkillCard(preflightSkill({
        type: 'instruction',
        source: 'self_authored',
        is_self_authored: true,
        review_gate: { executable_review: false },
        review_findings: [],
    }));
    assert.match(html, /Make runnable/);
});

test('menu Review is hidden on a fresh repairable preflight failure (marketplace parity)', () => {
    const html = renderInstalledSkillCard(preflightSkill());
    assert.doesNotMatch(html, /skills-menu-item skills-review"/);
    // Non-repairable native keeps its Review affordance.
    const native = renderInstalledSkillCard(preflightSkill({ source: 'native', payload_root: '' }));
    assert.match(native, /skills-menu-item skills-review"/);
});
