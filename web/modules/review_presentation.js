import { escapeHtmlAttr } from './utils.js';
import { harnessIdentityMarkup } from './harness_presentation.js';
import { reconcileReviewMarkup } from './review_dom_patch.js';

const escapeHtmlText = escapeHtmlAttr;

const SURFACE_ORDER = new Map([
    ['skill', 0],
    ['plan', 1],
    ['task_acceptance', 2],
]);

const ACTIVE_STATES = new Set(['queued', 'running', 'open', 'working', 'pending']);
const ERROR_STATES = new Set(['failed', 'error', 'blocked', 'blockers', 'timeout']);
const WARNING_STATES = new Set([
    'warnings', 'warning', 'degraded', 'interrupted', 'cancelled',
    'review_required', 'revise_plan', 'rail_degraded', 'cycles_exhausted',
]);
const SUCCESS_STATES = new Set([
    'clean', 'passed', 'pass', 'succeeded', 'success', 'completed', 'closed', 'green',
]);
const TERMINAL_STATES = new Set([
    ...SUCCESS_STATES, 'terminal',
    ...ERROR_STATES, ...WARNING_STATES,
]);
// These values belong to the lifecycle of the review job, not to the
// review's semantic verdict.  Keep them terminal for activity purposes while
// preventing a live lifecycle frame from looking like a verified PASS.
const LIFECYCLE_TERMINAL_STATES = new Set(['succeeded', 'success', 'completed']);

const text = (value) => String(value ?? '').trim();
const finiteCount = (value) => {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.trunc(number) : null;
};

function normalizedState(value, fallback = 'unavailable') {
    const state = text(value).toLowerCase();
    if (ACTIVE_STATES.has(state)) {
        if (state === 'pending') return 'queued';
        return state === 'open' || state === 'working' ? 'running' : state;
    }
    if (LIFECYCLE_TERMINAL_STATES.has(state)) return 'terminal';
    if (TERMINAL_STATES.has(state)) return 'terminal';
    if (state === 'unavailable' || state === 'absent') return 'unavailable';
    if (state === 'superseded') return 'superseded';
    return fallback;
}

function statusTone(state, verdict = '') {
    const token = text(verdict || state).toLowerCase();
    if (ERROR_STATES.has(token) || token === 'fail') return 'error';
    if (WARNING_STATES.has(token)) return 'warn';
    if (ACTIVE_STATES.has(token) || state === 'running' || state === 'queued') return 'working';
    if (SUCCESS_STATES.has(token)) return 'done';
    return 'neutral';
}

function attemptIdentity(attempt, fallback = '') {
    return text(
        attempt?.id || attempt?.job_id || attempt?.wave_id || attempt?.panel_id
        || attempt?.request_fingerprint || fallback,
    );
}

function normalizedExecutions(value, legacyExecution = null) {
    const rows = Array.isArray(value) ? value : [];
    if (rows.length) return rows.filter((item) => item && typeof item === 'object');
    const executed = legacyExecution?.executed;
    return executed && typeof executed === 'object' ? [executed] : [];
}

function executionsFromReviewRecord(record) {
    if (!record || typeof record !== 'object') return [];
    const direct = normalizedExecutions(record.executions, record.execution);
    const actorExecutions = (Array.isArray(record.actors) ? record.actors : [])
        .flatMap((actor) => normalizedExecutions(actor?.executions, actor?.execution));
    return [...direct, ...actorExecutions];
}

function uniformAttemptInitiator(attempts, fallback = '') {
    if (!attempts.length) return text(fallback);
    const initiators = attempts.map((attempt) => text(attempt.initiatorTaskId));
    const known = initiators.filter(Boolean);
    if (!known.length) return text(fallback);
    if (known.length !== initiators.length) return '';
    const unique = new Set(initiators);
    return unique.size === 1 ? initiators[0] : '';
}

function normalizeSkillAttempt(attempt, defaults = {}, ordinal = 0) {
    if (!attempt || typeof attempt !== 'object') return null;
    const skill = text(attempt.skill || defaults.skill);
    const jobId = text(attempt.job_id || attempt.jobId);
    const id = attemptIdentity(attempt, [
        text(attempt.content_hash), text(attempt.review_round),
        text(attempt.snapshot_attempt), String(ordinal + 1),
    ].join(':'));
    if (!id) return null;
    const lifecycleStatus = text(attempt.lifecycle_status || attempt.job_status);
    const explicitStatus = text(
        attempt.review_status || attempt.review_verdict || attempt.status || defaults.status,
    );
    const lifecycleOnly = attempt.lifecycle_only === true
        || (!attempt.review_status && !attempt.review_verdict
            && LIFECYCLE_TERMINAL_STATES.has(explicitStatus.toLowerCase()));
    const rawStatus = lifecycleOnly ? '' : explicitStatus;
    const state = lifecycleOnly
        ? normalizedState(lifecycleStatus || explicitStatus, defaults.state || 'unavailable')
        : normalizedState(rawStatus, defaults.state || 'unavailable');
    return {
        id,
        surface: 'skill',
        state,
        tone: statusTone(state, rawStatus),
        verdict: rawStatus,
        timestamp: text(attempt.ts || attempt.timestamp),
        ordinal,
        label: [
            attempt.review_round != null ? `round ${attempt.review_round}` : '',
            attempt.snapshot_attempt != null ? `attempt ${attempt.snapshot_attempt}` : '',
        ].filter(Boolean).join(' · ') || `attempt ${ordinal + 1}`,
        summary: text(attempt.summary || attempt.text || attempt.terminal_reason || attempt.error)
            || (lifecycleOnly ? 'Review verdict unavailable.' : ''),
        superseded: Boolean(attempt.superseded),
        replayed: Boolean(attempt.replayed || attempt.replay || attempt.replay_of || attempt.replayed_from_ts),
        revised: Boolean(attempt.snapshot_revised || attempt.revised_snapshot || attempt.revised),
        initiatorTaskId: text(
            attempt.initiator_task_id || attempt.origin_task_id || attempt.task_id,
        ),
        executions: normalizedExecutions(attempt.executions, attempt.execution),
        execution: attempt.execution && typeof attempt.execution === 'object' ? attempt.execution : null,
        lifecycleOnly,
        detailRef: skill && jobId ? { surface: 'skill', skill, jobId } : null,
        detailText: '',
    };
}

