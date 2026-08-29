// Available subagents editor shared by Settings and first-run onboarding.
// Reviewer quorum and role policy stay in reviewer_slots.js; only neutral
// route/model/account/effort presentation is shared.

import {
    FACET_ACCOUNTS,
    FACET_CATALOG,
    FACET_QUOTA,
    READ_OK,
    accountRows,
    bindStatusSurface,
    boundedStatusRefresh,
    claudexorStatus,
    familyLabel,
} from './claudexor_status_store.js';
import { renderSegmentedField } from './page_header.js';
import { harnessIdentityMarkup } from './harness_presentation.js';
import {
    EFFORT_CHOICES,
    ROUTE_KIND_AGENT_SESSION,
    ROUTE_KIND_API_MODEL,
    compoundSessionEffortConflict,
    composeSessionTarget,
    decodeRouteChoice,
    describeExecutionEvidence,
    effortSelectHtml,
    encodeRouteChoice,
    indexProfilesByHarness,
    mintStableId,
    modelsGapNote,
    profileOptionsFor,
    routeChoiceGroups,
    selectHtml,
    serializeRouteSpec,
    sessionModelOptions,
    splitSessionTarget,
} from './route_editor_primitives.js';
import { sessionRouteAvailability } from './subagent_status_primitives.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

export const MAX_AVAILABLE_SUBAGENTS = 10;
export const SUBAGENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

const SETTING_KEYS = new Set(['enabled', 'items']);
const ROW_KEYS = new Set(['subagent_id', 'name', 'recommended_use', 'route', 'effort']);
const ROUTE_KEYS = new Set(['kind', 'target_id', 'credential_profile_id']);

function ownUnknownKeys(value, allowed) {
    return Object.keys(value || {}).filter((key) => !allowed.has(key));
}

function readableName(subagentId) {
    const words = String(subagentId || '').replace(/[_-]+/g, ' ').trim();
    return words ? words.replace(/\b\w/g, (letter) => letter.toUpperCase()) : 'Subagent';
}

function canonicalRow(row) {
    const route = serializeRouteSpec(row?.route || {}, {
        apiKind: ROUTE_KIND_API_MODEL,
        credentialField: 'credential_profile_id',
    });
    route.target_id = String(route.target_id || '').trim();
    if (route.credential_profile_id !== undefined) {
        route.credential_profile_id = String(route.credential_profile_id || '').trim();
        if (!route.credential_profile_id) delete route.credential_profile_id;
    }
    return {
        subagent_id: String(row?.subagent_id || '').trim(),
        name: String(row?.name || '').trim(),
        recommended_use: String(row?.recommended_use || ''),
        route,
        ...(row?.effort ? { effort: String(row.effort).trim().toLowerCase() } : {}),
    };
}

function attachUiKeys(setting, previousItems = []) {
    const previous = new Map((previousItems || []).map(
        (row) => [String(row.subagent_id || ''), row._uiKey],
    ));
    const taken = new Set();
    setting.items = setting.items.map((row) => {
        let key = previous.get(String(row.subagent_id || '')) || '';
        if (!key || taken.has(key)) key = mintStableId('actor_row', taken);
        taken.add(key);
        return { ...row, _uiKey: key };
    });
    return setting;
}

