import assert from 'node:assert/strict';
import test from 'node:test';

import {
    executorChip,
    formatReviewProjection,
    summarizeChatLiveEvent,
    taskOutcomeSeverity,
} from '../modules/log_events.js';

test('shared task severity never paints degraded or best-effort outcomes green', () => {
    const clean = {
        outcome_axes: {
            lifecycle: { status: 'completed' }, execution: { status: 'ok' },
            objective: { status: 'pass' }, review: { status: 'pass' },
            artifacts: { status: 'ready' },
        },
    };
    assert.equal(taskOutcomeSeverity(clean), 'done');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'best_effort' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, objective: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'degraded' } },
    }), 'warn');
    assert.equal(taskOutcomeSeverity({
        ...clean, outcome_axes: { ...clean.outcome_axes, review: { status: 'fail' } },
    }), 'error');
    assert.equal(taskOutcomeSeverity({
        ...clean,
        outcome_axes: {
            ...clean.outcome_axes,
            execution: { status: 'degraded', reason_code: 'review_skipped_deadline_reserve' },
            objective: { status: 'degraded', source: 'task_acceptance_deadline_reserve' },
            review: {
                status: 'skipped', eligibility: 'eligible', run_count: 0,
                // v6.78.0: canonical host status + typed reason (the reason literal
                // is what execution.reason_code / objective.source key off).
                acceptance_decision: {
                    status: 'finalized_unaccepted',
                    reason: 'review_skipped_deadline_reserve',
                },
            },
        },
    }), 'warn');
});

test('review projection exposes panel binding and every actor without raw output', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_abc', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'DEGRADED', transport_status: 'partial', parse_status: 'malformed',
        quorum: { required: 2, contributed: 1, configured: 3 },
        enforcement_impact: 'degrades_completion', reason: 'one actor malformed',
        candidate_hash: 'candidate', evidence_revision: 'evidence', fence_hash: 'fence-hash',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'DEGRADED',
                outcome_tier: 'best_effort',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'uncertain',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'timeout',
                parse_status: 'malformed', semantic_verdict: '',
                quorum_contribution: false, enforcement_impact: 'abstains', reason: 'timeout',
            },
        ],
    }] });
    assert.match(text, /panel_abc/);
    assert.match(text, /candidate_hash=candidate/);
    assert.match(text, /fence_hash=fence-hash/);
    assert.match(text, /slot_1.*claude-fable-5.*verdict=DEGRADED/);
    assert.match(text, /slot_1.*outcome_tier=best_effort/);
    assert.match(text, /slot_2.*transport=timeout.*parse=malformed/);
    assert.doesNotMatch(text, /raw_text/);
});

test('mixed panel renders quorum participation separately from pass support and veto', () => {
    const text = formatReviewProjection({ panels: [{
        panel_id: 'panel_mixed', surface: 'task_acceptance', authority: 'host_root',
        aggregate_signal: 'FAIL', transport_status: 'success', parse_status: 'valid',
        quorum: { required: 2, contributed: 3, configured: 3 },
        enforcement_impact: 'requires_revision', reason: 'one valid reviewer vetoed',
        actors: [
            {
                slot_id: 'slot_1', actor_role: 'task acceptance', provider: 'anthropic',
                model: 'anthropic/claude-fable-5', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_2', actor_role: 'task acceptance', provider: 'openai',
                model: 'openai/gpt-5.6-sol', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'PASS',
                quorum_contribution: true, enforcement_impact: 'supports_pass', reason: 'ready',
            },
            {
                slot_id: 'slot_3', actor_role: 'task acceptance', provider: 'google',
                model: 'google/gemini-3.5-flash', transport_status: 'success',
                parse_status: 'valid', semantic_verdict: 'FAIL',
                quorum_contribution: true, enforcement_impact: 'veto', reason: 'gap',
            },
        ],
    }] });
    assert.match(text, /panel_mixed.*verdict=FAIL.*quorum=3\/3 \(required 2\)/);
    assert.match(text, /slot_1.*verdict=PASS.*quorum=contributes.*enforcement=supports_pass/);
    assert.match(text, /slot_3.*verdict=FAIL.*quorum=contributes.*enforcement=veto/);
});