function normalizeSkillGroup(group, row = {}, { allowRowTaskIdFallback = true } = {}) {
    if (!group || typeof group !== 'object' || text(group.surface) !== 'skill') return null;
    const id = text(group.id || group.group_id);
    const ownerTaskId = text(group.presentation_owner_task_id);
    if (!id || !ownerTaskId) return null;
    const groupStatus = text(group.status);
    const lifecycleStatus = text(group.lifecycle_status || groupStatus || row.status);
    const groupVerdict = text(group.verdict);
    const lifecycleOnly = group.lifecycle_only === true
        || (!group.review_status && !group.verdict
            && LIFECYCLE_TERMINAL_STATES.has(lifecycleStatus.toLowerCase()))
        || (!group.review_status && !group.review_verdict
            && groupVerdict.toLowerCase() === lifecycleStatus.toLowerCase()
            && LIFECYCLE_TERMINAL_STATES.has(lifecycleStatus.toLowerCase()));
    const defaults = {
        skill: text(group.skill || row.skill),
        status: lifecycleOnly
            ? text(group.review_status || group.review_verdict)
            : text(group.review_status || group.verdict || group.status || row.status),
        state: normalizedState(group.state || group.status || row.status),
        initiatorTaskId: text(
            group.initiator_task_id || group.origin_task_id || row.origin_task_id
            || (allowRowTaskIdFallback ? row.task_id : ''),
        ),
    };
    const rawAttempts = Array.isArray(group.attempts) ? group.attempts : [];
    const attempts = rawAttempts
        .map((attempt, index) => normalizeSkillAttempt(attempt, defaults, index))
        .filter(Boolean);
    if (!attempts.length && (row.job_id || group.job_id || group.lifecycle_id)) {
        const fallback = normalizeSkillAttempt({
            ...row,
            ...group,
            job_id: group.job_id || row.job_id || group.lifecycle_id,
            lifecycle_only: lifecycleOnly,
        }, defaults, 0);
        if (fallback) attempts.push(fallback);
    }
    const latest = attempts[attempts.length - 1] || null;
    const state = normalizedState(
        group.state || group.lifecycle_status || group.status || latest?.state,
        latest?.state || 'unavailable',
    );
    const projectedCount = finiteCount(group.projected_attempt_count);
    const count = projectedCount ?? attempts.length;
    return {
        id,
        surface: 'skill',
        label: 'Skill review',
        subject: defaults.skill,
        presentationOwnerTaskId: ownerTaskId,
        subjectTaskId: text(group.subject_task_id),
        initiatorTaskId: uniformAttemptInitiator(attempts, defaults.initiatorTaskId),
        state,
        tone: statusTone(
            state,
            lifecycleOnly ? text(group.review_status || group.review_verdict || latest?.verdict)
                : text(group.verdict || group.review_status || group.status || latest?.verdict),
        ),
        verdict: lifecycleOnly
            ? text(group.review_status || group.review_verdict || latest?.verdict)
            : text(group.verdict || group.review_status || group.status || latest?.verdict),
        summary: text(group.summary || latest?.summary)
            || (lifecycleOnly ? 'Review verdict unavailable.' : ''),
        lifecycleOnly,
        activeCount: finiteCount(group.active_count) ?? (state === 'queued' || state === 'running' ? 1 : 0),
        attemptCount: Math.max(count, attempts.length),
        countIsAuthoritative: group.count_is_authoritative === true,
        attempts,
    };
}

export function reviewGroupFromHistoryRow(row) {
    return normalizeSkillGroup(row?.review_group, row);
}

export function classifyReviewLifecycle(row) {
    const lifecycle = row?.lifecycle || row?.progress_meta?.lifecycle;
    if (!lifecycle || typeof lifecycle !== 'object' || text(lifecycle.kind) !== 'review') {
        return { classification: 'not_review', group: null };
    }
    const groupId = text(lifecycle.group_id || lifecycle.review_group_id);
    const ownerTaskId = text(lifecycle.presentation_owner_task_id);
    if (!groupId || !ownerTaskId) {
        return { classification: 'source_incomplete', group: null };
    }
    const status = text(lifecycle.status || lifecycle.phase || 'queued');
    const reviewStatus = text(lifecycle.review_status || lifecycle.review_verdict);
    const lifecycleOnly = !reviewStatus;
    const group = normalizeSkillGroup({
        surface: 'skill',
        id: groupId,
        presentation_owner_task_id: ownerTaskId,
        subject_task_id: lifecycle.subject_task_id,
        initiator_task_id: lifecycle.initiator_task_id || lifecycle.origin_task_id,
        skill: lifecycle.target || lifecycle.skill,
        state: status,
        status: reviewStatus,
        review_status: reviewStatus,
        lifecycle_status: status,
        lifecycle_only: lifecycleOnly,
        active_count: ['pending', 'queued', 'running'].includes(status.toLowerCase()) ? 1 : 0,
        projected_attempt_count: lifecycle.projected_attempt_count ?? (lifecycle.job_id || lifecycle.id ? 1 : 0),
        count_is_authoritative: lifecycle.count_is_authoritative === true,
        attempts: lifecycle.job_id || lifecycle.id ? [{
            ...lifecycle,
            job_id: lifecycle.job_id || lifecycle.id,
            status: reviewStatus,
            review_status: reviewStatus,
            lifecycle_status: status,
            lifecycle_only: lifecycleOnly,
        }] : [],
    }, row, { allowRowTaskIdFallback: false });
    return {
        classification: group ? 'source_complete' : 'source_incomplete',
        group,
    };
}