/** Parse without replacing malformed saved bytes with an empty list. */
export function parseAvailableSubagentsSetting(value) {
    if (value === undefined || value === null || value === '') {
        return { setting: null, error: 'Available subagents configuration was not loaded' };
    }
    let input = value;
    if (typeof input === 'string') {
        try {
            input = JSON.parse(input);
        } catch (error) {
            return { setting: null, error: `saved value is not valid JSON: ${error.message || error}` };
        }
    }
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
        return { setting: null, error: 'saved value must be an object' };
    }
    const settingUnknown = ownUnknownKeys(input, SETTING_KEYS);
    if (settingUnknown.length) {
        return { setting: null, error: `saved value has unknown field: ${settingUnknown[0]}` };
    }
    if (typeof input.enabled !== 'boolean' || !Array.isArray(input.items)) {
        return { setting: null, error: 'saved value needs a boolean enabled flag and an items list' };
    }
    if (input.items.length > MAX_AVAILABLE_SUBAGENTS) {
        return { setting: null, error: `saved value has more than ${MAX_AVAILABLE_SUBAGENTS} rows` };
    }
    const canonicalItems = [];
    for (const [index, row] of input.items.entries()) {
        if (!row || typeof row !== 'object' || Array.isArray(row)) {
            return { setting: null, error: `row ${index + 1} must be an object` };
        }
        const rowUnknown = ownUnknownKeys(row, ROW_KEYS);
        if (rowUnknown.length) {
            return { setting: null, error: `row ${index + 1} has unknown field: ${rowUnknown[0]}` };
        }
        if (typeof row.subagent_id !== 'string') {
            return { setting: null, error: `row ${index + 1} stable ID must be a string` };
        }
        if (row.name !== undefined && typeof row.name !== 'string') {
            return { setting: null, error: `row ${index + 1} name must be a string` };
        }
        if (typeof row.recommended_use !== 'string') {
            return { setting: null, error: `row ${index + 1} recommended use must be a string` };
        }
        if (row.effort !== undefined && row.effort !== null && typeof row.effort !== 'string') {
            return { setting: null, error: `row ${index + 1} effort must be a string` };
        }
        if (!row.route || typeof row.route !== 'object' || Array.isArray(row.route)) {
            return { setting: null, error: `row ${index + 1} needs a route object` };
        }
        const routeUnknown = ownUnknownKeys(row.route, ROUTE_KEYS);
        if (routeUnknown.length) {
            return { setting: null, error: `row ${index + 1} route has unknown field: ${routeUnknown[0]}` };
        }
        if (typeof row.route.kind !== 'string') {
            return { setting: null, error: `row ${index + 1} route kind must be a string` };
        }
        if (typeof row.route.target_id !== 'string') {
            return { setting: null, error: `row ${index + 1} route target must be a string` };
        }
        if (row.route.credential_profile_id !== undefined
            && row.route.credential_profile_id !== null
            && typeof row.route.credential_profile_id !== 'string') {
            return { setting: null, error: `row ${index + 1} account pin must be a string` };
        }
        const routeKind = row.route.kind.trim().toLowerCase();
        if (![ROUTE_KIND_API_MODEL, ROUTE_KIND_AGENT_SESSION].includes(routeKind)) {
            return { setting: null, error: `row ${index + 1} has unsupported route kind` };
        }
        if (routeKind !== ROUTE_KIND_AGENT_SESSION
            && String(row.route.credential_profile_id || '').trim()) {
            return { setting: null, error: `row ${index + 1} has an account pin on an API route` };
        }
        canonicalItems.push(canonicalRow({
            ...row,
            route: { ...row.route, kind: routeKind },
        }));
    }
    const setting = { enabled: input.enabled, items: canonicalItems };
    const errors = validateAvailableSubagentsSetting(setting);
    if (errors.length) return { setting: null, error: `saved value is invalid: ${errors[0]}` };
    return {
        setting,
        error: '',
    };
}

export function validateAvailableSubagentsSetting(setting) {
    const errors = [];
    if (!setting || typeof setting.enabled !== 'boolean' || !Array.isArray(setting.items)) {
        return ['Available subagents configuration is not loaded.'];
    }
    if (setting.items.length > MAX_AVAILABLE_SUBAGENTS) {
        errors.push(`Available subagents supports at most ${MAX_AVAILABLE_SUBAGENTS} rows.`);
    }
    const ids = new Set();
    setting.items.forEach((row, index) => {
        const label = `Row ${index + 1}`;
        const id = String(row?.subagent_id || '').trim();
        if (!SUBAGENT_ID_PATTERN.test(id)) {
            errors.push(`${label} needs a stable ID using letters, numbers, ., _ or - (maximum 64 characters).`);
        } else if (ids.has(id)) {
            errors.push(`${label} repeats stable ID “${id}”.`);
        }
        ids.add(id);
        const route = row?.route || {};
        if (![ROUTE_KIND_API_MODEL, ROUTE_KIND_AGENT_SESSION].includes(route.kind)) {
            errors.push(`${label} must use API model or Agent session.`);
        }
        if (!String(route.target_id || '').trim()) {
            errors.push(`${label} needs a model or agent-session route.`);
        }
        if (route.kind !== ROUTE_KIND_AGENT_SESSION && route.credential_profile_id) {
            errors.push(`${label} can pin an account only for an Agent session.`);
        }
        if (route.kind === ROUTE_KIND_AGENT_SESSION) {
            const target = String(route.target_id || '');
            const parts = target.split('=');
            if (/\s|:/.test(target) || parts.length > 2
                || !SUBAGENT_ID_PATTERN.test(parts[0] || '')
                || (parts.length === 2 && !parts[1])) {
                errors.push(`${label} Agent session must use harness or harness=model without whitespace or legacy :effort.`);
            }
        }
        if (row.effort && !EFFORT_CHOICES.includes(String(row.effort))) {
            errors.push(`${label} has an unsupported reasoning effort.`);
        }
        const encodedEffort = route.kind === ROUTE_KIND_AGENT_SESSION
            ? compoundSessionEffortConflict(route.target_id, row.effort) : '';
        if (encodedEffort) {
            errors.push(`${label} effort “${row.effort}” conflicts with compound route effort “${encodedEffort}”.`);
        }
    });
    return errors;
}