test('subagent terminal projection keeps review complete without using it as activity', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_role: 'reviewer',
        subagent_event: 'completed',
        result: 'Concrete child result',
        write_surface: 'none',
        status: 'completed',
        outcome_axes: {
            lifecycle: { status: 'completed' },
            execution: { status: 'ok' },
            objective: { status: 'best_effort' },
            review: { status: 'degraded' },
            artifacts: { status: 'ready' },
        },
        review_projection: { panels: [{
            panel_id: 'panel_child', surface: 'task_acceptance', authority: 'host_root',
            aggregate_signal: 'DEGRADED', transport_status: 'success', parse_status: 'valid',
            quorum: { required: 1, contributed: 1, configured: 1 },
            enforcement_impact: 'degrades_completion', reason: 'needs owner context',
            actors: [],
        }] },
    });
    assert.equal(summary.terminal, true);
    assert.equal(summary.phase, 'warn');
    assert.equal(summary.expandByDefault, true);
    assert.equal(summary.activityPreview, 'Concrete child result');
    assert.match(summary.body, /panel_child/);
    assert.match(summary.fullBody, /^\[REVIEW\][\s\S]*panel_child/);
    assert.match(summary.fullBody, /\[RESULT\]\nConcrete child result/);
    assert.deepEqual(summary.meta, ['write=none', 'status=completed']);
});

test('terminal subagent activity prefers the authoritative result over emitter boilerplate', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_event: 'completed',
        content: 'Subagent child completed (researcher).',
        result: 'The evidence-backed conclusion and its concrete next step.',
        status: 'completed',
    });
    assert.equal(summary.activityPreview, 'The evidence-backed conclusion and its concrete next step.');
    assert.match(summary.fullBody, /Subagent child completed/);
    assert.match(summary.fullBody, /\[RESULT\]\nThe evidence-backed conclusion/);
});

test('review-only subagent projection explicitly has no collapsed activity', () => {
    const summary = summarizeChatLiveEvent({
        type: 'send_message',
        is_progress: true,
        delegation_role: 'subagent',
        parent_task_id: 'parent',
        subagent_task_id: 'child',
        subagent_event: 'completed',
        review_projection: { panels: [{
            panel_id: 'panel_review_only', surface: 'task_acceptance', authority: 'host_root',
            aggregate_signal: 'PASS', transport_status: 'success', parse_status: 'valid',
            quorum: { required: 1, contributed: 1, configured: 1 },
            enforcement_impact: 'supports_pass', reason: 'review only', actors: [],
        }] },
    });
    assert.equal(summary.activityPreview, '');
    assert.match(summary.body, /panel_review_only/);
    assert.equal(summary.expandByDefault, true);
});

// ---------------------------------------------------------------------------
// Phase 6, owner directive #1: the per-bubble / per-subagent executor chip.
// «бейдж точно нужен, но не рекламный … что ТУТ бабл \ субагент на codex»
// ---------------------------------------------------------------------------

test('a delegated frame yields a small harness chip; an ordinary frame yields none', () => {
    const chip = executorChip({ executor_route: 'codex' });
    assert.equal(chip.harness, 'codex');
    // ONE honest label — `{harness} · {state}` — never a bare product name: a
    // hover-only caveat is invisible on touch, to AT, and in copies, so the
    // run-state qualifier lives in the label itself.
    assert.equal(chip.label, 'codex · no run yet');
    assert.ok(chip.icon);                        // icon, Claudexor-style
    assert.match(chip.title, /codex/);
    // WHERE it ran is all the chip can see. It has no access to the run's spend, so
    // it must not describe the billing: "on your codex subscription" told the owner
    // the work was covered, which the ledger alone can say (and only when the harness
    // says it — a zero there may mean unknown pricing, never "free").
    assert.ok(!/subscription/i.test(chip.title), chip.title);
    // The opaque `harness=model` spelling prints the HARNESS part only.
    assert.equal(executorChip({ executor_route: 'codex=gpt-5.6-sol' }).label, 'codex · no run yet');
    // An unknown harness still gets a chip (a generic mark), never a crash.
    assert.equal(executorChip({ executor_route: 'opencode' }).label, 'opencode · no run yet');
    // ABSENT FACT -> no chip at all: no placeholder, no "api" noise on every
    // ordinary bubble (the native path is the unremarkable case).
    assert.equal(executorChip({}), null);
    assert.equal(executorChip({ executor_route: '' }), null);
    assert.equal(executorChip(null), null);
});