export function reviewGroupFromLifecycle(row) {
    return classifyReviewLifecycle(row).group;
}

function lifecyclePointerPayload(row) {
    const direct = row?.lifecycle_pointer;
    if (direct && typeof direct === 'object') return direct;
    const nested = row?.progress_meta?.lifecycle_pointer;
    return nested && typeof nested === 'object' ? nested : null;
}

export function classifyReviewLifecyclePointer(row) {
    const pointer = lifecyclePointerPayload(row);
    if (!pointer || text(pointer.kind) !== 'review') {
        return { classification: 'not_pointer', group: null };
    }
    const groupId = text(pointer.group_id || pointer.review_group_id);
    const ownerTaskId = text(pointer.presentation_owner_task_id);
    if (!groupId || !ownerTaskId) {
        return { classification: 'source_incomplete', group: null };
    }
    const status = text(pointer.status || 'queued');
    const reviewStatus = text(pointer.review_status || pointer.review_verdict);
    const lifecycleOnly = !reviewStatus;
    const initiatorTaskId = text(pointer.initiator_task_id || pointer.origin_task_id);
    const group = normalizeSkillGroup({
        surface: 'skill',
        id: groupId,
        presentation_owner_task_id: ownerTaskId,
        subject_task_id: pointer.subject_task_id,
        initiator_task_id: initiatorTaskId,
        skill: pointer.target || pointer.skill,
        state: status,
        status: reviewStatus,
        review_status: reviewStatus,
        lifecycle_status: status,
        lifecycle_only: lifecycleOnly,
        active_count: ['pending', 'queued', 'running'].includes(status.toLowerCase()) ? 1 : 0,
        projected_attempt_count: pointer.projected_attempt_count ?? (pointer.job_id ? 1 : 0),
        count_is_authoritative: pointer.count_is_authoritative === true,
        attempts: pointer.job_id ? [{
            ...pointer,
            initiator_task_id: initiatorTaskId,
            status: reviewStatus,
            review_status: reviewStatus,
            lifecycle_status: status,
            lifecycle_only: lifecycleOnly,
        }] : [],
    }, row, { allowRowTaskIdFallback: false });
    return {
        classification: group ? 'source_complete' : 'source_incomplete',
        group,
    };
}

function planAttempt(wave, index, isCurrent) {
    if (!wave || typeof wave !== 'object') return null;
    const fingerprint = text(wave.request_fingerprint);
    const id = attemptIdentity(wave, `${fingerprint || 'wave'}:${wave.cycle_index ?? index + 1}`);
    if (!id) return null;
    const verdict = text(wave.aggregate || 'UNKNOWN');
    const superseded = Boolean(wave.superseded) || !isCurrent;
    // A persisted wave is a completed reviewer call even when the plan gate
    // intentionally remains open (`closed=false`). Liveness belongs only to a
    // current attempt for which no matching wave has landed yet.
    const state = superseded ? 'superseded' : 'terminal';
    return {
        id,
        surface: 'plan',
        state,
        tone: statusTone(state, verdict),
        verdict,
        timestamp: text(wave.ts || wave.timestamp || wave.closed_at),
        ordinal: index,
        label: wave.cycle_index != null ? `wave ${wave.cycle_index}` : `wave ${index + 1}`,
        summary: text(wave.reason || wave.summary),
        superseded,
        replayed: Boolean(wave.replayed || wave.cached),
        revised: Boolean(wave.revised),
        initiatorTaskId: '',
        executions: executionsFromReviewRecord(wave),
        execution: null,
        detailRef: null,
        detailText: `${planWaveDetail(wave)}\nCost unavailable`.trim(),
    };
}

function currentPlanAttempt(current, index) {
    const fingerprint = text(current?.fingerprint);
    const status = text(current?.status).toLowerCase();
    if (!fingerprint || !status) return null;
    const state = normalizedState(status);
    const detailText = [
        `Status: ${status}`,
        current.reason ? `Reason: ${text(current.reason)}` : '',
        'Review result unavailable.',
        'Cost unavailable',
    ].filter(Boolean).join('\n');
    return {
        id: fingerprint,
        surface: 'plan',
        state,
        tone: statusTone(state, status),
        verdict: status,
        timestamp: text(current.ts || current.timestamp),
        ordinal: index,
        label: 'current attempt',
        summary: text(current.reason),
        superseded: false,
        replayed: false,
        revised: false,
        initiatorTaskId: '',
        executions: normalizedExecutions(current.executions, current.execution),
        execution: null,
        detailRef: null,
        detailText,
    };
}

function planWaveDetail(wave) {
    const lines = [
        wave.aggregate ? `Verdict: ${wave.aggregate}` : '',
        wave.closed != null ? `Closed: ${wave.closed ? 'yes' : 'no'}` : '',
        wave.paid != null ? `Reviewer panel dispatched: ${wave.paid ? 'yes' : 'no'}` : '',
        wave.quorum_unreachable ? 'Quorum unavailable' : '',
        wave.cycles_exhausted ? 'Review cycles exhausted' : '',
        wave.reason ? `Reason: ${wave.reason}` : '',
    ];
    return lines.filter(Boolean).join('\n');
}

