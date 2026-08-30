// Review lanes UI (phase 6.2/6.3, revised per owner finding #6) — the rows in
// Agents → Review lanes over the ONE structured setting
// (OUROBOROS_REVIEWER_SLOTS; the settings key keeps its name, the section does
// not — D-10 moved these rows out of the Models tab and renamed them).
//
// Shape rules are the owner's:
//  * ONE flat picker per row (decision 1=B replaced the earlier two-level
//    source+route pair): the roster rows from Available subagents lead as
//    references, then the inline channels — "API model" plus one entry per
//    login-capable harness — never the full flat model catalog (hundreds of
//    options made the list unusable, finding #6). A referenced row's route/
//    model/effort/account are shown as READ-ONLY derived facts (the roster
//    stays their SSOT); the stored forms are mutually exclusive:
//    {slot_id, route, effort} XOR {slot_id, subagent_id, effort}.
//  * On the API route the model id is a FREE-TEXT input with a datalist of
//    catalog suggestions — the same catalog-assisted entry the model cards
//    use. On a harness route the MODEL is a dropdown fed by Claudexor
//    discovery. No invalid combinations can be composed.
//  * The provider shown for a delegated row is the HARNESS NAME (codex,
//    claude, cursor, …) — never "Claudexor", and never a `provider::model`
//    string syntax.
//  * Effort is the EXISTING per-model effort mechanism, one dropdown per row.
// Capability badges DISPLAY facts; they configure nothing.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { bindStatusSurface, boundedStatusRefresh, claudexorStatus } from './claudexor_status_store.js';
import { harnessIdentityMarkup } from './harness_presentation.js';
import { formatRelativeAge } from './ui_helpers.js';
import * as routeEditor from './route_editor_primitives.js';
import {
    availableSubagentsLoadValue,
    parseAvailableSubagentsSetting,
} from './subagents_settings.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

export const ROUTE_KIND_API = 'api_chat';
export const ROUTE_KIND_SESSION = routeEditor.ROUTE_KIND_AGENT_SESSION;

// Select-value prefix marking a configured-subagent reference in the one
// flat reviewer picker (decision 1=B); everything else is a route choice.
export const SUBAGENT_CHOICE_PREFIX = 'subagent:';
// The route select's single API entry. The target model id never lives in
// the select — display always matches the stored target (finding #6c: a
// fresh row used to DISPLAY the first catalog model while storing '').
export const API_ROUTE_CHOICE = routeEditor.API_ROUTE_CHOICE;

export const EFFORT_CHOICES = routeEditor.EFFORT_CHOICES;

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function mintSlotId(prefix, takenIds) {
    return routeEditor.mintStableId(prefix, takenIds);
}

export function encodeRouteChoice(row) {
    return routeEditor.encodeRouteChoice(row);
}

export function decodeRouteChoice(value) {
    return routeEditor.decodeRouteChoice(value, { apiKind: ROUTE_KIND_API });
}

// Claudexor's own reviewer-panel spelling: harness[=model]. Never '::'.
export function composeSessionTarget(harness, model) {
    return routeEditor.composeSessionTarget(harness, model);
}

export function splitSessionTarget(target) {
    return routeEditor.splitSessionTarget(target);
}

// `catalogKnown` / `accountsKnown` = the matching facet of the status payload
// was actually READ (claudexor_status_store.facetReadState). Only then does the
// absence of a harness/model/account mean anything about that harness/model/
// account. With the daemon stopped, unreachable, or that one read refused, the
// backend answers `harnesses: []` by construction, and this UI used to decorate
// EVERY saved row with "(not in discovery)" — an accusation nobody had earned,
// and the exact screen the owner reported (2026-08-08). The saved value keeps
// its option either way (that guard is what stops a Save from erasing a pin);
// only the LABEL's claim follows the facet: "(not in discovery)" is a VERDICT —
// it says we looked and did not find it — and is licensed ONLY by a facet that
// was read. An unread facet says "(not checked)": we did not look, and the
// owner's saved value is almost certainly still there. The two facets are
// INDEPENDENT: a failed account read must not silence the catalog's own honest
// verdict.
export function routeChoiceGroups({ harnesses = [], currentChoice = '', catalogKnown = true, apiLabel } = {}) {
    return routeEditor.routeChoiceGroups({ harnesses, currentChoice, catalogKnown, apiLabel });
}

export function indexProfilesByHarness(payload) {
    return routeEditor.indexProfilesByHarness(payload);
}

// One index entry, whichever spelling it arrived in: the index emits
// `{id, enabled}` objects; older call sites and tests still hand plain id
// strings, which read as enabled (the same fail-open rule as the index).
function profileEntry(entry) {
    return routeEditor.profileEntry(entry);
}

export function buildReviewerSlotsSetting(state) {
    const rowOut = (row) => {
        // The two stored forms are mutually exclusive: a configured-subagent
        // reference never duplicates route knobs (the roster row is their
        // SSOT), and an empty explicit effort is OMITTED so the roster row's
        // own effort keeps deciding.
        if (row.subagent_id) {
            const out = {
                slot_id: String(row.slot_id || ''),
                subagent_id: String(row.subagent_id),
            };
            if (row.effort) out.effort = String(row.effort);
            return out;
        }
        const out = {
            slot_id: String(row.slot_id || ''),
            route: routeEditor.serializeRouteSpec(row.route, {
                apiKind: ROUTE_KIND_API,
                credentialField: 'profile_id',
            }),
        };
        if (row.effort) out.effort = String(row.effort);
        return out;
    };
    const advisory = state.advisory || {};
    let advisoryOut;
    if (advisory.subagent_id) {
        advisoryOut = {
            enabled: advisory.enabled !== false,
            subagent_id: String(advisory.subagent_id),
        };
        if (advisory.effort) advisoryOut.effort = String(advisory.effort);
    } else {
        // The advisory row is on the SHARED closed route vocabulary now:
        // api_chat (routed catalog model, bounded native inspection episode)
        // or agent_session. The retired legacy kind 'api' (Claude-SDK
        // spellings) still parses server-side but is never WRITTEN again.
        advisoryOut = {
            enabled: advisory.enabled !== false,
            route: { kind: advisory.route?.kind === ROUTE_KIND_SESSION ? ROUTE_KIND_SESSION : ROUTE_KIND_API,
                     target_id: String(advisory.route?.target_id || '') },
            effort: advisory.route?.kind === ROUTE_KIND_SESSION
                ? String(advisory.effort || '') : (advisory.effort || 'low'),
        };
        if (advisoryOut.route.kind === ROUTE_KIND_SESSION && advisory.route?.profile_id) {
            advisoryOut.route.profile_id = String(advisory.route.profile_id);
        }
    }
    return JSON.stringify({
        triad: (state.triad || []).map(rowOut),
        scope: (state.scope || []).map(rowOut),
        advisory: advisoryOut,
    });
}