export function buildAvailableSubagentsSetting(setting) {
    return {
        enabled: Boolean(setting?.enabled),
        items: (setting?.items || []).map((row) => {
            const out = canonicalRow(row);
            out.subagent_id = out.subagent_id.trim();
            out.route.target_id = out.route.target_id.trim();
            if (out.route.credential_profile_id) {
                out.route.credential_profile_id = out.route.credential_profile_id.trim();
            }
            if (out.effort) out.effort = out.effort.trim();
            if (!out.name.trim()) out.name = readableName(out.subagent_id);
            return out;
        }),
    };
}

export function subagentSettingsFingerprint(value) {
    const parsed = parseAvailableSubagentsSetting(value);
    return parsed.setting
        ? JSON.stringify(buildAvailableSubagentsSetting(parsed.setting))
        : JSON.stringify(value ?? null);
}

export function availableSubagentsSavePayload({ loaded = false, parseError = '', setting } = {}) {
    if (!loaded || parseError) return {};
    return { OUROBOROS_SUBAGENTS: buildAvailableSubagentsSetting(setting) };
}

/** Preview the current Settings draft without turning its generated actor rows into owner input. */
export function availableSubagentsPreviewPayload(settingsDraft, subscriptionsConnected) {
    const payload = {
        ...(settingsDraft || {}),
        subscriptionsConnected: Boolean(subscriptionsConnected),
    };
    delete payload.OUROBOROS_SUBAGENTS;
    return payload;
}

export function generatedPreviewCanReplace({
    dirty = false, outerDraftClean = true, parsedSetting = null,
} = {}) {
    return !dirty && outerDraftClean && Boolean(parsedSetting);
}

function diagnosticsText(diagnostics, out = []) {
    if (!diagnostics) return out;
    if (typeof diagnostics === 'string') {
        if (diagnostics.trim()) out.push(diagnostics.trim());
        return out;
    }
    if (Array.isArray(diagnostics)) {
        diagnostics.forEach((item) => diagnosticsText(item, out));
        return out;
    }
    if (typeof diagnostics !== 'object') return out;
    const message = diagnostics.message || diagnostics.detail || diagnostics.error;
    if (message) {
        const code = String(diagnostics.code || '').trim();
        out.push(`${code ? `${code}: ` : ''}${String(message)}`);
        return out;
    }
    Object.values(diagnostics).forEach((item) => diagnosticsText(item, out));
    return out;
}

function harnessMap(snapshot) {
    return Object.fromEntries((snapshot?.harnesses || [])
        .filter((harness) => harness?.id)
        .map((harness) => [String(harness.id), harness]));
}

function connectedHarnessIds(snapshot) {
    return new Set(accountRows(snapshot)
        .filter((row) => row?.enabled !== false
            && String(row?.status?.verification || '') === 'passed')
        .map((row) => String(row.harness || '')));
}

function executionFor(snapshot, subagentId) {
    const receipt = snapshot?.subagent_last_delegation;
    if (!receipt || typeof receipt !== 'object') return null;
    return String(receipt.selected_subagent_id || '') === String(subagentId || '')
        ? receipt : null;
}

function savedIntentStatus(row, state) {
    const intent = state.dirty ? 'Draft intent' : (state.baselineLabel || 'Saved intent');
    if (row.route.kind !== ROUTE_KIND_AGENT_SESSION) {
        return `${intent} · API model · availability is checked when a child starts`;
    }
    return `${intent} · ${sessionRouteAvailability(row, state)}`;
}

function focusSnapshot(host, doc) {
    const active = doc?.activeElement;
    if (!active || !host?.contains?.(active)) return null;
    const row = active.closest?.('[data-subagent-row]');
    return {
        rowId: row?.dataset?.subagentRow || '',
        field: active.dataset?.subagentField || '',
        start: typeof active.selectionStart === 'number' ? active.selectionStart : null,
        end: typeof active.selectionEnd === 'number' ? active.selectionEnd : null,
        scrollTop: host.scrollTop,
    };
}

function restoreFocus(host, saved) {
    if (!saved) return;
    const rows = host.querySelectorAll?.('[data-subagent-row]') || [];
    const row = [...rows].find((item) => item.dataset?.subagentRow === saved.rowId);
    const field = row?.querySelector?.(`[data-subagent-field="${saved.field}"]`);
    if (field?.focus) field.focus({ preventScroll: true });
    if (saved.start !== null && field?.setSelectionRange) {
        field.setSelectionRange(saved.start, saved.end);
    }
    host.scrollTop = saved.scrollTop;
}

