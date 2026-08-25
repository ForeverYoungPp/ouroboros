import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    classifyReviewLifecycle,
    createReviewHydrator,
    createReviewPresentationController,
    mergeReviewGroup,
    planReviewGroupFromTaskDetail,
    renderReviewsSection,
    reviewExecutionEvidence,
    reviewGroupFromHistoryRow,
    reviewGroupFromLifecycle,
    reviewGroupsFromTaskDetail,
    taskAcceptanceGroupFromTaskDetail,
} from '../modules/review_presentation.js';
import { getLogTaskGroupId, isGroupedTaskEvent } from '../modules/log_events.js';
import { loadSkillReviewDetail } from '../modules/skill_review_card.js';

const groupedSkillRow = (overrides = {}) => ({
    system_type: 'skill_review',
    skill: 'alpha',
    status: 'clean',
    job_id: 'job-2',
    task_id: 'initiator-child',
    review_group: {
        surface: 'skill',
        id: 'task:root:alpha',
        presentation_owner_task_id: 'root',
        projected_attempt_count: 2,
        count_is_authoritative: false,
        attempts: [
            { job_id: 'job-1', skill: 'alpha', status: 'blockers', review_round: 1, snapshot_attempt: 1 },
            { job_id: 'job-2', skill: 'alpha', status: 'clean', review_round: 2, snapshot_attempt: 1 },
        ],
        ...overrides,
    },
});

test('task-bound Skill projection uses only the explicit presentation owner', () => {
    const group = reviewGroupFromHistoryRow(groupedSkillRow());
    assert.equal(group.presentationOwnerTaskId, 'root');
    assert.equal(group.subjectTaskId, '');
    assert.equal(group.initiatorTaskId, 'initiator-child');
    assert.deepEqual(group.attempts.map((attempt) => attempt.id), ['job-1', 'job-2']);
    assert.equal(group.attemptCount, 2);
    assert.equal(group.countIsAuthoritative, false);

    assert.equal(reviewGroupFromHistoryRow(groupedSkillRow({ presentation_owner_task_id: '' })), null);
    assert.equal(reviewGroupFromHistoryRow({
        ...groupedSkillRow(),
        review_group: { ...groupedSkillRow().review_group, presentation_owner_task_id: undefined },
    }), null);
});

test('Skill subject stays absent unless the canonical source provides it', () => {
    assert.equal(reviewGroupFromHistoryRow(groupedSkillRow()).subjectTaskId, '');
    assert.equal(reviewGroupFromHistoryRow(groupedSkillRow({ subject_task_id: 'child' })).subjectTaskId, 'child');
    assert.equal(reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-1',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } }).subjectTaskId, '');
});

test('typed review lifecycle distinguishes source-incomplete rows from unrelated lifecycle', () => {
    assert.equal(classifyReviewLifecycle({ lifecycle: { kind: 'install' } }).classification, 'not_review');
    assert.equal(classifyReviewLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'manual-1',
    } }).classification, 'source_incomplete');
    assert.equal(classifyReviewLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-1',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } }).classification, 'source_complete');
});

test('live lifecycle ignores its synthetic outer task id and updates the same group', () => {
    const live = reviewGroupFromLifecycle({
        task_id: 'skill_lifecycle_review_alpha_job-2',
        progress_meta: { lifecycle: {
            kind: 'review', status: 'running', target: 'alpha', id: 'job-2',
            group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
            initiator_task_id: 'initiator-child', snapshot_revised: true,
            replayed_from_ts: '2026-08-24T00:00:00Z',
        } },
    });
    assert.equal(live.presentationOwnerTaskId, 'root');
    assert.equal(live.initiatorTaskId, 'initiator-child');
    assert.equal(live.activeCount, 1);
    assert.equal(live.attempts[0].id, 'job-2');
    assert.equal(live.attempts[0].revised, true);
    assert.equal(live.attempts[0].replayed, true);
});

