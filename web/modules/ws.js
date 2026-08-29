import { apiFetch } from './api_client.js';
/** WebSocket manager; connect after modules register listeners. */

// Consecutive healthy /api/state probes tolerated while the socket stays down
// before recovery forces a reload (at most one per disconnect episode) — the
// fuse for a browser runtime whose WebSocket stack froze.
export const RECOVERY_HEALTHY_PROBE_LIMIT = 4;

/**
 * SSOT for the served-SHA reload decision, shared by the post-open state
 * refresh and the socket-down recovery probe.
 *
 * A client that never connected has nothing to reconcile: keep the page.
 * After a connection existed, an equal SHA keeps the page; a different SHA
 * means the server now serves other assets (reload); a previously-known SHA
 * that disappears or garbles means the page can no longer be proven current
 * (reload). When no non-empty SHA was ever remembered AND the server serves
 * none either — run-from-source installs never expose one — nothing was ever
 * provable and nothing changed: keep (owner-flagged default, netres Q19).
 *
 * @param {*} prevSha last remembered served SHA (may be null/empty)
 * @param {*} servedSha SHA reported by /api/state (may be absent/malformed)
 * @param {boolean} previouslyConnected whether this client ever had an open socket
 * @returns {'keep'|'reload_changed'|'reload_unknown'}
 */
export function decide(prevSha, servedSha, previouslyConnected) {
    const served = typeof servedSha === 'string' ? servedSha.trim() : '';
    const prev = typeof prevSha === 'string' ? prevSha.trim() : '';
    if (!previouslyConnected) return 'keep';
    if (!prev && !served) return 'keep';
    if (!served || !prev) return 'reload_unknown';
    return prev === served ? 'keep' : 'reload_changed';
}