export function availableSubagentRowMarkup(row, state, index = 0) {
    const ordinal = index + 1;
    const rowKey = row._uiKey || row.subagent_id;
    const headingId = `available-subagent-${rowKey}-heading`;
    const session = row.route.kind === ROUTE_KIND_AGENT_SESSION;
    const split = session ? splitSessionTarget(row.route.target_id) : { harness: '', model: '' };
    const harnesses = harnessMap(state.snapshot);
    const routeGroups = routeChoiceGroups({
        harnesses: state.catalogKnown ? (state.snapshot?.harnesses || []) : [],
        currentChoice: encodeRouteChoice(row),
        catalogKnown: state.catalogKnown,
    });
    const modelOptions = sessionModelOptions(harnesses[split.harness], split.model, {
        catalogKnown: state.catalogKnown,
    });
    const profileOptions = profileOptionsFor(
        (indexProfilesByHarness(state.snapshot)[split.harness]) || [],
        row.route.credential_profile_id || '',
        { accountsKnown: state.accountsKnown },
    );
    const evidence = describeExecutionEvidence(executionFor(state.snapshot, row.subagent_id));
    const gap = session ? modelsGapNote(harnesses[split.harness], state.catalogKnown) : '';
    const meta = [savedIntentStatus(row, state), gap, evidence ? `Last actual run: ${evidence}` : '']
        .filter(Boolean).join(' · ');
    const routeIdentity = session
        ? harnessIdentityMarkup(split.harness, {
            // A retained snapshot is useful for preserving the controls, but
            // its daemon-provided product name is evidence only while the
            // current catalog read is known. During a read gap the shared
            // presentation catalog supplies the safe, stable fallback.
            label: familyLabel(split.harness, state.snapshot, {
                catalogKnown: state.catalogKnown,
            }),
            className: 'available-subagent-route-identity',
        })
        : harnessIdentityMarkup('api', {
            channel: 'api',
            className: 'available-subagent-route-identity',
        });
    return `
        <article class="available-subagent-row" data-subagent-row="${escapeHtml(rowKey)}" aria-labelledby="${escapeHtml(headingId)}">
            <h4 class="available-subagent-heading" id="${escapeHtml(headingId)}">Subagent ${ordinal}</h4>
            <div class="available-subagent-route-identity-wrap">${routeIdentity}</div>
            <label class="available-subagent-purpose">Description
                <textarea data-subagent-field="recommended_use" rows="2" aria-label="Description for Subagent ${ordinal}" placeholder="When should Ouroboros choose this subagent?">${escapeHtml(row.recommended_use)}</textarea>
            </label>
            <div class="available-subagent-route">
                ${selectHtml(`data-subagent-field="route" aria-label="Type for Subagent ${ordinal}"`, routeGroups, encodeRouteChoice(row))}
                ${session
                    ? selectHtml(`data-subagent-field="model" aria-label="Agent session model for Subagent ${ordinal}"`, [{ label: '', options: modelOptions }], split.model)
                    : `<input data-subagent-field="model" list="available-subagent-api-model-catalog" value="${escapeHtml(row.route.target_id || '')}" placeholder="provider/model-id" autocomplete="off" spellcheck="false" aria-label="API model for Subagent ${ordinal}">`}
                ${session
                    ? selectHtml(`data-subagent-field="account" aria-label="Agent session account for Subagent ${ordinal}"`, [{ label: '', options: profileOptions }], row.route.credential_profile_id || '')
                    : ''}
                ${effortSelectHtml(`data-subagent-field="effort" aria-label="Reasoning effort for Subagent ${ordinal}"`, row.effort || '', 'route default')}
            </div>
            <div class="available-subagent-meta">${escapeHtml(meta)}</div>
            <div class="available-subagent-actions">
                <button type="button" class="btn btn-default" data-subagent-duplicate aria-label="Duplicate Subagent ${ordinal}">Duplicate</button>
                <button type="button" class="btn btn-default" data-subagent-remove aria-label="Remove Subagent ${ordinal}">Remove</button>
            </div>
        </article>`;
}

export function availableSubagentsRenderSignature(state, nowMs = Date.now()) {
    return JSON.stringify([
        state.loaded,
        state.parseError,
        state.setting,
        state.baselineLabel,
        state.source,
        diagnosticsText(state.diagnostics),
        state.statusError,
        state.catalogKnown,
        state.accountsKnown,
        state.quotaKnown,
        state.snapshot?.harnesses || [],
        accountRows(state.snapshot),
        state.snapshot?.quota || [],
        state.snapshot?.subagent_last_delegation || null,
        (state.setting?.items || []).map((row) => row?.route?.kind === ROUTE_KIND_AGENT_SESSION
            ? sessionRouteAvailability(row, state, nowMs) : ''),
        state.apiModels,
    ]);
}