export function planReviewGroupFromTaskDetail(detail, ownerTaskId = '') {
    const owner = text(ownerTaskId || detail?.task_id);
    const stateRecord = detail?.plan_review_state;
    if (!owner || !stateRecord || typeof stateRecord !== 'object') return null;
    const current = stateRecord.current_attempt && typeof stateRecord.current_attempt === 'object'
        ? stateRecord.current_attempt : {};
    const waves = Array.isArray(stateRecord.waves) ? stateRecord.waves : [];
    if (!waves.length && !text(current.status)) return null;
    const currentFingerprint = text(current.fingerprint);
    const currentWaveIndex = currentFingerprint
        ? waves.findIndex((wave) => text(wave?.request_fingerprint) === currentFingerprint)
        : (waves.length ? waves.length - 1 : -1);
    const attempts = waves
        .map((wave, index) => planAttempt(
            wave,
            index,
            index === currentWaveIndex,
        ))
        .filter(Boolean);
    let currentAttempt = currentWaveIndex >= 0
        ? attempts.find((attempt) => attempt.id === attemptIdentity(
            waves[currentWaveIndex],
            `${currentFingerprint || 'wave'}:${waves[currentWaveIndex]?.cycle_index ?? currentWaveIndex + 1}`,
        )) || null
        : null;
    const currentStatus = text(current.status || (currentAttempt ? 'closed' : 'open')).toLowerCase();
    let state = 'terminal';
    let activeCount = 0;
    if (!currentAttempt) {
        const unmatchedAttempt = currentPlanAttempt(current, attempts.length);
        if (unmatchedAttempt) {
            attempts.push(unmatchedAttempt);
            currentAttempt = unmatchedAttempt;
            state = unmatchedAttempt.state;
            activeCount = state === 'queued' || state === 'running' ? 1 : 0;
        } else if (currentStatus) {
            state = normalizedState(currentStatus);
        }
    }
    const currentVerdict = text(currentAttempt?.verdict || currentStatus);
    return {
        id: `plan:${owner}`,
        surface: 'plan',
        label: 'Plan review',
        subject: '',
        presentationOwnerTaskId: owner,
        subjectTaskId: owner,
        initiatorTaskId: owner,
        state,
        tone: statusTone(state, currentVerdict),
        verdict: currentVerdict,
        summary: text(current.reason || currentAttempt?.summary),
        activeCount,
        attemptCount: Math.max(attempts.length, finiteCount(stateRecord.waves_omitted) != null
            ? attempts.length + finiteCount(stateRecord.waves_omitted) : attempts.length),
        countIsAuthoritative: finiteCount(stateRecord.waves_omitted) === 0,
        attempts,
    };
}

function compactCoverage(coverage) {
    if (!coverage || typeof coverage !== 'object') return '';
    return Object.entries(coverage)
        .filter(([, value]) => value !== '' && value !== null && value !== undefined)
        .map(([key, value]) => `${key}=${String(value)}`)
        .join(', ');
}

export function formatReviewProjection(projection) {
    const panels = Array.isArray(projection?.panels) ? projection.panels : [];
    const lines = [];
    panels.forEach((panel, panelIndex) => {
        if (!panel || typeof panel !== 'object') return;
        const quorum = panel.quorum && typeof panel.quorum === 'object' ? panel.quorum : {};
        const panelId = String(panel.panel_id || `panel-${panelIndex + 1}`);
        lines.push(
            `Review panel ${panelId}: ${String(panel.surface || 'review')} · authority=${String(panel.authority || 'unspecified')} · verdict=${String(panel.aggregate_signal || 'UNKNOWN')} · transport=${String(panel.transport_status || 'unknown')} · parse=${String(panel.parse_status || 'unknown')} · quorum=${String(quorum.contributed ?? 0)}/${String(quorum.configured ?? 0)} (required ${String(quorum.required ?? 0)}) · enforcement=${String(panel.enforcement_impact || 'unknown')}${panel.single_reviewer_no_diversity ? ' · single-reviewer (no diversity)' : ''}${panel.dialogue && panel.dialogue.status ? ` · dialogue=${String(panel.dialogue.status)}` : ''}${panel.superseded ? ' · superseded' : ''}`,
        );
        if (panel.reason) lines.push(`Panel reason: ${String(panel.reason)}`);
        const coverage = compactCoverage(panel.coverage);
        if (coverage) lines.push(`Panel coverage: ${coverage}`);
        const binding = [
            panel.candidate_hash ? `candidate_hash=${String(panel.candidate_hash)}` : '',
            panel.evidence_revision ? `evidence_revision=${String(panel.evidence_revision)}` : '',
            panel.fence_hash ? `fence_hash=${String(panel.fence_hash)}` : '',
            panel.binding_hash ? `binding_hash=${String(panel.binding_hash)}` : '',
        ].filter(Boolean);
        if (binding.length) lines.push(`Panel binding: ${binding.join(' · ')}`);
        (Array.isArray(panel.actors) ? panel.actors : []).forEach((actor) => {
            if (!actor || typeof actor !== 'object') return;
            lines.push(
                `Reviewer ${String(actor.slot_id || '?')}: role=${String(actor.actor_role || 'reviewer')} · provider=${String(actor.provider || 'unknown')} · model=${String(actor.model || 'unknown')} · transport=${String(actor.transport_status || 'unknown')} · parse=${String(actor.parse_status || 'unknown')} · verdict=${String(actor.semantic_verdict || 'none')}${actor.outcome_tier ? ` · outcome_tier=${String(actor.outcome_tier)}` : ''}${actor.dialogue_status ? ` · dialogue=${String(actor.dialogue_status)}` : ''} · quorum=${actor.quorum_contribution ? 'contributes' : 'abstains'} · enforcement=${String(actor.enforcement_impact || 'unknown')}`,
            );
            const actorCoverage = compactCoverage(actor.coverage);
            if (actorCoverage) lines.push(`Reviewer ${String(actor.slot_id || '?')} coverage: ${actorCoverage}`);
            if (actor.reason) lines.push(`Reviewer ${String(actor.slot_id || '?')} reason: ${String(actor.reason)}`);
        });
    });
    return lines.join('\n');
}