// ---------------------------------------------------------------------------
// Configured-subagent references (decision 5A). The roster list reuses the
// Available-subagents data source: settings.js hands the loaded settings
// document to adoptSubagentRoster below, parsed by the SAME parser that
// section uses — never a second schema.
// ---------------------------------------------------------------------------

export function encodeReviewerChoice(row) {
    return row?.subagent_id
        ? SUBAGENT_CHOICE_PREFIX + String(row.subagent_id)
        : encodeRouteChoice(row);
}

export function reviewerChoiceGroups({
    roster = [], rosterKnown = true, row = {}, harnesses = [],
    catalogKnown = true, apiLabel,
} = {}) {
    // Decision 1=B: ONE flat picker — the Available-subagents references lead
    // (facts-first labels, decision 2=A), then the inline channels. A saved
    // reference that fell out of the roster keeps its option (the same
    // survive-the-save rule the roster select carried); an inline row passes
    // its own choice down so an undiscovered harness stays displayable too.
    const groups = [];
    const savedId = String(row?.subagent_id || '');
    const rosterOptions = subagentOptionsFor(roster, savedId, { rosterKnown })
        .map((option) => ({ ...option, value: SUBAGENT_CHOICE_PREFIX + option.value }));
    if (rosterOptions.length) {
        groups.push({ label: 'Available subagents', options: rosterOptions });
    }
    const currentChoice = savedId ? '' : encodeRouteChoice(row);
    groups.push(...routeChoiceGroups({ harnesses, currentChoice, catalogKnown, apiLabel }));
    return groups;
}