/** One isolated editor instance; Settings keeps a singleton wrapper below. */
export function createAvailableSubagentsEditor({
    hostId = 'available-subagents-editor',
    doc = () => (typeof document === 'undefined' ? null : document),
    win = () => (typeof window === 'undefined' ? null : window),
    store = claudexorStatus,
    onChange = () => {},
    onDirtyChange = () => {},
    isOuterDraftClean = () => true,
    onGeneratedApply = () => {},
    allowUnloadedOmission = false,
    previewGenerated = null,
    baselineLabel = 'Saved intent',
} = {}) {
    const getDoc = typeof doc === 'function' ? doc : () => doc;
    const getWin = typeof win === 'function' ? win : () => win;
    const state = {
        loaded: false,
        parseError: '',
        unloadedOmissionAllowed: false,
        setting: { enabled: true, items: [] },
        source: '',
        diagnostics: [],
        dirty: false,
        baselineLabel: String(baselineLabel || 'Saved intent'),
        statusError: '',
        catalogKnown: false,
        accountsKnown: false,
        quotaKnown: false,
        snapshot: null,
        apiModels: [],
        signature: '',
        statusDisposer: null,
        catalogDisposer: null,
        previewSignature: '',
        previewGeneration: 0,
    };

    function host() {
        return getDoc()?.getElementById?.(hostId) || null;
    }

    function adoptStatus() {
        state.statusError = store?.error || '';
        state.catalogKnown = store?.facet?.(FACET_CATALOG) === READ_OK;
        state.accountsKnown = store?.facet?.(FACET_ACCOUNTS) === READ_OK;
        state.quotaKnown = store?.facet?.(FACET_QUOTA) === READ_OK;
        state.snapshot = store?.snapshot || null;
    }

    function validationErrors() {
        if (!state.loaded) {
            // An unrelated Settings save may omit a field the response did not
            // load at all. Once the response carries saved bytes or an explicit
            // migration/repair candidate, though, its parse error is actionable
            // and must block rather than masquerade as an accepted repair.
            if (state.unloadedOmissionAllowed) return [];
            return [state.parseError
                || 'Available subagents draft is still loading. Retry the preview before finishing.'];
        }
        if (state.parseError) return [state.parseError];
        return validateAvailableSubagentsSetting(state.setting);
    }

    function renderValidation() {
        const box = host()?.querySelector?.('[data-subagents-validation]');
        if (!box) return;
        const errors = validationErrors();
        box.hidden = !errors.length;
        box.textContent = errors[0] || '';
    }

    function markDirty({ structural = false } = {}) {
        if (!state.dirty) {
            state.dirty = true;
            onDirtyChange(true);
        }
        // Text inputs already paint their own value. Advancing the signature
        // here lets an unchanged late status settle skip a destructive
        // innerHTML rewrite; structural changes deliberately request one.
        state.signature = structural ? '' : availableSubagentsRenderSignature(state);
        renderValidation();
        onChange(buildAvailableSubagentsSetting(state.setting));
    }

    function bindRows(container) {
        container.querySelectorAll?.('[data-subagent-row]').forEach((rowElement) => {
            const row = state.setting.items.find(
                (item) => (item._uiKey || item.subagent_id) === rowElement.dataset.subagentRow,
            );
            if (!row) return;
            rowElement.querySelector('[data-subagent-field="recommended_use"]')?.addEventListener('input', (event) => {
                row.recommended_use = String(event.target.value || '');
                markDirty();
            });
            rowElement.querySelector('[data-subagent-field="route"]')?.addEventListener('change', (event) => {
                const decoded = decodeRouteChoice(event.target.value, { apiKind: ROUTE_KIND_API_MODEL });
                row.route = decoded.kind === ROUTE_KIND_AGENT_SESSION
                    ? { kind: ROUTE_KIND_AGENT_SESSION, target_id: decoded.harness }
                    : { kind: ROUTE_KIND_API_MODEL, target_id: '' };
                markDirty({ structural: true });
                paint();
            });
            rowElement.querySelector('[data-subagent-field="model"]')?.addEventListener(
                row.route.kind === ROUTE_KIND_AGENT_SESSION ? 'change' : 'input',
                (event) => {
                    if (row.route.kind === ROUTE_KIND_AGENT_SESSION) {
                        const { harness } = splitSessionTarget(row.route.target_id);
                        row.route.target_id = composeSessionTarget(harness, event.target.value);
                    } else {
                        row.route.target_id = String(event.target.value || '');
                    }
                    markDirty();
                },
            );
            rowElement.querySelector('[data-subagent-field="account"]')?.addEventListener('change', (event) => {
                const pin = String(event.target.value || '');
                if (pin) row.route.credential_profile_id = pin;
                else delete row.route.credential_profile_id;
                markDirty({ structural: true });
                paint();
            });
            rowElement.querySelector('[data-subagent-field="effort"]')?.addEventListener('change', (event) => {
                const effort = String(event.target.value || '');
                if (effort) row.effort = effort;
                else delete row.effort;
                markDirty();
            });
            rowElement.querySelector('[data-subagent-duplicate]')?.addEventListener('click', () => {
                if (state.setting.items.length >= MAX_AVAILABLE_SUBAGENTS) return;
                const copy = canonicalRow(row);
                copy.subagent_id = mintStableId(`${row.subagent_id || 'subagent'}_copy`,
                    state.setting.items.map((item) => item.subagent_id));
                copy.name = `${row.name || readableName(row.subagent_id)} copy`;
                copy._uiKey = mintStableId('actor_row',
                    state.setting.items.map((item) => item._uiKey));
                state.setting.items.splice(state.setting.items.indexOf(row) + 1, 0, copy);
                markDirty({ structural: true });
                paint();
            });
            rowElement.querySelector('[data-subagent-remove]')?.addEventListener('click', () => {
                const index = state.setting.items.indexOf(row);
                if (index >= 0) state.setting.items.splice(index, 1);
                markDirty({ structural: true });
                paint();
            });
        });
    }

    function paint() {
        const container = host();
        if (!container) return false;
        const nextSignature = availableSubagentsRenderSignature(state);
        if (nextSignature === state.signature) return false;
        const focused = focusSnapshot(container, getDoc());
        state.signature = nextSignature;
        const errors = validationErrors();
        const diagnostics = diagnosticsText(state.diagnostics);
        const source = state.source ? `Source: ${state.source}.` : '';
        const readProblem = state.statusError
            ? 'Live agent availability could not be read. Saved rows remain unchanged.' : '';
        container.innerHTML = `
            <div class="available-subagents-toolbar">
                <label class="local-toggle">
                    <input type="checkbox" data-subagents-enabled ${state.setting.enabled ? 'checked' : ''} ${state.loaded ? '' : 'disabled'}>
                    Enabled
                </label>
                <span class="available-subagents-count">${state.setting.items.length}/${MAX_AVAILABLE_SUBAGENTS}</span>
                <button type="button" class="btn btn-default" data-subagent-add
                    ${!state.loaded || state.setting.items.length >= MAX_AVAILABLE_SUBAGENTS ? 'disabled' : ''}>Add subagent</button>
            </div>
            <div class="available-subagents-source">${escapeHtml([source, readProblem].filter(Boolean).join(' '))}</div>
            <div data-subagents-diagnostics class="available-subagents-diagnostics" ${diagnostics.length ? '' : 'hidden'}>${escapeHtml(diagnostics.join(' · '))}</div>
            <div data-subagents-validation class="available-subagents-diagnostics" data-tone="error" ${errors.length ? '' : 'hidden'}>${escapeHtml(errors[0] || '')}</div>
            <div class="available-subagents-list">
                ${state.loaded
                    ? state.setting.items.map((row, index) => availableSubagentRowMarkup(row, state, index)).join('')
                        || '<div class="available-subagents-empty">No subagents configured. Add one, or leave the list empty to make no actors available.</div>'
                    : '<div class="available-subagents-empty">The saved configuration could not be loaded, so this editor will not replace it.</div>'}
            </div>
            <datalist id="available-subagent-api-model-catalog">
                ${state.apiModels.map((model) => `<option value="${escapeHtml(model)}"></option>`).join('')}
            </datalist>`;
        container.querySelector('[data-subagents-enabled]')?.addEventListener('change', (event) => {
            state.setting.enabled = Boolean(event.target.checked);
            markDirty();
        });
        container.querySelector('[data-subagent-add]')?.addEventListener('click', () => {
            if (state.setting.items.length >= MAX_AVAILABLE_SUBAGENTS) return;
            const id = mintStableId('subagent', state.setting.items.map((row) => row.subagent_id));
            state.setting.items.push({
                subagent_id: id,
                name: readableName(id),
                recommended_use: '',
                route: { kind: ROUTE_KIND_API_MODEL, target_id: '' },
                _uiKey: mintStableId('actor_row',
                    state.setting.items.map((row) => row._uiKey)),
            });
            markDirty({ structural: true });
            paint();
        });
        bindRows(container);
        restoreFocus(container, focused);
        return true;
    }

    function load(value, { source = '', diagnostics = [], allowOmission = false } = {}) {
        // Invalidate a preview launched for the previous settings document.
        // A late response must never overwrite a freshly loaded configured row.
        state.previewGeneration += 1;
        state.previewSignature = '';
        const parsed = parseAvailableSubagentsSetting(value);
        state.loaded = Boolean(parsed.setting);
        state.parseError = parsed.error;
        state.unloadedOmissionAllowed = Boolean(
            allowUnloadedOmission && allowOmission && !parsed.setting,
        );
        if (parsed.setting) state.setting = attachUiKeys(parsed.setting, state.setting.items);
        state.source = String(source || '');
        state.diagnostics = diagnostics;
        state.dirty = false;
        state.signature = '';
        onDirtyChange(false);
        paint();
        return { loaded: state.loaded, error: state.parseError };
    }

    function applyGeneratedPreview(response) {
        const parsed = parseAvailableSubagentsSetting(response?.available_subagents);
        state.source = String(response?.source || state.source || 'onboarding_default');
        state.diagnostics = response?.diagnostics || [];
        let outerDraftClean = false;
        try {
            outerDraftClean = Boolean(isOuterDraftClean());
        } catch (error) {
            outerDraftClean = false;
        }
        const canApply = generatedPreviewCanReplace({
            dirty: state.dirty,
            outerDraftClean,
            parsedSetting: parsed.setting,
        });
        if (canApply) {
            state.loaded = true;
            state.parseError = '';
            state.setting = attachUiKeys(parsed.setting, state.setting.items);
            onDirtyChange(false);
            onGeneratedApply(buildAvailableSubagentsSetting(state.setting));
        } else if (!parsed.setting && !state.loaded) {
            state.parseError = parsed.error;
        }
        state.signature = '';
        paint();
        return { applied: canApply, error: parsed.error };
    }

    function setPreviewFailure(error) {
        const message = String(
            error?.body?.detail || error?.body?.error || error?.message || error,
        );
        const code = String(error?.body?.code || '').trim();
        state.diagnostics = [
            `${code ? `${code}: ` : ''}${message}`,
            ...diagnosticsText(error?.body?.diagnostics),
        ];
        if (!state.loaded) {
            state.parseError = `Available subagents preview failed: ${state.diagnostics.join(' · ')}`;
        }
        state.signature = '';
        paint();
    }

    async function reloadStatus() {
        await boundedStatusRefresh(store);
        adoptStatus();
        paint();
        // Generated rows are enrichment, never a second unbounded gate on the
        // Settings critical path. The response is generation- and clean-gated.
        void maybeRefreshGeneratedPreview({ force: true });
    }

    async function maybeRefreshGeneratedPreview({ force = false } = {}) {
        if (typeof previewGenerated !== 'function' || state.source !== 'undecided' || state.dirty) {
            return false;
        }
        let outerDraftClean = false;
        try {
            outerDraftClean = Boolean(isOuterDraftClean());
        } catch (error) {
            outerDraftClean = false;
        }
        if (!outerDraftClean) return false;
        const connected = state.accountsKnown
            ? [...connectedHarnessIds(state.snapshot)].sort()
            : [];
        const signature = JSON.stringify([state.accountsKnown, connected]);
        if (!force && signature === state.previewSignature) return false;
        state.previewSignature = signature;
        const generation = ++state.previewGeneration;
        try {
            const response = await previewGenerated({
                subscriptionsConnected: state.accountsKnown && connected.length > 0,
            });
            if (generation !== state.previewGeneration) return false;
            // This is still the unsaved migration/default candidate.  Preserve
            // that provenance so a later clean account-status change may
            // refresh it again; onboarding editors keep the endpoint source.
            const result = applyGeneratedPreview({ ...response, source: state.source });
            if (!result.applied) state.previewSignature = '';
            return result.applied;
        } catch (error) {
            if (generation !== state.previewGeneration) return false;
            setPreviewFailure(error);
            return false;
        }
    }

    function mount({ bindStatus = true } = {}) {
        adoptStatus();
        if (bindStatus && !state.statusDisposer) {
            state.statusDisposer = bindStatusSurface(store, {
                elementId: hostId,
                includeModels: true,
                doc: getDoc,
                win: getWin,
                listener: () => {
                    adoptStatus();
                    paint();
                    void maybeRefreshGeneratedPreview();
                },
            });
        }
        if (!state.catalogDisposer) {
            const target = getDoc();
            const onCatalog = (event) => {
                state.apiModels = (event?.detail?.items || [])
                    .map((item) => String(item.value || item.id || ''))
                    .filter(Boolean);
                state.signature = '';
                paint();
            };
            target?.addEventListener?.('settings-model-catalog:updated', onCatalog);
            state.catalogDisposer = () => target?.removeEventListener?.('settings-model-catalog:updated', onCatalog);
        }
        state.signature = '';
        paint();
    }

    function destroy() {
        state.statusDisposer?.();
        state.catalogDisposer?.();
        state.statusDisposer = null;
        state.catalogDisposer = null;
    }

    return {
        mount,
        destroy,
        load,
        paint,
        reloadStatus,
        refreshGeneratedPreview: maybeRefreshGeneratedPreview,
        applyGeneratedPreview,
        setPreviewFailure,
        validate: validationErrors,
        collect: () => availableSubagentsSavePayload(state),
        get setting() { return buildAvailableSubagentsSetting(state.setting); },
        get loaded() { return state.loaded; },
        get dirty() { return state.dirty; },
        get parseError() { return state.parseError; },
    };
}