test('the chip is layered truth: decision before evidence, receipt only from evidence', () => {
    // PRE-COMPLETION: the route is a DISPATCH decision, so the chip states only
    // that — never "Ran on your ... account", a receipt nothing has issued yet.
    // The claude harness prints its product name (owner decision: "Claude Code").
    const dispatched = executorChip({ executor_route: 'claude' });
    assert.equal(dispatched.label, 'Claude Code · no run yet');
    assert.match(dispatched.title, /Dispatched to Claude Code/);
    assert.match(dispatched.title, /no delegated-run receipt yet/);
    assert.match(dispatched.title, /itself runs on the API/);
    assert.doesNotMatch(dispatched.title, /Ran on your/);

    // EVIDENCE with settled runs: the completion-seam reconciliation proved the
    // delegation happened, so the chip may finally say so — with the count and
    // the ledger-derived subscription spend in the tooltip detail layer.
    const delegated = executorChip({
        executor_route: 'claude',
        execution_evidence: {
            delegated_runs_started: 1, delegated_runs_settled: 1,
            delegated_runs_succeeded: 1, delegated_runs_failed: 0,
            subscription_cost_usd: 0.0, harness_models: ['claude-sonnet'],
        },
    });
    assert.equal(delegated.label, 'Claude Code · 1 run');
    assert.match(delegated.title, /Delegated to your Claude Code account — 1 run settled, \$0\.00 subscription/);

    // ALL-FAILED runs must never read as success: the failure count rides the
    // LABEL, not only the tooltip (delegated_runs_failed was previously unread
    // and two failed runs rendered as a clean "2 run(s)" receipt).
    const allFailed = executorChip({
        executor_route: 'codex',
        execution_evidence: {
            delegated_runs_started: 2, delegated_runs_settled: 2,
            delegated_runs_succeeded: 0, delegated_runs_failed: 2,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.equal(allFailed.label, 'codex · 2 runs, 2 failed');
    assert.match(allFailed.title, /2 failed/);

    // STARTED but none settled: as far as the durable rows know, the run is
    // still out — the label says so instead of a bare harness name.
    const inFlight = executorChip({
        executor_route: 'codex',
        execution_evidence: {
            delegated_runs_started: 1, delegated_runs_settled: 0,
            delegated_runs_succeeded: 0, delegated_runs_failed: 0,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.equal(inFlight.label, 'codex · running');
    assert.match(inFlight.title, /1 run\(s\) started, none settled/);

    // EVIDENCE with zero runs: the route was assigned and no delegated run left
    // a durable record — the chip points at the ledger instead of asserting
    // "ran natively" as a fact (a run started past a failed durable write is
    // still possible: started_uncustodied).
    const unused = executorChip({
        executor_route: 'claude',
        execution_evidence: {
            delegated_runs_started: 0, delegated_runs_settled: 0,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.equal(unused.label, 'Claude Code · no run yet');
    assert.match(unused.title, /no durable record of a delegated run/);

    // Unreadable custody evidence is UNKNOWN, never a "no run" receipt: the
    // zero counts ride evidence_read_failed and must not render as "recorded".
    const unreadable = executorChip({
        executor_route: 'claude',
        execution_evidence: {
            delegated_runs_started: 0, delegated_runs_settled: 0,
            evidence_read_failed: true,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.match(unreadable.label, /evidence unavailable/);
    assert.match(unreadable.title, /could not be read/);
    assert.match(unreadable.title, /unknown/);
    assert.doesNotMatch(unreadable.label, /no run yet/);
    // Zero custody rows prove neither non-execution nor the API path
    // (started_uncustodied exists) — the title must assert NEITHER.
    assert.doesNotMatch(unused.title, /natively|ran on the API/);

    // evidence_read_failed is honoured PAST recorded starts too: the partial
    // work-order replay can fail with started>0, and a confident settled/spend
    // receipt over admittedly incomplete evidence would be a lie.
    const unreadablePastStart = executorChip({
        executor_route: 'codex',
        execution_evidence: {
            delegated_runs_started: 2, delegated_runs_settled: 2,
            delegated_runs_succeeded: 2, delegated_runs_failed: 0,
            evidence_read_failed: true,
            subscription_cost_usd: 1.23, harness_models: [],
        },
    });
    assert.match(unreadablePastStart.label, /evidence unavailable/);
    assert.match(unreadablePastStart.title, /at least 2 delegated run\(s\) started/);
    assert.match(unreadablePastStart.title, /final counts are unknown/);
    assert.doesNotMatch(unreadablePastStart.title, /\$/);

    // Undisclosed spend never renders as a number.
    const undisclosed = executorChip({
        executor_route: 'codex',
        execution_evidence: {
            delegated_runs_started: 2, delegated_runs_settled: 2,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.match(undisclosed.title, /spend undisclosed/);
    assert.doesNotMatch(undisclosed.title, /\$/);

    // An estimated sum never renders as an exact receipt: the ~ prefix rides.
    const estimated = executorChip({
        executor_route: 'codex',
        execution_evidence: {
            delegated_runs_started: 1, delegated_runs_settled: 1,
            subscription_cost_usd: 0.42, subscription_cost_estimated: true,
            harness_models: [],
        },
    });
    assert.match(estimated.title, /~\$0\.42 subscription/);
});

test('the terminal substrate FACT rides the tooltip beside the counts', () => {
    // `actual_substrate` is the completion seam's typed claim (Q1A) and was a
    // dead wire field: on the frame, in the history allowlist, rendered nowhere.
    const nativeOnly = executorChip({
        executor_route: 'codex',
        actual_substrate: 'native_only',
        execution_evidence: {
            delegated_runs_started: 0, delegated_runs_settled: 0,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.equal(nativeOnly.label, 'codex · no run yet');
    assert.match(nativeOnly.title, /no harness run recorded/);

    const used = executorChip({
        executor_route: 'codex',
        actual_substrate: 'harness_used',
        execution_evidence: {
            delegated_runs_started: 1, delegated_runs_settled: 1,
            delegated_runs_succeeded: 1, delegated_runs_failed: 0,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.match(used.title, /custody evidence confirms a harness run/);

    const attempted = executorChip({
        executor_route: 'codex',
        actual_substrate: 'harness_attempted',
        execution_evidence: {
            delegated_runs_started: 1, delegated_runs_settled: 1,
            delegated_runs_succeeded: 0, delegated_runs_failed: 1,
            subscription_cost_usd: null, harness_models: [],
        },
    });
    assert.match(attempted.title, /harness attempted, no delegated run succeeded/);

    // An unknown/absent enum adds no clause — and never crashes the chip.
    const bare = executorChip({ executor_route: 'codex', actual_substrate: 'something_new' });
    assert.doesNotMatch(bare.title, /—\s*$/);
});

test('the chip rides both projections: an ordinary progress bubble and a subagent row', () => {
    const bubble = summarizeChatLiveEvent({
        is_progress: true, content: 'working on the thing', task_id: 't1',
        executor_route: 'codex',
    });
    assert.equal(bubble.executorChip.harness, 'codex');

    const subagentRow = summarizeChatLiveEvent({
        is_progress: true, content: 'child progress',
        delegation_role: 'subagent', subagent_task_id: 'child-1',
        subagent_event: 'running', subagent_role: 'implementer',
        executor_route: 'claude=sonnet-5',
    });
    assert.equal(subagentRow.executorChip.harness, 'claude');

    // Neither projection invents a chip when the fact is absent.
    assert.equal('executorChip' in summarizeChatLiveEvent({
        is_progress: true, content: 'plain', task_id: 't2',
    }), false);
    assert.equal('executorChip' in summarizeChatLiveEvent({
        is_progress: true, content: 'plain child', delegation_role: 'subagent',
        subagent_task_id: 'child-2', subagent_event: 'running',
    }), false);
});