export function taskAcceptanceGroupFromTaskDetail(detail, ownerTaskId = '') {
    const owner = text(ownerTaskId || detail?.task_id);
    const projection = detail?.review_projection;
    const panels = (Array.isArray(projection?.panels) ? projection.panels : [])
        .filter((panel) => text(panel?.surface) === 'task_acceptance');
    if (!owner || !panels.length) return null;
    const attempts = panels.map((panel, index) => {
        const verdict = text(panel?.aggregate_signal || 'UNKNOWN');
        return {
            id: attemptIdentity(panel, `panel:${index + 1}`),
            surface: 'task_acceptance',
            state: panel?.superseded ? 'superseded' : 'terminal',
            tone: statusTone('terminal', verdict),
            verdict,
            timestamp: text(panel?.ts || panel?.timestamp),
            ordinal: index,
            label: `panel ${text(panel?.panel_id || index + 1)}`,
            summary: text(panel?.reason),
            superseded: Boolean(panel?.superseded),
            replayed: false,
            revised: false,
            initiatorTaskId: owner,
            executions: executionsFromReviewRecord(panel),
            execution: null,
            detailRef: null,
            detailText: `${formatReviewProjection({ panels: [panel] })}\nCost unavailable`.trim(),
        };
    });
    const latest = attempts.at(-1);
    return {
        id: `task_acceptance:${owner}`,
        surface: 'task_acceptance',
        label: 'Task acceptance',
        subject: '',
        presentationOwnerTaskId: owner,
        subjectTaskId: owner,
        initiatorTaskId: owner,
        state: 'terminal',
        tone: statusTone('terminal', latest?.verdict),
        verdict: text(latest?.verdict),
        summary: text(latest?.summary),
        activeCount: 0,
        attemptCount: attempts.length,
        countIsAuthoritative: true,
        attempts,
    };
}

export function reviewGroupsFromTaskDetail(detail, ownerTaskId = '') {
    return [
        planReviewGroupFromTaskDetail(detail, ownerTaskId),
        taskAcceptanceGroupFromTaskDetail(detail, ownerTaskId),
    ].filter(Boolean);
}

export function mergeReviewGroup(store, incoming) {
    if (!(store instanceof Map) || !incoming?.id || !incoming.presentationOwnerTaskId) return null;
    const prior = store.get(incoming.id);
    if (!prior) {
        const created = { ...incoming, attempts: [...incoming.attempts] };
        store.set(created.id, created);
        return created;
    }
    const incomingIds = new Set(incoming.attempts.map((attempt) => attempt.id));
    const priorById = new Map(prior.attempts.map((attempt) => [attempt.id, attempt]));
    const mergedById = new Map(priorById);
    let hasStaleActiveAttempt = false;
    let introducedActiveAttempt = false;
    for (const attempt of incoming.attempts) {
        const previous = priorById.get(attempt.id);
        const previousTerminal = previous?.state === 'terminal' || previous?.state === 'superseded';
        const incomingActive = attempt.state === 'queued' || attempt.state === 'running';
        // Attempt ids are immutable domain references. A delayed lifecycle row
        // for the same attempt may arrive after its terminal history row, but
        // it cannot make that physical attempt non-terminal again.
        let mergedAttempt = previousTerminal && incomingActive
            ? { ...attempt, ...previous }
            : { ...(previous || {}), ...attempt };
        // A lifecycle terminal frame can arrive after the domain history row
        // for the same attempt.  It contributes timing/execution facts, but
        // its `succeeded`/`completed` word is not a review verdict and must
        // not erase an already-proved semantic result.
        if (attempt.lifecycleOnly && previous?.verdict) {
            mergedAttempt = {
                ...mergedAttempt,
                state: previous.state,
                tone: previous.tone,
                verdict: previous.verdict,
                summary: previous.summary || attempt.summary,
                lifecycleOnly: false,
            };
        }
        mergedById.set(attempt.id, mergedAttempt);
        if (incomingActive && previousTerminal) hasStaleActiveAttempt = true;
        if (incomingActive && !previousTerminal && !previous) introducedActiveAttempt = true;
    }
    if (incoming.surface === 'plan') {
        // Plan task detail owns one canonical current fingerprint. An omitted
        // prior unmatched attempt was replaced, not merely absent from a
        // bounded history window; retain its reason but retire its liveness.
        for (const [id, attempt] of mergedById) {
            if (
                !incomingIds.has(id)
                && (attempt.state === 'queued' || attempt.state === 'running')
            ) {
                mergedById.set(id, {
                    ...attempt, state: 'superseded', tone: 'neutral', superseded: true,
                });
            }
        }
    }
    const priorIds = prior.attempts.map((attempt) => attempt.id);
    const priorOnly = priorIds.filter((id) => !incomingIds.has(id));
    // A projection that contains every known attempt owns their order (for
    // example, terminal Skill history arriving after one live row). A bounded
    // projection that omits a known attempt cannot move it to the front: keep
    // the established order and append only genuinely new identities.
    const order = priorOnly.length === 0
        ? incoming.attempts.map((attempt) => attempt.id)
        : [...priorIds, ...incoming.attempts
            .filter((attempt) => !priorById.has(attempt.id))
            .map((attempt) => attempt.id)];
    const staleActiveRegression = (
        (prior.state === 'terminal' || prior.state === 'superseded')
        && (incoming.state === 'queued' || incoming.state === 'running')
        && hasStaleActiveAttempt
        && !introducedActiveAttempt
    );
    const merged = {
        ...prior,
        ...incoming,
        attempts: order.map((id) => mergedById.get(id)).filter(Boolean),
        attemptCount: Math.max(prior.attemptCount || 0, incoming.attemptCount || 0, mergedById.size),
    };
    if (staleActiveRegression) {
        merged.state = prior.state;
        merged.tone = prior.tone;
        merged.verdict = prior.verdict;
        merged.summary = prior.summary;
        merged.activeCount = prior.activeCount;
    }
    const activeAttempts = merged.attempts.filter(
        (attempt) => attempt.state === 'queued' || attempt.state === 'running',
    );
    // A bounded/history projection can omit the newest live attempt. Preserve
    // that attempt's liveness until a source explicitly updates the same
    // immutable attempt id to a terminal state.
    if (activeAttempts.length) {
        const active = activeAttempts.at(-1);
        merged.state = activeAttempts.some((attempt) => attempt.state === 'running') ? 'running' : 'queued';
        merged.tone = 'working';
        merged.verdict = active.verdict || merged.state;
        merged.summary = active.summary || merged.summary;
        merged.activeCount = activeAttempts.length;
    }
    if (incoming.lifecycleOnly && incoming.attempts.length && !activeAttempts.length) {
        // Project a lifecycle-only group from the latest merged attempt.  A
        // late frame for an existing attempt inherits that attempt's typed
        // verdict; a genuinely new attempt stays neutral instead of borrowing
        // the previous attempt's group-level PASS.
        const latestAttempt = merged.attempts.at(-1);
        if (latestAttempt) {
            merged.state = latestAttempt.state;
            merged.tone = latestAttempt.tone;
            merged.verdict = latestAttempt.verdict || '';
            merged.summary = latestAttempt.summary || merged.summary;
            merged.lifecycleOnly = Boolean(latestAttempt.lifecycleOnly);
        }
    }
    merged.initiatorTaskId = uniformAttemptInitiator(
        merged.attempts,
        incoming.initiatorTaskId || prior.initiatorTaskId,
    );
    store.set(merged.id, merged);
    return merged;
}