test('lifecycle provenance ignores a synthetic outer task id but preserves an explicit origin', () => {
    const lifecycle = {
        kind: 'review', status: 'running', target: 'alpha', id: 'job-2',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    };
    assert.equal(reviewGroupFromLifecycle({
        task_id: 'skill_lifecycle_review_alpha_job-2', lifecycle,
    }).initiatorTaskId, '');
    assert.equal(reviewGroupFromLifecycle({
        task_id: 'skill_lifecycle_review_alpha_job-2', origin_task_id: 'real-initiator', lifecycle,
    }).initiatorTaskId, 'real-initiator');
});

test('Logs groups review lifecycle only under its explicit presentation owner', () => {
    const incomplete = {
        type: 'task_progress', is_progress: true, task_id: 'skill_lifecycle_review_alpha_manual',
        lifecycle: { kind: 'review', status: 'running', target: 'alpha', job_id: 'manual' },
    };
    assert.equal(getLogTaskGroupId(incomplete), '');
    assert.equal(isGroupedTaskEvent(incomplete), false);

    const complete = {
        ...incomplete,
        lifecycle: {
            ...incomplete.lifecycle,
            group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
        },
    };
    assert.equal(getLogTaskGroupId(complete), 'root');
    assert.equal(isGroupedTaskEvent(complete), true);

    const ordinary = { type: 'task_progress', is_progress: true, task_id: 'ordinary-task' };
    assert.equal(getLogTaskGroupId(ordinary), 'ordinary-task');
    assert.equal(isGroupedTaskEvent(ordinary), true);
});

test('group reducer keeps one row and ordered projected attempts across live to terminal', () => {
    const store = new Map();
    const live = reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-2',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } });
    mergeReviewGroup(store, live);
    mergeReviewGroup(store, reviewGroupFromHistoryRow(groupedSkillRow()));
    assert.equal(store.size, 1);
    const settled = store.get('task:root:alpha');
    assert.deepEqual(settled.attempts.map((attempt) => attempt.id), ['job-1', 'job-2']);
    assert.equal(settled.activeCount, 0);
    assert.equal(settled.state, 'terminal');
});

