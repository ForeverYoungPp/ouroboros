import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';
// Cycle note: toast.js imports normalizeTone from this module. Both edges only
// call the imported function inside function bodies (never at module eval), so
// the ES-module cycle is benign.
import { showToast } from './toast.js';

const TONES = new Set(['ok', 'danger', 'warn', 'muted', 'info']);
const TONE_ALIASES = Object.freeze({
    error: 'danger',
    success: 'ok',
    warning: 'warn',
    neutral: 'muted',
});
const SAFE_FIELD_TYPES = new Set(['text', 'number', 'url', 'email', 'password', 'textarea', 'select', 'checkbox']);

function safeFieldType(value) {
    const type = String(value || 'text').toLowerCase();
    return SAFE_FIELD_TYPES.has(type) ? type : 'text';
}

function safeNumericAttribute(name, value) {
    if (value === '' || value === null || value === undefined || !Number.isFinite(Number(value))) return '';
    return ` ${name}="${escapeHtml(value)}"`;
}

/** Render the narrow host-owned field contract shared by Widgets and Settings. */
export function renderSafeField(field = {}, savedValues = {}, options = {}) {
    const rawName = String(field.name || '');
    const name = escapeHtml(rawName);
    const label = escapeHtml(field.label || rawName);
    const type = safeFieldType(field.type);
    const hasSaved = type !== 'password' && Object.prototype.hasOwnProperty.call(savedValues || {}, rawName);
    const saved = type === 'password' ? '' : (hasSaved ? savedValues[rawName] : field.default);
    const value = escapeHtml(saved ?? '');
    const placeholder = field.placeholder ? ` placeholder="${escapeHtml(field.placeholder)}"` : '';
    const required = field.required ? ' required' : '';
    const disabled = field.disabled || options.disabled ? ' disabled' : '';
    const fieldClass = escapeHtml(options.fieldClass || 'widget-field');
    const inlineClass = escapeHtml(options.inlineClass || `${options.fieldClass || 'widget-field'} widget-field-inline`);
    const helpClass = escapeHtml(options.helpClass || 'widget-field-help');
    const maxSpan = Math.max(1, Math.min(4, Number(options.maxSpan) || 4));
    const span = Math.max(1, Math.min(maxSpan, Number(field.span) || 1));
    const spanClass = options.spanClassPrefix ? ` ${escapeHtml(options.spanClassPrefix)}${span}` : '';
    const help = field.help ? `<small class="${helpClass}">${escapeHtml(field.help)}</small>` : '';
    if (type === 'textarea') {
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><textarea name="${name}"${placeholder}${required}${disabled}>${value}</textarea>${help}</label>`;
    }
    if (type === 'select') {
        const optionsHtml = (Array.isArray(field.options) ? field.options : []).map((option) => {
            const optionValue = typeof option === 'object' && option !== null ? option.value : option;
            const optionLabel = typeof option === 'object' && option !== null ? (option.label ?? option.value) : option;
            const selected = String(optionValue ?? '') === String(saved ?? '') ? ' selected' : '';
            return `<option value="${escapeHtml(optionValue ?? '')}"${selected}>${escapeHtml(optionLabel ?? '')}</option>`;
        }).join('');
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><select name="${name}"${required}${disabled}>${optionsHtml}</select>${help}</label>`;
    }
    if (type === 'checkbox') {
        return `<label class="${inlineClass}${spanClass}"><input type="checkbox" name="${name}"${saved ? ' checked' : ''}${required}${disabled}> <span>${label}</span>${help}</label>`;
    }
    const numeric = type === 'number'
        ? `${safeNumericAttribute('min', field.min)}${safeNumericAttribute('max', field.max)}${safeNumericAttribute('step', field.step)}`
        : '';
    const autocomplete = type === 'password' ? ' autocomplete="new-password"' : '';
    return `<label class="${fieldClass}${spanClass}"><span>${label}</span><input type="${type}" name="${name}" value="${value}"${placeholder}${numeric}${required}${disabled}${autocomplete}>${help}</label>`;
}

/** Collect values according to the same closed field contract used for rendering. */
export function collectSafeFieldValues(form, fields = [], { includePasswords = true } = {}) {
    const values = {};
    for (const field of Array.isArray(fields) ? fields : []) {
        const name = String(field?.name || '');
        const type = safeFieldType(field?.type);
        if (!name || (type === 'password' && !includePasswords)) continue;
        const input = form?.elements?.namedItem
            ? form.elements.namedItem(name)
            : form?.elements?.[name];
        if (!input) continue;
        values[name] = type === 'checkbox' ? Boolean(input.checked) : input.value;
    }
    return values;
}

