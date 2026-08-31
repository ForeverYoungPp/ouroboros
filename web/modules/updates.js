import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { showToast } from './toast.js';
import { apiClient, apiFetch } from './api_client.js';
import { verifiedUpdatePlan } from './update_status.js';

// Known non-state warnings are folded into their verdict states; everything
// else is surfaced verbatim so a backend warning class can never vanish.
const STATE_WARNINGS = new Set(['official_status_requires_check', 'managed_updates_unavailable']);

function humanizeCheckedAt(iso) {
    if (!iso) return '';
    const then = Date.parse(iso);
    if (Number.isNaN(then)) return '';
    const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (minutes < 2) return 'checked just now';
    if (minutes < 90) return `checked ${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 36) return `checked ${hours} h ago`;
    return `checked ${new Date(then).toISOString().slice(0, 10)}`;
}

function repoSlug(url) {
    const match = /github\.com[/:]([^/]+\/[^/.]+)/.exec(String(url || ''));
    return match ? match[1] : String(url || '');
}

function extraWarnings(data) {
    return (Array.isArray(data.warnings) ? data.warnings : [])
        .filter((w) => !STATE_WARNINGS.has(String(w).split(':', 1)[0]) && !STATE_WARNINGS.has(w));
}

// Pure verdict: durable server state × transient client phase → one
// presentation descriptor. The button is always a real next action; facts
// travel as status line, hint, and chips (ARCHITECTURE §3: unavailable,
// divergent, dirty, unsafe, failed-check, rollback, and restart-required
// states stay visible — they are just not buttons).
export function updateVerdict(data = {}, phase = '') {
    const checkedAgo = humanizeCheckedAt(data.checked_at);
    const chips = [];
    if (data.official_repo_url) chips.push({ label: 'Official repo', value: repoSlug(data.official_repo_url) });
    if (data.target_ref) chips.push({ label: 'Target', value: data.target_ref });
    if (data.update_channel) chips.push({ label: 'Channel', value: data.update_channel, link: 'settings-advanced' });
    const divergence = [];
    if (data.behind) divergence.push(`${data.behind} incoming`);
    if (data.ahead) divergence.push(`${data.ahead} local`);
    if (data.dirty_count) divergence.push(`${data.dirty_count} dirty`);
    if (divergence.length) chips.push({ label: 'Divergence', value: divergence.join(' / ') });

    const warnings = extraWarnings(data);
    const base = { chips, warnings, checkedAgo };

    if (phase === 'loading') return { ...base, state: 'loading', tone: 'neutral', headline: 'Loading update status…', hint: '', action: null };
    if (phase === 'checking') return { ...base, state: 'checking', tone: 'neutral', headline: 'Checking the official channel…', hint: '', action: { id: 'check', label: 'Checking…', disabled: true } };
    if (phase === 'preflighting') return { ...base, state: 'preflighting', tone: 'neutral', headline: 'Preparing the update…', hint: '', action: { id: 'update', label: 'Preparing…', disabled: true } };
    if (phase === 'updating') return { ...base, state: 'updating', tone: 'neutral', headline: 'Applying the update…', hint: '', action: { id: 'update', label: 'Updating…', disabled: true } };
    if (phase === 'restarting') return { ...base, state: 'restarting', tone: 'ok', headline: 'Update applied — restarting…', hint: 'The page reloads by itself when the server is back.', action: { id: 'restart', label: 'Restarting…', disabled: true } };
    if (phase === 'restart_required') return { ...base, state: 'restart_required', tone: 'warn', headline: 'The update landed, but the automatic restart failed.', hint: 'Restart Ouroboros to finish.', action: { id: 'restart', label: 'Restart now' } };

    const unmanaged = data.managed === false
        || (Array.isArray(data.warnings) && data.warnings.includes('managed_updates_unavailable'));
    if (unmanaged) {
        return {
            ...base,
            state: 'unmanaged', tone: 'disabled',
            headline: 'Managed updates are unavailable for this checkout.',
            hint: 'Use git directly, or install a launcher-managed build.',
            action: null,
        };
    }
    if (data.update_tx?.active) {
        const task = data.update_tx.task_id ? ` (task ${data.update_tx.task_id})` : '';
        return {
            ...base,
            state: 'resolving', tone: 'warn',
            headline: 'A conflicting update is being resolved under review.',
            hint: `Watch progress in chat${task}. Applying another update waits for this resolution.`,
            action: null,
        };
    }
    const requiresCheck = !data.from_cache && Array.isArray(data.warnings) && data.warnings.includes('official_status_requires_check');
    if (requiresCheck && !data.checked_at) {
        return {
            ...base,
            state: 'unchecked', tone: 'neutral',
            headline: 'Official update status has not been checked yet.',
            hint: '',
            action: { id: 'check', label: 'Check for updates' },
        };
    }
    if (data.check_ok === false) {
        return {
            ...base,
            state: 'check_failed', tone: 'error',
            headline: 'Could not check the official update channel.',
            hint: warnings.length ? warnings.join(' · ') : 'Try again when the network is available.',
            warnings: [],
            action: { id: 'check', label: 'Check for updates' },
        };
    }
    const currentVersion = data.current_version || 'unknown';
    const currentSha = data.current_short_sha || '?';
    if (data.available) {
        const latestVersion = data.latest_version || 'unknown';
        const latestSha = data.latest_short_sha || '?';
        const unsafe = !data.safe_to_apply;
        if (data.latest_message) chips.push({ label: 'Latest', value: data.latest_message });
        return {
            ...base,
            state: unsafe ? 'available_unsafe' : 'available',
            tone: unsafe ? 'warn' : 'ok',
            headline: `Update available: ${currentVersion} (${currentSha}) -> ${latestVersion} (${latestSha})`,
            hint: [
                unsafe ? 'Local commits or uncommitted changes diverge from the official line; applying merges them.' : '',
                checkedAgo,
            ].filter(Boolean).join(' · '),
            action: { id: 'update', label: `Update to ${latestVersion}` },
        };
    }
    // "Up to date" is only claimed over an actual check result: a fresh
    // check_ok, or a cache-carried timestamp of the last real check.
    if (data.check_ok === true || data.checked_at) {
        return {
            ...base,
            state: 'current', tone: 'ok',
            headline: `Ouroboros is up to date at ${currentVersion} (${currentSha}).`,
            hint: checkedAgo ? `As of the last check — ${checkedAgo}.` : '',
            action: { id: 'check', label: 'Check again' },
        };
    }
    return {
        ...base,
        state: 'unknown', tone: 'neutral',
        headline: 'Update status is unknown.',
        hint: warnings.length ? warnings.join(' · ') : 'Run a check to get the official status.',
        warnings: [],
        action: { id: 'check', label: 'Check for updates' },
    };
}

export function initUpdates({ mount, state, ws, showPage }) {
    const page = document.createElement('div');
    page.id = 'page-updates';
    page.className = 'settings-embedded-content';
    page.innerHTML = `
        <div class="updates-scroll">
            <section class="updates-card" id="updates-status-card">
                <div class="updates-card-title">Official Updates</div>
                <div class="updates-status">
                    <span class="updates-status-dot" id="updates-dot" data-tone="neutral"></span>
                    <span class="updates-headline" id="updates-summary">Loading update status...</span>
                </div>
                <div class="updates-hint" id="updates-hint" hidden></div>
                <div class="updates-meta" id="updates-meta"></div>
                <div class="settings-action-row updates-action-row">
                    <span class="updates-action-note" id="updates-action-note"></span>
                    <button class="btn btn-primary" id="btn-update-primary" hidden></button>
                </div>
                <details class="updates-recovery">
                    <summary>Recovery</summary>
                    <p class="updates-recovery-copy">Replace the active checkout with the exact official version from the selected channel. A rescue copy is saved first, but this is intentionally more destructive than an ordinary update.</p>
                    <div class="updates-recovery-actions">
                        <button class="btn btn-danger btn-sm" id="btn-update-replace">Replace with Official Version (Recovery)</button>
                        <button class="btn btn-default btn-sm" id="updates-promote">Save recovery point</button>
                    </div>
                    <div class="updates-branch" id="updates-current"></div>
                    <h4 class="updates-subhead">Restore a previous version</h4>
                    <div id="updates-commits" class="updates-restore-list"></div>
                    <h4 class="updates-subhead">Official releases</h4>
                    <div id="updates-official-tags" class="updates-restore-list"></div>
                </details>
            </section>
        </div>
    `;
    mount.appendChild(page);

    const primaryBtn = page.querySelector('#btn-update-primary');
    const replaceBtn = page.querySelector('#btn-update-replace');
    const dot = page.querySelector('#updates-dot');
    const summary = page.querySelector('#updates-summary');
    const hint = page.querySelector('#updates-hint');
    const meta = page.querySelector('#updates-meta');
    const actionNote = page.querySelector('#updates-action-note');
    const current = page.querySelector('#updates-current');
    const commitsDiv = page.querySelector('#updates-commits');
    const officialTagsDiv = page.querySelector('#updates-official-tags');
    let latestStatus = null;
    let phase = 'loading';

    function chipHtml(chip) {
        const body = `<strong>${escapeHtml(chip.label)}:</strong> ${escapeHtml(chip.value)}`;
        if (chip.link === 'settings-advanced') {
            return `<button type="button" class="updates-chip updates-chip-link" data-open-settings-advanced title="Change in Settings -> Advanced">${body}</button>`;
        }
        return `<span class="updates-chip">${body}</span>`;
    }

    function render() {
        const verdict = updateVerdict(latestStatus || {}, phase);
        dot.dataset.tone = verdict.tone;
        summary.textContent = verdict.headline;
        const hintText = [verdict.hint, ...(verdict.warnings || []).map((w) => `Warning: ${w}`)]
            .filter(Boolean).join(' · ');
        hint.textContent = hintText;
        hint.hidden = !hintText;
        meta.innerHTML = (verdict.chips || []).map(chipHtml).join('');
        actionNote.textContent = verdict.checkedAgo || '';
        if (verdict.action) {
            primaryBtn.hidden = false;
            primaryBtn.disabled = Boolean(verdict.action.disabled);
            primaryBtn.textContent = verdict.action.label;
            primaryBtn.dataset.action = verdict.action.id;
        } else {
            primaryBtn.hidden = true;
            primaryBtn.dataset.action = '';
        }
        replaceBtn.disabled = ['updating', 'preflighting', 'restarting', 'resolving', 'unmanaged'].includes(verdict.state);
    }

    function setPhase(next) {
        phase = next;
        render();
    }

    async function loadStatus({ fetchRemote = false } = {}) {
        setPhase(fetchRemote ? 'checking' : 'loading');
        try {
            const data = await (fetchRemote ? apiClient.updateCheck() : apiClient.updateStatus());
            latestStatus = data;
            setPhase('');
            renderOfficialTags(data.official_tags || []);
        } catch (err) {
            latestStatus = { managed: true, warnings: [`status_error:${err.message || err}`], check_ok: false };
            setPhase('');
        }
    }

    function renderRestoreRow({ label, date, message, target, restorable }) {
        const row = document.createElement('div');
        row.className = 'updates-restore-row';
        const when = (date || '').slice(0, 16).replace('T', ' ');
        row.innerHTML = `
            <span class="updates-restore-label">${escapeHtml(label)}</span>
            <span class="updates-restore-date">${escapeHtml(when)}</span>
            <span class="updates-restore-msg">${escapeHtml((message || '').slice(0, 96))}</span>
            ${restorable ? `<button class="btn btn-danger btn-xs" data-target="${escapeHtml(target)}">Restore</button>` : ''}
        `;
        if (restorable) row.querySelector('button').addEventListener('click', () => rollback(target));
        return row;
    }

    function renderOfficialTags(tags) {
        officialTagsDiv.innerHTML = '';
        (tags || []).forEach((tag) => {
            officialTagsDiv.appendChild(renderRestoreRow({
                label: tag.tag || '', date: '', message: (tag.sha || '').slice(0, 12),
                target: '', restorable: false,
            }));
        });
        if (!tags?.length) officialTagsDiv.innerHTML = '<div class="updates-empty">Run a check to load official releases.</div>';
    }

    async function loadVersions() {
        try {
            const resp = await apiFetch('/api/git/log', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            current.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;
            // One restore list (owner decision 2026-08-31): tags that point at a
            // listed commit become labels on that commit's row; tags whose
            // target is older than the listed window keep their own row.
            const tagsBySha = new Map();
            const unmatchedTags = [];
            (data.tags || []).forEach((tag) => {
                if (tag.sha) {
                    const rows = tagsBySha.get(tag.sha) || [];
                    rows.push(tag);
                    tagsBySha.set(tag.sha, rows);
                } else {
                    unmatchedTags.push(tag);
                }
            });
            commitsDiv.innerHTML = '';
            const seenTagShas = new Set();
            (data.commits || []).forEach((commit) => {
                const tagged = tagsBySha.get(commit.sha) || [];
                tagged.forEach((tag) => seenTagShas.add(tag.sha));
                const tagNames = tagged.map((tag) => tag.tag).join(', ');
                commitsDiv.appendChild(renderRestoreRow({
                    label: tagNames || commit.short_sha || commit.sha?.slice(0, 8) || '?',
                    date: commit.date,
                    message: commit.message,
                    target: commit.sha,
                    restorable: true,
                }));
            });
            (data.tags || []).forEach((tag) => {
                if (tag.sha && seenTagShas.has(tag.sha)) return;
                commitsDiv.appendChild(renderRestoreRow({
                    label: tag.tag, date: tag.date, message: tag.message,
                    target: tag.tag, restorable: true,
                }));
            });
            unmatchedTags.forEach((tag) => {
                commitsDiv.appendChild(renderRestoreRow({
                    label: tag.tag, date: tag.date, message: tag.message,
                    target: tag.tag, restorable: true,
                }));
            });
            if (!commitsDiv.children.length) commitsDiv.innerHTML = '<div class="updates-empty">No commits found</div>';
        } catch (err) {
            commitsDiv.innerHTML = `<div class="updates-empty updates-empty-error">Failed to load: ${escapeHtml(err.message || err)}</div>`;
            current.textContent = 'Branch: unknown';
        }
    }

    async function rollback(target) {
        const confirmed = await openConfirmDialog({
            title: 'Roll back',
            body: `Roll back to ${target}?\n\nA rescue snapshot of the current state will be saved. The server will restart.`,
            confirmLabel: 'Roll back',
            danger: true,
        });
        if (!confirmed) return;
        try {
            const resp = await apiFetch('/api/git/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(`Rollback successful: ${data.message}. Server is restarting...`, 'success');
                setPhase('restarting');
            } else if (data.status === 'restart_required') {
                showToast(`Rollback completed: ${data.message}. Restart Ouroboros to finish.`, 'error');
                setPhase('restart_required');
            } else {
                const suffix = data.restart_required
                    ? ' Runtime shutdown was incomplete; restart Ouroboros before retrying.'
                    : '';
                showToast(`Rollback failed: ${data.error || 'unknown error'}${suffix}`, 'error');
                if (data.restart_required) setPhase('restart_required');
            }
        } catch (err) {
            showToast('Rollback failed: ' + (err.message || err), 'error');
        }
    }

    function applyFailureText(err) {
        const body = err?.body || {};
        const parts = [String(err.message || err)];
        if (body.reason) parts.push(`reason: ${body.reason}`);
        if (Array.isArray(body.blockers) && body.blockers.length) {
            parts.push(`blocked by: ${body.blockers.slice(0, 5).join(', ')}`);
        }
        if (body.rolled_back) parts.push('the checkout was rolled back');
        if (body.smoke) parts.push('the post-update smoke check failed');
        if (typeof body.estimated_wave_usd === 'number') {
            parts.push(`assisted review needs ~$${body.estimated_wave_usd}` + (
                typeof body.remaining_usd === 'number' ? ` of $${body.remaining_usd} remaining` : ''
            ));
        }
        if (body.stash_note) parts.push(body.stash_note);
        return parts.join(' · ');
    }

    async function applyUpdate() {
        if (!latestStatus?.available) return;
        setPhase('preflighting');
        try {
            const preflight = await apiClient.updatePreflight();
            const verified = verifiedUpdatePlan(preflight);
            if (!verified) {
                throw new Error(preflight?.merge_plan?.error || 'The update plan could not be verified. No files were changed.');
            }
            const { plan, strategy } = verified;
            const conflictCount = (plan.code_conflict_paths || []).length + (plan.doc_conflict_paths || []).length;
            const assisted = strategy === 'assisted';
            const proceed = await openConfirmDialog({
                title: `Update ${String(plan.base_sha).slice(0, 8)} -> ${String(plan.target_sha).slice(0, 8)}`,
                body: assisted
                    ? `${conflictCount} conflict(s) need resolution. Ouroboros will resolve the merge as a reviewed task (model spend applies); progress lands in chat, and the server restarts after the reviewed commit.`
                    : `Git applies this update directly (${plan.local_dirty_count || 0} local change(s) are stashed and restored). The server restarts when it lands.`,
                confirmLabel: assisted ? 'Start reviewed resolution' : 'Update now',
            });
            if (!proceed) {
                setPhase('');
                return;
            }
            setPhase('updating');
            const data = await apiClient.updateApply(strategy, plan);
            if (data.status === 'assisted_started') {
                showToast('Ouroboros is resolving the update merge under review. Watch progress in chat.', 'success');
                latestStatus = {
                    ...latestStatus,
                    update_tx: { active: true, phase: 'assisted_running', task_id: data.task_id || '' },
                };
                setPhase('');
            } else if (data.status === 'restart_required') {
                showToast('Update landed, but automatic restart failed. Restart Ouroboros to finish.', 'error');
                setPhase('restart_required');
            } else {
                showToast('Update applied. Server is restarting.', 'success');
                setPhase('restarting');
            }
        } catch (err) {
            showToast('Update failed: ' + applyFailureText(err), 'error');
            setPhase(err?.body?.restart_required ? 'restart_required' : '');
        }
    }

    async function replaceWithOfficial() {
        const proceed = await openConfirmDialog({
            title: 'Replace with official version',
            body: 'Recovery will replace the active checkout with the exact official version from the selected channel.\n\nA rescue snapshot and a local keep branch preserve a copy, but the active branch will be reset. Continue?',
            confirmLabel: 'Replace checkout',
            danger: true,
        });
        if (!proceed) return;
        replaceBtn.disabled = true;
        try {
            const preflight = await apiClient.updatePreflight();
            const plan = preflight?.merge_plan || {};
            if (!plan.base_sha || !plan.target_sha) {
                throw new Error(plan.error || 'Could not resolve an exact recovery target.');
            }
            const data = await apiClient.updateApply('replace', plan, { confirmRecovery: true });
            if (data.status === 'restart_required') {
                showToast('Recovery landed, but automatic restart failed. Restart Ouroboros to finish.', 'error');
                setPhase('restart_required');
            } else {
                showToast('Official version restored. Server is restarting.', 'success');
                setPhase('restarting');
            }
        } catch (err) {
            const restartRequired = Boolean(err?.body?.restart_required);
            const suffix = restartRequired ? ' Runtime shutdown was incomplete; restart Ouroboros before retrying.' : '';
            showToast('Recovery failed: ' + (err.message || err) + suffix, 'error');
            if (restartRequired) setPhase('restart_required');
            replaceBtn.disabled = false;
        }
    }

    async function restartNow() {
        primaryBtn.disabled = true;
        try {
            await apiFetch('/api/command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ cmd: '/restart' }),
            });
            showToast('Restart requested.', 'success');
            setPhase('restarting');
        } catch (err) {
            showToast('Restart failed: ' + (err.message || err), 'error');
            primaryBtn.disabled = false;
        }
    }

    primaryBtn.addEventListener('click', () => {
        const action = primaryBtn.dataset.action;
        if (action === 'check') {
            loadStatus({ fetchRemote: true });
            loadVersions();
        } else if (action === 'update') {
            applyUpdate();
        } else if (action === 'restart') {
            restartNow();
        }
    });
    replaceBtn.addEventListener('click', replaceWithOfficial);
    meta.addEventListener('click', (event) => {
        if (event.target.closest?.('[data-open-settings-advanced]')) {
            showPage?.('settings');
            document.querySelector('[data-settings-tab="advanced"]')?.click();
        }
    });
    page.querySelector('#updates-promote').addEventListener('click', async () => {
        const confirmedPromote = await openConfirmDialog({
            title: 'Save recovery point',
            body: 'Move this installation\'s local recovery branch (ouroboros-stable) to the current checkout?\n\nThis is the fallback the runtime boots when the working branch breaks. It does not publish anything and does not change the official QA feed of any install.',
            confirmLabel: 'Save recovery point',
        });
        if (!confirmedPromote) return;
        try {
            const resp = await apiFetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(data.message, 'success');
            } else {
                showToast('Error: ' + (data.error || 'unknown'), 'error');
            }
        } catch (err) {
            showToast('Failed: ' + (err.message || err), 'error');
        }
    });

    // The panel is mounted once for the whole app lifetime (app.js), so the
    // WS subscription is deliberately installation-lifetime — no disposer.
    ws?.on?.('update_status_ready', () => {
        if (phase === '' || phase === 'loading') loadStatus({ fetchRemote: false });
    });
    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab !== 'updates' || state.activePage !== 'dashboard') return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
}