test('terminal attempt state is monotonic while a genuinely new attempt restores liveness', () => {
    const store = new Map();
    mergeReviewGroup(store, reviewGroupFromHistoryRow(groupedSkillRow()));

    mergeReviewGroup(store, reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-2',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } }));
    const stale = store.get('task:root:alpha');
    assert.equal(stale.state, 'terminal');
    assert.equal(stale.activeCount, 0);
    assert.equal(stale.attempts.find((attempt) => attempt.id === 'job-2')?.state, 'terminal');

    mergeReviewGroup(store, reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-3',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } }));
    const next = store.get('task:root:alpha');
    assert.equal(next.state, 'running');
    assert.equal(next.activeCount, 1);
    assert.equal(next.attempts.find((attempt) => attempt.id === 'job-3')?.state, 'running');
});

test('an unmatched open Plan attempt restores liveness before its first wave lands', () => {
    const settledFingerprint = 'a'.repeat(64);
    const nextFingerprint = 'b'.repeat(64);
    const wave = {
        request_fingerprint: settledFingerprint,
        cycle_index: 1,
        aggregate: 'GREEN',
        closed: true,
    };
    const store = new Map();
    mergeReviewGroup(store, planReviewGroupFromTaskDetail({
        task_id: 'root',
        plan_review_state: {
            current_attempt: { fingerprint: settledFingerprint, status: 'closed' },
            waves: [wave],
        },
    }));

    mergeReviewGroup(store, planReviewGroupFromTaskDetail({
        task_id: 'root',
        plan_review_state: {
            current_attempt: { fingerprint: nextFingerprint, status: 'open' },
            waves: [wave],
        },
    }));

    const next = store.get('plan:root');
    assert.equal(next.state, 'running');
    assert.equal(next.activeCount, 1);
    assert.equal(next.verdict, 'open');
    assert.equal(next.tone, 'working');
});

test('plan review retains current and superseded waves without inventing authority', () => {
    const detail = {
        task_id: 'root',
        plan_review_state: {
            current_attempt: { fingerprint: 'new', status: 'open', reason: '' },
            waves_omitted: 0,
            waves: [
                { request_fingerprint: 'old', cycle_index: 1, aggregate: 'GREEN', closed: true },
                { request_fingerprint: 'new', cycle_index: 2, aggregate: 'REVIEW_REQUIRED', closed: false },
                { request_fingerprint: 'cached', cycle_index: 3, aggregate: 'GREEN', closed: true },
            ],
        },
    };
    const group = planReviewGroupFromTaskDetail(detail);
    assert.equal(group.presentationOwnerTaskId, 'root');
    assert.equal(group.state, 'terminal');
    assert.equal(group.activeCount, 0);
    assert.equal(group.attempts[0].superseded, true);
    assert.equal(group.attempts[1].superseded, false);
    assert.equal(group.attempts[1].verdict, 'REVIEW_REQUIRED');
    assert.equal(group.attempts[2].superseded, true);
    assert.equal(group.verdict, 'REVIEW_REQUIRED');
    assert.equal(group.countIsAuthoritative, true);

    delete detail.plan_review_state.waves_omitted;
    assert.equal(planReviewGroupFromTaskDetail(detail).countIsAuthoritative, false);
});

test('plan liveness comes only from an unmatched open current attempt', () => {
    const fingerprint = 'a'.repeat(64);
    const old = 'b'.repeat(64);
    const open = planReviewGroupFromTaskDetail({
        task_id: 'root',
        plan_review_state: {
            current_attempt: { fingerprint, status: 'open' },
            waves: [{ request_fingerprint: old, aggregate: 'GREEN', closed: true }],
        },
    });
    assert.equal(open.state, 'running');
    assert.equal(open.activeCount, 1);
    assert.equal(open.verdict, 'open');
    assert.equal(open.tone, 'working');
    assert.equal(open.attempts[0].superseded, true);

    for (const [status, expectedState] of [
        ['unavailable', 'unavailable'],
        ['rail_degraded', 'terminal'],
        ['cycles_exhausted', 'terminal'],
    ]) {
        const group = planReviewGroupFromTaskDetail({
            task_id: 'root',
            plan_review_state: { current_attempt: { fingerprint, status }, waves: [] },
        });
        assert.equal(group.state, expectedState);
        assert.equal(group.activeCount, 0);
        assert.equal(group.verdict, status);
    }
});

test('review tones use an explicit success allowlist', () => {
    const states = [
        ['PASS', 'done'], ['GREEN', 'done'],
        ['REVIEW_REQUIRED', 'warn'], ['REVISE_PLAN', 'warn'], ['DEGRADED', 'warn'],
        ['UNKNOWN', 'neutral'], ['transport_error', 'neutral'], ['timeout', 'error'],
    ];
    for (const [status, tone] of states) {
        const group = reviewGroupFromHistoryRow(groupedSkillRow({ status, verdict: status }));
        assert.equal(group.tone, tone, status || '(no verdict)');
    }
    const pending = reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'pending', target: 'alpha', job_id: 'job-pending',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } });
    assert.equal(pending.state, 'queued');
    assert.equal(pending.activeCount, 1);
});

test('task acceptance adapts only task_acceptance panels; advisory and commit stay omitted', () => {
    const detail = {
        task_id: 'root',
        review_projection: { panels: [
            { panel_id: 'accept', surface: 'task_acceptance', aggregate_signal: 'PASS', actors: [] },
            { panel_id: 'commit', surface: 'commit', aggregate_signal: 'PASS', actors: [] },
            { panel_id: 'advisory', surface: 'advisory', aggregate_signal: 'PASS', actors: [] },
        ] },
    };
    const group = taskAcceptanceGroupFromTaskDetail(detail);
    assert.deepEqual(group.attempts.map((attempt) => attempt.id), ['accept']);
    assert.deepEqual(reviewGroupsFromTaskDetail(detail).map((item) => item.surface), ['task_acceptance']);
});

test('renderer is quiet, accessible and never invents review dollars', () => {
    const group = reviewGroupFromHistoryRow(groupedSkillRow());
    const html = renderReviewsSection([group], {
        sectionExpanded: true,
        expandedGroups: new Set([group.id]),
        expandedAttempts: new Set([`${group.id}:job-1`]),
    });
    assert.match(html, /<section class="chat-live-reviews"/);
    assert.match(html, /data-review-section-toggle aria-expanded="true"/);
    assert.match(html, /data-review-group-toggle="task:root:alpha" aria-expanded="true"/);
    assert.match(html, /data-skill-review-job="job-1"/);
    assert.match(html, /Initiated by task initiator-child/);
    assert.match(html, /data-review-attempt-detail="task:root:alpha:job-1"[^>]*aria-busy="false"/);
    assert.match(html, /2 shown/);
    assert.doesNotMatch(html, /\$\d|cost=/i);
});