export function subagentOptionLabel(row) {
    const route = row?.route || {};
    const parts = [`#${String(row?.subagent_id || '')}`];
    if (route.kind === ROUTE_KIND_SESSION) {
        const split = splitSessionTarget(route.target_id);
        parts.push(split.harness || 'agent session');
        if (split.model) parts.push(split.model);
    } else {
        parts.push('API');
        if (route.target_id) parts.push(route.target_id);
    }
    if (row?.effort) parts.push(row.effort);
    // Free text is a caption, never identity: one line, bounded, with the
    // characters that could visually reorder or break the facts stripped
    // (bidi controls, newlines).
    const use = String(row?.recommended_use || '')
        .replace(/[\u202A-\u202E\u2066-\u2069\u200E\u200F\u061C]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    // Code POINTS, not UTF-16 units: a slice must never split a surrogate pair.
    const points = Array.from(use);
    const hint = points.length > 48 ? `${points.slice(0, 45).join('')}…` : use;
    return parts.join(' · ') + (hint ? ` — ${hint}` : '');
}

export function subagentOptionsFor(roster, savedId, { rosterKnown = true } = {}) {
    // Same survive-the-save rule as profileOptionsFor: the select's value must
    // EXIST as an option, or the browser silently redraws the row as the first
    // roster entry and the next Save really rewires the reviewer. And the
    // absence claim follows provenance: only a roster that was actually READ
    // may say a saved reference is not in it.
    // Label contract (owner decision 2=A): DERIVED FACTS lead — channel first,
    // then target and effort — so the delivery is visible BEFORE selection and
    // free-text intent can never disguise it; the description is a trimmed,
    // sanitized single-line caption after the facts.
    const options = (roster || []).map((row) => ({
        value: String(row.subagent_id || ''),
        label: subagentOptionLabel(row),
    }));
    const saved = String(savedId || '');
    if (saved && !options.some((option) => option.value === saved)) {
        options.push({
            value: saved,
            label: `${saved} (${rosterKnown ? 'not in the roster' : 'not checked'})`,
        });
    }
    return options;
}

export function describeSubagentReference(subagentId, roster, { rosterKnown = true } = {}) {
    // The DERIVED facts, disclosed read-only (never editable knobs): the
    // roster row is the SSOT for a referenced reviewer's route/model/effort/
    // account, so this line only reports what that row says.
    const row = (roster || []).find(
        (item) => String(item.subagent_id || '') === String(subagentId || ''));
    if (!row) {
        return rosterKnown
            ? 'Configured subagent — no roster row with this ID exists; the saved reference is kept and the review will refuse rather than reroute'
            : 'Configured subagent — the roster could not be read, so its route is not shown; the saved reference is unchanged';
    }
    const route = row.route || {};
    const parts = [];
    if (route.kind === ROUTE_KIND_SESSION) {
        const split = splitSessionTarget(route.target_id);
        parts.push(split.harness ? `${split.harness} session` : 'agent session');
        if (split.model) parts.push(split.model);
        if (route.credential_profile_id) parts.push(`account ${route.credential_profile_id}`);
    } else {
        parts.push(route.target_id ? `API model ${route.target_id}` : 'API model (unset)');
    }
    if (row.effort) parts.push(`effort ${row.effort}`);
    return `Runs as ${parts.join(' · ')} — from its roster row under Available subagents`;
}

export function describeLastExecution(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const effective = entry.effective || {};
    const parts = [];
    // APPLIED facts only: a session run whose telemetry predates the engine
    // receipt shows honest absence, never the requested value as applied.
    // One quiet line (owner feedback): the route is spelled only when it says
    // more than the row's own delivery badge (which agent really ran),
    // and the timestamp is humanized — the raw route + ISO instant stay
    // recoverable in the line's tooltip (lastRunMetaTitle).
    const route = String(effective.route || '');
    if (route.startsWith('agent_session')) {
        const harness = route.slice('agent_session'.length).replace(/^:/, '');
        parts.push(harness ? `${harness} session` : 'agent session');
        parts.push(effective.model || 'model not disclosed');
    } else if (effective.model) {
        parts.push(effective.model);
    }
    if (effective.profile_id) parts.push(`account ${effective.profile_id}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    // No applied effort is rendered: none exists upstream, so the key is not emitted.
    if (effective.verdict_method && effective.verdict_method !== 'structured'
        && effective.verdict_method !== 'strict_parse') {
        parts.push(`verdict via ${effective.verdict_method.replace(/_/g, ' ')}`);
    }
    const deltas = Array.isArray(entry.capability_delta) ? entry.capability_delta.length : 0;
    if (deltas) parts.push(`${deltas} capability delta${deltas === 1 ? '' : 's'} disclosed`);
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    if (when) parts.push(when);
    return parts.join(' · ');
}

function lastRunMetaTitle(entry) {
    // The tooltip keeps the raw facts the visible line compresses away.
    const route = String(entry?.effective?.route || 'api_chat');
    const ts = String(entry?.ts || '');
    return `UI projection of capability_delta (D22) — ran as ${route}${ts ? ` at ${ts}` : ''}`;
}

export function harnessModelsKnown(harness, catalogKnown = true) {
    // Discovery is TWO reads, not one. The catalog read can land — daemon
    // globally `running`, `catalogKnown` true — while THIS harness's model
    // list refuses: the endpoint answers `models: []` with a typed
    // `models_error` for exactly that harness (claudexor_accounts.py, the
    // per-harness `harness_models` probe). Reading the empty list as discovery
    // then labelled a saved model "(not in discovery)", which claims a
    // successful discovery proved its absence — while no discovery happened.
    return routeEditor.harnessModelsKnown(harness, catalogKnown);
}

export function modelsGapNote(harness, catalogKnown = true) {
    // The typed gap, SAID rather than left as a silently short list. Only when
    // the catalog itself was read: with the catalog unread the section note
    // above already explains everything, and this would be a second sentence
    // for the same silence.
    return routeEditor.modelsGapNote(harness, catalogKnown);
}

export function sessionModelOptions(harness, currentModel, { catalogKnown = true } = {}) {
    // The ONE model-options fragment for a session row's model select (triad,
    // scope, advisory, and the Subagents section import it too). "Engine
    // default model" is the empty tail; a SAVED model discovery no longer
    // lists keeps a "(not in discovery)" option, or the browser silently
    // redraws the select as the first entry and the next Save erases the pin
    // (same rule as profileOptionsFor / routeChoiceGroups).
    return routeEditor.sessionModelOptions(harness, currentModel, { catalogKnown });
}

export function profileOptionsFor(profiles, savedPin, { accountsKnown = true } = {}) {
    // Mirrors the model list's own rule: a SAVED pin the daemon no longer discovers
    // (account signed out, daemon down, profile renamed) matched no option, so the
    // select fell back to its first entry and redrew the row as "automatic rotation".
    // The pin only LOOKED gone — until the owner saved the panel, which then really
    // did delete it, silently widening which account the reviewer may spend.
    // A DISABLED account stays selectable, labeled "(disabled)": the engine's
    // typed refusal is the authority on whether a pinned run may use it
    // (D-U6), so the label is honesty, not a gate — hiding or greying the
    // option would be a second, client-side gate the design rejected.
    return routeEditor.profileOptionsFor(profiles, savedPin, { accountsKnown });
}

export function pinnedAccountWarning({ triad = [], scope = [], advisory = null,
                                       profilesByHarness = {}, accountsKnown = false } = {}) {
    // A removed (or signed-out) account must not silently reroute the row that
    // pinned it. `profileOptionsFor` already keeps such a pin selectable, so
    // the row itself never changes under the owner — this is the ONE actionable
    // sentence that says why it now reads "(not in discovery)".
    //
    // Only an ACTUALLY READ account list licenses the claim (BIBLE P1): with
    // the accounts facet unread every pin would look missing, and the tab's
    // service banner is already saying nobody could be asked.
    if (!accountsKnown) return '';
    const missing = [];
    const rows = [...triad, ...scope, ...(advisory ? [{ ...advisory, slot_id: 'advisory' }] : [])];
    for (const row of rows) {
        // A configured-subagent reference carries no pin of its own — the
        // roster row is that route's SSOT, checked on its own surface.
        if (row?.subagent_id) continue;
        const route = row?.route || {};
        if (route.kind !== ROUTE_KIND_SESSION) continue;
        const pin = String(route.profile_id || '');
        if (!pin) continue;
        const harness = splitSessionTarget(route.target_id).harness;
        if ((profilesByHarness[harness] || []).some((entry) => profileEntry(entry).id === pin)) continue;
        const label = `${harness} · ${pin}`;
        if (!missing.includes(label)) missing.push(label);
    }
    if (!missing.length) return '';
    return `${missing.length === 1 ? 'A review row is' : `${missing.length} review rows are`} `
        + `pinned to an account the agent service no longer lists (${missing.join(', ')}). `
        + 'Those rows are shown as-is and will refuse rather than reroute — pick another '
        + 'account or automatic rotation below, or sign that account back in under Accounts.';
}

export function capabilityBadge(row, harnessesById, { catalogKnown = true } = {}) {
    // DISPLAY-only facts: never a control (6.2).
    if (row.route.kind === ROUTE_KIND_SESSION) {
        // "route not discovered" is a claim about the ROUTE; with no successful
        // discovery it is a claim about the read. Say nothing rather than
        // something false — the one section note above already says why.
        if (!catalogKnown) return 'agent session — retrieves context with its own tools';
        const harness = harnessesById?.[splitSessionTarget(row.route.target_id).harness];
        const status = harness ? (harness.status || 'unknown') : 'not discovered';
        return `agent session — retrieves context with its own tools · route ${status}`;
    }
    return 'API delivery';
}

export function advisoryRouteTransition(prev, decoded, memory = {}) {
    // Route-kind switching must not WIPE a stored target (finding #7c): the
    // old handler wrote target_id:'' on every change, so flipping to a
    // session and back — or just touching the select — silently discarded the
    // saved advisory target on the next Save. Each kind remembers the last
    // route it held; a kind the user returns to is restored, and the stored
    // route only changes when the user actually picks something else.
    const current = (prev && typeof prev === 'object' && prev.kind)
        ? prev : { kind: ROUTE_KIND_API, target_id: '' };
    const next = { ...memory };
    if (decoded.kind === ROUTE_KIND_SESSION) {
        const prevHarness = current.kind === ROUTE_KIND_SESSION
            ? splitSessionTarget(current.target_id).harness : '';
        if (prevHarness === decoded.harness) return { route: current, memory: next };
        if (current.kind === ROUTE_KIND_SESSION) next.session = { ...current };
        else next.api = { ...current };
        const stash = next.session;
        const route = (stash && splitSessionTarget(stash.target_id).harness === decoded.harness)
            ? { ...stash }
            : { kind: ROUTE_KIND_SESSION, target_id: decoded.harness };
        return { route, memory: next };
    }
    if (current.kind !== ROUTE_KIND_SESSION) return { route: current, memory: next };
    next.session = { ...current };
    return {
        route: next.api ? { ...next.api } : { kind: ROUTE_KIND_API, target_id: '' },
        memory: next,
    };
}

// ---------------------------------------------------------------------------
// DOM section (Agents → Review lanes). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    configError: '',
    loadError: '',
    source: '',
    triad: [],
    scope: [],
    advisory: { enabled: true, route: { kind: ROUTE_KIND_API, target_id: '' }, effort: 'low', subagent_id: '' },
    limits: { triad: 10, scope: 4, advisory: 1 },
    lastExecutions: {},
    catalogModels: [],
    harnesses: [],
    profilesByHarness: {},
    // The configured-subagent roster (OUROBOROS_SUBAGENTS items) the reference
    // selects offer. Display-only here; the Available-subagents section owns
    // editing. rosterKnown follows the same provenance rule as the facets: an
    // unreadable roster licenses no absence claim about a saved reference.
    roster: [],
    rosterKnown: false,
    // PER-FACET provenance: the route/model lists come from the CATALOG facet,
    // the account pins from the ACCOUNTS facet, and one can be authoritative
    // while the other was never read. Only an `ok` facet may license a row-level
    // "(not in discovery)" label.
    catalogKnown: false,
    accountsKnown: false,
    store: claudexorStatus,
    disposers: [],
    onChange: () => {},
};

// Per-kind memory for the advisory route select (see advisoryRouteTransition):
// seeded from the LOADED setting so a kind round-trip restores the saved target.
const advisoryRouteMemory = { api: null, session: null };

export function renderReviewerSlotsSection() {
    return `
        <div class="form-section" id="reviewer-slots-section">
            <h3>Review lanes</h3>
            <div class="settings-section-copy">
                Who reviews each commit: the triad rows, the scope rows, and one optional advisory
                pre-reviewer. Each row picks its reviewer from one list — a configured subagent
                from Available subagents above, an API model, or an agent on your subscription —
                plus its own reasoning effort.
            </div>
            <div class="settings-inline-note">
                Rows routed to a subscription never fall back to API spend: if every eligible window
                is exhausted, the review waits for capacity. Commit, plan, scope, advisory, and skill
                review follow their configured rows. Task acceptance remains API-only: it uses
                configured API rows, or the shipped defaults when none remain.
            </div>
            <div id="reviewer-slots-error" class="ui-status" data-tone="error" hidden></div>
            <div id="reviewer-slots-pins" class="settings-inline-status" data-tone="warn" hidden></div>
            <datalist id="reviewer-api-model-catalog"></datalist>
            <h4 class="reviewer-slots-heading">Triad slots <span class="muted" id="reviewer-triad-limit" title="The commit gate's real ceiling"></span></h4>
            <div id="reviewer-triad-rows" class="reviewer-slot-rows"></div>
            <div class="settings-toolbar">
                <button type="button" class="btn btn-default" id="btn-add-triad-slot">Add triad slot</button>
            </div>
            <h4 class="reviewer-slots-heading">Scope slots <span class="muted" id="reviewer-scope-limit" title="The scope pool's real width"></span></h4>
            <div class="settings-inline-note">An agent row reads the repository with its own read-only tools instead of
                being handed one assembled pack. Its verdict is authoritative once that agent's context window is
                confirmed at 200K or more; Ouroboros does not attest which files the agent opened.</div>
            <div id="reviewer-scope-rows" class="reviewer-slot-rows"></div>
            <div class="settings-toolbar">
                <button type="button" class="btn btn-default" id="btn-add-scope-slot">Add scope slot</button>
            </div>
            <h4 class="reviewer-slots-heading">Advisory pre-reviewer</h4>
            <div id="reviewer-advisory-row" class="reviewer-slot-rows"></div>
            <div class="settings-inline-note">
                Disabling the advisory is a standing decision with a constitutional consequence:
                every reviewed commit then records an <strong>audited bypass</strong> instead of an
                advisory verdict. Nothing is skipped silently.
            </div>
        </div>
    `;
}

function harnessesById() {
    const map = {};
    for (const h of state.harnesses) map[h.id] = h;
    return map;
}

function selectHtml(attrs, groups, selected) {
    return routeEditor.selectHtml(attrs, groups, selected);
}

function effortSelectHtml(attrs, selected, surfaceDefault) {
    // Compact closed state (owner feedback on field proportions): the wordy
    // "Default (scope review effort)" label made this select as wide as the
    // model field. Which setting the default follows moves to the tooltip.
    return routeEditor.effortSelectHtml(attrs, selected, surfaceDefault);
}

export function reviewerRouteIdentityMarkup(route, harnesses = {}, {
    catalogKnown = false,
} = {}) {
    if (route?.kind !== ROUTE_KIND_SESSION) {
        return harnessIdentityMarkup('api', {
            channel: 'api',
            className: 'reviewer-slot-route-identity',
        });
    }
    const split = splitSessionTarget(route.target_id);
    const harness = harnesses?.[split.harness];
    return harnessIdentityMarkup(split.harness, {
        label: catalogKnown ? String(harness?.display_name || '') : '',
        className: 'reviewer-slot-route-identity',
    });
}

function reviewerPickerHtml(attrs, row, { apiLabel } = {}) {
    // The one flat reviewer picker (decision 1=B): roster references first,
    // then the inline channels.
    const groups = reviewerChoiceGroups({
        roster: state.roster,
        rosterKnown: state.rosterKnown,
        row,
        harnesses: state.harnesses,
        catalogKnown: state.catalogKnown,
        apiLabel,
    });
    return selectHtml(attrs, groups, encodeReviewerChoice(row));
}

function subagentIdentityMarkup(row) {
    // Identity follows the DERIVED roster route: a session reference shows its
    // harness, anything else the API channel — same shared markup direct rows use.
    const rosterRow = (state.roster || []).find(
        (item) => String(item.subagent_id || '') === String(row.subagent_id || ''));
    const route = rosterRow?.route?.kind === ROUTE_KIND_SESSION
        ? { kind: ROUTE_KIND_SESSION, target_id: rosterRow.route.target_id }
        : { kind: ROUTE_KIND_API, target_id: '' };
    return reviewerRouteIdentityMarkup(route, harnessesById(), { catalogKnown: state.catalogKnown });
}

function rowHtml(row, group) {
    const { catalogKnown, accountsKnown } = state;
    if (row.subagent_id) {
        const last = state.lastExecutions[row.slot_id];
        const lastText = last ? describeLastExecution(last) : '';
        const metaParts = [describeSubagentReference(row.subagent_id, state.roster, { rosterKnown: state.rosterKnown })];
        if (lastText) metaParts.push(`Last run: ${lastText}`);
        return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            ${subagentIdentityMarkup(row)}
            <div class="reviewer-slot-controls">
                ${reviewerPickerHtml('data-slot-route aria-label="Reviewer"', row)}
                ${effortSelectHtml('data-slot-effort aria-label="Reasoning effort"', row.effort || '', 'subagent default')}
                <button type="button" class="btn btn-default" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
    }
    const session = row.route.kind === ROUTE_KIND_SESSION;
    const split = session ? splitSessionTarget(row.route.target_id) : { harness: '', model: '' };
    const harness = session ? harnessesById()[split.harness] : null;
    const modelOptions = sessionModelOptions(harness, split.model, { catalogKnown });
    const profiles = session ? (state.profilesByHarness[split.harness] || []) : [];
    const profileOptions = profileOptionsFor(profiles, row.route.profile_id, { accountsKnown });
    const last = state.lastExecutions[row.slot_id];
    const lastText = last ? describeLastExecution(last) : '';
    // ONE quiet meta line per row (owner feedback): the delivery badge and the
    // last-run projection share it; nothing is dropped — the raw route + ISO
    // timestamp live in the tooltip.
    const metaParts = [capabilityBadge(row, harnessesById(), { catalogKnown })];
    const modelsGap = session ? modelsGapNote(harness, catalogKnown) : '';
    if (modelsGap) metaParts.push(modelsGap);
    if (lastText) metaParts.push(`Last run: ${lastText}`);
    const surfaceDefault = group === 'scope' ? 'scope review effort' : 'review effort';
    return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            ${reviewerRouteIdentityMarkup(row.route, harnessesById(), { catalogKnown })}
            <div class="reviewer-slot-controls">
                ${reviewerPickerHtml('data-slot-route aria-label="Reviewer"', row)}
                ${session ? '' : `<input data-slot-custom-api list="reviewer-api-model-catalog" placeholder="provider/model-id" value="${escapeHtml(row.route.target_id || '')}" spellcheck="false" aria-label="API model id">`}
                ${session ? selectHtml('data-slot-model aria-label="Harness model"', [{ label: '', options: modelOptions }], split.model) : ''}
                ${session && profileOptions.length > 1 ? selectHtml('data-slot-profile aria-label="Credential account"', [{ label: '', options: profileOptions }], row.route.profile_id || '') : ''}
                ${effortSelectHtml('data-slot-effort aria-label="Reasoning effort"', row.effort, surfaceDefault)}
                <button type="button" class="btn btn-default" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
}

function advisoryHtml() {
    const advisory = state.advisory;
    const { catalogKnown, accountsKnown } = state;
    const last = state.lastExecutions.advisory_slot_1;
    const lastText = last ? describeLastExecution(last) : '';
    if (advisory.subagent_id) {
        const metaParts = [describeSubagentReference(advisory.subagent_id, state.roster, { rosterKnown: state.rosterKnown })];
        if (lastText) metaParts.push(`Last run: ${lastText}`);
        return `
        <div class="reviewer-slot-row" data-advisory-row>
            ${subagentIdentityMarkup(advisory)}
            <div class="reviewer-slot-controls">
                <label class="local-toggle"><input type="checkbox" data-advisory-enabled ${advisory.enabled !== false ? 'checked' : ''}> Enabled</label>
                ${reviewerPickerHtml('data-advisory-route aria-label="Advisory reviewer"', advisory)}
                ${effortSelectHtml('data-advisory-effort aria-label="Advisory effort"', advisory.effort || '', 'subagent default')}
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
    }
    // The advisory row shares the closed route vocabulary (api_chat |
    // agent_session): its api_chat entry is a routed catalog model running a
    // bounded native inspection episode — labeled neutrally, never a vendor
    // name (the retired Claude-SDK 'api' kind is parse-only migration now).
    const session = advisory.route?.kind === ROUTE_KIND_SESSION;
    const split = session ? splitSessionTarget(advisory.route.target_id)
        : { harness: '', model: '' };
    // Session branch: the SAME model-options fragment the triad rows use —
    // rewriting it here would lose the "(not in discovery)" guard and let a
    // Save with the daemon down erase the owner's model. Api branch: the same
    // catalog-assisted free-text entry the triad rows use (the advisory
    // api_chat target IS a routed catalog model id now).
    const modelOptions = session
        ? sessionModelOptions(harnessesById()[split.harness], split.model, { catalogKnown }) : [];
    const profiles = session ? (state.profilesByHarness[split.harness] || []) : [];
    const profileOptions = session
        ? profileOptionsFor(profiles, advisory.route?.profile_id, { accountsKnown }) : [];
    const metaParts = [capabilityBadge({ route: advisory.route || {} }, harnessesById(), { catalogKnown })];
    const modelsGap = session ? modelsGapNote(harnessesById()[split.harness], catalogKnown) : '';
    if (modelsGap) metaParts.push(modelsGap);
    if (lastText) metaParts.push(`Last run: ${lastText}`);
    return `
        <div class="reviewer-slot-row" data-advisory-row>
            ${reviewerRouteIdentityMarkup(advisory.route, harnessesById(), { catalogKnown })}
            <div class="reviewer-slot-controls">
                <label class="local-toggle"><input type="checkbox" data-advisory-enabled ${advisory.enabled !== false ? 'checked' : ''}> Enabled</label>
                ${reviewerPickerHtml('data-advisory-route aria-label="Advisory reviewer"', advisory, { apiLabel: 'API model (inspection episode)' })}
                ${session
                    ? selectHtml('data-advisory-model aria-label="Advisory harness model"', [{ label: '', options: modelOptions }], split.model)
                    : `<input data-advisory-api-model list="reviewer-api-model-catalog" placeholder="provider/model-id — empty = default" value="${escapeHtml(advisory.route?.target_id || '')}" spellcheck="false" aria-label="Advisory model id">`}
                ${session && profileOptions.length > 1 ? selectHtml('data-advisory-profile aria-label="Advisory credential account"', [{ label: '', options: profileOptions }], advisory.route?.profile_id || '') : ''}
                ${effortSelectHtml(
                    'data-advisory-effort aria-label="Advisory effort"',
                    session ? advisory.effort : (advisory.effort === 'low' ? '' : advisory.effort),
                    session ? 'route default' : 'low',
                )}
            </div>
            <div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(metaParts.join(' · '))}</div>
        </div>
    `;
}

function renderRows() {
    const errorBox = document.getElementById('reviewer-slots-error');
    if (errorBox) {
        errorBox.hidden = !(state.configError || state.loadError);
        errorBox.textContent = state.configError
            ? `Saved reviewer-slot configuration is invalid and blocks reviews: ${state.configError}. `
              + 'To repair it, add at least one triad slot and one scope slot below, then Save'
              + (state.triad.length && state.scope.length ? '.' : ' — Save will report the missing rows.')
            : (state.loadError
                ? `Could not reach the reviewer-slot settings — ${state.loadError}. Your saved configuration is unchanged; retry when the connection is back.`
                : '');
    }
    // Why the agent service could not be read is the TAB's banner now (one
    // place, not one per section). What stays here is the fact only this
    // section knows: a row pinned to an account that is really gone.
    const pinsBox = document.getElementById('reviewer-slots-pins');
    if (pinsBox) {
        const text = pinnedAccountWarning(state);
        pinsBox.hidden = !text;
        pinsBox.textContent = text;
    }
    const triadBox = document.getElementById('reviewer-triad-rows');
    const scopeBox = document.getElementById('reviewer-scope-rows');
    const advisoryBox = document.getElementById('reviewer-advisory-row');
    if (!triadBox || !scopeBox || !advisoryBox) return;
    triadBox.innerHTML = state.triad.map((row) => rowHtml(row, 'triad')).join('')
        || '<div class="muted">No triad slots configured.</div>';
    scopeBox.innerHTML = state.scope.map((row) => rowHtml(row, 'scope')).join('')
        || '<div class="muted">No scope slots configured.</div>';
    advisoryBox.innerHTML = advisoryHtml();
    const datalist = document.getElementById('reviewer-api-model-catalog');
    if (datalist) {
        datalist.innerHTML = state.catalogModels
            .map((id) => `<option value="${escapeHtml(id)}"></option>`).join('');
    }
    // The count stays in the heading; what the limit MEANS moved to the span's
    // title (owner feedback: headers carried parenthetical jargon).
    const triadLimit = document.getElementById('reviewer-triad-limit');
    if (triadLimit) triadLimit.textContent = `${state.triad.length}/${state.limits.triad}`;
    const scopeLimit = document.getElementById('reviewer-scope-limit');
    if (scopeLimit) scopeLimit.textContent = `${state.scope.length}/${state.limits.scope}`;
    const addTriad = document.getElementById('btn-add-triad-slot');
    if (addTriad) addTriad.disabled = state.triad.length >= state.limits.triad;
    const addScope = document.getElementById('btn-add-scope-slot');
    if (addScope) addScope.disabled = state.scope.length >= state.limits.scope;
    bindRowEvents();
}

function findRow(group, slotId) {
    const rows = group === 'scope' ? state.scope : state.triad;
    return rows.find((row) => row.slot_id === slotId) || null;
}

function bindRowEvents() {
    const section = document.getElementById('reviewer-slots-section');
    if (!section) return;
    section.querySelectorAll('.reviewer-slot-row[data-slot-id]').forEach((rowEl) => {
        const group = rowEl.dataset.slotGroup;
        const row = findRow(group, rowEl.dataset.slotId);
        if (!row) return;
        rowEl.querySelector('[data-slot-route]')?.addEventListener('change', (event) => {
            const value = String(event.target.value || '');
            if (value.startsWith(SUBAGENT_CHOICE_PREFIX)) {
                // A reference pick. The inline route stays stashed on the row
                // object — picking a channel again restores it.
                row.subagent_id = value.slice(SUBAGENT_CHOICE_PREFIX.length);
                renderRows();
                state.onChange();
                return;
            }
            row.subagent_id = '';
            if (!row.route?.kind) row.route = { kind: ROUTE_KIND_API, target_id: '' };
            const decoded = decodeRouteChoice(value);
            if (decoded.kind === ROUTE_KIND_SESSION) {
                const prevHarness = row.route.kind === ROUTE_KIND_SESSION
                    ? splitSessionTarget(row.route.target_id).harness : '';
                if (prevHarness !== decoded.harness) {
                    row.route = { kind: ROUTE_KIND_SESSION, target_id: decoded.harness, profile_id: '' };
                }
            } else if (row.route.kind !== ROUTE_KIND_API) {
                // A session spec is not an API model id: the input starts
                // empty (placeholder visible), display matching state.
                row.route = { kind: ROUTE_KIND_API, target_id: '' };
            }
            renderRows();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-custom-api]')?.addEventListener('input', (event) => {
            row.route.target_id = String(event.target.value || '').trim();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-model]')?.addEventListener('change', (event) => {
            const split = splitSessionTarget(row.route.target_id);
            row.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        rowEl.querySelector('[data-slot-profile]')?.addEventListener('change', (event) => {
            row.route.profile_id = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-effort]')?.addEventListener('change', (event) => {
            row.effort = String(event.target.value || '');
            state.onChange();
        });
        rowEl.querySelector('[data-slot-remove]')?.addEventListener('click', () => {
            const rows = group === 'scope' ? state.scope : state.triad;
            const index = rows.indexOf(row);
            if (index >= 0) rows.splice(index, 1);
            renderRows();
            state.onChange();
        });
    });
    const advisoryEl = section.querySelector('[data-advisory-row]');
    if (advisoryEl) {
        advisoryEl.querySelector('[data-advisory-enabled]')?.addEventListener('change', (event) => {
            state.advisory.enabled = Boolean(event.target.checked);
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-route]')?.addEventListener('change', (event) => {
            const value = String(event.target.value || '');
            if (value.startsWith(SUBAGENT_CHOICE_PREFIX)) {
                state.advisory.subagent_id = value.slice(SUBAGENT_CHOICE_PREFIX.length);
                // '' effort = the roster row's own effort decides; the api
                // default 'low' must not silently override the reference.
                state.advisory.effort = '';
                renderRows();
                state.onChange();
                return;
            }
            state.advisory.subagent_id = '';
            if (!state.advisory.route?.kind) {
                state.advisory.route = { kind: ROUTE_KIND_API, target_id: '' };
            }
            const result = advisoryRouteTransition(
                state.advisory.route, decodeRouteChoice(value), advisoryRouteMemory);
            state.advisory.route = result.route;
            Object.assign(advisoryRouteMemory, result.memory);
            if (state.advisory.route.kind !== ROUTE_KIND_SESSION && !state.advisory.effort) {
                state.advisory.effort = 'low';
            }
            renderRows();
            state.onChange();
        });
        // Mirrors of the triad-row model/api/profile handlers. These controls
        // exist only on their own kind's branch of advisoryHtml, so each
        // querySelector binds at most one of them per render.
        advisoryEl.querySelector('[data-advisory-model]')?.addEventListener('change', (event) => {
            const split = splitSessionTarget(state.advisory.route?.target_id);
            state.advisory.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-api-model]')?.addEventListener('input', (event) => {
            state.advisory.route.target_id = String(event.target.value || '').trim();
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-profile]')?.addEventListener('change', (event) => {
            state.advisory.route.profile_id = String(event.target.value || '');
            state.onChange();
        });
        advisoryEl.querySelector('[data-advisory-effort]')?.addEventListener('change', (event) => {
            const selected = String(event.target.value || '');
            // Empty means "the surface's default": low on the api route, the
            // route/roster-row default on a session or subagent reference.
            state.advisory.effort = selected
                || (state.advisory.subagent_id || state.advisory.route?.kind === ROUTE_KIND_SESSION
                    ? '' : 'low');
            state.onChange();
        });
    }
}

