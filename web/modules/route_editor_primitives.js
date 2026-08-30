// Neutral route-editor primitives shared by Available subagents and Review
// lanes. Semantic owners keep their own schemas: reviewer rows serialize
// `api_chat` + `profile_id`; task actors serialize `api_model` +
// `credential_profile_id`.

import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

export const ROUTE_KIND_API_MODEL = 'api_model';
export const ROUTE_KIND_AGENT_SESSION = 'agent_session';
export const API_ROUTE_CHOICE = 'api';
export const EFFORT_CHOICES = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

export function mintStableId(prefix, takenIds) {
    const taken = new Set(takenIds || []);
    for (let attempt = 0; attempt < 1000; attempt += 1) {
        const candidate = `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
        if (!taken.has(candidate)) return candidate;
    }
    return `${prefix}_${Date.now().toString(36)}`;
}

export function composeSessionTarget(harness, model) {
    const h = String(harness || '').trim();
    const m = String(model || '').trim();
    return m ? `${h}=${m}` : h;
}

export function splitSessionTarget(target) {
    const raw = String(target || '');
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, model: '' };
    return { harness: raw.slice(0, eq), model: raw.slice(eq + 1) };
}

/** Effort already encoded in a Cursor/Agy compound model slug, if any. */
export function compoundSessionEffort(target) {
    const { harness, model } = splitSessionTarget(target);
    if (!['cursor', 'agy'].includes(String(harness || '')) || !model) return '';
    const compound = model.toLowerCase().endsWith('-fast') ? model.slice(0, -5) : model;
    const encoded = compound.slice(compound.lastIndexOf('-') + 1).toLowerCase();
    return EFFORT_CHOICES.includes(encoded) ? encoded : '';
}

/** Return the encoded effort only when a separate field contradicts it. */
export function compoundSessionEffortConflict(target, effort) {
    const encoded = compoundSessionEffort(target);
    const requested = String(effort || '').trim().toLowerCase();
    return encoded && requested && encoded !== requested ? encoded : '';
}

export function encodeRouteChoice(row) {
    if (row?.route?.kind === ROUTE_KIND_AGENT_SESSION) {
        return `session:${splitSessionTarget(row.route.target_id).harness}`;
    }
    return API_ROUTE_CHOICE;
}

export function decodeRouteChoice(value, { apiKind = ROUTE_KIND_API_MODEL } = {}) {
    const raw = String(value || '');
    if (raw.startsWith('session:')) {
        return { kind: ROUTE_KIND_AGENT_SESSION, harness: raw.slice('session:'.length) };
    }
    return { kind: apiKind };
}

export function normalizeRouteSpec(route, {
    apiKind = ROUTE_KIND_API_MODEL,
    apiAliases = ['api_model', 'api_chat', 'api'],
} = {}) {
    const input = route && typeof route === 'object' ? route : {};
    const kind = input.kind === ROUTE_KIND_AGENT_SESSION
        ? ROUTE_KIND_AGENT_SESSION
        : (apiAliases.includes(String(input.kind || '')) ? apiKind : String(input.kind || apiKind));
    return {
        kind,
        target_id: String(input.target_id || ''),
        credential_pin: String(input.credential_pin
            || input.credential_profile_id || input.profile_id || ''),
    };
}

export function serializeRouteSpec(route, {
    apiKind = ROUTE_KIND_API_MODEL,
    credentialField = 'credential_profile_id',
} = {}) {
    const normalized = normalizeRouteSpec(route, { apiKind });
    const out = {
        kind: normalized.kind === ROUTE_KIND_AGENT_SESSION
            ? ROUTE_KIND_AGENT_SESSION : apiKind,
        target_id: normalized.target_id,
    };
    if (out.kind === ROUTE_KIND_AGENT_SESSION && normalized.credential_pin) {
        out[credentialField] = normalized.credential_pin;
    }
    return out;
}

function undiscoveredLabel(value, known) {
    return `${value} (${known ? 'not in discovery' : 'not checked'})`;
}

export function routeChoiceGroups({
    harnesses = [], currentChoice = '', catalogKnown = true, apiLabel = 'API model',
} = {}) {
    const sessionValues = (harnesses || [])
        .filter((harness) => harness && harness.id)
        .map((harness) => ({
            value: `session:${harness.id}`,
            label: `${harness.display_name || harness.id} (agent)`,
            disabled: harness.status && harness.status !== 'ok' && !harness.enabled,
        }));
    const savedChoice = String(currentChoice || '');
    if (savedChoice.startsWith('session:')
        && !sessionValues.some((option) => option.value === savedChoice)) {
        sessionValues.push({
            value: savedChoice,
            label: undiscoveredLabel(savedChoice.slice('session:'.length), catalogKnown),
        });
    }
    return [
        { label: 'API', options: [{ value: API_ROUTE_CHOICE, label: apiLabel }] },
        sessionValues.length
            ? { label: 'Agents — subscriptions', options: sessionValues }
            : { label: 'Agents — subscriptions', options: [{
                value: '',
                disabled: true,
                label: catalogKnown
                    ? 'None available — connect one under Accounts above'
                    : 'Could not be listed — see the service banner above',
            }] },
    ];
}

export function indexProfilesByHarness(payload) {
    const byHarness = {};
    const profiles = payload?.profiles?.profiles || [];
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        const harness = String(profile.harness_id || '');
        const id = String(profile.profile_id || '');
        if (!harness || !id) continue;
        (byHarness[harness] = byHarness[harness] || []).push({
            id,
            enabled: profile.enabled !== false,
        });
    }
    return byHarness;
}

export function profileEntry(entry) {
    if (typeof entry === 'string') return { id: entry, enabled: true };
    return { id: String(entry?.id || ''), enabled: entry?.enabled !== false };
}

export function harnessModelsKnown(harness, catalogKnown = true) {
    return Boolean(catalogKnown) && !String(harness?.models_error || '');
}

export function modelsGapNote(harness, catalogKnown = true) {
    return catalogKnown && String(harness?.models_error || '')
        ? 'model list could not be read' : '';
}

export function sessionModelOptions(harness, currentModel, { catalogKnown = true } = {}) {
    const models = harness?.models || [];
    const options = [
        { value: '', label: 'Engine default model' },
        ...models.map((model) => ({
            value: String(model.id || model.value || model),
            label: String(model.id || model.label || model),
        })),
    ];
    if (currentModel && !options.some((option) => option.value === currentModel)) {
        options.push({
            value: currentModel,
            label: undiscoveredLabel(currentModel, harnessModelsKnown(harness, catalogKnown)),
        });
    }
    return options;
}

export function profileOptionsFor(profiles, savedPin, { accountsKnown = true } = {}) {
    const options = [
        { value: '', label: 'Account: automatic rotation' },
        ...(profiles || []).map(profileEntry).filter((profile) => profile.id).map((profile) => ({
            value: profile.id,
            label: `Account: ${profile.id} (pinned)${profile.enabled ? '' : ' (disabled)'}`,
        })),
    ];
    if (savedPin && !options.some((option) => option.value === savedPin)) {
        options.push({
            value: savedPin,
            label: `Account: ${undiscoveredLabel(savedPin, accountsKnown)}`,
        });
    }
    return options;
}

export function selectHtml(attrs, groups, selected) {
    const options = (groups || []).map((group) => {
        const body = (group.options || []).map((option) => {
            const isSelected = option.value === selected ? ' selected' : '';
            const disabled = option.disabled ? ' disabled' : '';
            return `<option value="${escapeHtml(option.value)}"${isSelected}${disabled}>${escapeHtml(option.label)}</option>`;
        }).join('');
        return group.label
            ? `<optgroup label="${escapeHtml(group.label)}">${body}</optgroup>` : body;
    }).join('');
    return `<select ${attrs}>${options}</select>`;
}

export function effortSelectHtml(attrs, selected, surfaceDefault = 'route default') {
    const options = [
        { value: '', label: 'Default effort' },
        ...EFFORT_CHOICES.map((effort) => ({ value: effort, label: effort })),
    ];
    return selectHtml(
        `${attrs} title="Reasoning effort — default: ${escapeHtml(surfaceDefault)}"`,
        [{ label: '', options }],
        selected || '',
    );
}

export function describeExecutionEvidence(entry) {
    if (!entry || typeof entry !== 'object') return '';
    if ('requested_model' in entry || 'applied_model' in entry) {
        const parts = [];
        const route = String(entry.route || '');
        if (route) parts.push(`${route} session`);
        // Last-actual evidence is APPLIED telemetry only. Older receipts may
        // retain the requested route while omitting what the harness actually
        // served; never dress that requested value up as execution truth.
        const model = String(entry.applied_model || '');
        if (model) parts.push(model);
        else if (entry.requested_model) parts.push('model not disclosed');
        const account = String(entry.applied_profile || '');
        if (account) parts.push(`account ${account}`);
        const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
        if (when) parts.push(when);
        return parts.join(' · ');
    }
    const effective = entry.effective || entry;
    const parts = [];
    const route = String(effective.route || effective.kind || '');
    if (route.startsWith(ROUTE_KIND_AGENT_SESSION)) {
        const harness = route.slice(ROUTE_KIND_AGENT_SESSION.length).replace(/^:/, '')
            || splitSessionTarget(effective.target_id || '').harness;
        parts.push(harness ? `${harness} session` : 'agent session');
    } else if (route) {
        parts.push('API model');
    }
    if (effective.model) parts.push(String(effective.model));
    const account = effective.credential_profile_id || effective.profile_id;
    if (account) parts.push(`account ${account}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    if (when) parts.push(when);
    return parts.join(' · ');
}
