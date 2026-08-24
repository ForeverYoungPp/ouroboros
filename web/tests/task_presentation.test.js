import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    OWNER_STOP_DETAIL_MARKER,
    summarizeChatLiveEvent,
    summarizeLogEvent,
    taskPresentation,
} from '../modules/log_events.js';

const chatSource = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');
const activitySource = readFileSync(new URL('../modules/chat_activity.js', import.meta.url), 'utf8');
const logEventsSource = readFileSync(new URL('../modules/log_events.js', import.meta.url), 'utf8');

const terminalCases = [
    ['clean Done', { status: 'completed' }, { phase: 'done', headline: 'Done' }],
    ['Done with warnings', {
        status: 'completed', outcome_axes: { execution: { status: 'degraded' } },
    }, { phase: 'warn', headline: 'Done with warnings' }],
    ['Failed', {
        status: 'failed', reason_code: 'delegated_custody_unreconciled',
    }, { phase: 'error', headline: 'Failed' }],
    ['Cancelled', { status: 'cancelled' }, { phase: 'cancelled', headline: 'Cancelled' }],
];

test('factual task presentation uses the approved five-word family', () => {
    assert.deepEqual(taskPresentation({ status: 'running' }), {
        phase: 'working', headline: 'Working',
    });
    for (const [name, payload, expected] of terminalCases) {
        assert.deepEqual(taskPresentation(payload), expected, name);
    }
});

test('live task_done and replay/log task truth have phase and headline parity', () => {
    for (const [name, payload, expected] of terminalCases) {
        const evt = { type: 'task_done', ...payload };
        const live = summarizeChatLiveEvent(evt);
        const replay = summarizeLogEvent(evt);
        assert.deepEqual({ phase: live.phase, headline: live.headline }, expected, `${name}/live`);
        assert.deepEqual({ phase: replay.phase, headline: replay.headline }, expected, `${name}/replay`);
        assert.doesNotMatch(`${live.headline} ${replay.headline}`, /Issue|Notice|delegated_custody_unreconciled/);
        if (payload.reason_code) {
            assert.match(live.body, /Reason: delegated_custody_unreconciled/);
            assert.ok(replay.meta.includes('delegated_custody_unreconciled'));
        }
    }
    assert.match(chatSource, /const presentation = taskPresentation\(msg \|\| \{\}\);/);
});

test('owner soft-stop is factual Done and keeps its marker in details', () => {
    const evt = {
        type: 'task_done', status: 'done', reason_code: 'owner_requested_finalization',
        outcome_axes: { execution: { status: 'best_effort' } },
    };
    const live = summarizeChatLiveEvent(evt);
    const replay = summarizeLogEvent(evt);
    assert.deepEqual({ phase: live.phase, headline: live.headline }, { phase: 'done', headline: 'Done' });
    assert.deepEqual({ phase: replay.phase, headline: replay.headline }, { phase: 'done', headline: 'Done' });
    assert.ok(live.meta.includes(OWNER_STOP_DETAIL_MARKER));
    assert.ok(replay.meta.includes(OWNER_STOP_DETAIL_MARKER));
    assert.doesNotMatch(live.headline, /owner_requested_finalization/);
});

test('failed child remains a compact local fact without owner-alarm semantics', () => {
    const child = summarizeChatLiveEvent({
        type: 'send_message', is_progress: true, delegation_role: 'subagent',
        parent_task_id: 'root-working', subagent_task_id: 'child-failed',
        subagent_role: 'researcher', subagent_event: 'failed', status: 'failed',
        error: 'daemon unreachable', reason_code: 'delegated_custody_unreconciled',
    });
    assert.equal(child.phase, 'error');
    assert.equal(child.terminal, true);
    assert.match(child.headline, /— Failed$/);
    assert.doesNotMatch(child.headline, /Issue|Attention|delegated_custody_unreconciled/);
    assert.equal('ownerAlarm' in child, false);
    assert.equal('notification' in child, false);
});

test('interrupted child stays retryable with a Working chip and inspectable detail', () => {
    const child = summarizeChatLiveEvent({
        type: 'send_message', is_progress: true, delegation_role: 'subagent',
        parent_task_id: 'root-working', subagent_task_id: 'child-interrupted',
        subagent_role: 'researcher', subagent_event: 'interrupted', status: 'interrupted',
        error: 'transport interrupted; retry remains available',
    });
    assert.equal(child.phase, 'warn');
    assert.equal(child.terminal, false);
    assert.equal(child.visible, true);
    assert.match(child.headline, /— Working$/);
    assert.match(child.fullBody, /transport interrupted; retry remains available/);
    assert.equal(taskPresentation({}, child.terminal ? child.phase : 'working').headline, 'Working');
    assert.match(chatSource, /const chipPhase = record\.finished \? activePhase : 'working';/);
    assert.match(chatSource, /taskPresentation\(\{\}, chipPhase\)\.headline/);
});

test('nonterminal diagnostics stay visible facts but never promote the task', () => {
    const diagnostics = [
        summarizeChatLiveEvent({ type: 'llm_round_error', error: 'temporary provider error' }),
        summarizeChatLiveEvent({ type: 'tool_timeout', tool: 'delegate_wait' }),
        summarizeChatLiveEvent({ type: 'tool_call_finished', tool: 'run_command', is_error: true }),
        summarizeChatLiveEvent({ type: 'task_checkpoint', checkpoint_kind: 'context_fit_low_retry' }),
    ];
    for (const diagnostic of diagnostics) {
        assert.equal(diagnostic.visible, true);
        assert.equal(diagnostic.promote, false);
        assert.equal(diagnostic.terminal, false);
    }
    assert.doesNotMatch(chatSource, /showContextFitToast|context-fit:/);
    assert.match(chatSource, /function showTaskIncidentToast\(msg\)/);
    const success = summarizeChatLiveEvent({ type: 'task_done', status: 'completed' });
    assert.deepEqual({ phase: success.phase, headline: success.headline }, { phase: 'done', headline: 'Done' });
    assert.match(chatSource, /const shouldPromote = Boolean\(summary\.promote\) \|\| record\.finished;/);
    assert.match(chatSource, /record\.updates > 1 \? record\.titleEl\.textContent : ''/);
    assert.match(chatSource, /\|\| 'Working\.\.\.'/);
    assert.doesNotMatch(chatSource, /record\.lastHumanHeadline \|\| headline/);
});

test('unknown keyword-shaped Chat event does not synthesize an alarm', () => {
    const unknown = summarizeChatLiveEvent({
        type: 'future_worker_crash_recovered', error: 'diagnostic payload',
    });
    assert.equal(unknown.visible, false);
    assert.equal(unknown.promote, false);
    assert.equal(unknown.terminal, false);
    assert.doesNotMatch(unknown.headline, /Issue|Attention/);
    const chatSummarizer = logEventsSource.slice(
        logEventsSource.indexOf('export function summarizeChatLiveEvent'),
        logEventsSource.indexOf('export function duplicateLogEventKey'),
    );
    assert.doesNotMatch(chatSummarizer, /t\.includes\('error'\)|t\.includes\('crash'\)|t\.includes\('fail'\)/);
});

test('header status has no terminal-attention state or writer', () => {
    assert.doesNotMatch(chatSource, /lastTerminalAttention/);
    assert.doesNotMatch(activitySource, /lastTerminalAttention|text: 'Attention'/);
});