function addRow(group) {
    const rows = group === 'scope' ? state.scope : state.triad;
    const limit = group === 'scope' ? state.limits.scope : state.limits.triad;
    if (rows.length >= limit) return;
    const taken = [...state.triad, ...state.scope].map((row) => row.slot_id);
    rows.push({
        slot_id: mintSlotId(group === 'scope' ? 'scope' : 'triad', taken),
        route: { kind: ROUTE_KIND_API, target_id: '' },
        subagent_id: '',
        effort: '',
    });
    renderRows();
    state.onChange();
}

export async function reloadReviewerSlots() {
    try {
        const resp = await apiFetch('/api/reviewer-slots', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.loadError = '';
        state.configError = String(data.config_error || '');
        state.source = String(data.source || '');
        state.limits = data.limits || state.limits;
        state.lastExecutions = data.last_executions || {};
        // Rows spread as-is so a subagent_id reference rides along; a direct
        // route gets its own object. The advisory's non-session kind is
        // normalized to the shared api_chat spelling here — the retired legacy
        // 'api' (Claude-SDK) kind is parse-only server-side and this UI never
        // writes it again.
        const rowIn = (row) => ({ ...row, route: { ...(row.route || {}) } });
        state.triad = Array.isArray(data.triad) ? data.triad.map(rowIn) : [];
        state.scope = Array.isArray(data.scope) ? data.scope.map(rowIn) : [];
        state.advisory = data.advisory ? rowIn(data.advisory) : state.advisory;
        if (state.advisory.route?.kind !== ROUTE_KIND_SESSION) {
            state.advisory.route = { ...(state.advisory.route || {}), kind: ROUTE_KIND_API };
        }
        // Seed the per-kind route memory with the SAVED route, so switching
        // the advisory select away and back restores it (finding #7c).
        advisoryRouteMemory[state.advisory.route?.kind === ROUTE_KIND_SESSION ? 'session' : 'api']
            = { ...(state.advisory.route || {}) };
        // The VIEW loaded even when the saved value is invalid — that is exactly the
        // state the owner repairs from, and treating it as "not loaded" made the
        // save drop the repair (see collectReviewerSlots).
        state.loaded = true;
    } catch (error) {
        // A transport failure is NOT a verdict on the saved configuration: the
        // config-error banner accuses the owner's settings of blocking review, and
        // a network blip must never say that. Separate field, separate sentence.
        state.loaded = false;
        state.loadError = `could not load reviewer slots: ${error.message || error}`;
    }
    // ONE status read for the whole app (the accounts panel and the Subagents
    // section share this request; `includeModels` is sticky, so no later read
    // downgrades the snapshot these selects depend on). Awaited only for a
    // BOUNDED beat: this read can wake a cold Claudexor daemon and walk
    // per-harness model discovery, and awaiting it outright held the Save
    // button (loadSettings awaits this function) hostage for the whole probe.
    // A warm daemon settles inside the beat and keeps the exact old
    // semantics; a cold one keeps refreshing in the background and the status
    // surface binding repaints these rows when the snapshot lands.
    await boundedStatusRefresh(state.store);
    adoptStatusSnapshot();
    renderRows();
}

function adoptStatusSnapshot() {
    // Each facet answers for itself. A never-read catalog and a never-read
    // account store are separate gaps, and neither is evidence that a saved
    // route or pin no longer exists. The tab's ONE service banner explains the
    // gap — every facet it lost, named — so this section adds no second
    // sentence about it; a facet that WAS read keeps its authoritative list.
    state.catalogKnown = state.store.catalogKnown;
    state.accountsKnown = state.store.accountsKnown;
    const snapshot = state.store.snapshot || {};
    state.harnesses = state.catalogKnown && Array.isArray(snapshot.harnesses) ? snapshot.harnesses : [];
    state.profilesByHarness = state.accountsKnown ? indexProfilesByHarness(snapshot) : {};
}

// The roster the «Configured subagent» selects offer, adopted from the SAME
// settings document (and parser) the Available-subagents section loads —
// settings.js calls this from applySettings, right beside that section's own
// load. A roster that could not be parsed licenses no absence claim about a
// saved reference (rosterKnown=false), exactly like an unread status facet.
export function adoptSubagentRoster(settings) {
    const parsed = parseAvailableSubagentsSetting(availableSubagentsLoadValue(settings));
    state.roster = parsed.setting ? parsed.setting.items : [];
    state.rosterKnown = Boolean(parsed.setting);
    renderRows();
}

export function initReviewerSlots({ onChange, store = claudexorStatus } = {}) {
    destroyReviewerSlots();
    state.onChange = typeof onChange === 'function' ? onChange : () => {};
    state.store = store;
    // Seed from whatever the shared store already holds, so a render triggered
    // before the first notify (the model-catalog event) is not rendered from a
    // blank derived state.
    adoptStatusSnapshot();
    // Follow the shared read: when the daemon comes up while Settings is open
    // the rows stop claiming "(not in discovery)" without a page reload. That
    // promise needs the SHARED surface binding — a bare subscribe() carries no
    // visibility predicate, and the store never polls for a subscriber that
    // cannot say it is on screen, so nothing ever arrived to react to. Only a
    // change this section RENDERS repaints: a repaint on every poll tick would
    // drop the caret out of the API-model field mid-typing.
    let signature = '';
    state.disposers.push(bindStatusSurface(state.store, {
        elementId: 'reviewer-triad-rows',
        includeModels: true,
        listener: () => {
            adoptStatusSnapshot();
            const next = JSON.stringify([state.catalogKnown, state.accountsKnown,
                state.harnesses, state.profilesByHarness]);
            if (next === signature) return;
            signature = next;
            renderRows();
        },
    }));
    document.getElementById('btn-add-triad-slot')?.addEventListener('click', () => addRow('triad'));
    document.getElementById('btn-add-scope-slot')?.addEventListener('click', () => addRow('scope'));
    const onCatalog = (event) => {
        const items = event?.detail?.items || [];
        state.catalogModels = items.map((item) => String(item.value || item.id || '')).filter(Boolean);
        renderRows();
    };
    document.addEventListener('settings-model-catalog:updated', onCatalog);
    state.disposers.push(() => document.removeEventListener('settings-model-catalog:updated', onCatalog));
    // The initial load is driven by settings.js loadSettings(), which awaits
    // reloadReviewerSlots() BEFORE taking the clean-draft baseline — otherwise
    // the async arrival of the rows would read as an unsaved edit.
}

export function destroyReviewerSlots() {
    for (const dispose of state.disposers.splice(0)) {
        try { dispose(); } catch (err) { /* a broken disposer must not block the rest */ }
    }
}

// #126, pure and node-tested: what the settings save sends for reviewer slots.
// Never author the setting from an UNLOADED view (an unrelated save must not
// overwrite the owner's configuration with an empty page), and a transport
// failure is not a verdict on the saved value either. But a LOADED view always
// sends what it shows — including an empty triad/scope. The old empty-set
// guard silently dropped the key, so deleting every row Saved "successfully"
// while saving nothing; now the backend's own 400 («triad needs at least one
// slot») surfaces through the existing failed-save status. Validation SSOT
// stays on the backend — no client-side duplicate.
export function reviewerSlotsSavePayload({ loaded = false, loadError = '', triad = [], scope = [], advisory } = {}) {
    if (loadError || !loaded) return {};
    return { OUROBOROS_REVIEWER_SLOTS: buildReviewerSlotsSetting({ triad, scope, advisory }) };
}

export function collectReviewerSlots() {
    // A config_error is NOT the unloaded case — the stored value is already
    // invalid and blocking review, the endpoint returns no rows with it, and
    // refusing here made the documented repair path swallow the owner's
    // replacement rows and still report success.
    return reviewerSlotsSavePayload(state);
}