test('attempt marks require an explicit executed receipt and keep intent separate', () => {
    const executed = {
        executed: { kind: 'agent_session', harness: 'claude', model: 'claude-fable-5' },
        requested: { kind: 'agent_session', harness: 'cursor' },
    };
    assert.deepEqual(reviewExecutionEvidence(executed), {
        harness: 'claude', channel: '', label: '', model: 'claude-fable-5',
    });
    assert.equal(reviewExecutionEvidence({ requested: { harness: 'claude' } }), null);

    const group = reviewGroupFromHistoryRow(groupedSkillRow({
        attempts: [{
            job_id: 'job-executed', skill: 'alpha', status: 'clean', execution: executed,
        }, {
            job_id: 'job-requested', skill: 'alpha', status: 'clean',
            execution: { requested: { kind: 'agent_session', harness: 'cursor' } },
        }],
    }));
    const html = renderReviewsSection([group], {
        sectionExpanded: true,
        expandedGroups: new Set([group.id]),
    });
    assert.match(html, /data-harness-identity="claude"/);
    assert.match(html, /Claude Code/);
    assert.match(html, /claude-fable-5/);
    assert.doesNotMatch(html, /data-harness-identity="cursor"/);

    const api = reviewExecutionEvidence({ executed: { kind: 'api_chat', model: 'openai\/gpt' } });
    assert.deepEqual(api, { harness: 'api', channel: 'api', label: '', model: 'openai\/gpt' });
});

test('initiator detail is omitted when it is the owner', () => {
    const row = groupedSkillRow({ initiator_task_id: 'root' });
    assert.doesNotMatch(renderReviewsSection([reviewGroupFromHistoryRow(row)], {
        sectionExpanded: true,
        expandedGroups: new Set(['task:root:alpha']),
    }), /Initiated by task/);
});

test('review updates never change owner disclosure state', () => {
    const host = { innerHTML: '', addEventListener() {} };
    const summary = { hidden: true, textContent: '' };
    const disclosure = { sectionExpanded: false, expandedGroups: new Set(), expandedAttempts: new Set() };
    const controller = createReviewPresentationController({ host, summary, disclosure });
    controller.update(reviewGroupFromLifecycle({ lifecycle: {
        kind: 'review', status: 'running', target: 'alpha', job_id: 'job-1',
        group_id: 'task:root:alpha', presentation_owner_task_id: 'root',
    } }));
    controller.update(reviewGroupFromHistoryRow(groupedSkillRow()));
    assert.equal(disclosure.sectionExpanded, false);
    assert.deepEqual([...disclosure.expandedGroups], []);
    assert.deepEqual([...disclosure.expandedAttempts], []);
});