export function availableSubagentsEditorHost(hostId = 'available-subagents-editor') {
    return `<div id="${escapeHtml(hostId)}" class="available-subagents-editor">
        <div class="available-subagents-empty">Loading Available subagents…</div>
    </div>`;
}

export function renderSubagentsSection() {
    return `
        <div class="form-section" id="subagents-section">
            <h3>Available subagents</h3>
            <div class="settings-section-copy">
                Describe when Ouroboros should choose each numbered subagent, then select how it runs.
                Internal references stay stable automatically. A route that is unavailable stays saved
                and returns an explicit refusal instead of silently changing actor or model. An unpinned
                session row may rotate among compatible healthy accounts for that same route.
            </div>
            ${availableSubagentsEditorHost()}
            <div class="settings-effort-card">
                <label>Allow mutative subagents</label>
                <input id="s-allow-mutative-subagents" type="hidden" value="on">
                ${renderSegmentedField({
                    target: 's-allow-mutative-subagents',
                    title: 'Applies on the next task; no restart required.',
                    options: [
                        { value: 'off', label: 'Off' },
                        { value: 'auto', label: 'Auto' },
                        { value: 'on', label: 'On' },
                    ],
                })}
                <div class="settings-inline-note">
                    Whether a subagent may write in an isolated worktree, external workspace, or
                    from-scratch project. Read-only subagents remain available. Auto follows runtime
                    mode; this applies to new child tasks without a restart.
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Active subagents per root</label>
                    <input id="s-active-subagents" type="number" min="1" max="500" value="6">
                    <div class="settings-inline-note">How many children one root task may run at once.</div>
                </div>
                <div class="form-field">
                    <label>Subagent depth</label>
                    <input id="s-subagent-depth" type="number" min="0" max="10" value="3">
                    <div class="settings-inline-note">How deep the chain may nest. <code>0</code> turns delegation off entirely.</div>
                </div>
            </div>
            <details class="settings-subsection" id="delegation-advanced">
                <summary>Advanced — where subagents check out their work</summary>
                <div class="settings-subsection-body">
                    <div class="form-grid two">
                        <div class="form-field">
                            <label>Subagent worktree root</label>
                            <input id="s-subagent-worktree-root" type="text" placeholder="~/Ouroboros/subagent_worktrees">
                        </div>
                        <div class="form-field">
                            <label>Subagent projects root (genesis)</label>
                            <input id="s-subagent-projects-root" type="text" placeholder="~/Ouroboros/projects">
                        </div>
                    </div>
                    <div class="settings-inline-note">
                        Leave either root blank for its default under <code>~/Ouroboros/</code>.
                        Genesis projects are durable; worktrees follow the GC retention setting.
                    </div>
                </div>
            </details>
        </div>`;
}