export function normalizeTone(tone = 'muted', fallback = 'muted') {
    const canonical = (value) => {
        const clean = String(value || '').toLowerCase();
        const normalized = TONE_ALIASES[clean] || clean;
        return TONES.has(normalized) ? normalized : '';
    };
    return canonical(tone) || canonical(fallback) || 'muted';
}

export function renderToneBadge(label, tone = 'muted', className = 'skills-badge') {
    const cleanTone = normalizeTone(tone);
    return `<span class="${className} ${className}-${cleanTone}">${escapeHtml(label || '')}</span>`;
}

export function installedTime(item) {
    const time = Date.parse(item?.installed_at || item?.provenance?.installed_at || item?.provenance?.updated_at || '');
    return Number.isFinite(time) ? time : 0;
}

export function formatRelativeAge(time, freshLabel = 'Just installed') {
    if (!time) return '';
    const minutes = Math.floor(Math.max(0, Date.now() - time) / 60000);
    if (minutes < 2) return freshLabel;
    if (minutes < 90) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return days < 45 ? `${days}d ago` : new Date(time).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/**
 * Shared design-system action button for host-stamped system chat rows
 * (Project lifecycle rows and future system-message actions). One semantic
 * button role — `.btn.btn-default.btn-sm` — plus the layout-only
 * `.system-message-action` hook; callers place it inside a
 * `.system-message-actions` container.
 */
export function createSystemMessageAction({ label, onClick, disabled = false, ariaLabel = '' } = {}) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-default btn-sm system-message-action';
    btn.textContent = String(label || '');
    if (disabled) btn.disabled = true;
    if (ariaLabel) btn.setAttribute('aria-label', ariaLabel);
    if (typeof onClick === 'function') btn.addEventListener('click', onClick);
    return btn;
}

export function setInlineStatus(el, text, tone = 'muted') {
    if (!el) return;
    const next = text || '';
    if (el.textContent !== next) el.textContent = next;
    el.dataset.tone = normalizeTone(tone);
}

export async function openViaHostBridge(url, filename = 'file') {
    const api = window.pywebview?.api;
    const openBridge = api?.open_file_with_default_app;
    if (openBridge) {
        const result = await openBridge(url, filename);
        if (!result?.ok) throw new Error(result?.error || 'open failed');
        return { ...result, native: true };
    }
    // Version-skew fallback: the served frontend auto-updates via the managed
    // repo, but the outer desktop launcher only changes on a full app reinstall,
    // so a packaged launcher can predate open_file_with_default_app (added
    // v6.58.3) while still shipping download_file_to_downloads with an
    // open_external flag (since v5.5.0). Reuse that long-shipped bridge — it
    // saves to ~/Downloads AND opens externally — instead of window.open, which
    // is a silent no-op in the desktop WKWebView.
    const downloadBridge = api?.download_file_to_downloads;
    if (downloadBridge) {
        const result = await downloadBridge(url, filename, true);
        if (!result?.ok) throw new Error(result?.error || 'open failed');
        return { ...result, native: true };
    }
    // True web / non-desktop: open in a new tab. This never navigates the app
    // itself; the browser previews (e.g. PDF) or downloads per its own handling.
    window.open(url, '_blank', 'noopener');
    return { ok: true, native: false };
}

export async function downloadViaHostBridge(url, filename = 'download', { openExternal = false, fetchOptions = {} } = {}) {
    const bridge = window.pywebview?.api?.download_file_to_downloads;
    if (bridge) {
        const result = await bridge(url, filename, Boolean(openExternal));
        if (!result?.ok) throw new Error(result?.error || 'desktop download failed');
        return { ...result, native: true };
    }
    const resp = await apiFetch(url, fetchOptions);
    if (!resp.ok) throw new Error(`download failed: HTTP ${resp.status}`);
    const blobUrl = URL.createObjectURL(await resp.blob());
    const link = document.createElement('a');
    Object.assign(link, { href: blobUrl, download: filename, rel: 'noopener' });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    return { ok: true, native: false };
}

// ---------------------------------------------------------------------------
// Desktop-shell link interception (pywebview parity).
//
// The embedded WebView is created without new-window or download delegates, so
// `window.open()`, `<a target="_blank">`, and `<a download>` are silent no-ops
// in the desktop shell. ONE shell-only interceptor closes the whole class: a
// delegated document click listener plus a `window.open` shim, installed only
// once the pywebview bridge exists (the bridge appears asynchronously after
// load, announced by the `pywebviewready` event). In an ordinary browser
// nothing is installed — zero behavior change. Bridge METHODS are still
// feature-detected per call: the packaged launcher only updates on reinstall
// while this served frontend updates with the managed repo, so each class has
// an explicit version-skew fallback (copy-link toast / honest "not available"
// toast) instead of a silently dead control.