test('an open exact Skill detail survives a review re-render while its read is in flight', async () => {
    const detailStore = new Map();
    const details = [];
    const loads = [];
    let resolveFetch;
    let fetches = 0;
    const fetchGate = new Promise((resolve) => { resolveFetch = resolve; });
    const host = {
        ownerDocument: { activeElement: null },
        addEventListener() {},
        contains: () => false,
        querySelector: () => null,
        querySelectorAll(selector) {
            return selector === '[data-review-attempt-detail]' && details.length
                ? [details.at(-1)] : [];
        },
        set innerHTML(value) {
            this._html = value;
            if (!value.includes('data-skill-review-job="job-1"')) return;
            details.push({
                dataset: {
                    reviewAttemptDetail: 'task:root:alpha:job-1',
                    skillReviewSkill: 'alpha',
                    skillReviewJob: 'job-1',
                },
                hidden: false,
                innerHTML: '',
                attrs: {},
                setAttribute(key, next) { this.attrs[key] = next; },
            });
        },
        get innerHTML() { return this._html || ''; },
    };
    const disclosure = {
        sectionExpanded: true,
        expandedGroups: new Set(['task:root:alpha']),
        expandedAttempts: new Set(['task:root:alpha:job-1']),
    };
    const controller = createReviewPresentationController({
        host,
        summary: { hidden: true, textContent: '' },
        disclosure,
        onLoadSkillDetail(detail) {
            loads.push(loadSkillReviewDetail(detail, {
                skill: detail.dataset.skillReviewSkill,
                jobId: detail.dataset.skillReviewJob,
            }, {
                store: detailStore,
                fetchImpl: async () => {
                    fetches += 1;
                    await fetchGate;
                    return { ok: true, json: async () => ({ markdown: 'exact detail' }) };
                },
                render: (markdown) => markdown,
            }));
        },
    });
    controller.update(reviewGroupFromHistoryRow(groupedSkillRow()));
    await Promise.resolve();
    const firstDetail = details.at(-1);
    controller.update(reviewGroupFromHistoryRow(groupedSkillRow({ status: 'warnings' })));
    const rebuiltDetail = details.at(-1);
    assert.notEqual(rebuiltDetail, firstDetail);
    assert.equal(fetches, 1);
    assert.equal(rebuiltDetail.dataset.state, 'loading');
    resolveFetch();
    await Promise.all(loads);
    assert.equal(rebuiltDetail.dataset.state, 'loaded');
    assert.match(rebuiltDetail.innerHTML, /exact detail/);
});

test('typed invalidations dedupe revisions and guarantee one trailing refresh', async () => {
    const reads = [];
    const applied = [];
    const deferred = () => {
        let resolve;
        const promise = new Promise((done) => { resolve = done; });
        return { promise, resolve };
    };
    const hydrator = createReviewHydrator({
        fetchDetail(taskId) {
            const gate = deferred();
            reads.push({ taskId, gate });
            return gate.promise;
        },
        applyDetail(_taskId, detail) {
            applied.push(detail.revision);
            return true;
        },
    });

    const firstRevision = 'a'.repeat(64);
    const secondRevision = 'b'.repeat(64);
    const first = hydrator.hydrate('root', firstRevision);
    await Promise.resolve();
    const duplicate = hydrator.hydrate('root', firstRevision);
    const newer = hydrator.hydrate('root', secondRevision);
    const duplicatePending = hydrator.hydrate('root', secondRevision);
    assert.equal(reads.length, 1);
    reads[0].gate.resolve({ revision: firstRevision });
    await first;
    await Promise.resolve();
    assert.equal(reads.length, 2);
    reads[1].gate.resolve({ revision: secondRevision });
    await newer;
    await duplicatePending;
    await duplicate;
    assert.deepEqual(applied, [firstRevision, secondRevision]);
    assert.equal(await hydrator.hydrate('root', secondRevision), false);
    assert.equal(reads.length, 2);
});

test('applied-revision invalidation preserves and joins an in-flight physical read', async () => {
    let resolveRead;
    let reads = 0;
    const revision = 'c'.repeat(64);
    const hydrator = createReviewHydrator({
        fetchDetail() {
            reads += 1;
            return new Promise((resolve) => { resolveRead = resolve; });
        },
        applyDetail: () => true,
    });

    const first = hydrator.hydrate('root', revision);
    await Promise.resolve();
    hydrator.invalidateApplied();
    const joined = hydrator.hydrate('root', revision);
    assert.equal(reads, 1, 'presentation reset did not duplicate the in-flight GET');
    resolveRead({ revision });
    await Promise.all([first, joined]);
    assert.equal(await hydrator.hydrate('root', revision), false,
        'the joined read restored the applied revision receipt');
    assert.equal(reads, 1);
});

test('invalid review revisions stay opaque and do not become ordered counters', async () => {
    let reads = 0;
    const hydrator = createReviewHydrator({
        fetchDetail: async () => ({ ok: true }),
        applyDetail: () => { reads += 1; return true; },
    });
    await hydrator.hydrate('root', 42);
    await hydrator.hydrate('root', 42);
    assert.equal(reads, 2);
});