let settingsEditor = null;

function settingsSource(settings) {
    return String(settings?._meta?.available_subagents?.source
        || settings?._meta?.available_subagents_source
        || settings?.OUROBOROS_SUBAGENTS_SOURCE
        || 'configured');
}

export function availableSubagentsLoadValue(settings) {
    const raw = settings?.OUROBOROS_SUBAGENTS;
    if (raw !== undefined && raw !== null && raw !== '') return raw;
    return settings?._meta?.available_subagents?.candidate ?? raw;
}

/** Whether this response carries owner or repair bytes that must be fixed in-place. */
export function availableSubagentsHasExplicitDraft(settings) {
    const raw = settings?.OUROBOROS_SUBAGENTS;
    if (raw !== undefined && raw !== null && raw !== '') return true;
    const meta = settings?._meta?.available_subagents;
    return meta != null
        && Object.prototype.hasOwnProperty.call(meta, 'candidate')
        && meta.candidate !== undefined
        && meta.candidate !== null
        && meta.candidate !== '';
}

export function initSubagentsSection({
    onChange,
    isOuterDraftClean,
    onGeneratedApply,
    previewGenerated = null,
    store = claudexorStatus,
} = {}) {
    destroySubagentsSection();
    settingsEditor = createAvailableSubagentsEditor({
        store,
        onChange: typeof onChange === 'function' ? onChange : () => {},
        isOuterDraftClean: typeof isOuterDraftClean === 'function'
            ? isOuterDraftClean : () => true,
        onGeneratedApply: typeof onGeneratedApply === 'function'
            ? onGeneratedApply : () => {},
        allowUnloadedOmission: true,
        previewGenerated,
    });
    settingsEditor.mount();
}

export function applySubagentsSettings(settings) {
    if (!settingsEditor) return;
    const meta = settings?._meta?.available_subagents || {};
    settingsEditor.load(availableSubagentsLoadValue(settings), {
        source: settingsSource(settings),
        diagnostics: meta.diagnostics || meta.diagnostic || [],
        allowOmission: !availableSubagentsHasExplicitDraft(settings),
    });
}

export async function reloadSubagentsSection() {
    await settingsEditor?.reloadStatus();
}

export function destroySubagentsSection() {
    settingsEditor?.destroy();
    settingsEditor = null;
}

export function collectSubagentsSettings() {
    return settingsEditor?.collect() || {};
}

export function validateSubagentsDraft() {
    return settingsEditor?.validate() || ['Available subagents editor is not loaded.'];
}

// Compatibility name retained for focused callers; the signature now covers
// the actor list rather than the retired singleton route.
export const renderSignature = availableSubagentsRenderSignature;