export function orderedReviewGroups(store) {
    const groups = store instanceof Map ? [...store.values()] : Array.isArray(store) ? [...store] : [];
    return groups.sort((a, b) => (SURFACE_ORDER.get(a.surface) ?? 99) - (SURFACE_ORDER.get(b.surface) ?? 99));
}

export function reviewGroupCounts(store) {
    const groups = orderedReviewGroups(store);
    return {
        groupCount: groups.length,
        activeCount: groups.reduce((total, group) => total + (finiteCount(group.activeCount) || 0), 0),
    };
}

function reviewRevision(value) {
    const revision = text(value).toLowerCase();
    return /^[0-9a-f]{64}$/.test(revision) ? revision : null;
}

/**
 * Per-task invalidation hydrator. Revisions are opaque content SHA-256 tokens,
 * not counters. A distinct token arriving during a GET schedules one trailing
 * read; an identical applied/in-flight/pending token is a no-op/same flight.
 */
export function createReviewHydrator({ fetchDetail, applyDetail } = {}) {
    const states = new Map();

    const start = (taskId, state, revision) => {
        const generation = ++state.generation;
        state.inFlightRevision = revision;
        const request = Promise.resolve()
            .then(() => fetchDetail(taskId))
            .then((detail) => applyDetail(taskId, detail))
            .then((applied) => {
                if (applied !== false && revision !== null) state.appliedRevision = revision;
                return applied;
            })
            .catch(() => false)
            .finally(() => {
                if (state.inFlight !== request || state.generation !== generation) return;
                state.inFlight = null;
                state.inFlightRevision = null;
                const trailing = state.pendingRevision;
                state.pendingRevision = null;
                if (trailing !== null && trailing !== state.appliedRevision) start(taskId, state, trailing);
            });
        state.inFlight = request;
        return request;
    };

    return {
        hydrate(taskIdValue, revisionValue = null) {
            const taskId = text(taskIdValue);
            if (!taskId) return Promise.resolve(false);
            const revision = reviewRevision(revisionValue);
            let state = states.get(taskId);
            if (!state) {
                state = {
                    appliedRevision: null,
                    inFlightRevision: null,
                    pendingRevision: null,
                    inFlight: null,
                    generation: 0,
                };
                states.set(taskId, state);
            }
            if (revision !== null && revision === state.appliedRevision) {
                return Promise.resolve(false);
            }
            if (state.inFlight) {
                if (revision !== null && revision === state.pendingRevision) {
                    return state.inFlight.then(() => state.inFlight || false);
                }
                if (
                    revision !== null
                    && revision !== state.inFlightRevision
                ) {
                    state.pendingRevision = revision;
                    return state.inFlight.then(() => state.inFlight || false);
                }
                return state.inFlight;
            }
            return start(taskId, state, revision);
        },
        invalidateApplied(taskIdValue = '') {
            const taskId = text(taskIdValue);
            if (taskId) {
                const state = states.get(taskId);
                if (state) state.appliedRevision = null;
                return;
            }
            // A full DOM rebuild discards the projection that an applied
            // revision hydrated, but it must retain and join any physical GET
            // already in flight. Reset only the applied presentation receipt.
            for (const state of states.values()) state.appliedRevision = null;
        },
        clear() {
            states.clear();
        },
    };
}

function attemptMeta(attempt) {
    return [
        attempt.timestamp,
        attempt.verdict || (attempt.lifecycleOnly
            ? (['queued', 'running'].includes(attempt.state) ? attempt.state : 'review verdict unavailable')
            : ''),
        attempt.superseded ? 'superseded' : '',
        attempt.replayed ? 'replay' : '',
        attempt.revised ? 'revised snapshot' : '',
    ].filter(Boolean).join(' · ');
}

/**
 * Return only an explicit executed receipt. Requested/effective route intent
 * is deliberately ignored: identity markup must not manufacture execution.
 */
export function reviewExecutionEvidence(execution) {
    const wrapped = execution?.executed && typeof execution.executed === 'object';
    const executed = wrapped
        ? execution.executed
        : execution;
    if (!executed || typeof executed !== 'object') return null;
    const kind = text(executed.kind || executed.route_kind || executed.channel).toLowerCase();
    const harness = text(executed.harness_id || executed.harness);
    const api = ['api', 'api_chat', 'api_model'].includes(kind);
    const harnessReceipt = kind === 'harness' || kind === 'agent_session' || (wrapped && Boolean(harness));
    if (!api && (!harnessReceipt || !harness)) return null;
    return {
        harness: api ? 'api' : harness,
        channel: api ? 'api' : '',
        label: text(executed.label),
        model: text(executed.model || executed.model_id),
    };
}