const BRIDGE_ARTIFACTS_RE = /^\/api\/tasks\/[^/]+\/artifacts\//;

/** Classify a URL for the desktop shell: file | external | bytes | default. */
export function classifyShellUrl(rawUrl, baseHref = '') {
    const raw = String(rawUrl || '').trim();
    if (!raw) return { kind: 'default', url: '' };
    if (/^(data|blob):/i.test(raw)) return { kind: 'bytes', url: raw };
    let parsed;
    try { parsed = new URL(raw, baseHref || undefined); } catch { return { kind: 'default', url: raw }; }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return { kind: 'default', url: raw };
    let sameOrigin = false;
    try { sameOrigin = Boolean(baseHref) && parsed.origin === new URL(baseHref).origin; } catch {}
    if (sameOrigin && (parsed.pathname === '/api/files/download'
        || parsed.pathname.startsWith('/api/extensions/')
        || BRIDGE_ARTIFACTS_RE.test(parsed.pathname))) {
        // Path-only form: the launcher guard re-joins it onto the loopback
        // origin itself, exactly like the existing bridge callers pass it.
        return { kind: 'file', url: parsed.pathname + parsed.search };
    }
    // Any other http(s) target ("leave the app" intent) belongs in the owner's
    // real browser — including same-origin pages, which have no tab here.
    return { kind: 'external', url: parsed.href };
}

function bridgeFilenameFromUrl(url, fallback = 'download') {
    try {
        const parsed = new URL(url, 'http://localhost');
        const fromQuery = parsed.searchParams.get('path') || '';
        const candidate = decodeURIComponent((fromQuery || parsed.pathname).split('/').pop() || '');
        return candidate || fallback;
    } catch { return fallback; }
}

function filenameForMime(mime) {
    const ext = String(mime || '').split('/')[1]?.split(/[+;]/)[0]?.toLowerCase() || '';
    return `download.${/^[a-z0-9]{1,10}$/.test(ext) ? ext : 'bin'}`;
}

function base64FromArrayBuffer(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
}

async function shellBytesPayload(url, filename, win) {
    if (/^data:/i.test(url)) {
        const comma = url.indexOf(',');
        const meta = comma >= 0 ? url.slice(5, comma) : '';
        if (/;base64$/i.test(meta)) {
            return {
                name: filename || filenameForMime(meta.split(';')[0]),
                b64: url.slice(comma + 1).replace(/\s+/g, ''),
            };
        }
    }
    // blob: URLs and non-base64 data: URLs: let fetch decode them.
    const fetcher = typeof win?.fetch === 'function' ? win.fetch.bind(win) : fetch;
    const blob = await (await fetcher(url)).blob();
    return {
        name: filename || filenameForMime(blob.type),
        b64: base64FromArrayBuffer(await blob.arrayBuffer()),
    };
}

async function copyShellLink(url, win, doc) {
    try {
        if (!win.navigator?.clipboard?.writeText) throw new Error('no async clipboard');
        await win.navigator.clipboard.writeText(url);
    } catch {
        const area = doc.createElement('textarea');
        area.value = url;
        area.setAttribute('readonly', '');
        doc.body.appendChild(area);
        try {
            area.select();
            const copied = typeof doc.execCommand === 'function' && doc.execCommand('copy') === true;
            if (!copied) throw new Error('Clipboard access is unavailable');
        } finally {
            area.remove();
        }
    }
}

async function routeShellUrl(kind, url, deps) {
    const { api, win, doc, toast, openFile, downloadFile, filename = '', wantsDownload = false } = deps;
    try {
        if (kind === 'file') {
            const name = filename || bridgeFilenameFromUrl(url);
            if (wantsDownload) await downloadFile(url, name);
            else await openFile(url, name);
        } else if (kind === 'external') {
            if (api?.open_external_url) {
                const result = await api.open_external_url(url);
                if (!result?.ok) throw new Error(result?.error || 'open failed');
            } else {
                // Old packaged launcher without open_external_url: hand the
                // owner the link instead of leaving a silently dead control.
                await copyShellLink(url, win, doc);
                toast('Link copied — open it in your browser.', 'info');
            }
        } else if (kind === 'bytes') {
            if (!api?.save_bytes_to_downloads) {
                toast("Saving isn't available in the app — open in a browser.", 'warn');
                return;
            }
            const payload = await shellBytesPayload(url, filename, win);
            const result = await api.save_bytes_to_downloads(payload.name, payload.b64);
            if (!result?.ok) throw new Error(result?.error || 'save failed');
            const saved = String(result.path || '').split(/[\\/]/).pop() || payload.name;
            toast(`Saved to Downloads: ${saved}`, 'ok');
        }
    } catch (error) {
        toast(`Could not ${kind === 'bytes' ? 'save file' : 'open link'}: ${error?.message || error}`, 'error');
    }
}