test('review re-render restores keyboard focus to the equivalent disclosure control', () => {
    const doc = { activeElement: null };
    const buttons = new Map();
    let clickHandler = null;
    const makeButton = (kind, key = '') => ({
        dataset: kind === 'group' ? { reviewGroupToggle: key } : {},
        matches: (selector) => kind === 'section' && selector === '[data-review-section-toggle]',
        closest(selector) {
            return kind === 'group' && selector === '[data-review-group-toggle]' ? this : null;
        },
        focus() { doc.activeElement = this; },
    });
    const host = {
        ownerDocument: doc,
        addEventListener(type, handler) { if (type === 'click') clickHandler = handler; },
        contains: (candidate) => [...buttons.values()].includes(candidate),
        querySelector: (selector) => selector === '[data-review-section-toggle]' ? buttons.get('section') : null,
        querySelectorAll: (selector) => selector === '[data-review-group-toggle]' ? [buttons.get('group')] : [],
        set innerHTML(value) {
            this._html = value;
            buttons.set('section', makeButton('section'));
            buttons.set('group', makeButton('group', 'task:root:alpha'));
        },
        get innerHTML() { return this._html || ''; },
    };
    const summary = { hidden: true, textContent: '' };
    const disclosure = { sectionExpanded: false, expandedGroups: new Set(), expandedAttempts: new Set() };
    const controller = createReviewPresentationController({ host, summary, disclosure });
    controller.update(reviewGroupFromHistoryRow(groupedSkillRow()));
    const oldGroupButton = buttons.get('group');
    doc.activeElement = oldGroupButton;
    clickHandler({ target: oldGroupButton });
    assert.equal(disclosure.expandedGroups.has('task:root:alpha'), true);
    assert.equal(doc.activeElement, buttons.get('group'));
    assert.notEqual(doc.activeElement, oldGroupButton);
});

test('Retry keeps keyboard focus on the live detail status while refetching', () => {
    const doc = { activeElement: null };
    let clickHandler = null;
    let retryOptions = null;
    const detail = {
        dataset: { skillReviewSkill: 'alpha', skillReviewJob: 'job-1' },
        setAttribute(key, value) { this[key] = value; },
        focus() { doc.activeElement = this; },
    };
    const retry = { closest: (selector) => selector === '[data-review-attempt-detail]' ? detail : null };
    const host = {
        ownerDocument: doc,
        addEventListener(type, handler) { if (type === 'click') clickHandler = handler; },
        contains: () => true,
        querySelectorAll: () => [],
    };
    createReviewPresentationController({
        host,
        summary: { hidden: true, textContent: '' },
        disclosure: {},
        onLoadSkillDetail(_detail, options) { retryOptions = options; },
    });
    clickHandler({ target: { closest: (selector) => selector === '[data-skill-review-retry]' ? retry : null } });
    assert.deepEqual(retryOptions, { retry: true });
    assert.equal(doc.activeElement, detail);
    assert.equal(detail.tabindex, '-1');
});

test('generic expandByDefault plumbing is gone from Chat and log projection', () => {
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const logs = readFileSync(new URL('../modules/log_events.js', import.meta.url), 'utf8');
    assert.doesNotMatch(chat, /expandByDefault/);
    assert.doesNotMatch(logs, /expandByDefault/);
    assert.doesNotMatch(chat, /stickyExpandedSlots/);
});

test('history and live chat intercept owner-bound lifecycle before generic progress', () => {
    const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
    const pass1 = chat.slice(chat.indexOf('// First pass builds'), chat.indexOf('// Pass 2 inserts cards'));
    assert.ok(pass1.indexOf('attachReviewFromRow(msg') >= 0);
    assert.ok(pass1.indexOf('attachReviewFromRow(msg') < pass1.indexOf('if (msg.is_progress)'));

    const liveStart = chat.indexOf("if (msg.role === 'assistant' || msg.role === 'system')");
    const live = chat.slice(liveStart, chat.indexOf("onWs('message_annotation'", liveStart));
    assert.ok(live.indexOf('attachReviewFromRow(msg') >= 0);
    assert.ok(live.indexOf('attachReviewFromRow(msg') < live.indexOf('if (msg.is_progress)'));
});