export function reviewExecutionEvidenceList(executions, legacyExecution = null) {
    const rows = normalizedExecutions(executions, legacyExecution);
    const seen = new Set();
    const evidence = [];
    for (const row of rows) {
        const normalized = reviewExecutionEvidence(row);
        if (!normalized) continue;
        const key = [normalized.harness, normalized.channel, normalized.label, normalized.model].join('\u0000');
        if (seen.has(key)) continue;
        seen.add(key);
        evidence.push(normalized);
    }
    return evidence;
}

export function reviewReferenceFromRow(row) {
    const directType = text(row?.system_type || row?.type || row?.event);
    const direct = directType === 'review_reference' ? row : null;
    const nested = row?.review_reference && typeof row.review_reference === 'object'
        ? row.review_reference
        : (row?.progress_meta?.review_reference && typeof row.progress_meta.review_reference === 'object'
            ? row.progress_meta.review_reference
            : null);
    const reference = direct || nested;
    if (!reference || text(reference.surface) !== 'plan_review') return null;
    const presentationOwnerTaskId = text(
        reference.presentation_owner_task_id || reference.task_id
        || row?.presentation_owner_task_id || row?.task_id,
    );
    if (!presentationOwnerTaskId) return null;
    return {
        surface: 'plan_review',
        presentationOwnerTaskId,
        stateRevision: text(reference.state_revision),
        reviewFingerprint: text(reference.review_fingerprint),
    };
}

export function renderReviewsSection(groupsInput, disclosure = {}) {
    const groups = orderedReviewGroups(groupsInput);
    if (!groups.length) return '';
    const expandedGroups = disclosure.expandedGroups instanceof Set ? disclosure.expandedGroups : new Set();
    const expandedAttempts = disclosure.expandedAttempts instanceof Set ? disclosure.expandedAttempts : new Set();
    const sectionExpanded = disclosure.sectionExpanded === true;
    const { groupCount, activeCount } = reviewGroupCounts(groups);
    const countText = `${groupCount}${activeCount ? ` · ${activeCount} active` : ''}`;
    const groupHtml = groups.map((group) => {
        const groupExpanded = expandedGroups.has(group.id);
        const shown = group.countIsAuthoritative ? `${group.attemptCount}` : `${group.attempts.length} shown`;
        const attempts = group.attempts.map((attempt) => {
            const attemptKey = `${group.id}:${attempt.id}`;
            const attemptExpanded = expandedAttempts.has(attemptKey);
            const skillRef = attempt.detailRef?.surface === 'skill' ? attempt.detailRef : null;
            const detailAttrs = skillRef
                ? ` data-skill-review-skill="${escapeHtmlAttr(skillRef.skill)}" data-skill-review-job="${escapeHtmlAttr(skillRef.jobId)}"`
                : '';
            const detail = skillRef ? '' : escapeHtmlText(attempt.detailText || attempt.summary || 'No additional detail projected.');
            const executions = reviewExecutionEvidenceList(attempt.executions, attempt.execution);
            const executionMarkup = executions.map((execution) => (
                harnessIdentityMarkup(execution.harness, {
                    channel: execution.channel,
                    label: execution.label,
                    className: 'chat-review-execution-identity',
                }) + (execution.model
                    ? `<span class="chat-review-execution-model">${escapeHtmlText(execution.model)}</span>`
                    : '')
            )).join('');
            const attemptInitiator = (
                attempt.initiatorTaskId
                && attempt.initiatorTaskId !== group.presentationOwnerTaskId
                && attempt.initiatorTaskId !== group.initiatorTaskId
            )
                ? `<div class="chat-review-initiator">Initiated by task ${escapeHtmlText(attempt.initiatorTaskId)}</div>`
                : '';
            return `
                <div class="chat-review-attempt ${escapeHtmlAttr(attempt.tone)}${attempt.superseded ? ' superseded' : ''}" data-review-attempt="${escapeHtmlAttr(attemptKey)}">
                    <div class="chat-review-attempt-main">
                        <span class="chat-review-attempt-label">${escapeHtmlText(attempt.label)}</span>
                        <span class="chat-review-attempt-meta">${escapeHtmlText(attemptMeta(attempt))}</span>
                        ${executionMarkup ? `<span class="chat-review-execution">${executionMarkup}</span>` : ''}
                    </div>
                    ${attemptInitiator}
                    <button type="button" class="chat-review-detail-toggle" data-review-attempt-toggle="${escapeHtmlAttr(attemptKey)}" aria-expanded="${attemptExpanded ? 'true' : 'false'}">${attemptExpanded ? 'Hide details' : 'Show details'}</button>
                    <div class="chat-review-attempt-detail" data-review-attempt-detail="${escapeHtmlAttr(attemptKey)}"${detailAttrs} aria-busy="false"${attemptExpanded ? '' : ' hidden'}>${detail}</div>
                </div>`;
        }).join('');
        const initiator = group.initiatorTaskId && group.initiatorTaskId !== group.presentationOwnerTaskId
            ? `<div class="chat-review-initiator">Initiated by task ${escapeHtmlText(group.initiatorTaskId)}</div>`
            : '';
        return `
            <div class="chat-review-group ${escapeHtmlAttr(group.tone)}" data-review-group="${escapeHtmlAttr(group.id)}">
                <button type="button" class="chat-review-group-toggle" data-review-group-toggle="${escapeHtmlAttr(group.id)}" aria-expanded="${groupExpanded ? 'true' : 'false'}">
                    <span class="chat-review-group-main">
                        <span class="chat-review-group-label">${escapeHtmlText(group.label)}</span>
                        ${group.subject ? `<span class="chat-review-subject">${escapeHtmlText(group.subject)}</span>` : ''}
                    </span>
                    <span class="chat-review-group-meta">${escapeHtmlText([group.verdict || (group.lifecycleOnly
                        ? (['queued', 'running'].includes(group.state) ? group.state : 'review verdict unavailable')
                        : group.state), shown].filter(Boolean).join(' · '))}</span>
                </button>
                <div class="chat-review-attempts"${groupExpanded ? '' : ' hidden'}>
                    <div class="chat-review-group-cost">Cost unavailable</div>
                    ${initiator}${attempts}
                </div>
            </div>`;
    }).join('');
    return `
        <section class="chat-live-reviews" data-review-section data-expanded="${sectionExpanded ? '1' : '0'}">
            <button type="button" class="chat-review-section-toggle" data-review-section-toggle aria-expanded="${sectionExpanded ? 'true' : 'false'}">
                <span>Reviews</span><span class="chat-review-section-count">${escapeHtmlText(countText)}</span>
            </button>
            <div class="chat-review-groups"${sectionExpanded ? '' : ' hidden'}>${groupHtml}</div>
        </section>`;
}