/**
 * Install the shell-only interceptor. Idempotence and browser neutrality:
 * installs at most once per document/window pair, and only when the pywebview
 * bridge is (or becomes) present.
 */
export function installDesktopShellLinkInterceptor({
    win = window,
    doc = document,
    toast = showToast,
    openFile = openViaHostBridge,
    downloadFile = downloadViaHostBridge,
} = {}) {
    let installed = false;
    const install = () => {
        if (installed) return;
        installed = true;
        const nativeOpen = typeof win.open === 'function' ? win.open.bind(win) : () => null;
        const deps = (extra) => ({
            api: win.pywebview?.api, win, doc, toast, openFile, downloadFile, ...extra,
        });
        // With NO file-bridge method at all, openViaHostBridge/downloadViaHostBridge
        // fall back to window.open / anchor clicks — which would re-enter this
        // interceptor. Keep the native default for that (ancient-launcher) case.
        const fileBridgeReady = (api) => Boolean(api?.open_file_with_default_app || api?.download_file_to_downloads);
        doc.addEventListener('click', (event) => {
            const api = win.pywebview?.api;
            if (!api || event.defaultPrevented) return;
            const anchor = typeof event.target?.closest === 'function' ? event.target.closest('a[href]') : null;
            if (!anchor) return;
            const wantsDownload = anchor.hasAttribute('download');
            if (anchor.getAttribute('target') !== '_blank' && !wantsDownload) return;
            const { kind, url } = classifyShellUrl(anchor.getAttribute('href'), win.location?.href);
            if (kind === 'default' || (kind === 'file' && !fileBridgeReady(api))) return;
            event.preventDefault();
            void routeShellUrl(kind, url, deps({
                wantsDownload,
                filename: String(anchor.getAttribute('download') || ''),
            }));
        });
        win.open = (url, target, features) => {
            const api = win.pywebview?.api;
            const { kind, url: routed } = api
                ? classifyShellUrl(url, win.location?.href)
                : { kind: 'default', url };
            if (kind === 'default' || (kind === 'file' && !fileBridgeReady(api))) {
                return nativeOpen(url, target, features);
            }
            void routeShellUrl(kind, routed, deps({}));
            return null;
        };
    };
    if (win.pywebview?.api) install();
    else win.addEventListener?.('pywebviewready', install, { once: true });
}

/**
 * Decision helper for Windows Alt/Alt+Shift keyboard layout switch focus-lock.
 * When focus is within an editable text input, prevents standalone Alt keydown
 * from triggering Windows window-menu activation (which beeps and drops the next keystroke).
 * AltGr is intentionally preserved in both engine shapes: Chromium reports it as
 * Ctrl+Alt (excluded via ctrlKey), Firefox as key='AltGraph' (excluded by name).
 */
export function shouldSuppressWindowsAltMenu(event, activeElement) {
    if (!event) return false;
    if (event.key === 'AltGraph') return false;
    const isAlt = (event.key === 'Alt' || event.code === 'AltLeft' || event.code === 'AltRight') && !event.ctrlKey;
    if (!isAlt) return false;
    if (!activeElement) return false;
    const tag = String(activeElement.tagName || '').toUpperCase();
    return tag === 'INPUT' || tag === 'TEXTAREA' || Boolean(activeElement.isContentEditable);
}

/**
 * Install the standalone-Alt menu-lock guard on a document's window. Chromium
 * hosts decide menu activation from the Alt KEYDOWN default while Firefox-style
 * hosts decide on KEYUP, so both phases carry the same suppression test. Each
 * top-level document installs its own guard (the onboarding wizard runs in an
 * iframe with its own window, where the SPA listener cannot see events). The
 * guard is an app-lifetime singleton like the other top-level app.js listeners,
 * so it deliberately registers no disposer.
 */
export function installAltMenuSuppression(target = window) {
    const doc = target.document || document;
    const suppress = (event) => {
        if (shouldSuppressWindowsAltMenu(event, doc.activeElement)) {
            event.preventDefault();
        }
    };
    target.addEventListener('keydown', suppress, { capture: true });
    target.addEventListener('keyup', suppress, { capture: true });
}