export class WS {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.listeners = {};
        this.reconnectDelay = 1000;
        this.maxDelay = 10000;
        this._wasConnected = false;
        this._lastSha = null;
        this._lastMessageAt = 0;
        this._reconnectTimer = null;
        this._uiRecoveryTimer = null;
        this._uiRecoveryProbeInFlight = false;
        this._watchdogTimer = null;
        this._recoveryHealthyProbes = 0;
        this._recoveryReloadFired = false;
        this._pendingMessages = [];
        this._nextClientMessageId = 1;
    }

    _getUrl() {
        return typeof this.url === 'function' ? this.url() : this.url;
    }

    _clearReconnectTimer() {
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
    }

    _clearUiRecoveryTimer() {
        if (this._uiRecoveryTimer) {
            clearTimeout(this._uiRecoveryTimer);
            this._uiRecoveryTimer = null;
        }
    }

    _clearWatchdogTimer() {
        if (this._watchdogTimer) {
            clearInterval(this._watchdogTimer);
            this._watchdogTimer = null;
        }
    }

    _freshWindowUrl(reason = '') {
        const url = new URL(window.location.href);
        url.searchParams.set('_ouro_refresh', String(Date.now()));
        if (reason) url.searchParams.set('_ouro_reason', reason);
        return url.toString();
    }

    _refreshWindow(reason = '') {
        window.location.replace(this._freshWindowUrl(reason));
    }

    _applyShaDecision(servedSha, previouslyConnected, storeServedSha) {
        const decision = decide(this._lastSha, servedSha, previouslyConnected);
        // Only the post-open refresh remembers the served SHA. A pre-connect
        // recovery probe must never adopt a restarted server's SHA: doing so
        // would make the eventual post-open compare a self-fulfilling "keep"
        // and leave stale assets unhealed.
        if (storeServedSha && decision === 'keep'
                && typeof servedSha === 'string' && servedSha.trim()) {
            this._lastSha = servedSha.trim();
        }
        return decision;
    }

    _reloadForShaDecision(decision) {
        // Reload on SHA change so PyWebView picks up new JS/CSS after restart;
        // an unproveable SHA after a reconnect is treated the same way.
        this._refreshWindow(decision === 'reload_changed' ? 'sha-change' : 'sha-unknown');
    }

    _scheduleUiRecovery(reason, delay = 15000) {
        // At most ONE recovery probe chain per episode: while a probe is in
        // flight the timer slot is empty, so arming must also be gated on the
        // in-flight flag — otherwise send()/_scheduleReconnect during a hung
        // probe pile up extra probes whose late resolutions would all count
        // toward the healthy fuse at once and could force a reload exactly
        // when connectivity returns, destroying queued outbound messages.
        if (this._uiRecoveryTimer || this._uiRecoveryProbeInFlight) return;
        this._uiRecoveryTimer = setTimeout(async () => {
            this._uiRecoveryTimer = null;
            this._uiRecoveryProbeInFlight = true;
            let rearm = false;
            try {
                let healthy = false;
                let servedSha;
                try {
                    const resp = await apiFetch('/api/state', { cache: 'no-store' });
                    if (resp.ok) {
                        healthy = true;
                        try {
                            const body = await resp.json();
                            servedSha = body ? body.sha : undefined;
                        } catch {}
                    }
                } catch {}
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    // The reconnect won the race while the probe was in flight;
                    // the post-open refresh owns the SHA decision now.
                    return;
                }
                if (healthy) {
                    const decision = this._applyShaDecision(servedSha, this._wasConnected, false);
                    if (decision !== 'keep') {
                        this._reloadForShaDecision(decision);
                        return;
                    }
                    this._recoveryHealthyProbes += 1;
                    if (this._recoveryHealthyProbes >= RECOVERY_HEALTHY_PROBE_LIMIT
                            && !this._recoveryReloadFired) {
                        // The server is reachable and serves the same page, yet the
                        // socket never reopens: force one reload per disconnect
                        // episode instead of reconnecting in place forever.
                        this._recoveryReloadFired = true;
                        this._refreshWindow(reason);
                        return;
                    }
                } else {
                    this._recoveryHealthyProbes = 0;
                }
                rearm = true;
            } finally {
                // Cleared before the re-arm below so the next arm attempt is
                // not self-blocked; cleared on every exit so a bail or reload
                // never leaves recovery permanently disarmed.
                this._uiRecoveryProbeInFlight = false;
            }
            if (rearm) {
                this._scheduleUiRecovery(reason, Math.min(Math.round(delay * 1.5), 30000));
            }
        }, delay);
    }

    _startWatchdog(socket) {
        this._clearWatchdogTimer();
        this._watchdogTimer = setInterval(() => {
            if (this.ws !== socket || socket.readyState !== WebSocket.OPEN) return;
            if (Date.now() - this._lastMessageAt < 45000) return;
            console.warn('WebSocket watchdog forcing reconnect after stale inbound stream');
            try { socket.close(); } catch {}
        }, 10000);
    }

    _scheduleReconnect() {
        if (this._reconnectTimer) return;
        document.getElementById('reconnect-overlay')?.classList.add('visible');
        this._scheduleUiRecovery('socket-disconnect', 15000);
        const delay = this.reconnectDelay;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, delay);
        this.reconnectDelay = Math.min(Math.round(this.reconnectDelay * 1.5), this.maxDelay);
    }

    _refreshStateAfterOpen(previouslyConnected) {
        apiFetch('/api/state', { cache: 'no-store' }).then(async (resp) => {
            if (!resp.ok) return;
            let servedSha;
            try {
                servedSha = (await resp.json())?.sha;
            } catch {}
            const decision = this._applyShaDecision(servedSha, previouslyConnected, true);
            if (decision !== 'keep') this._reloadForShaDecision(decision);
        }).catch(() => {});
    }

    _flushPendingMessages() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this._pendingMessages.length === 0) {
            return;
        }
        const queued = [...this._pendingMessages];
        this._pendingMessages = [];
        for (const msg of queued) {
            try {
                this.ws.send(JSON.stringify(msg));
                this.emit('outbound_sent', {
                    clientMessageId: msg.client_message_id || '',
                    queued: true,
                    type: msg.type || '',
                });
            } catch {
                this._pendingMessages.unshift(msg);
                this._scheduleReconnect();
                break;
            }
        }
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        const socket = new WebSocket(this._getUrl());
        this.ws = socket;
        const previouslyConnected = this._wasConnected;
        let disconnected = false;

        const handleDisconnect = () => {
            if (disconnected) return;
            disconnected = true;
            if (this.ws === socket) this.ws = null;
            this._clearWatchdogTimer();
            this.emit('close');
            this._scheduleReconnect();
        };

        socket.onopen = () => {
            if (this.ws !== socket) return;
            this._wasConnected = true;
            this._lastMessageAt = Date.now();
            this._clearReconnectTimer();
            this._clearUiRecoveryTimer();
            this._recoveryHealthyProbes = 0;
            this._recoveryReloadFired = false;
            this.reconnectDelay = 1000;
            this._startWatchdog(socket);
            // perf2 P4 [Gemini#3]: the CLIENT owns reconnect truth. A chat
            // instance created while the socket was already open would read its
            // per-instance "seen an open before" flag as false on the next real
            // reconnect and skip the full rebuild; previouslyConnected cannot.
            this.emit('open', { previouslyConnected });
            document.getElementById('reconnect-overlay')?.classList.remove('visible');
            this._refreshStateAfterOpen(previouslyConnected);
            this._flushPendingMessages();
        };

        socket.onerror = () => {
            handleDisconnect();
            try { socket.close(); } catch {}
        };

        socket.onclose = () => {
            handleDisconnect();
        };

        socket.onmessage = (e) => {
            this._lastMessageAt = Date.now();
            try {
                const msg = JSON.parse(e.data);
                this.emit('message', msg);
                if (msg.type) this.emit(msg.type, msg);
            } catch (err) {
                console.error('WebSocket message handling failed:', err);
            }
        };
    }

    send(msg, options = {}) {
        const payload = { ...msg };
        if (!payload.client_message_id && payload.type === 'chat') {
            payload.client_message_id = `msg-${Date.now()}-${this._nextClientMessageId++}`;
        }
        if (this.ws?.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(payload));
                this.emit('outbound_sent', {
                    clientMessageId: payload.client_message_id || '',
                    queued: false,
                    type: payload.type || '',
                });
                return { status: 'sent', clientMessageId: payload.client_message_id || '' };
            } catch {}
        }
        if (options.queue === false) {
            return { status: 'failed', clientMessageId: payload.client_message_id || '' };
        }
        if (this._pendingMessages.length >= 100) {
            const dropped = this._pendingMessages.shift();
            this.emit('outbound_dropped', {
                clientMessageId: (dropped && dropped.client_message_id) || '',
                type: (dropped && dropped.type) || '',
            });
        }
        this._pendingMessages.push(payload);
        this.emit('outbound_queued', {
            clientMessageId: payload.client_message_id || '',
            type: payload.type || '',
        });
        this._scheduleReconnect();
        this.connect();
        return { status: 'queued', clientMessageId: payload.client_message_id || '' };
    }

    isConnected() {
        return this.ws?.readyState === WebSocket.OPEN;
    }

    // Every subscription returns its disposer (UI resource lifecycle, P3): a
    // chat instance collects these and releases them in destroy(). Listeners
    // are a Set per event — insertion order is preserved, and subscribing the
    // same function twice collapses to one call (no such call-site exists).
    on(event, fn) {
        const set = (this.listeners[event] ||= new Set());
        set.add(fn);
        return () => set.delete(fn);
    }

    // Emit over a snapshot so a listener added during emit does not receive
    // that same emit, and a disposal during emit cannot skip a neighbor.
    // Listener errors propagate to the caller, matching the old array path.
    emit(event, data) {
        [...(this.listeners[event] || [])].forEach(fn => fn(data));
    }
}

export function createWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return new WS(() => `${proto}//${location.host}/ws`);
}