/**
 * One interactive renderer for the Reviews subsection. The owning Chat card
 * supplies durable-per-instance disclosure state and exact detail/hydration
 * callbacks; review events only call update(), never change disclosure.
 */
export function createReviewPresentationController({
    host,
    summary,
    disclosure,
    onHydrate = () => {},
    onLoadSkillDetail = () => {},
    onLayout = () => {},
} = {}) {
    const groups = new Map();
    const state = disclosure || {};
    if (!(state.expandedGroups instanceof Set)) state.expandedGroups = new Set();
    if (!(state.expandedAttempts instanceof Set)) state.expandedAttempts = new Set();
    if (state.sectionExpanded !== true) state.sectionExpanded = false;

    const focusedControl = () => {
        const active = host?.ownerDocument?.activeElement;
        if (!active || !host?.contains?.(active)) return null;
        if (active.matches?.('[data-review-section-toggle]')) return { kind: 'section', key: '' };
        if (active.dataset?.reviewGroupToggle) return { kind: 'group', key: active.dataset.reviewGroupToggle };
        if (active.dataset?.reviewAttemptToggle) return { kind: 'attempt', key: active.dataset.reviewAttemptToggle };
        const detail = active.closest?.('[data-review-attempt-detail]');
        if (detail?.dataset?.reviewAttemptDetail) {
            return { kind: 'attempt', key: detail.dataset.reviewAttemptDetail };
        }
        return null;
    };

    const restoreFocus = (focused) => {
        if (!focused) return;
        let target = null;
        if (focused.kind === 'section') target = host.querySelector?.('[data-review-section-toggle]');
        const attribute = focused.kind === 'group'
            ? 'reviewGroupToggle'
            : (focused.kind === 'attempt' ? 'reviewAttemptToggle' : '');
        if (attribute) {
            target = Array.from(host.querySelectorAll?.(`[data-${focused.kind === 'group' ? 'review-group-toggle' : 'review-attempt-toggle'}]`) || [])
                .find((candidate) => candidate.dataset?.[attribute] === focused.key);
        }
        target?.focus?.();
    };

    const render = () => {
        if (!host || !summary) return;
        const focused = focusedControl();
        const { groupCount, activeCount } = reviewGroupCounts(groups);
        summary.hidden = groupCount === 0;
        summary.textContent = groupCount
            ? `Reviews ${groupCount}${activeCount ? ` · ${activeCount} active` : ''}`
            : '';
        const reconciled = reconcileReviewMarkup(host, renderReviewsSection(groups, state));
        const active = host?.ownerDocument?.activeElement;
        if (!reconciled || !active || !host.contains?.(active)) restoreFocus(focused);
        for (const detail of host.querySelectorAll?.('[data-review-attempt-detail]') || []) {
            if (
                !detail.hidden
                && detail.dataset?.skillReviewSkill
                && detail.dataset?.skillReviewJob
            ) onLoadSkillDetail(detail);
        }
    };

    host?.addEventListener('click', (event) => {
        const retry = event.target?.closest?.('[data-skill-review-retry]');
        if (retry) {
            const detail = retry.closest?.('[data-review-attempt-detail]');
            if (detail?.dataset?.skillReviewSkill && detail?.dataset?.skillReviewJob) {
                onLoadSkillDetail(detail, { retry: true });
                detail.setAttribute?.('tabindex', '-1');
                detail.focus?.();
                onLayout();
            }
            return;
        }
        const sectionToggle = event.target?.closest?.('[data-review-section-toggle]');
        if (sectionToggle) {
            state.sectionExpanded = !state.sectionExpanded;
            render();
            if (state.sectionExpanded) onHydrate();
            onLayout();
            return;
        }
        const groupToggle = event.target?.closest?.('[data-review-group-toggle]');
        if (groupToggle) {
            const groupId = groupToggle.dataset.reviewGroupToggle || '';
            if (!groupId) return;
            if (state.expandedGroups.has(groupId)) state.expandedGroups.delete(groupId);
            else state.expandedGroups.add(groupId);
            render();
            if (state.expandedGroups.has(groupId)) onHydrate();
            onLayout();
            return;
        }
        const attemptToggle = event.target?.closest?.('[data-review-attempt-toggle]');
        if (!attemptToggle) return;
        const attemptKey = attemptToggle.dataset.reviewAttemptToggle || '';
        if (!attemptKey) return;
        const opening = !state.expandedAttempts.has(attemptKey);
        if (opening) state.expandedAttempts.add(attemptKey);
        else state.expandedAttempts.delete(attemptKey);
        render();
        onLayout();
    });

    return {
        groups,
        render,
        update(group) {
            const merged = mergeReviewGroup(groups, group);
            if (merged) render();
            return merged;
        },
        updateMany(nextGroups) {
            let changed = false;
            for (const group of Array.isArray(nextGroups) ? nextGroups : []) {
                if (mergeReviewGroup(groups, group)) changed = true;
            }
            if (changed) render();
            return changed;
        },
    };
}
