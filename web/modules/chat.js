import {
    escapeHtmlAttr,
    escapeHtmlText as escapeHtml,
    renderMarkdown,
} from './utils.js';
import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { createSystemMessageAction, downloadViaHostBridge, openViaHostBridge } from './ui_helpers.js';
import { clientSurfaceField } from './client_surface.js';
import { apiClient, apiFetch, fetchTaskDetail, fetchTaskDetailStrict } from './api_client.js';
import {
    OWNER_STOP_DETAIL_MARKER,
    getLogTaskGroupId,
    isGroupedTaskEvent,
    isTerminalTaskDetail,
    normalizeLogTs,
    ownerHurryProjection,
    summarizeChatLiveEvent,
    taskCancelPending,
    taskOutcomeSeverity,
    taskPresentation,
    taskSoftStopPending,
    taskStoppedWithSummary,
    taskDoneIsTerminal,
    keepStickyExecutorChip,
    taskTerminalPhase,
} from './log_events.js';
import {
    ACTION_FINALIZE,
    ACTION_HURRY,
    REUSABLE_TASK_IDS,
    TASK_CONTROL_TRIGGER_LABEL,
    cancelRunEligibility,
    hurryTaskAction,
    openTaskControlMenu,
    requestStop,
    taskControlBusy,
} from './task_control_menu.js';
import { openConfirmDialog } from './confirm_dialog.js';
import {
    captureLiveCardPhaseState,
    desiredLiveCardPhase,
    replayTerminalPhase,
    restoreLiveCardPhaseState,
    setLiveCardPhase,
} from './task_phase_chip.js';
import {
    loadSkillReviewDetail,
    nestedSkillReviewRef,
    renderSkillReviewDisclosure,
    wireSkillReviewDisclosure,
} from './skill_review_card.js';
import {
    classifyReviewLifecycle,
    classifyReviewLifecyclePointer,
    createReviewPresentationController,
    createReviewHydrator,
    reviewReferenceFromRow,
    reviewGroupFromHistoryRow,
    reviewGroupsFromTaskDetail,
    setReviewAnchor,
} from './review_presentation.js';
import { harnessIdentityMarkup } from './harness_presentation.js';
import {
    createHistoryResyncScheduler,
    createRebuildBatch,
    insertTimelineNode,
    loadOlderControlState,
    nextQuotaEscalation,
} from './chat_render_batch.js';
import {
    COLLAPSED_ACTIVITY_MAX,
    boundActivityPreview,
    buildTimelineItemHtml,
    buildMessageKey,
    chatLogThreadAccepts,
    chatMediaMessageKey,
    chatThreadAccepts,
    clearStickyCardState,
    confirmAndSendPanic,
    computeDerivedChatStatus,
    computeHydratedDirectActivities,
    documentMessageKey,
    durableChatMediaUrl,
    formatMsgTime,
    getOrCreateChatSessionId,
    headerBudgetPresentation,
    isBackgroundTaskId,
    isFileDrag,
    isForegroundLiveCard,
    isNonTerminalMediaHistoryRow,
    isTerminalTaskPhase,
    loadChatInputHistory,
    liveLineRowToggleKey,
    mergeStickyCostMeta,
    partitionLocalEchoJournal,
    pendingAttachmentBytes,
    projectCollapsedActivity,
    positiveTaskTerminalFact,
    projectIdFromTask,
    rawTimestampEpoch,
    reconcileHydratedDirectActivities,
    reconnectBannerText,
    routingAnnotationText,
    saveChatInputHistory,
    senderLabel,
    shouldAlwaysShowTaskCard,
    shouldFirePanic,
    taskCostMeta,
    taskCostProjection,
    unconfirmedForegroundCardIds,
    withTaskCostMeta,
} from './chat_activity.js';

export {
    COLLAPSED_ACTIVITY_MAX,
    boundActivityPreview,
    chatMediaMessageKey,
    clearStickyCardState,
    confirmAndSendPanic,
    computeDerivedChatStatus,
    computeHydratedDirectActivities,
    headerBudgetPresentation,
    insertTimelineNode,
    isTerminalTaskDetail,
    isTerminalTaskPhase,
    durableChatMediaUrl,
    isNonTerminalMediaHistoryRow,
    liveLineRowToggleKey,
    mergeStickyCostMeta,
    projectCollapsedActivity,
    rawTimestampEpoch,
    reconcileHydratedDirectActivities,
    taskCostMeta,
    taskCostProjection,
    shouldFirePanic,
};

const PROJECT_ROW_TYPES = new Set(['project_started', 'project_completion_summary']);
const CHAT_STORAGE_KEY = 'ouro_chat';
const CHAT_DRAFT_KEY = 'ouro_chat_draft';
const CHAT_INPUT_HISTORY_KEY = 'ouro_chat_input_history';
const MAX_PENDING_ATTACHMENTS = 10;
const MAX_ATTACHMENT_FILE_BYTES = 50 * 1024 * 1024;
const MAX_PENDING_ATTACHMENT_BYTES = 100 * 1024 * 1024;
const shownIncidentToastKeys = new Set();

function showTaskIncidentToast(msg) {
    const incident = String(msg?.task_incident || '').trim();
    if (!incident) return;
    const key = String(msg?.toast_once || `${msg?.task_id || ''}:${incident}`).trim();
    if (!key || shownIncidentToastKeys.has(key)) return;
    shownIncidentToastKeys.add(key);
    if (shownIncidentToastKeys.size > 500) {
        const oldest = shownIncidentToastKeys.values().next().value;
        shownIncidentToastKeys.delete(oldest);
    }
    showToast(String(msg?.content || msg?.text || incident), 'error');
}

export function initChat(ctx) {
    // Back-compat main-chat entry: one full-page instance bound to chat 1.
    return createChatInstance(ctx);
}

export function createChatInstance({
    ws, state, updateUnreadBadge, openSettingsTab, openDashboardTab,
    stateSnapshots,
    chatId = 1, projectId = '', idPrefix = 'chat', mountEl = null,
    asPanel = false, title = 'Chat', initialScrollState = null,
    // perf2 P4.2: app.js signal "a project panel is opening right now" — Main
    // defers its first hydration to it (bounded by an unconditional deadline).
    isProjectOpening = null,
}) {
    const container = mountEl || document.getElementById('content');
    const chatSessionId = getOrCreateChatSessionId(sessionStorage, globalThis.crypto);
    const isMain = chatId === 1;
    // Per-thread storage so a project thread never bleeds into the main chat.
    const storeKey = (base) => (isMain ? base : `${base}:${chatId}`);

    const page = document.createElement('div');
    page.id = asPanel ? `panel-${idPrefix}` : 'page-chat';
    page.className = asPanel ? 'chat-instance-panel' : 'page active';
    // A project panel reuses the lean `.project-panel-bar` (title + close) from
    // index.html, so it renders a minimal status-only header — NOT the overlay
    // page header (that would duplicate the title and drag in the GLOBAL
    // Evolve/Restart/Panic/budget chrome, which belongs to the one agent, not a
    // single project thread). The main chat keeps the full overlay header.
    const headerHtml = asPanel
        ? `<div class="chat-panel-statusbar"><span id="chat-status" class="status-badge offline">Connecting...</span></div>`
        : renderPageHeader({
            title: title,
            icon: PAGE_ICONS.chat,
            variant: 'overlay',
            className: 'chat-page-header',
            actionsHtml: `
                <div class="chat-header-actions" id="chat-header-actions">
                    <button class="chat-header-btn" type="button" data-chat-command="restart" title="Restart agent">Restart</button>
                    <button class="chat-header-btn danger" type="button" data-chat-command="panic" title="Stop all workers">Panic</button>
                    <details class="chat-header-more">
                        <summary class="chat-header-btn" title="More agent controls">More</summary>
                        <div class="chat-header-menu">
                            <button class="chat-header-menu-item" type="button" data-chat-command="bg" title="Toggle background consciousness">Consciousness</button>
                            <button class="chat-header-menu-item" type="button" data-chat-command="evolve" title="Toggle evolution mode">Evolve</button>
                            <button class="chat-header-menu-item" type="button" data-chat-command="review" title="Run review now">Review</button>
                        </div>
                    </details>
                </div>
                <button class="chat-budget-pill" id="chat-budget-pill" type="button" title="Open budget controls" aria-label="Open budget controls">
                    <span class="chat-budget-text" id="chat-budget-text">Loading…</span>
                    <div class="chat-budget-bar">
                        <div class="chat-budget-bar-fill" id="chat-budget-bar-fill"></div>
                    </div>
                </button>
                <span id="chat-status" class="status-badge offline">Connecting...</span>
            `,
        });
    page.innerHTML = `
        ${headerHtml}
        <div id="chat-messages"></div>
        <div id="chat-input-area">
            <div id="chat-attachment-preview" class="chat-attachment-preview"></div>
            <div class="chat-input-wrap">
                <div class="chat-toolbar-row">
                    <div class="chat-composer-pills" id="chat-composer-pills">
                        <button class="chat-swarm" id="chat-swarm" type="button" data-armed="false" title="Swarm: route your next message into a new managed task, run a deep plan review with plan_task, then delegate when parallel work helps. Auto-disarms after sending.">Swarm</button>
                        <div class="chat-context-mode" id="chat-context-mode" data-context-mode="max" role="group" aria-label="Context size mode" title="Context mode (owner setting). Low fits ~200K / local models; Max is full. Saves immediately; lowering to Low requires Ouroboros to be idle.">
                            <button class="chat-seg" type="button" data-mode="low">Low</button>
                            <button class="chat-seg" type="button" data-mode="max">Max</button>
                        </div>
                    </div>
                </div>
                <div class="chat-text-row">
                    <button class="chat-attach-btn" id="chat-attach" type="button" title="Attach file">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                    </button>
                    <input type="file" id="chat-file-input" class="chat-file-input-hidden" accept="*/*" multiple>
                    <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
                    <div class="chat-send-group">
                        <button class="chat-scroll-bottom-btn" id="chat-scroll-bottom" type="button" aria-label="Scroll to latest message" title="Scroll to latest message">
                            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/></svg>
                        </button>
                        <button class="chat-send-inline" id="chat-send" title="Send message">Send</button>
                    </div>
                </div>
            </div>
        </div>
    `;
    if (idPrefix !== 'chat') {
        // Instance-namespaced ids + mirror classes so the shared #chat-* CSS
        // (extended with .chat-* twins) keeps styling secondary instances.
        page.querySelectorAll('[id]').forEach((el) => {
            if (el.id.startsWith('chat-')) {
                el.classList.add(el.id);
                el.id = idPrefix + '-' + el.id.slice(5);
            }
        });
    }
    container.appendChild(page);

    const byId = (suffix) => page.querySelector(`[id="${idPrefix}-${suffix}"]`);
    const messagesDiv = byId('messages');
    const input = byId('input');
    const inputArea = byId('input-area');
    const sendBtn = byId('send');
    const statusBadge = byId('status');
    const headerActions = byId('header-actions');
    const pageHeader = page.querySelector('.chat-page-header');
    const budgetPill = byId('budget-pill');
    const attachBtn = byId('attach');
    const fileInput = byId('file-input');
    const attachmentPreview = byId('attachment-preview');
    const scrollBottomBtn = byId('scroll-bottom');
    let pendingAttachments = [];
    let attachmentsUploading = false;
    let nestedSubagentsExpanded = false;

    // Instance lifecycle (P3): destroy() flips this so rAF loops and late async
    // continuations become no-ops instead of touching a removed DOM subtree.
    let destroyed = false;
    // Every ws.on subscription's disposer, released together in destroy().
    const wsDisposers = [];
    const onWs = (event, fn) => wsDisposers.push(ws.on(event, fn));

    async function loadUiPreferences() {
        try {
            const prefs = await apiClient.uiPreferences();
            if (destroyed) return;
            nestedSubagentsExpanded = prefs?.nested_subagents_expanded === true;
        } catch {
            nestedSubagentsExpanded = false;
        }
    }

    function updateAttachmentPreview() {
        if (!pendingAttachments.length) {
            attachmentPreview.classList.remove('visible');
            attachmentPreview.innerHTML = '';
            requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
            return;
        }
        attachmentPreview.classList.add('visible');
        attachmentPreview.innerHTML = pendingAttachments.map((item) => `
            <span class="attach-badge" data-attachment-id="${escapeHtmlAttr(item.id)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
                <span class="attach-name" title="${escapeHtmlAttr(item.display_name)}">${escapeHtml(item.display_name)}</span>
                <button class="attach-remove" type="button" title="Remove" aria-label="Remove attachment ${escapeHtmlAttr(item.display_name)}" data-attachment-remove="${escapeHtmlAttr(item.id)}" ${attachmentsUploading ? 'disabled aria-disabled="true"' : ''}>×</button>
            </span>
        `).join('');
        requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
        attachmentPreview.querySelectorAll('[data-attachment-remove]').forEach((button) => {
            button.addEventListener('click', () => {
                if (attachmentsUploading) return;
                const removeId = button.getAttribute('data-attachment-remove') || '';
                pendingAttachments = pendingAttachments.filter((item) => item.id !== removeId);
                updateAttachmentPreview();
            });
        });
    }

    // Shared paperclip/paste stager; upload still happens only on Send.
    function stagePendingFiles(files) {
        const incoming = Array.from(files || []).filter(Boolean);
        if (!incoming.length) return;
        if (attachmentsUploading) {
            showToast('Wait for the current upload to finish before changing attachments.', 'error');
            return;
        }
        if (pendingAttachments.length + incoming.length > MAX_PENDING_ATTACHMENTS) {
            showToast(`Attach up to ${MAX_PENDING_ATTACHMENTS} files per message.`, 'error');
            return;
        }
        const oversized = incoming.find((file) => Number(file.size || 0) > MAX_ATTACHMENT_FILE_BYTES);
        if (oversized) {
            showToast(`Each attachment must be ${Math.round(MAX_ATTACHMENT_FILE_BYTES / (1024 * 1024))} MB or smaller.`, 'error');
            return;
        }
        const incomingBytes = incoming.reduce((total, file) => total + Number(file.size || 0), 0);
        if (pendingAttachmentBytes(pendingAttachments) + incomingBytes > MAX_PENDING_ATTACHMENT_BYTES) {
            const limitMb = Math.round(MAX_PENDING_ATTACHMENT_BYTES / (1024 * 1024));
            showToast(`Attachments are limited to ${limitMb} MB total per message.`, 'error');
            return;
        }
        pendingAttachments = pendingAttachments.concat(incoming.map((file) => ({
            id: (globalThis.crypto && typeof crypto.randomUUID === 'function')
                ? crypto.randomUUID()
                : `attachment-${Date.now()}-${Math.random().toString(16).slice(2)}`,
            file,
            display_name: file.name || 'upload',
        })));
        updateAttachmentPreview();
    }

    async function cleanupUploadedAttachments(uploaded) {
        const filenames = uploaded
            .map((item) => item.filename)
            .filter(Boolean);
        if (!filenames.length) return;
        const results = await Promise.allSettled(filenames.map(async (filename) => {
            const resp = await apiFetch('/api/chat/upload', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename }),
            });
            if (!resp.ok) throw new Error(`DELETE ${filename} failed with HTTP ${resp.status}`);
        }));
        const failed = results.filter((result) => result.status === 'rejected');
        if (failed.length) {
            console.warn('Failed to clean up uploaded chat attachments after send failure', failed);
        }
    }

    function setAttachmentUploadState(uploading) {
        attachmentsUploading = uploading;
        attachBtn.disabled = uploading;
        attachBtn.classList.toggle('uploading', uploading);
        fileInput.disabled = uploading;
        input.disabled = uploading;
        updateAttachmentPreview();
    }

    attachBtn.addEventListener('click', () => fileInput.click());

    // Local-only staging avoids orphan uploads and fast-send races.
    fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = '';
        stagePendingFiles(files);
    });

    // Image paste uses the same stager; only image matches call preventDefault().
    // Timestamped names keep repeated clipboard images distinct.
    input.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        const pastedImages = [];
        for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            if (item && item.kind === 'file' && typeof item.type === 'string' && item.type.startsWith('image/')) {
                const blob = item.getAsFile();
                if (!blob) continue;
                const ext = (item.type.split('/')[1] || 'png').split(';')[0].trim() || 'png';
                const ts = Date.now() + i;
                const safeBlob = blob instanceof File
                    ? new File([blob], `clipboard-${ts}.${ext}`, { type: blob.type })
                    : new File([blob], `clipboard-${ts}.${ext}`, { type: item.type });
                pastedImages.push(safeBlob);
            }
        }
        if (!pastedImages.length) return;
        e.preventDefault();
        stagePendingFiles(pastedImages);
    });

    let fileDragDepth = 0;
    function setFileDragActive(active) {
        inputArea.classList.toggle('drag-active', Boolean(active));
    }
    page.addEventListener('dragenter', (event) => {
        if (!isFileDrag(event)) return;
        event.preventDefault();
        fileDragDepth += 1;
        setFileDragActive(true);
    });
    page.addEventListener('dragover', (event) => {
        if (!isFileDrag(event)) return;
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
        setFileDragActive(true);
    });
    page.addEventListener('dragleave', (event) => {
        if (!isFileDrag(event)) return;
        fileDragDepth = Math.max(0, fileDragDepth - 1);
        if (fileDragDepth === 0) setFileDragActive(false);
    });
    page.addEventListener('drop', (event) => {
        if (!isFileDrag(event)) return;
        event.preventDefault();
        fileDragDepth = 0;
        setFileDragActive(false);
        stagePendingFiles(event.dataTransfer?.files || []);
    });

    // Pass 1 builds live cards in memory; pass 2 inserts them in transcript order.
    let _syncPass1Active = false;
    // perf2 P4 follow-up (double-fetch fix): true while syncHistory replays the
    // fetched rows into cards — pass 1, pass 2 AND the terminal-resolution
    // sweep (both the rebuildAll and the routine branch). Finished transitions
    // raised inside that replay must not schedule the 700ms post-completion
    // resync: the data just came from the canonical source. The replay block
    // is fully synchronous, so no live WS frame can ever observe the flag.
    let _historyReplayActive = false;

    const persistedHistory = [];
    const seenMessageKeys = new Set();
    const messageKeyOrder = [];
    const pendingUserBubbles = new Map();
    const inputHistory = loadChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY);
    let inputHistoryIndex = inputHistory.length;
    let inputDraft = '';
    let historyLoaded = false;
    let inputHistorySeededFromServer = false; // set true only after a successful server-side recall seed
    let historySyncPromise = null;
    let lastHistorySyncSucceeded = false;
    let historyPaintGeneration = 0;
    // perf2 P4.1 [GPT#12 + Fable#1]: STICKY single-flight hydration promise.
    // Unlike historySyncPromise it survives success, so hydration triggers
    // (bootstrap IIFE, first non-reconnect socket open, refreshHistory without
    // a new revision) short-circuit instead of refetching. Any FAILED sync
    // resets it; scheduleHistorySync and the reconnect path never consult it.
    let initialHydrationPromise = null;
    // perf2 P4.1 [GPT#17]: the offline bootstrap painted the sessionStorage
    // fallback and set historyLoaded=true — the first successful sync after
    // the server comes back must still rebuild the feed from durable history.
    let offlineBootstrapPainted = false;
    // perf2 P4.1: highest project revision whose history has been fetched;
    // refreshHistory only bypasses the sticky promise for a NEWER revision.
    let lastLoadedHistoryRevision = 0;
    // perf2 P4.2: one-shot idle gate for Main's deferred first hydration.
    let hydrationGatePromise = null;
    // perf2 P4.3: non-null only inside a rebuildAll replay — routes per-row
    // feed insertion / meta / count / layout / typing / status / persist
    // through one end-of-batch application. Routine syncs never set it.
    let _rebuildBatch = null;
    // perf2 P4.5: server window verdict + the explicit Load-older quotas.
    let historyWindow = null;
    let historyQuotaOverride = null;
    let loadingOlderHistory = false;
    let welcomeShown = false;
    // Per-instance viewport intent. Content growth does not emit a user scroll,
    // so `_savedStick` survives a large live-card mutation that would make a
    // post-mutation `isNearBottom()` check lose the owner's prior intent.
    // A recreated project instance seeds these from the scroll state stashed by
    // app.js when its predecessor was destroyed (single-live-panel policy);
    // `_initialScrollPending` defers the actual restore until first paint.
    let _savedScrollTop = Math.max(0, Number(initialScrollState?.scrollTop) || 0);
    let _savedStick = initialScrollState ? initialScrollState.stick !== false : true;
    let _initialScrollPending = Boolean(initialScrollState) && !_savedStick;
    let _restoring = false;
    let _viewportMutationDepth = 0;
    const isInstanceVisible = () =>
        Boolean(messagesDiv) && messagesDiv.offsetParent !== null && !document.hidden;
    const liveCardRecords = new Map();
    const markReviewAnchor = (r, on = false) => setReviewAnchor(r, on, setLiveCardPhase);
    const explicitCardExpansion = new Map();
    const reviewDisclosureByTask = new Map();
    const skillReviewDetailStore = new Map();
    const reviewHydrator = createReviewHydrator({
        fetchDetail: fetchTaskDetailStrict,
        applyDetail: (id, d) => !destroyed && attachTaskDetailReviews(id, d),
        onState: (id, status) => !destroyed
            && liveCardRecords.get(id)?.reviewController?.setHydrateStatus?.(status),
    });
    // A task_named frame can arrive before the card's record exists; buffer it.
    const pendingSuggestedNames = new Map();
    const taskUiStates = new Map();
    // Busy-chat decision turns reuse the normal agent/event path for ordering and
    // observability, but they are not user tasks. Their structural backend marker
    // suppresses the transient card while preserving the typed routing annotation
    // and any non-empty final conversational answer as separate UI roles.
    const ephemeralDecisionTaskIds = new Set();
    // Server-confirmed in-flight direct/ephemeral/managed activities.
    const activeDirectActivities = new Map();
    // Local user submissions awaiting server confirmation (clientMessageId
    // -> { clientMessageId, timestamp }).
    const pendingSubmissions = new Map();
    // Bounded conclusions block late root typing and stale state snapshots; reusable
    // logical task slots are cleared whenever their cycle settles.
    const concludedDirectActivities = new Map();
    const CONCLUDED_ACTIVITY_LEDGER_MAX = 200;
    // Retryable queue-loss candidates plus process-local single-flight reads.
    const missingManagedTaskIds = new Set();
    const managedTaskDetailReads = new Set();
    // Owner rows kept until history confirms client_message_id.
    const localEchoJournal = new Map();
    const LOCAL_ECHO_JOURNAL_MAX = 50;

    function recordLocalEcho(clientMessageId, text, ts) {
        if (!clientMessageId) return;
        localEchoJournal.set(clientMessageId, { clientMessageId, text, ts, annotation: null });
        while (localEchoJournal.size > LOCAL_ECHO_JOURNAL_MAX) {
            localEchoJournal.delete(localEchoJournal.keys().next().value);
        }
    }

    function recordConcludedActivity(activityId) {
        const aid = String(activityId || '').trim();
        if (!aid) return;
        missingManagedTaskIds.delete(aid);
        concludedDirectActivities.delete(aid);
        concludedDirectActivities.set(aid, Date.now());
        while (concludedDirectActivities.size > CONCLUDED_ACTIVITY_LEDGER_MAX) {
            const oldest = concludedDirectActivities.keys().next().value;
            concludedDirectActivities.delete(oldest);
        }
    }
    function recordTerminalActivity(taskId) {
        const id = String(taskId || '').trim();
        if (!id) return;
        activeDirectActivities.delete(id);
        missingManagedTaskIds.delete(id);
        if (REUSABLE_TASK_IDS.has(id)) concludedDirectActivities.delete(id);
        else recordConcludedActivity(id);
    }
    // Finished task ids hidden from routine syncs until reload/reconnect rebuilds history.
    const retiredTaskIds = new Set();
    // The owner's last main-chat request, handed to the next live card it spawns so a
    // "turn into project" conversion can name the project from it (P1).
    let _pendingCardObjective = '';
    let activeLiveGroupId = '';
    let pendingReconnectSync = false;  // Set when a fromReconnect sync arrives while one is already in-flight.
    let pendingReconnectBannerText = readPendingReconnectBanner();

    function registerEphemeralDecisionFrame(frame) {
        return withStableViewport(() => registerEphemeralDecisionFrameMutation(frame));
    }

    function registerEphemeralDecisionFrameMutation(frame) {
        const taskId = String(frame?.task_id || '').trim();
        if (!taskId) return false;
        if (frame?.ephemeral_decision) {
            ephemeralDecisionTaskIds.add(taskId);
            const taskState = taskUiStates.get(taskId);
            if (taskState?.cleanupTimer) clearTimeout(taskState.cleanupTimer);
            taskUiStates.delete(taskId);
            const record = liveCardRecords.get(taskId);
            if (record) {
                record.root?.remove();
                liveCardRecords.delete(taskId);
            }
            pendingSuggestedNames.delete(taskId);
            if (activeLiveGroupId === taskId) activeLiveGroupId = '';
        }
        return ephemeralDecisionTaskIds.has(taskId);
    }

    function readPendingReconnectBanner() {
        try {
            const url = new URL(window.location.href);
            return reconnectBannerText(url.searchParams.get('_ouro_reason') || '');
        } catch {
            return '';
        }
    }

    function clearPendingReconnectBanner() {
        try {
            const url = new URL(window.location.href);
            if (!url.searchParams.has('_ouro_reason') && !url.searchParams.has('_ouro_refresh')) return;
            url.searchParams.delete('_ouro_reason');
            url.searchParams.delete('_ouro_refresh');
            window.history.replaceState({}, '', url);
        } catch {}
    }

    function rememberMessageKey(key) {
        if (!key || seenMessageKeys.has(key)) return;
        seenMessageKeys.add(key);
        messageKeyOrder.push(key);
        if (messageKeyOrder.length > 2000) {
            const oldest = messageKeyOrder.shift();
            if (oldest) seenMessageKeys.delete(oldest);
        }
    }

    function stampNodeTimestamp(node, raw, { anchor = false } = {}) {
        if (!node) return false;
        const epoch = rawTimestampEpoch(raw);
        if (!Number.isFinite(epoch)) return false;
        if (anchor && node.dataset.ts) {
            const current = Number(node.dataset.ts);
            const next = Number.isFinite(current) ? Math.min(current, epoch) : epoch;
            node.dataset.ts = String(next);
            return Number.isFinite(current) && next < current;
        } else {
            node.dataset.ts = String(epoch);
        }
        return false;
    }

    function setStatus(kind, text) {
        // perf2 P4.3: replay frames never touch the badge; the reducer
        // (syncChatStatus) writes it once after the batch.
        if (_rebuildBatch || !statusBadge) return;
        statusBadge.className = `status-badge ${kind}`;
        statusBadge.textContent = text;
    }

    function syncHeaderControlState(data) {
        headerActions?.querySelectorAll('[data-chat-command]').forEach((button) => {
            const cmd = button.dataset.chatCommand;
            if (cmd === 'evolve') {
                button.classList.toggle('on', !!data?.evolution_enabled);
                if (data?.evolution_state?.detail) button.title = data.evolution_state.detail;
            } else if (cmd === 'bg') {
                button.classList.toggle('on', !!data?.bg_consciousness_enabled);
                if (data?.bg_consciousness_state?.detail) button.title = data.bg_consciousness_state.detail;
            }
        });
        // Mark More while background mode is active in the menu.
        const moreSummary = headerActions?.querySelector('.chat-header-more > summary');
        if (moreSummary) {
            const anyActive = !!data?.evolution_enabled || !!data?.bg_consciousness_enabled;
            moreSummary.classList.toggle('has-active', anyActive);
        }
        const ctxBtn = byId('context-mode');
        if (ctxBtn && typeof data?.context_mode === 'string') {
            ctxBtn.dataset.contextMode = data.context_mode === 'low' ? 'low' : 'max';
        }
        const budget = headerBudgetPresentation(data);
        const budgetText = byId('budget-text');
        const budgetFill = byId('budget-bar-fill');
        if (budgetText) budgetText.textContent = budget.label;
        if (budgetFill) budgetFill.style.width = `${budget.fillPct}%`;
    }

    function hydrateStateSnapshot(data, snapshotRequestedAt = Infinity, snapshotGeneration = 0) {
        syncHeaderControlState(data);
        const activities = Array.isArray(data?.active_chat_activities)
            ? data.active_chat_activities
            : data?.active_direct_turns;
        if (Array.isArray(activities)) {
            hydrateDirectActivities(activities, snapshotRequestedAt, snapshotGeneration);
        }
    }

    async function refreshHeaderControlState(force = false) {
        if (!force && state.activePage !== 'chat') return;
        const request = stateSnapshots.begin();
        try {
            const resp = await apiFetch('/api/state', { cache: 'no-store' });
            if (!resp.ok) {
                if (stateSnapshots.isCurrent(request)) {
                    syncHeaderControlState({ accounting: { available: false } });
                }
                return;
            }
            const data = await resp.json();
            stateSnapshots.apply(request, data);
        } catch {
            if (stateSnapshots.isCurrent(request)) {
                syncHeaderControlState({ accounting: { available: false } });
            }
        }
    }

    function persistVisibleHistory() {
        try {
            sessionStorage.setItem(storeKey(CHAT_STORAGE_KEY), JSON.stringify(persistedHistory.slice(-200)));
        } catch {}
    }

    const NEAR_BOTTOM_THRESHOLD_PX = 160;

    function isNearBottom(threshold = NEAR_BOTTOM_THRESHOLD_PX) {
        const remaining = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
        return remaining <= threshold;
    }

    function captureVisibleTimelineAnchor(excludeNode = null) {
        // The Load-older control is excluded like .typing-bubble [GPT#13]:
        // anchoring must land on the first visible TIMESTAMPED node, or a
        // Load-older restore would pin the button itself and drift the view.
        const nodes = Array.from(messagesDiv.children).filter(
            (node) => node !== excludeNode
                && !excludeNode?.contains?.(node)
                && !node.classList.contains('typing-bubble')
                && !node.classList.contains('chat-load-older')
        );
        const messagesRect = messagesDiv.getBoundingClientRect();
        const topNode = nodes.find((item) => {
            const rect = item.getBoundingClientRect();
            return rect.bottom > messagesRect.top && rect.top < messagesRect.bottom;
        }) || null;
        if (!topNode) return null;

        // A live-card can span several screens while the reader is inside a
        // child summary or timeline line. Preserve that visible boundary, not
        // merely the root card whose own top may be far above the viewport.
        let node = topNode;
        if (topNode.classList.contains('chat-live-card')) {
            const selector = [
                '.chat-live-card',
                '[data-live-summary-button]',
                '[data-live-title]',
                '[data-live-activity]',
                '[data-live-meta]',
                '.chat-live-actions',
                '.chat-live-line',
                '.chat-live-project-card-btn',
            ].join(',');
            const candidates = [topNode, ...topNode.querySelectorAll(selector)]
                .map((candidate) => {
                    let depth = 0;
                    let parent = candidate === topNode ? null : candidate.parentElement;
                    while (parent && topNode.contains(parent) && parent !== topNode) {
                        depth += 1;
                        parent = parent.parentElement;
                    }
                    return { node: candidate, rect: candidate.getBoundingClientRect(), depth };
                })
                .filter(({ node: candidate, rect }) => candidate.getClientRects().length
                    && rect.width > 0
                    && rect.height > 0
                    && rect.bottom > messagesRect.top
                    && rect.top < messagesRect.bottom);
            const belowTop = candidates
                .filter(({ rect }) => rect.top >= messagesRect.top)
                .sort((a, b) => (a.rect.top - b.rect.top) || (b.depth - a.depth));
            const crossing = candidates
                .filter(({ rect }) => rect.top <= messagesRect.top && rect.bottom > messagesRect.top)
                .sort((a, b) => b.depth - a.depth);
            node = belowTop[0]?.node || crossing[0]?.node || topNode;
        }

        const cardChain = [];
        let card = node.classList.contains('chat-live-card')
            ? node
            : node.closest?.('.chat-live-card');
        while (card && messagesDiv.contains(card)) {
            cardChain.push({
                node: card,
                taskId: card.dataset?.taskId || '',
                offset: card.getBoundingClientRect().top - messagesRect.top,
            });
            card = card.parentElement?.closest?.('.chat-live-card') || null;
        }

        const ts = topNode.dataset?.ts || '';
        const anchorRole = [
            '[data-live-summary-button]',
            '[data-live-title]',
            '[data-live-activity]',
            '[data-live-meta]',
            '.chat-live-actions',
            '.chat-live-project-card-btn',
        ].find((candidate) => node.matches?.(candidate)) || '';
        return {
            node,
            cardChain,
            lineKey: node.matches?.('.chat-live-line') ? (node.dataset?.liveLineKey || '') : '',
            anchorRole,
            topNode,
            clientMessageId: topNode.dataset?.clientMessageId || '',
            ts,
            ordinal: ts ? nodes.filter((item) => item.dataset?.ts === ts).indexOf(topNode) : -1,
            offset: node.getBoundingClientRect().top - messagesRect.top,
            topOffset: topNode.getBoundingClientRect().top - messagesRect.top,
        };
    }

    function restoreVisibleTimelineAnchor(anchor) {
        if (!anchor) return false;
        const isRendered = (node) => {
            if (!node?.isConnected || !messagesDiv.contains(node)) return false;
            const rect = node.getBoundingClientRect();
            return node.getClientRects().length > 0 && rect.width > 0 && rect.height > 0;
        };
        const restoreNode = (node, offset) => {
            if (!isRendered(node)) return false;
            const currentOffset = node.getBoundingClientRect().top
                - messagesDiv.getBoundingClientRect().top;
            messagesDiv.scrollTop += currentOffset - offset;
            return true;
        };

        if (restoreNode(anchor.node, anchor.offset)) return true;

        const cardChain = Array.isArray(anchor.cardChain) && anchor.cardChain.length
            ? anchor.cardChain
            : [];
        const resolveCard = (entry) => {
            if (isRendered(entry?.node)) return entry.node;
            if (!entry?.taskId) return null;
            const record = liveCardRecords.get(entry.taskId);
            return isRendered(record?.root) ? record.root : null;
        };
        const ownerCard = resolveCard(cardChain[0]);
        if (ownerCard && anchor.lineKey) {
            const line = Array.from(ownerCard.querySelectorAll('.chat-live-line'))
                .find((candidate) => candidate.dataset?.liveLineKey === anchor.lineKey
                    && candidate.closest('.chat-live-card') === ownerCard);
            if (restoreNode(line, anchor.offset)) return true;
        }
        if (ownerCard && anchor.anchorRole) {
            const roleNode = Array.from(ownerCard.querySelectorAll(anchor.anchorRole))
                .find((candidate) => candidate.closest('.chat-live-card') === ownerCard);
            if (restoreNode(roleNode, anchor.offset)) return true;
        }
        for (const entry of cardChain) {
            if (restoreNode(resolveCard(entry), entry.offset)) return true;
        }

        let node = isRendered(anchor.topNode) ? anchor.topNode : null;
        if (!node && anchor.clientMessageId) {
            node = Array.from(messagesDiv.children).find(
                (item) => item.dataset?.clientMessageId === anchor.clientMessageId
            ) || null;
        }
        if (!node && anchor.ts) {
            const matches = Array.from(messagesDiv.children).filter((item) => item.dataset?.ts === anchor.ts);
            node = matches[anchor.ordinal] || matches[0] || null;
        }
        return restoreNode(node, anchor.topOffset ?? anchor.offset);
    }

    function withStableViewport(mutate) {
        if (typeof mutate !== 'function') return undefined;
        if (_viewportMutationDepth > 0 || _restoring || !isInstanceVisible()) return mutate();

        const followBottom = _savedStick || isNearBottom();
        const anchor = followBottom ? null : captureVisibleTimelineAnchor();
        _viewportMutationDepth = 1;
        try {
            return mutate();
        } finally {
            _viewportMutationDepth = 0;
            if (isInstanceVisible()) {
                if (followBottom) messagesDiv.scrollTop = messagesDiv.scrollHeight;
                else restoreVisibleTimelineAnchor(anchor);
                _savedScrollTop = messagesDiv.scrollTop;
                _savedStick = followBottom || isNearBottom();
                updateScrollButton();
            }
        }
    }

    function insertMessageNode(node, options = {}) {
        if (!node) return;
        // perf2 P4.3 (rebuildAll only): collect into the detached batch. One
        // stable sort + one fragment mount replace per-row chronological
        // insertion; the end-of-sync anchor restore replaces the per-row
        // insertedAboveViewport compensation. Routine syncs and live frames
        // (batch inactive) keep the chronological insertTimelineNode path.
        if (_rebuildBatch) {
            _rebuildBatch.collect(node);
            return;
        }
        const shouldStick = Boolean(options.forceStick) || isNearBottom();
        const isMounted = node.parentNode === messagesDiv;
        if (isMounted && !options.reorderExisting) {
            if (shouldStick) messagesDiv.scrollTop = messagesDiv.scrollHeight;
            updateScrollButton();
            return;
        }
        const reorderAnchor = isMounted && !shouldStick
            ? captureVisibleTimelineAnchor(node)
            : null;
        // Scope to THIS instance's column — a global id lookup would resolve to
        // the first panel's typing node and misplace project-thread messages.
        const typing = messagesDiv.querySelector('.typing-bubble');
        insertTimelineNode(messagesDiv, node, typing, { stickToBottom: shouldStick });
        if (reorderAnchor) restoreVisibleTimelineAnchor(reorderAnchor);
        // A new message arriving while the user is scrolled up reveals the
        // jump-to-newest button instead of silently piling up off-screen.
        updateScrollButton();
    }

    function createTaskUiState(taskId) {
        if (!taskId) return null;
        const taskState = {
            taskId,
            toolCalls: 0,
            forceCard: false,
            cardVisible: false,
            completed: false,
            completedPhase: '',
            bufferedLiveUpdates: [],
            cleanupTimer: null,
        };
        taskUiStates.set(taskId, taskState);
        return taskState;
    }

    function getTaskUiState(taskId = '', createIfMissing = true) {
        if (!taskId) return null;
        if (taskUiStates.has(taskId)) return taskUiStates.get(taskId);
        return createIfMissing ? createTaskUiState(taskId) : null;
    }

    function scheduleTaskUiCleanup(taskState, delayMs = 120000) {
        if (!taskState) return;
        if (taskState.cleanupTimer) clearTimeout(taskState.cleanupTimer);
        taskState.cleanupTimer = setTimeout(() => {
            taskUiStates.delete(taskState.taskId);
            // Keep the finished card interactive, but mark it retired so routine
            // syncs do not rebuild duplicates. Reload/reconnect clears this set.
            if (!REUSABLE_TASK_IDS.has(taskState.taskId) && taskState.taskId !== '') {
                retiredTaskIds.add(taskState.taskId);
            }
        }, delayMs);
    }

    function bufferLiveUpdate(taskState, summary, ts, dedupeKey = '', rawTs = '') {
        if (!taskState || !summary) return;
        taskState.bufferedLiveUpdates.push({
            summary,
            ts,
            rawTs,
            dedupeKey: dedupeKey || summary.dedupeKey || '',
        });

    }

    function reanchorTaskCard(
        record,
        rawTs,
        { suppressDomInsert = false } = {},
        seen = new Set(),
    ) {
        if (!record || seen.has(record.groupId)) return false;
        seen.add(record.groupId);
        const movedEarlier = stampNodeTimestamp(record.root, rawTs, { anchor: true });
        if (record.isSubagent) {
            const parent = liveCardRecords.get(record.parentGroupId);
            const parentMoved = reanchorTaskCard(parent, rawTs, { suppressDomInsert }, seen);
            return movedEarlier || parentMoved;
        }
        if (!movedEarlier) return false;
        if (suppressDomInsert || _syncPass1Active) {
            record._anchorOrderDirty = true;
            return true;
        }
        insertMessageNode(record.root, { reorderExisting: true });
        record._anchorOrderDirty = false;
        return true;
    }

    function reanchorVisibleTaskCard(taskState, rawTs, options = {}) {
        if (!taskState?.cardVisible) return false;
        return reanchorTaskCard(liveCardRecords.get(taskState.taskId), rawTs, options);
    }

    function revealBufferedCardIfNeeded(taskState, { suppressDomInsert = false, rawTs = '' } = {}) {
        return withStableViewport(() => revealBufferedCardMutation(
            taskState, { suppressDomInsert, rawTs },
        ));
    }

    function revealBufferedCardMutation(taskState, { suppressDomInsert = false, rawTs = '' } = {}) {
        if (!taskState) return;
        if (taskState.cardVisible) {
            reanchorVisibleTaskCard(taskState, rawTs, { suppressDomInsert });
            return;
        }
        if (!(taskState.forceCard || taskState.toolCalls > 0 || shouldAlwaysShowTaskCard(taskState.taskId))) {
            return;
        }
        taskState.cardVisible = true;
        activeLiveGroupId = taskState.taskId;
        const subagentInfo = subagentChildParents.get(taskState.taskId);
        const record = subagentInfo
            ? getSubagentCardRecord(
                taskState.taskId,
                subagentInfo.parentId,
                subagentInfo.role,
            )
            : getLiveCardRecord(taskState.taskId);
        let anchorMovedEarlier = false;
        if (!record.isSubagent) {
            anchorMovedEarlier = stampNodeTimestamp(record.root, rawTs, { anchor: true });
            for (const update of taskState.bufferedLiveUpdates) {
                anchorMovedEarlier = stampNodeTimestamp(
                    record.root, update.rawTs, { anchor: true }
                ) || anchorMovedEarlier;
            }
        }
        ensureLiveCardVisible(record, { suppressDomInsert, reorderExisting: anchorMovedEarlier });
        const bufferedUpdates = [...taskState.bufferedLiveUpdates];
        taskState.bufferedLiveUpdates = [];
        for (const update of bufferedUpdates) {
            applyLiveCardState(update.summary, taskState.taskId, update.ts, update.dedupeKey, {
                suppressDomInsert,
                rawTs: update.rawTs,
            });
        }
        if (taskState.completed) {
            finishLiveCard(taskState.taskId, taskState.completedPhase || 'done');
        }
    }

    function markTaskToolCall(taskId, count = 1, minimumOnly = false, rawTs = '') {
        const taskState = getTaskUiState(taskId, true);
        if (!taskState) return null;
        const safeCount = Math.max(0, Number(count) || 0);
        if (minimumOnly) {
            taskState.toolCalls = Math.max(taskState.toolCalls, safeCount);
        } else {
            taskState.toolCalls += safeCount;
        }
        revealBufferedCardIfNeeded(taskState, { rawTs });
        return taskState;
    }

    function forceTaskCard(taskId, rawTs = '') {
        const taskState = getTaskUiState(taskId, true);
        if (!taskState) return null;
        taskState.forceCard = true;
        revealBufferedCardIfNeeded(taskState, { rawTs });
        return taskState;
    }

    function markAssistantReply(taskId = '') {
        const resolvedTaskId = taskId || '';
        if (!resolvedTaskId) return;
        const taskState = getTaskUiState(resolvedTaskId, false);
        if (!taskState) return;
        taskState.completed = true;
        taskState.completedPhase = taskState.completedPhase || 'done';
        if (!taskState.cardVisible) {
            scheduleTaskUiCleanup(taskState, 30000);
            return;
        }
        scheduleTaskUiCleanup(taskState);
    }

    function markTaskComplete(taskId = '', phase = '') {
        const taskState = getTaskUiState(taskId, false);
        if (!taskState) return;
        taskState.completed = true;
        if (phase) taskState.completedPhase = phase;
    }

    // v6.82 (P5): task ids whose progress carried the supervisor's host-attested
    // `cancelable` marker (queue tasks the cancel endpoint can genuinely reach).
    // Learned from live WS frames and history replay alike, possibly before the
    // card exists, so it lives beside the card records rather than on them.
    const cancelableTaskIds = new Set();

    function queueTaskLiveUpdate(summary, taskId, ts, dedupeKey = '', rawTs = '') {
        return withStableViewport(() => queueTaskLiveUpdateMutation(
            summary, taskId, ts, dedupeKey, rawTs,
        ));
    }

    function queueTaskLiveUpdateMutation(summary, taskId, ts, dedupeKey = '', rawTs = '') {
        const resolvedTaskId = taskId || activeLiveGroupId || '';
        if (!resolvedTaskId) return;
        const taskState = getTaskUiState(resolvedTaskId, true);
        if (!taskState) return;
        // Even an already-completed card must absorb an earlier historical/nested
        // event into its chronology anchor before lifecycle policy ignores the
        // event's content.
        reanchorVisibleTaskCard(taskState, rawTs);
        if (taskState.completed && !isTerminalTaskPhase(summary.phase || '', summary.terminal)) {
            // A non-terminal event on a reusable id starts a fresh visible cycle.
            if (REUSABLE_TASK_IDS.has(resolvedTaskId)) {
                if (taskState.cleanupTimer) clearTimeout(taskState.cleanupTimer);
                taskState.completed = false;
                taskState.completedPhase = '';
                taskState.cardVisible = false;
                taskState.bufferedLiveUpdates = [];
                taskState.toolCalls = 0;
                taskState.forceCard = false;
                const oldRec = liveCardRecords.get(resolvedTaskId);
                if (oldRec) {
                    oldRec.root?.remove();
                    liveCardRecords.delete(resolvedTaskId);
                }
                retiredTaskIds.delete(resolvedTaskId);
            } else {
                return;
            }
        }
        if (summary.phase === 'error' || summary.phase === 'timeout' || (summary.terminal && summary.phase === 'warn')) {
            taskState.forceCard = true;
        }
        if (!taskState.cardVisible) {
            bufferLiveUpdate(taskState, summary, ts, dedupeKey, rawTs);
            revealBufferedCardIfNeeded(taskState, { rawTs });
            return;
        }
        applyLiveCardState(summary, resolvedTaskId, ts, dedupeKey, { rawTs });
    }

    async function turnTaskIntoProject(record) {
        if (!record || record.root?.dataset?.projectCreating === '1' || record.root?.dataset?.projectCreated === '1') return;
        const taskId = String(record.groupId || '').trim();
        const projectId = projectIdFromTask(taskId);
        record.root.dataset.projectCreating = '1';
        const actions = record.turnProjectBtn?.parentElement || record.root.querySelector('.chat-live-actions');
        if (actions) {
            withStableViewport(() => {
                actions.innerHTML = '<button type="button" class="btn btn-xs btn-default" disabled>Creating project…</button>';
                record.cancelRunBtn = null;
            });
        }
        try {
            // One-click convert (owner P1): no name prompt, no extra LLM call.
            // The SERVER derives the project name (gateway/projects.py
            // _derive_project_name: title -> objective -> queue snapshot). We also
            // hand it the owner's original request as a fallback hint so a still
            // in-progress DIRECT chat task — which has no server-side title/objective
            // yet — is named from what the owner asked, not "New project".
            const payload = await apiClient.projectFromTask(taskId, projectId, '', record.objectiveHint || '');
            const project = payload.project || { id: projectId, name: projectId };
            showToast(`Project created: ${project.name || project.id}`, 'ok');
            window.dispatchEvent(new CustomEvent('ouro:project-created', { detail: { project } }));
            markCardConverted(record, project);
        } catch (exc) {
            showToast(`Project creation failed: ${exc.message || exc}`, 'error');
            delete record.root.dataset.projectCreating;
            if (actions) {
                withStableViewport(() => {
                    actions.innerHTML = '<button type="button" class="btn btn-xs btn-default" data-turn-into-project>Turn into project</button>';
                    record.turnProjectBtn = actions.querySelector('[data-turn-into-project]');
                    // Re-wire the click handler — innerHTML replaced the original node,
                    // so without this the restored button would be dead after a
                    // transient failure (T5).
                    record.turnProjectBtn?.addEventListener('click', (event) => {
                        event.stopPropagation();
                        turnTaskIntoProject(record);
                    });
                    // P5: innerHTML also dropped a rendered "Cancel run" — restore it.
                    record.cancelRunBtn = null;
                    syncCancelRunButton(record);
                });
            }
        }
    }

    // v6.82 (P5): "Cancel run" on live pooled ROOT cards. Forced cancel of the
    // selected task AND its live subtree (explicit cascade — the endpoint's
    // default stays single-task for headless callers). Gated on the supervisor's
    // host-attested `cancelable` marker so a direct-chat turn (which mints a
    // card of the same shape but has no queue entry) never shows a dead button.
    function ensureLiveActionsEl(record) {
        if (!record?.root
            || record.root.dataset.projectCreated === '1'
            || record.root.dataset.projectCreating === '1') return null;
        let actions = record.root.querySelector('.chat-live-actions');
        if (!actions) {
            actions = document.createElement('div');
            actions.className = 'chat-live-actions';
            const timeline = record.timelineEl && record.timelineEl.parentElement === record.root
                ? record.timelineEl
                : null;
            record.root.insertBefore(actions, timeline);
        }
        return actions;
    }

    function syncCancelRunButton(record) {
        return withStableViewport(() => syncCancelRunButtonMutation(record));
    }

    function syncCancelRunButtonMutation(record) {
        if (!record?.root) return;
        const eligible = cancelRunEligibility({
            groupId: record.groupId,
            isSubagent: record.isSubagent,
            finished: record.finished,
            cancelable: cancelableTaskIds.has(record.groupId),
            converted: record.root.dataset.projectCreated === '1',
        });
        const existing = record.root.querySelector('[data-cancel-run]');
        if (!eligible) {
            existing?.remove();
            record.cancelRunBtn = null;
            return;
        }
        if (existing) {
            record.cancelRunBtn = existing;
            return;
        }
        const actions = ensureLiveActionsEl(record);
        if (!actions) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-xs btn-danger';
        btn.dataset.cancelRun = '1';
        btn.textContent = TASK_CONTROL_TRIGGER_LABEL;
        // S3 (Q2/HQ1): the trigger opens the three-action dropdown; dismissing
        // it continues the run. While a cancel intent is pending the menu
        // offers ONLY the hard escalation ("Stop now").
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            openTaskControlMenu(btn, {
                cancelPending: Boolean(record.cancelPendingPolicy),
                busy: taskControlBusy(record.groupId),
                onAction: (action) => (action === ACTION_HURRY
                    ? hurryTaskAction(record.groupId)
                    : cancelRunFromCard(record, action)),
            });
        });
        actions.appendChild(btn);
        record.cancelRunBtn = btn;
    }

    // Pending intent stays live until settled; soft stop shows Finalizing….
    function markLiveCardCancelPending(taskId = '', soft = false) {
        const record = liveCardRecords.get(String(taskId || '').trim());
        if (!record || record.finished || !record.phaseEl) return;
        record.cancelPendingPolicy = soft ? 'finalize' : 'immediate';
        record.finalizingHold = false;  // owner cancel outranks the hold
        setLiveCardPhase(
            record, 'working', soft ? 'Finalizing…' : 'Cancelling…',
            'chat-live-phase working cancelling',
        );
    }

    // Early final stays live while post-task synthesis runs.
    function markLiveCardFinalizing(taskId = '') {
        const record = liveCardRecords.get(String(taskId || '').trim());
        if (!record || record.finished || !record.phaseEl) return;
        markReviewAnchor(record);
        if (record.cancelPendingPolicy) return;
        record.finalizingHold = true;
        setLiveCardPhase(record, 'working', 'Finalizing…', 'chat-live-phase working finalizing');
    }

    // Snapshot / restore of the live phase state around the optimistic
    // "Cancelling…" mark (GR2-8a): a cancel request that FAILS must not leave
    // the optimistic phase lying on a card whose cancellation is not pending,
    // or erase an existing post-task finalization hold on the next progress frame.

    // Task-detail reconciliation for the cancel flow (GR2-8b): the typed
    // cancel_state projection is consulted FIRST — a live task wedged in the
    // legacy `cancel_requested` STATUS latch (intent, not outcome) must show
    // as cancel-pending, not resolve as a terminal "Cancelled" while the
    // supervisor is still tearing it down. Only genuinely settled statuses
    // (or an intent-free legacy latch, which is history awaiting boot
    // migration) fall through to the terminal seam.
    function reconcileCancelCardFromDetail(record, taskId, stored) {
        if (!stored || !record || record.finished) return;
        if (taskCancelPending(stored)) {
            markLiveCardCancelPending(taskId, taskSoftStopPending(stored));
            return;
        }
        if (taskDoneIsTerminal(stored)) {
            appendTaskSummaryToLiveCard({ ...stored, task_id: taskId });
        }
    }

    async function cancelRunFromCard(record, action = '') {
        const taskId = String(record?.groupId || '').trim();
        if (!taskId || record.finished) return;
        // Q2: the dropdown itself is the confirmation surface — dismissing it
        // continued the run, so a selected action executes immediately.
        const soft = action === ACTION_FINALIZE;
        const btn = record.cancelRunBtn;
        if (btn) btn.disabled = true;
        const priorPhase = captureLiveCardPhaseState(record);
        markLiveCardCancelPending(taskId, soft);
        try {
            await requestStop(taskId, action);
            // Durable detail heals lost best-effort task_done publication
            try {
                reconcileCancelCardFromDetail(record, taskId, await fetchTaskDetail(taskId));
            } catch {
                // The card still resolves on its own frame if one arrives.
            }
            // Soft stop keeps hard escalation reachable while finalizing.
            if (btn && !record.finished && record.cancelPendingPolicy === 'finalize') {
                btn.disabled = false;
            }
        } catch (exc) {
            if (exc?.status === 404 || record.finished) {
                // Completion won: remove the dead action, then reconcile detail.
                cancelableTaskIds.delete(taskId);
                record.cancelable = false;
                syncCancelRunButton(record);
                try {
                    reconcileCancelCardFromDetail(record, taskId, await fetchTaskDetail(taskId));
                } catch {
                    // A later terminal frame can still resolve the card.
                }
                return;
            }
            showToast(`Cancel failed: ${exc?.message || exc}`, 'error');
            // Reconcile durable truth before restoring any optimistic UI.
            let stored = null;
            try {
                stored = await fetchTaskDetail(taskId);
            } catch {}
            if (stored === null) {
                // Only a fetched, live, non-pending detail restores the button.
                return;
            }
            reconcileCancelCardFromDetail(record, taskId, stored);
            const stillPending = Boolean(taskCancelPending(stored));
            if (record.finished || stillPending) return;
            if (btn) btn.disabled = false;
            const restoredPhase = restoreLiveCardPhaseState(record, priorPhase);
            if (restoredPhase) {
                setLiveCardPhase(
                    record, restoredPhase.phase, restoredPhase.text, restoredPhase.className,
                );
            }
        }
    }

    function markTaskCancelable(taskId = '') {
        const id = String(taskId || '').trim();
        if (!id || cancelableTaskIds.has(id)) return;
        cancelableTaskIds.add(id);
        const record = liveCardRecords.get(id);
        if (record) syncCancelRunButton(record);
    }

    // One-way conversion (P3): the WHOLE card becomes a calm "project identity"
    // chip. The live task is now owned by the project panel (it's bound there),
    // so the main chat is freed — the card stops being a busy red task and
    // recolors to the project fuchsia. Plain wording (no "ack"); click opens the panel.
    function markCardConverted(record, project) {
        return withStableViewport(() => markCardConvertedMutation(record, project));
    }

    function markCardConvertedMutation(record, project) {
        delete record.root.dataset.projectCreating;
        record.root.dataset.projectCreated = '1';
        record.root.dataset.projectId = project.id || '';
        const name = String(project.name || project.id || 'Project').trim();
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chat-live-project-card-btn';
        const icon = document.createElement('span');
        icon.className = 'chat-live-project-icon';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = '📁';
        const nameEl = document.createElement('span');
        nameEl.className = 'chat-live-project-name';
        nameEl.textContent = name;  // textContent — no HTML injection from a project name
        const status = document.createElement('span');
        status.className = 'chat-live-project-status';
        status.textContent = 'running in background ↗';
        chip.append(icon, nameEl, status);
        chip.addEventListener('click', () => {
            window.dispatchEvent(new CustomEvent('ouro:open-project', { detail: { project } }));
        });
        // Atomic detach-and-reparent (C4.5): replaceChildren swaps the whole live
        // timeline (subagent cards, working bubble) for the chip in one paint.
        record.root.replaceChildren(chip);
        record.turnProjectBtn = null;
        record.cancelRunBtn = null;
        record.finished = true;
        // Recolor on the next frame so the 250ms fuchsia fade actually animates.
        requestAnimationFrame(() => record.root.classList.add('is-project'));
        signalChatFreed();  // subtle "this chat is free again" composer cue
    }

    // A brief composer brighten when a task leaves the main chat for a project —
    // a calm "you're free to start something else" signal (P3). Self-clearing.
    let _chatFreedTimer = null;
    function signalChatFreed() {
        const row = page.querySelector('.chat-text-row');
        if (!row) return;
        row.classList.add('chat-freed');
        if (_chatFreedTimer) clearTimeout(_chatFreedTimer);
        _chatFreedTimer = setTimeout(() => row.classList.remove('chat-freed'), 900);
    }

    function attachReviewGroup(group, rawTs = '') {
        const ownerTaskId = String(group?.presentationOwnerTaskId || '').trim();
        if (!ownerTaskId) return false;
        if (retiredTaskIds.has(ownerTaskId) && !liveCardRecords.has(ownerTaskId)) return true;
        const reviewAnchor = !liveCardRecords.has(ownerTaskId)
            && !taskUiStates.has(ownerTaskId) && !activeDirectActivities.has(ownerTaskId);
        const ownerState = forceTaskCard(ownerTaskId, rawTs);
        if (!ownerState?.cardVisible) return false;
        const record = liveCardRecords.get(ownerTaskId);
        if (reviewAnchor) markReviewAnchor(record, true);
        const merged = record?.reviewController?.update(group);
        if (merged) {
            if (!_syncPass1Active) ensureLiveCardVisible(record);
            if (rawTs) reanchorVisibleTaskCard(ownerState, rawTs);
            if (!record.reviewOwnerDetailObserved) {
                record.reviewOwnerDetailObserved = true;
                observeMissingManagedTask(ownerTaskId);
            }
        }
        return Boolean(merged);
    }

    function attachTaskDetailReviews(taskId, detail) {
        const id = String(taskId || '').trim();
        const groups = reviewGroupsFromTaskDetail(detail, id);
        if (!id || groups.length === 0) return false;
        if (!liveCardRecords.has(id)) forceTaskCard(id, detail?.ts || detail?.timestamp || '');
        const record = liveCardRecords.get(id);
        if (!record?.reviewController) return false;
        const changed = record.reviewController.updateMany(groups);
        reconcileCancelCardFromDetail(record, id, detail);
        return changed;
    }

    function hydrateCardReviews(taskId, revision = null) {
        return destroyed ? Promise.resolve(false) : reviewHydrator.hydrate(taskId, revision);
    }

    function attachReviewFromRow(row, rawTs = '', showPointerAck = false) {
        const pointer = classifyReviewLifecyclePointer(row);
        if (pointer.classification !== 'not_pointer') {
            const record = pointer.group && liveCardRecords.get(pointer.group.presentationOwnerTaskId);
            if (record?.reviewController) record.reviewController.update(pointer.group);
            else if (showPointerAck) {
                const ack = String(row?.text || row?.content || '').trim();
                if (ack) addMessage(
                    ack, 'assistant', !!row?.markdown, rawTs || row?.ts || null,
                    true, { systemType: 'lifecycle_pointer' },
                );
            }
            return true;
        }
        const historyGroup = reviewGroupFromHistoryRow(row);
        if (historyGroup) {
            return attachReviewGroup(historyGroup, rawTs || row?.ts || row?.timestamp || '');
        }
        const lifecycle = classifyReviewLifecycle(row);
        if (lifecycle.classification === 'source_complete') {
            const attached = attachReviewGroup(lifecycle.group, rawTs || row?.ts || row?.timestamp || '');
            if (lifecycle.group.activeCount === 0 && lifecycle.group.lifecycleStatus) scheduleHistorySync();
            return attached;
        }
        return lifecycle.classification === 'source_incomplete';
    }

    function handleReviewReference(row) {
        const reference = reviewReferenceFromRow(row);
        if (!reference) return false;
        const owner = reference.presentationOwnerTaskId;
        // A reference proves a review exists: mint the card so errors have a home.
        forceTaskCard(owner, row?.ts || '');
        hydrateCardReviews(owner, reference.stateRevision);
        return true;
    }

    function createLiveCardRecord(groupId = '', options = {}) {
        const normalizedGroupId = groupId || `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const timelineId = `chat-live-timeline-${normalizedGroupId.replace(/[^A-Za-z0-9_-]/g, '-')}`;
        const root = document.createElement('div');
        root.className = 'chat-live-card';
        root.dataset.taskId = normalizedGroupId;
        if (options.isSubagent) {
            root.classList.add('subagent');
            root.dataset.subagent = '1';
            root.dataset.parentTaskId = String(options.parentGroupId || '');
            root.dataset.subagentRole = String(options.role || '');
        }
        root.dataset.finished = '0';
        const initialExpanded = explicitCardExpansion.has(normalizedGroupId)
            ? explicitCardExpansion.get(normalizedGroupId)
            : Boolean(options.isSubagent && nestedSubagentsExpanded);
        root.dataset.expanded = initialExpanded ? '1' : '0';
        // No "Turn into project" for: subagent cards, non-main panels, or a task that
        // is ALREADY bound to a project (a project-chat follow-up) — see task_bindings
        // from /api/state, surfaced on window.__ouroTaskBindings (P2).
        const alreadyBound = !!(window.__ouroTaskBindings || {})[normalizedGroupId];
        const projectActionHtml = (
            isMain
            && !options.isSubagent
            && !alreadyBound
            && !ephemeralDecisionTaskIds.has(normalizedGroupId)
        )
            ? `<div class="chat-live-actions"><button type="button" class="btn btn-xs btn-default" data-turn-into-project>Turn into project</button></div>`
            : '';
        root.innerHTML = `
            <button type="button" class="chat-live-summary-button" data-live-summary-button aria-expanded="false" aria-controls="${escapeHtmlAttr(timelineId)}">
                <div class="chat-live-summary">
                    <div class="chat-live-summary-main">
                        <span class="chat-live-phase working" data-live-phase role="status" aria-live="polite" aria-atomic="true" aria-label="${options.isSubagent ? 'Subagent' : 'Task'} status: Working">Working</span>
                        <div class="chat-live-typing" data-live-typing aria-hidden="true">
                            <span></span><span></span><span></span>
                        </div>
                        <span class="chat-live-title" data-live-title>Waiting for work</span>
                    </div>
                    <div class="chat-live-summary-side">
                        <span class="chat-live-count" data-live-count hidden>2 notes</span>
                        <span class="chat-live-toggle" data-live-toggle>Show details</span>
                        <svg class="chat-live-chevron" width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                            <path d="M5 7.5 10 12.5 15 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"></path>
                        </svg>
                    </div>
                </div>
                <div class="chat-live-activity" data-live-activity></div>
                <div class="chat-live-meta" data-live-meta></div>
                <div class="chat-live-review-summary" data-live-review-summary hidden></div>
            </button>
            ${projectActionHtml}
            <div class="chat-live-timeline" data-live-timeline id="${escapeHtmlAttr(timelineId)}"></div>
            <div data-live-reviews-host></div>
        `;
        const record = {
            groupId: normalizedGroupId,
            root,
            summaryButtonEl: root.querySelector('[data-live-summary-button]'),
            phaseEl: root.querySelector('[data-live-phase]'),
            inlineTypingEl: root.querySelector('[data-live-typing]'),
            titleEl: root.querySelector('[data-live-title]'),
            activityEl: root.querySelector('[data-live-activity]'),
            countEl: root.querySelector('[data-live-count]'),
            metaEl: root.querySelector('[data-live-meta]'),
            reviewSummaryEl: root.querySelector('[data-live-review-summary]'),
            toggleEl: root.querySelector('[data-live-toggle]'),
            turnProjectBtn: root.querySelector('[data-turn-into-project]'),
            // P5: "Cancel run" button element (rendered lazily by syncCancelRunButton
            // once the host-attested cancelable marker is known for this task).
            cancelRunBtn: null,
            timelineEl: root.querySelector('[data-live-timeline]'),
            reviewsHostEl: root.querySelector('[data-live-reviews-host]'),
            updates: 0,
            finished: false,
            items: [],
            lastHumanHeadline: '',
            expandedLineKeys: new Set(),
            isSubagent: Boolean(options.isSubagent),
            parentGroupId: String(options.parentGroupId || ''),
            subagentRole: String(options.role || ''),
            subagentsEl: null,
            _anchorOrderDirty: false,
            // perf2 P4.4: collapsed timelines defer DOM building; the flag says
            // the rendered timeline DOM is stale relative to record.items.
            _timelineDirty: false,
            // perf2 P4.3: last frame's summary meta strings — meta renders from
            // record state (renderLiveCardMeta), once per card in a batch.
            _lastFrameMeta: [],
            // Hidden-page layout sync is deferred until page/visibility returns.
            _needsLayoutSync: false,
            // The owner's request that spawned this card (main, non-subagent only),
            // used to name a project on "turn into project" when the server has no
            // title/objective yet (P1, direct-chat conversion). One-shot handoff.
            objectiveHint: (isMain && !options.isSubagent) ? _pendingCardObjective : '',
            // The proactively-coined LLM name; becomes the card title when set.
            suggestedName: '',
            // P1 (v6.82): last bounded activity projection (remembered even while
            // the collapsed line is suppressed on unnamed root cards) + sticky cost.
            collapsedActivity: '',
            costMeta: null,
            reviewOwnerDetailObserved: false,
        };
        const reviewDisclosure = reviewDisclosureByTask.get(normalizedGroupId) || {
            sectionExpanded: false,
            expandedGroups: new Set(),
            expandedAttempts: new Set(),
        };
        reviewDisclosureByTask.set(normalizedGroupId, reviewDisclosure);
        record.reviewController = createReviewPresentationController({
            host: record.reviewsHostEl,
            summary: record.reviewSummaryEl,
            disclosure: reviewDisclosure,
            onHydrate: () => hydrateCardReviews(normalizedGroupId),
            onLoadSkillDetail: (detail, detailOptions = {}) => loadSkillReviewDetail(
                detail,
                nestedSkillReviewRef(detail),
                { store: skillReviewDetailStore, retry: detailOptions.retry === true },
            ).then(() => !destroyed && syncLiveCardLayout(record)),
            onLayout: () => syncLiveCardLayout(record),
        });
        if (isMain && !options.isSubagent) _pendingCardObjective = '';
        record.summaryButtonEl?.addEventListener('click', () => {
            const nowExpanded = record.root.dataset.expanded !== '1';
            explicitCardExpansion.set(record.groupId, nowExpanded);
            setLiveCardExpanded(record, nowExpanded);
            if (nowExpanded) hydrateCardReviews(record.groupId);
        });
        record.turnProjectBtn?.addEventListener('click', (event) => {
            event.stopPropagation();
            turnTaskIntoProject(record);
        });
        record.timelineEl?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-live-line-toggle]');
            // Row-surface disclosure (v6.71.0): any click on the line's
            // NON-interactive surface toggles it (guards live in the pure
            // helper: nested interactive elements, active text selection).
            const lineKey = button
                ? (button.dataset.liveLineToggle || '')
                : liveLineRowToggleKey(event.target, window.getSelection?.());
            if (!lineKey) return;
            const nowExpanded = !record.expandedLineKeys.has(lineKey);
            if (nowExpanded) record.expandedLineKeys.add(lineKey);
            else record.expandedLineKeys.delete(lineKey);
            renderLiveCardTimeline(record);
            syncLiveCardLayout(record);
            // Keyboard/AT continuity: focus the rebuilt line's REAL toggle button.
            record.timelineEl
                ?.querySelector(`[data-live-line-toggle="${(window.CSS && CSS.escape) ? CSS.escape(lineKey) : lineKey}"]`)
                ?.focus?.({ preventScroll: true });
            // P3: on expand, lazily fetch the genuinely-full output for a server-truncated
            // line (the WS preview was capped at 4000); cached on the item so a re-render
            // keeps it. Best-effort — the capped preview stays on failure.
            if (nowExpanded) {
                const item = record.items.find((it) => it.lineKey === lineKey);
                if (item && item.truncated && item.fullRef && !item.fetchedFull && !item._fetchingFull) {
                    fetchFullLineOutput(item, record);
                }
            }
        });
        liveCardRecords.set(normalizedGroupId, record);
        // Cluster B: apply a name that arrived (task_named) before this card existed.
        const _pendingName = pendingSuggestedNames.get(normalizedGroupId);
        if (_pendingName && !record.isSubagent) {
            pendingSuggestedNames.delete(normalizedGroupId);
            record.suggestedName = _pendingName;
            if (record.titleEl) record.titleEl.textContent = _pendingName;
        }
        resetLiveCardRecord(record);
        // P5: the cancelable marker may have arrived (scheduled progress frame /
        // history replay) before this card was minted.
        syncCancelRunButton(record);
        return record;
    }

    function getLiveCardRecord(groupId = '') {
        const normalizedGroupId = groupId || activeLiveGroupId || 'chat';
        return liveCardRecords.get(normalizedGroupId) || createLiveCardRecord(normalizedGroupId);
    }

    // Cluster B: apply the proactively-coined project name to a main card already on
    // screen (live `task_named` event or history replay). A main card's groupId IS its
    // task_id, so the lookup is direct. No-op until the card exists / without a name.
    function applySuggestedName(taskId, name) {
        return withStableViewport(() => applySuggestedNameMutation(taskId, name));
    }

    function applySuggestedNameMutation(taskId, name) {
        const tid = String(taskId || '').trim();
        const nm = String(name || '').trim();
        if (!tid || !nm) return;
        const record = liveCardRecords.get(tid);
        if (!record) {
            // Card not created yet (the namer raced ahead of the first progress event).
            // Buffer so createLiveCardRecord applies it when the card appears.
            // FIFO cap: `task_named` is the one broadcast without a thread gate,
            // so every instance buffers every task's name — bound the buffer so
            // a long-lived instance cannot grow it without limit (P3).
            pendingSuggestedNames.set(tid, nm);
            if (pendingSuggestedNames.size > 100) {
                const oldest = pendingSuggestedNames.keys().next().value;
                pendingSuggestedNames.delete(oldest);
            }
            return;
        }
        if (record.isSubagent) return;
        record.suggestedName = nm;
        if (record.titleEl) record.titleEl.textContent = nm;
        // P1 (v6.82): the collapsed activity line was suppressed while the card
        // was unnamed; populate it now from the remembered candidate so the live
        // task_named direct-DOM path does not depend on the next frame.
        renderCollapsedActivity(record, projectCollapsedActivity({
            suggestedName: nm,
            headline: record.collapsedActivity,
            previous: record.collapsedActivity,
        }));
    }

    // One renderer for the bounded collapsed projection. Full narration is
    // owned by timeline disclosure, never by a mouse-only title attribute.
    function renderCollapsedActivity(record, text) {
        if (!record?.activityEl) return;
        record.activityEl.textContent = text;
        record.activityEl.removeAttribute('title');
    }

    function ensureSubagentContainer(parentId = '') {
        if (!parentId) return null;
        const parentRecord = getLiveCardRecord(parentId);
        let container = parentRecord.subagentsEl;
        if (!container) {
            container = document.createElement('div');
            parentRecord.subagentsEl = container;
        }
        container.className = 'chat-subagents';
        container.dataset.subagentsFor = parentId;
        const anchor = parentRecord.reviewsHostEl || parentRecord.timelineEl;
        if (container.parentNode !== parentRecord.root || container.previousElementSibling !== anchor) {
            anchor?.insertAdjacentElement('afterend', container);
        }
        return container;
    }

    function getSubagentCardRecord(childId = '', parentId = '', role = '') {
        return withStableViewport(() => getSubagentCardRecordMutation(childId, parentId, role));
    }

    function getSubagentCardRecordMutation(childId = '', parentId = '', role = '') {
        if (!childId || !parentId) return null;
        const existing = liveCardRecords.get(childId);
        const record = existing || createLiveCardRecord(childId, {
            isSubagent: true,
            parentGroupId: parentId,
            role,
        });
        record.isSubagent = true;
        record.parentGroupId = parentId;
        record.subagentRole = role || record.subagentRole || '';
        record.root.classList.add('subagent');
        record.root.dataset.subagent = '1';
        record.root.dataset.parentTaskId = parentId;
        record.root.dataset.subagentRole = record.subagentRole;
        if (existing && !explicitCardExpansion.has(childId)) {
            setLiveCardExpanded(record, nestedSubagentsExpanded);
        }
        const container = ensureSubagentContainer(parentId);
        if (container && record.root.parentNode !== container) {
            container.appendChild(record.root);
        }
        const parentRecord = liveCardRecords.get(parentId);
        if (parentRecord) updateLiveCardCount(parentRecord);
        return record;
    }

    function setLiveCardTypingVisible(record, visible) {
        if (!record?.inlineTypingEl) return;
        record.inlineTypingEl.style.display = visible ? '' : 'none';
    }

    function resetLiveCardRecord(record) {
        record.updates = 0;
        record.finished = false;
        record.items = [];
        record.lastHumanHeadline = '';
        record.expandedLineKeys.clear();
        record._anchorOrderDirty = false;
        record._timelineDirty = false;
        record._lastFrameMeta = [];
        clearStickyCardState(record);
        record.titleEl.textContent = 'Working...';
        setLiveCardPhase(record, 'working');
        record.countEl.hidden = true;
        record.countEl.textContent = '0 notes';
        record.metaEl.innerHTML = '';
        record.timelineEl.innerHTML = '';
        record.root.dataset.finished = '0';
        setLiveCardTypingVisible(record, true);
        const expanded = explicitCardExpansion.has(record.groupId)
            ? explicitCardExpansion.get(record.groupId)
            : Boolean(record.isSubagent && nestedSubagentsExpanded);
        setLiveCardExpanded(record, expanded);
    }

    function ensureLiveCardVisible(
        record,
        { suppressDomInsert = false, reorderExisting = false } = {},
    ) {
        if (record?.isSubagent && record.parentGroupId) {
            if (!suppressDomInsert && !_syncPass1Active) {
                const parentRecord = getLiveCardRecord(record.parentGroupId);
                if (parentRecord.isSubagent && parentRecord.parentGroupId) {
                    ensureLiveCardVisible(parentRecord);
                } else {
                    insertMessageNode(parentRecord.root);
                }
                const container = ensureSubagentContainer(record.parentGroupId);
                if (container && record.root.parentNode !== container) {
                    container.appendChild(record.root);
                }
                updateLiveCardCount(parentRecord);
            }
            return;
        }
        if (!record.isSubagent && !suppressDomInsert && !_syncPass1Active) {
            insertMessageNode(record.root, { reorderExisting });
        }
    }

    function setLiveCardExpanded(record, expanded) {
        const mutate = () => {
            if (!record?.root) return;
            record.root.dataset.expanded = expanded ? '1' : '0';
            // perf2 P4.4: first expand materializes a lazily-deferred timeline
            // (its DOM was skipped while the card was collapsed/display:none).
            if (expanded && record._timelineDirty) renderLiveCardTimeline(record);
            syncLiveCardToggle(record);
            if (record.root.isConnected) {
                requestAnimationFrame(() => syncLiveCardLayout(record));
            }
        };
        return record?.root?.isConnected ? withStableViewport(mutate) : mutate();
    }

    function syncLiveCardToggle(record) {
        if (!record?.toggleEl) return;
        const expanded = record.root.dataset.expanded === '1';
        record.toggleEl.textContent = expanded ? 'Hide details' : 'Show details';
        record.summaryButtonEl?.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }

    function directSubagentCount(record) {
        return record?.subagentsEl?.querySelectorAll(':scope > .chat-live-card.subagent').length || 0;
    }

    function updateLiveCardCount(record) {
        // perf2 P4.3: one count render per card at the end of a replay batch.
        if (_rebuildBatch) {
            _rebuildBatch.touch(record);
            return;
        }
        if (!record?.countEl) return;
        const bits = [];
        if (record.items.length >= 2) bits.push(`${record.items.length} notes`);
        const children = directSubagentCount(record);
        if (children) bits.push(`${children} ${children === 1 ? 'child' : 'children'}`);
        record.countEl.hidden = bits.length === 0;
        record.countEl.textContent = bits.join(' · ');
    }

    function syncLiveCardLayout(record) {
        // perf2 P4.3: one layout sync per card after the batch mount.
        if (_rebuildBatch) {
            _rebuildBatch.touch(record);
            return;
        }
        if (!record?.root) return;
        // Hidden SPA/browser tabs report zero geometry; defer to avoid collapsed
        // cards. Generalized to panel instances: any visible host counts.
        const activePage = record.root.closest('.page.active');
        const panelHost = record.root.closest('.chat-instance-panel');
        // A panel counts as visible only when it is actually shown (not a
        // hidden/display:none secondary instance) — zero geometry otherwise.
        const visibleHost = activePage || (panelHost && panelHost.offsetParent !== null);
        if (!visibleHost || document.hidden) {
            record._needsLayoutSync = true;
            return;
        }
        record._needsLayoutSync = false;
        if (record.isSubagent && record.parentGroupId) {
            const parentRecord = liveCardRecords.get(record.parentGroupId);
            if (parentRecord?.root?.isConnected) {
                requestAnimationFrame(() => syncLiveCardLayout(parentRecord));
            }
        }
    }

    // Re-sync cards after SPA return or browser tab visibility restore, then put
    // the thread back where the user left it (P7) instead of at the very top.
    // Named handlers so destroy() can remove them (P3 lifecycle).
    const handlePageShown = (event) => {
        if (event?.detail?.page !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (record?.root?.isConnected) syncLiveCardLayout(record);
        }
        restoreScrollPosition();  // no-op for hidden panel instances
    };
    window.addEventListener('ouro:page-shown', handlePageShown);
    const handleVisibilityChange = () => {
        if (document.hidden) return;
        if (state.activePage !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (record?.root?.isConnected && record._needsLayoutSync) syncLiveCardLayout(record);
        }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    function isTimelinePinnedToBottom(record) {
        const el = record?.timelineEl;
        if (!el) return true;
        return el.scrollHeight - el.scrollTop - el.clientHeight <= 24;
    }

    // perf2 P4.4 (lazy LINEAGE bodies): a collapsed SUBAGENT timeline is
    // display:none, so building its DOM during a bulk replay is pure waste.
    // Data stays complete in record.items; DOM writers defer through this
    // guard while collapsed, and the first setLiveCardExpanded(true)
    // materializes the timeline. TOP-LEVEL cards render eagerly: their
    // collapsed timeline text is part of the feed DOM contract (ui-smoke
    // asserts it), and the deep-lineage fan-out lives in subagent children.
    function deferCollapsedTimeline(record) {
        if (!record) return true;
        if (!record.isSubagent) return false;
        if (record.root?.dataset?.expanded === '1') return false;
        record._timelineDirty = true;
        return true;
    }

    // Full rebuild for initial render and expand/collapse toggles.
    function renderLiveCardTimeline(record) {
        if (deferCollapsedTimeline(record)) return undefined;
        record._timelineDirty = false;
        return withStableViewport(() => {
            const el = record.timelineEl;
            const pinned = isTimelinePinnedToBottom(record);
            const prevTop = el.scrollTop;
            el.innerHTML = record.items.map((item) => buildTimelineItemHtml(item, record)).join('');
            el.scrollTop = pinned ? el.scrollHeight : prevTop;
        });
    }

    // P3: fetch the genuinely-full output for a server-truncated timeline line (the WS
    // preview was capped at 4000 chars), cache it on the item, then re-render if the line
    // is still expanded. The full text is fetched on demand (not pushed over the socket)
    // and shown in a bounded-scroll box. Best-effort — the capped preview stays on failure.
    async function fetchFullLineOutput(item, record) {
        item._fetchingFull = true;
        try {
            const resp = await apiFetch(`/api/tasks/${encodeURIComponent(item.fullRef)}`, { cache: 'no-store' });
            const data = resp && typeof resp.json === 'function' ? await resp.json() : resp;
            // Compose ALL available full fields — a subagent line can carry both a result AND a
            // (separately truncated) trace_summary, so `result || trace_summary` would hide the
            // full trace. Label each section when both are present.
            const result = String((data && data.result) || '').trim();
            const trace = String((data && data.trace_summary) || '').trim();
            let full = '';
            if (result && trace) full = `[RESULT]\n${result}\n\n[TRACE]\n${trace}`;
            else full = result || trace;
            if (full) item.fetchedFull = full;
        } catch {
            // best-effort: leave the capped preview on failure
        } finally {
            item._fetchingFull = false;
            if (!destroyed && record.expandedLineKeys.has(item.lineKey)) {
                const hadFocus = Boolean(
                    document.activeElement?.closest?.(`[data-live-line-key="${(window.CSS && CSS.escape) ? CSS.escape(item.lineKey) : item.lineKey}"]`),
                );
                renderLiveCardTimeline(record);
                syncLiveCardLayout(record);
                if (hadFocus) {
                    record.timelineEl
                        ?.querySelector(`[data-live-line-toggle="${(window.CSS && CSS.escape) ? CSS.escape(item.lineKey) : item.lineKey}"]`)
                        ?.focus?.({ preventScroll: true });
                }
            }
        }
    }

    // Append without disturbing existing DOM nodes.
    function appendTimelineItem(item, record) {
        if (deferCollapsedTimeline(record)) return;
        // Expanded but stale (dirty was set while collapsed): patching the
        // stale DOM would target wrong nodes — materialize from items instead.
        if (record._timelineDirty) return renderLiveCardTimeline(record);
        const pinned = isTimelinePinnedToBottom(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const node = wrapper.firstElementChild;
        if (node) {
            record.timelineEl.appendChild(node);
            if (record.root.dataset.expanded === '1' && pinned) {
                record.timelineEl.scrollTop = record.timelineEl.scrollHeight;
            }
        }
    }

    // Patch the last DOM node for dedup/count bumps.
    function patchLastTimelineItem(item, record) {
        // perf2 P4.4 [GPT#15]: collapsed → mark dirty and leave; stale
        // expanded DOM → full materialization instead of a mismatched patch.
        if (deferCollapsedTimeline(record)) return;
        if (record._timelineDirty) return renderLiveCardTimeline(record);
        const lastEl = record.timelineEl.lastElementChild;
        if (!lastEl) return renderLiveCardTimeline(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const newNode = wrapper.firstElementChild;
        if (newNode) record.timelineEl.replaceChild(newNode, lastEl);
    }

    // Patch a specific timeline node in place (evolving subagent dashboard rows).
    function patchTimelineItemAt(item, record) {
        // perf2 P4.4 [GPT#15]: same dirty/collapsed discipline as patch-last.
        if (deferCollapsedTimeline(record)) return;
        if (record._timelineDirty) return renderLiveCardTimeline(record);
        const key = String(item.lineKey || '').replace(/[^A-Za-z0-9_-]/g, '');
        const el = key ? record.timelineEl.querySelector(`[data-live-line-key="${key}"]`) : null;
        if (!el) return renderLiveCardTimeline(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const newNode = wrapper.firstElementChild;
        if (newNode) record.timelineEl.replaceChild(newNode, el);
    }

    function scheduleHistorySync() {
        historyResyncScheduler.schedule();
    }

    const historyResyncScheduler = createHistoryResyncScheduler({
        isReplayActive: () => _historyReplayActive,
        run: () => syncHistory({ includeUser: false }).catch(() => {}),
    });

    // The 12 cost-meta keys shared by both subagent whitelists (the delegation
    // trio stays inline in each literal — the wire test scans those literals).
    function costMetaKeys(src) {
        return {
            cost_usd: src?.cost_usd,
            accounted_upper_bound_usd: src?.accounted_upper_bound_usd,
            accounted_upper_bound_usd_with_children: src?.accounted_upper_bound_usd_with_children,
            cost_accounting_status: src?.cost_accounting_status,
            cost_accounting_error: src?.cost_accounting_error,
            cost_final: src?.cost_final,
            cost_usd_with_children: src?.cost_usd_with_children,
            cost_with_children_partial: src?.cost_with_children_partial,
            reserved_usd: src?.reserved_usd,
            unresolved_upper_bound_usd: src?.unresolved_upper_bound_usd,
            unknown_unmetered: src?.unknown_unmetered,
            non_final_rows: src?.non_final_rows,
        };
    }

    // perf2 P4.3: the ONE meta-line renderer, fed entirely from record state,
    // so a replay batch renders it exactly once per card.
    function renderLiveCardMeta(record) {
        if (!record?.metaEl) return;
        const executorChipHtml = record.executorChip
            ? `<span class="harness-chip chat-live-executor-chip" title="${escapeHtmlAttr(record.executorChip.title || '')}">`
              + harnessIdentityMarkup(record.executorChip.harness, {
                  label: record.executorChip.label || '',
                  className: 'chat-live-executor-identity',
              })
              + '</span>'
            : '';
        record.metaEl.innerHTML = executorChipHtml + [
            record.groupId === 'bg-consciousness' ? 'Background thinking' : '',
            ...(Array.isArray(record._lastFrameMeta) ? record._lastFrameMeta : []),
            ...((record.costMeta && Array.isArray(record.costMeta.meta)) ? record.costMeta.meta : []),
            record.latestActivityTs ? `Latest ${record.latestActivityTs}` : '',
        ].filter(Boolean).map((item) => `<span class="chat-live-meta-text">${escapeHtml(item)}</span>`).join('');
    }

    function applyLiveCardState(summary, groupId, ts, dedupeKey = '', options = {}) {
        return withStableViewport(() => applyLiveCardStateMutation(
            summary, groupId, ts, dedupeKey, options,
        ));
    }

    function applyLiveCardStateMutation(summary, groupId, ts, dedupeKey = '', { suppressDomInsert = false, rawTs = '' } = {}) {
        const nextGroupId = groupId || activeLiveGroupId || 'active';
        const record = getLiveCardRecord(nextGroupId);
        // A converted card is now a terminal project chip — its task is owned by the
        // project panel. Ignore ALL further frames (incl. terminal) so they neither
        // overwrite the chip nor dereference the nulled element refs (P3).
        if (record.root?.dataset?.projectCreated === '1') return;
        const nextPhase = summary.phase || '';
        if (record.finished && !isTerminalTaskPhase(nextPhase, summary.terminal)) {
            // A late cost frame still settles the finished card's cost meta.
            if (summary.costProjection) {
                record.costMeta = mergeStickyCostMeta(record.costMeta, summary.costProjection);
                if (_rebuildBatch) _rebuildBatch.touch(record);
                else renderLiveCardMeta(record);
            }
            return;
        }
        markReviewAnchor(record);

        if (!record.isSubagent) {
            activeLiveGroupId = nextGroupId;
            reanchorTaskCard(record, rawTs, { suppressDomInsert });
        }
        ensureLiveCardVisible(record, { suppressDomInsert });
        record.updates += 1;
        const wasFinished = record.finished;
        // Prefer the last meaningful headline when an update carries none (e.g. a
        // structured terminal marker), so finishing a card doesn't blank its title.
        const headline = summary.headline || record.lastHumanHeadline || 'Working...';
        const syntheticKey = summary.dedupeKey || dedupeKey || `${summary.phase || 'working'}|${headline}|${summary.body || ''}`;
        const isLegacyParentSubagentKey = syntheticKey.startsWith('parent-subagent:');
        const inPlaceByKey = isLegacyParentSubagentKey
            || syntheticKey.startsWith('subagent-lifecycle:')
            || syntheticKey.startsWith('subagent-progress:')
            || syntheticKey.startsWith('subagent-result:')
            || syntheticKey.startsWith('task_done|');
        if (!isLegacyParentSubagentKey) {
            record.finished = isTerminalTaskPhase(nextPhase, summary.terminal);
        }
        if (summary.human && headline) {
            record.lastHumanHeadline = headline;
        }

        const shouldPromote = Boolean(summary.promote) || record.finished;
        const activeHeadline = shouldPromote
            ? headline
            : (record.lastHumanHeadline
                || (record.updates > 1 ? record.titleEl.textContent : '')
                || 'Working...');
        const activePhase = record.finished
            ? (summary.phase || 'done')
            : (shouldPromote ? (summary.phase || 'working') : (record.phaseEl.dataset.phase || 'working'));

        const desiredPhase = desiredLiveCardPhase(record, activePhase);
        setLiveCardPhase(record, desiredPhase.phase, desiredPhase.text, desiredPhase.className);
        // Cluster B: a coined project name takes the title slot; the live activity
        // headline still renders in the timeline lines below. Falls back to the
        // activity headline until the proactive namer has produced a name.
        record.titleEl.textContent = record.suggestedName || activeHeadline;
        // The collapsed line is a compact presentation projection, while the
        // complete latest activity remains independently reachable through the
        // expanded timeline. Root cards accept activity only from human frames;
        // terminal "Done" markers must not overwrite the last real action.
        const previewSource = record.isSubagent
            ? String(summary.activityPreview ?? summary.body ?? '')
            : (summary.human ? String(summary.activityPreview ?? activeHeadline ?? '') : '');
        const activityCandidate = previewSource.trim();
        if (activityCandidate) record.collapsedActivity = boundActivityPreview(activityCandidate);
        const activityText = projectCollapsedActivity({
            isSubagent: record.isSubagent,
            suggestedName: record.suggestedName,
            headline: record.isSubagent ? '' : record.collapsedActivity,
            body: record.isSubagent ? record.collapsedActivity : '',
            previous: record.collapsedActivity,
        });
        renderCollapsedActivity(record, activityText);

        const shouldRenderLine = summary.visible !== false && Boolean(headline || summary.body);
        // Legacy parent-subagent rows update in place if replayed from old
        // history. Child-card lifecycle/progress rows also evolve in place.
        let timelineUpdate = 'none';
        let patchIndex = -1;
        if (shouldRenderLine) {
            const lastIdx = record.items.length - 1;
            // Full-array dedup (Variant A): match the incoming line's key ANYWHERE in
            // the card, not only against the last item. Otherwise a background
            // syncHistory(rebuildAll=false) re-feeds historical progress lines whose
            // key != the last item, and each gets re-appended → the "Notes" count
            // grows without bound on every sync/reconnect.
            const existingIdx = record.items.findIndex((it) => it.dedupeKey === syntheticKey);
            if (existingIdx !== -1 && inPlaceByKey) {
                const it = record.items[existingIdx];
                it.phase = summary.phase || it.phase;
                it.headline = headline || it.headline;
                it.fullHeadline = summary.fullHeadline || headline || it.fullHeadline;
                it.body = summary.body || '';
                it.fullBody = summary.fullBody || summary.body || it.fullBody || '';
                it.fullRef = summary.fullRef || it.fullRef || '';
                it.truncated = summary.truncated || it.truncated || false;
                it.ts = ts || it.ts;
                patchIndex = existingIdx;
                timelineUpdate = 'patch-at';
            } else if (existingIdx === lastIdx && existingIdx !== -1) {
                // Consecutive live duplicate of the most recent line → coalesce count.
                const it = record.items[existingIdx];
                it.count += 1;
                it.ts = ts || it.ts;
                it.fullHeadline = summary.fullHeadline || it.fullHeadline || it.headline;
                it.fullBody = summary.fullBody || it.fullBody || it.body;
                it.fullRef = summary.fullRef || it.fullRef || '';
                it.truncated = summary.truncated || it.truncated || false;
                timelineUpdate = 'patch-last';
            } else if (existingIdx !== -1) {
                // Already rendered earlier in this card (e.g. a historical progress line
                // re-fed by a background sync). Do NOT re-append (the unbounded "Notes"
                // growth) and do NOT bump its count — just keep its timestamp fresh.
                const it = record.items[existingIdx];
                it.ts = ts || it.ts;
                timelineUpdate = 'duplicate-skip';
            } else {
                const lineKey = `line-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                record.items.push({
                    phase: summary.phase || 'working',
                    headline: headline || 'Update',
                    fullHeadline: summary.fullHeadline || headline || 'Update',
                    body: summary.body || '',
                    fullBody: summary.fullBody || summary.body || '',
                    fullRef: summary.fullRef || '',
                    truncated: summary.truncated || false,
                    ts: ts || '',
                    count: 1,
                    dedupeKey: syntheticKey,
                    lineKey,
                });
                timelineUpdate = 'append';
            }
        }
        updateLiveCardCount(record);
        // "Latest" is an ACTIVITY clock, not a bookkeeping clock: a cost-only frame
        // (task_cost_finalized and friends carry no human narration) must not make a
        // silent card look freshly active. Only frames that actually said something
        // move it.
        if (ts && (summary.human || activityCandidate)) record.latestActivityTs = ts;
        // P1 (v6.82): sticky cost — only frames carrying task-scope accounting
        // evidence attach a costProjection; a costless frame re-renders the
        // previous projection instead of erasing it.
        if (summary.costProjection) {
            record.costMeta = mergeStickyCostMeta(record.costMeta, summary.costProjection);
        }
        // Phase 6: the chip is STICKY — quiet frames never erase it, and an
        // evidence chip is never downgraded (keepStickyExecutorChip).
        if (summary.executorChip
                && !keepStickyExecutorChip(record.executorChip, summary.executorChip)) {
            record.executorChip = summary.executorChip;
        }
        // perf2 P4.3: meta renders from record state — immediately on the live
        // path, once per card at the end of a rebuildAll replay batch.
        record._lastFrameMeta = Array.isArray(summary.meta) ? summary.meta : [];
        if (_rebuildBatch) _rebuildBatch.touch(record);
        else renderLiveCardMeta(record);
        // Incremental updates; full rebuilds stay limited to toggles.
        const lastItem = record.items[record.items.length - 1];
        if (timelineUpdate === 'append' && lastItem) {
            appendTimelineItem(lastItem, record);
        } else if (timelineUpdate === 'patch-last' && lastItem) {
            patchLastTimelineItem(lastItem, record);
        } else if (timelineUpdate === 'patch-at' && patchIndex !== -1) {
            patchTimelineItemAt(record.items[patchIndex], record);
        }
        ensureLiveCardVisible(record, { suppressDomInsert });
        syncLiveCardLayout(record);
        hideTypingIndicatorOnly();
        const justFinished = record.finished && !wasFinished;
        const drivesComposerStatus = !isBackgroundTaskId(nextGroupId);
        // P5: a finished card must not keep offering "Cancel run". A log-channel
        // task_done terminates the card HERE without passing finishLiveCard, so
        // the cancelable marker must be dropped on this path too (P3 growth cap).
        if (justFinished) {
            record.root.dataset.finished = '1';
            cancelableTaskIds.delete(record.groupId);
            syncCancelRunButton(record);
        }
        if (record.finished) {
            setLiveCardTypingVisible(record, false);
            markTaskComplete(nextGroupId, summary.phase || 'done');
            if (justFinished) {
                scheduleHistorySync();
            }
            syncLiveCardToggle(record);
            if (drivesComposerStatus) syncChatStatus();
        } else {
            setLiveCardTypingVisible(record, true);
            if (drivesComposerStatus || !hasActiveLiveCard()) {
                syncChatStatus();
            }
        }
    }

    function finishLiveCard(groupId = '', phase = '') {
        return withStableViewport(() => finishLiveCardMutation(groupId, phase));
    }

    function finishLiveCardMutation(groupId = '', phase = '') {
        const record = groupId
            ? liveCardRecords.get(groupId)
            : (activeLiveGroupId ? liveCardRecords.get(activeLiveGroupId) : null);
        if (!record) return;
        // A converted card is a terminal project chip now — ignore late terminal
        // frames so they neither overwrite the chip nor touch its element refs (T4).
        if (record.root?.dataset?.projectCreated === '1') return;
        markReviewAnchor(record);
        const wasFinished = record.finished;
        record.finished = true;
        record.finalizingHold = false;
        // A finished task can never be cancelled again; dropping the marker here
        // keeps the set from accumulating every task id of a long session (P3).
        cancelableTaskIds.delete(record.groupId);
        syncCancelRunButton(record);
        const presentation = taskPresentation(phase || 'done');
        const activePhase = presentation.phase;
        setLiveCardPhase(record, activePhase, presentation.headline);
        if (!record.suggestedName && !record.lastHumanHeadline) {
            record.titleEl.textContent = presentation.headline;
        }
        setLiveCardTypingVisible(record, false);
        markTaskComplete(record.groupId, activePhase);
        if (!wasFinished) {
            record.root.dataset.finished = '1';
            scheduleHistorySync();
        }
        syncLiveCardToggle(record);
        if (activeLiveGroupId === record.groupId) activeLiveGroupId = '';
        syncChatStatus();
    }

    function appendTaskSummaryToLiveCard(msg, { suppressDomInsert = false } = {}) {
        const taskId = msg?.task_id || activeLiveGroupId || '';
        const rawTs = msg?.ts || new Date().toISOString();
        if (registerEphemeralDecisionFrame(msg)) return;
        if (!taskId) {
            finishLiveCard(taskId, 'done');
            return;
        }
        // Restore the coined task name even when history retained no progress row.
        if (msg?.suggested_name) applySuggestedName(taskId, msg.suggested_name);
        const finalizing = msg?.task_phase === 'finalizing';
        const projectedReviews = reviewGroupsFromTaskDetail(msg, taskId);
        const hasAcceptanceReview = projectedReviews.length > 0;
        const taskState = getTaskUiState(taskId, hasAcceptanceReview || finalizing);
        if (!taskState) {
            finishLiveCard(taskId, 'done');
            return;
        }
        if (hasAcceptanceReview) taskState.forceCard = true;
        revealBufferedCardIfNeeded(taskState, { suppressDomInsert, rawTs });
        if (!taskState.cardVisible) {
            if (!finalizing) markAssistantReply(taskId);
            return;
        }
        const presentation = taskPresentation(finalizing ? 'working' : taskTerminalPhase(msg || {}));
        // P5: a cancelled root says "Cancelled", never a generic "Done" headline.
        // №8/Q3: an owner-requested soft stop is a SUCCESS — its own headline,
        // never warn-styled, with the owner-request marker in the details.
        const softStopped = taskStoppedWithSummary(msg || {});
        const softStopDetail = softStopped ? OWNER_STOP_DETAIL_MARKER : '';
        const reasonDetail = !softStopped && msg?.reason_code
            ? `Reason: ${String(msg.reason_code)}` : '';
        const record = liveCardRecords.get(taskId);
        record?.reviewController?.updateMany(projectedReviews);
        if (finalizing && record && !record.finished) record.finalizingHold = true;
        applyLiveCardState(
            {
                phase: presentation.phase,
                headline: presentation.headline,
                body: [softStopDetail, reasonDetail].filter(Boolean).join('\n'),
                visible: Boolean(softStopDetail || reasonDetail),
                human: false,
                promote: true,
                terminal: !finalizing,
                costProjection: taskCostProjection(msg, rawTs),
            },
            taskId,
            normalizeLogTs(rawTs),
            `task_done|${taskId}`,
            { suppressDomInsert, rawTs },
        );
        if (finalizing) return;
        finishLiveCard(taskId, presentation.phase);
        scheduleTaskUiCleanup(taskState);
    }

    // child task_id -> { parentId, role }, learned from subagent lifecycle pings.
    // Child cards are mounted under the parent card, but their phase/terminal
    // state is independent so a finished child cannot mark the parent done.
    const subagentChildParents = new Map();
    // Children whose card has reached a terminal phase — late non-lifecycle
    // progress for these must NOT revive it back to "working".
    const subagentTerminalChildren = new Set();

    // E2 (v6.39 UI): merge a subagent's parent/role/model, PRESERVING a previously-seen model
    // when a later (model-less) event — e.g. a synthesized terminal — updates the entry, so the
    // "role · model" headline survives the child's lifecycle.
    function setSubagentParent(childId, { parentId = '', role = '', model = '' } = {}) {
        const prev = subagentChildParents.get(childId) || {};
        subagentChildParents.set(childId, {
            parentId: parentId || prev.parentId || '',
            role: role || prev.role || '',
            model: String(model || '').trim() || prev.model || '',
        });
    }

    function learnSubagentLineage(msg) {
        if (String(msg?.delegation_role || '').toLowerCase() !== 'subagent') return '';
        const parentId = String(msg.parent_task_id || '').trim();
        const childId = String(msg.subagent_task_id || msg.task_id || '').trim();
        if (!parentId || !childId || parentId === childId) return '';
        setSubagentParent(childId, {
            parentId, role: String(msg.subagent_role || '').trim(), model: msg.model,
        });
        const event = String(msg.subagent_event || '').toLowerCase();
        const replayTerminal = msg.task_terminal_status
            ? taskDoneIsTerminal({ ...msg, status: String(msg.task_terminal_status) })
            : false;
        if (replayTerminal || ['completed', 'completed_warn', 'failed', 'cancelled', 'rejected'].includes(event)) {
            subagentTerminalChildren.add(childId);
        }
        return childId;
    }

    function summarizeSubagentCardFrame(evt, overrides = {}, rawTs = '') {
        const summary = summarizeChatLiveEvent({
            ...evt,
            type: 'send_message',
            is_progress: true,
            delegation_role: 'subagent',
            ...overrides,
        });
        return summary ? withTaskCostMeta(summary, evt, { rawTs }) : null;
    }

    function updateLiveCardFromProgressMessage(msg, { grantCancelAuthority = true } = {}) {
        const taskId = msg?.task_id || activeLiveGroupId || '';
        const rawTs = msg?.ts || new Date().toISOString();
        if (registerEphemeralDecisionFrame(msg)) return;
        if (attachReviewFromRow(msg, rawTs)) return;
        if (!taskId) return;
        // Mirrored progress never grants local Stop authority.
        if (grantCancelAuthority && msg?.cancelable === true && msg?.task_id) {
            markTaskCancelable(String(msg.task_id));
        }
        // Child lifecycle pings must not update the parent's terminal state.
        const lifecycleParent = String(msg?.parent_task_id || '').trim();
        if (
            msg?.subagent_event
            && lifecycleParent
            && updateSubagentCardFromEvent(msg, rawTs)
        ) {
            return;
        }
        // Keep a known child's own progress on its card.
        if (subagentChildParents.has(taskId)) {
            routeSubagentProgressToCard(taskId, msg);
            return;
        }
        // Progress messages are visible status; do not force-open completed replay.
        const taskState = getTaskUiState(taskId, true);
        const restartReusable = taskState?.completed && REUSABLE_TASK_IDS.has(taskId);
        if (taskState && !taskState.completed) taskState.forceCard = true;
        const summary = summarizeChatLiveEvent({
            type: 'send_message',
            is_progress: true,
            content: msg?.content || msg?.text || '',
            text: msg?.content || msg?.text || '',
            task_id: taskId,
            subagent_event: msg?.subagent_event || '',
            subagent_task_id: msg?.subagent_task_id || '',
            root_task_id: msg?.root_task_id || '',
            parent_task_id: msg?.parent_task_id || '',
            delegation_role: msg?.delegation_role || '',
            subagent_role: msg?.subagent_role || '',
            // Delegation trio: a forgotten key freezes the chip
            // (wire_contract.test.js pins all three).
            executor_route: msg?.executor_route || '',
            execution_evidence: msg?.execution_evidence,
            actual_substrate: msg?.actual_substrate || '',
            status: msg?.status || '',
            ...costMetaKeys(msg),
            result: msg?.result || '',
            trace_summary: msg?.trace_summary || '',
            error: msg?.error || '',
            artifact_status: msg?.artifact_status || '',
            lifecycle: msg?.lifecycle || null,
        });
        if (!summary) return;
        const presented = withTaskCostMeta(summary, msg, { rawTs });
        queueTaskLiveUpdate(presented, taskId, normalizeLogTs(rawTs), presented.dedupeKey || '', rawTs);
        if (restartReusable) forceTaskCard(taskId, rawTs);
        // Cluster B: history progress recs carry the coined name (live progress does
        // not — the live path uses the separate `task_named` event). Apply it after the
        // card exists so a reload shows the same title.
        if (msg?.suggested_name) applySuggestedName(taskId, msg.suggested_name);
        // History projects authoritative terminal truth onto the latest progress
        // anchor when the best-effort task_summary row is absent. Apply that truth
        // before a later ordinary assistant row closes the card, so replay cannot
        // freeze a degraded review as a green completion.
        if (
            msg?.task_terminal_status
            && taskDoneIsTerminal({ ...msg, status: String(msg.task_terminal_status) })
            && (msg?.outcome_axes || msg?.review_projection || msg?.reason_code)
        ) {
            appendTaskSummaryToLiveCard(msg);
        }
    }

    function updateSubagentCardFromEvent(evt, tsValue) {
        if (!evt || String(evt.delegation_role || '').toLowerCase() !== 'subagent') return false;
        const parentId = String(evt.parent_task_id || '').trim();
        const childId = String(evt.subagent_task_id || evt.task_id || '').trim();
        if (!parentId || !childId || parentId === childId) return false;
        const event = String(evt.subagent_event || '').toLowerCase();
        const role = String(evt.subagent_role || '').trim();
        setSubagentParent(childId, { parentId, role, model: evt.model });
        // Worker narration carries subagent_event="progress" too. It is activity,
        // not a lifecycle row: route it through the progress key so the later
        // terminal frame cannot overwrite the only full narration disclosure.
        if (![
            'scheduled', 'running', 'completed', 'completed_warn',
            'failed', 'cancelled', 'rejected', 'interrupted',
        ].includes(event)) {
            routeSubagentProgressToCard(childId, evt);
            return true;
        }
        const { model } = subagentChildParents.get(childId) || {};
        const rawTs = tsValue || new Date().toISOString();
        const summary = summarizeSubagentCardFrame(evt, {
            subagent_task_id: childId,
            parent_task_id: parentId,
            subagent_role: role,
            model,
        }, rawTs);
        if (!summary) return false;
        summary.dedupeKey = `subagent-lifecycle:${childId}`;
        // Interrupted is retryable and therefore non-terminal; the canonical
        // projector owns that distinction for both live and replay paths.
        if (summary.terminal) subagentTerminalChildren.add(childId);
        forceTaskCard(parentId, tsValue);
        const childState = getTaskUiState(childId, true);
        if (childState && !childState.completed) childState.forceCard = true;
        getSubagentCardRecord(childId, parentId, role);
        attachTaskDetailReviews(childId, evt);
        queueTaskLiveUpdate(
            summary,
            childId,
            normalizeLogTs(rawTs),
            summary.dedupeKey,
            rawTs,
        );
        return true;
    }

    // A known child's own (non-lifecycle) progress updates the linked child card.
    function routeSubagentProgressToCard(childId, msg) {
        const info = subagentChildParents.get(childId);
        if (!info) return;
        const { parentId, role, model } = info;
        const content = String(msg?.content || msg?.text || '').trim();
        if (!content) return;
        const rawTs = msg?.ts || new Date().toISOString();
        forceTaskCard(parentId, rawTs);
        const childState = getTaskUiState(childId, true);
        if (childState && !childState.completed) childState.forceCard = true;
        const record = getSubagentCardRecord(childId, parentId, role);
        const preserveTerminal = Boolean(record?.finished && subagentTerminalChildren.has(childId));
        const summary = summarizeSubagentCardFrame(msg, {
            content,
            text: content,
            subagent_event: 'running',
            subagent_task_id: childId,
            parent_task_id: parentId,
            subagent_role: role,
            model,
            // A replayed progress row may follow a terminal record because the
            // history pre-pass already knows the child's final state. Do not add
            // contradictory `status=running` metadata in that case.
            status: preserveTerminal ? '' : (msg?.status || ''),
        }, rawTs);
        if (!summary) return;
        summary.dedupeKey = `subagent-progress:${childId}`;
        if (preserveTerminal) {
            summary.phase = String(record.phaseEl?.dataset?.phase || 'done');
            summary.headline = String(record.titleEl?.textContent || summary.headline);
            summary.fullHeadline = summary.headline;
            summary.terminal = true;
        }
        queueTaskLiveUpdate(summary, childId, normalizeLogTs(rawTs), summary.dedupeKey, rawTs);
    }

    function routeSubagentFinalMessageToCard(taskId, msg) {
        const childId = String(taskId || '').trim();
        const info = subagentChildParents.get(childId);
        if (!childId || !info) return false;
        const { parentId, role, model } = info;
        const text = String(msg?.content || msg?.text || '').trim();
        const rawTs = msg?.ts || new Date().toISOString();
        forceTaskCard(parentId, rawTs);
        forceTaskCard(childId, rawTs);
        const record = getSubagentCardRecord(childId, parentId, role);
        const priorTerminalPhase = record?.finished ? String(record.phaseEl?.dataset?.phase || '') : '';
        const summary = summarizeSubagentCardFrame(msg, {
            content: '',
            text: '',
            result: text,
            subagent_event: 'completed',
            subagent_task_id: childId,
            parent_task_id: parentId,
            subagent_role: role,
            model,
        }, rawTs);
        if (!summary) return false;
        summary.dedupeKey = `subagent-result:${childId}`;
        if (priorTerminalPhase) {
            summary.phase = priorTerminalPhase;
            summary.headline = String(record.titleEl?.textContent || summary.headline);
            summary.fullHeadline = summary.headline;
            summary.terminal = true;
        }
        queueTaskLiveUpdate(summary, childId, normalizeLogTs(rawTs), summary.dedupeKey, rawTs);
        return true;
    }

    // Resolve a child's card from the child's terminal task_done
    // (which arrives on the log channel without subagent metadata).
    function routeSubagentTerminalToCard(childId, evt) {
        const info = subagentChildParents.get(childId);
        if (!info) return false;
        const status = String(evt.status || '').toLowerCase();
        const severity = taskOutcomeSeverity(evt);
        const interrupted = status === 'interrupted';
        const failed = severity === 'error' || status === 'failed';
        const cancelled = status === 'cancelled' || status === 'cancel_requested';
        const rejected = status === 'rejected_duplicate';
        const event = interrupted ? 'interrupted'
            : failed ? 'failed'
                : cancelled ? 'cancelled'
                    : rejected ? 'rejected'
                        : (severity === 'warn' ? 'completed_warn' : 'completed');
        updateSubagentCardFromEvent({
            delegation_role: 'subagent',
            parent_task_id: info.parentId,
            subagent_task_id: childId,
            subagent_role: info.role,
            subagent_event: event,
            model: info.model || '',
            // Second whitelist: a log-channel-only terminal still upgrades the chip.
            executor_route: evt.executor_route || '',
            execution_evidence: evt.execution_evidence,
            actual_substrate: evt.actual_substrate || '',
            review_projection: evt.review_projection,
            result: evt.result || '',
            error: evt.error || '',
            reason_code: evt.reason_code || '',
            ...costMetaKeys(evt),
        }, evt.ts || evt.timestamp || new Date().toISOString());
        return true;
    }

    function updateLiveCardFromLogEvent(evt) {
        if (!evt) return;
        const eventType = evt.type || evt.event || '';
        if (handleReviewReference(evt)) return;
        if (!isGroupedTaskEvent(evt)) return;
        if (registerEphemeralDecisionFrame(evt)) return;
        const taskId = getLogTaskGroupId(evt) || activeLiveGroupId || '';
        if (!taskId) return;
        const rawTs = evt.ts || evt.timestamp || new Date().toISOString();
        // Task-bound Skill lifecycle is presentation on its explicit owner,
        // never a synthetic lifecycle task card.
        if (attachReviewFromRow(evt, rawTs)) return;
        if (eventType === 'owner_hurry') {
            // HQ1: compact task-card status ONLY — never a timeline row or any
            // chat bubble (the summarizer also hides this family, visible=false).
            if (ownerHurryProjection(evt).applied) {
                liveCardRecords.get(taskId)?.root?.setAttribute('data-owner-hurry', '1');
            }
            return;
        }
        // A known subagent child's log events update its linked child card.
        if (subagentChildParents.has(taskId)) {
            if (eventType === 'task_done') {
                routeSubagentTerminalToCard(taskId, evt);
                return;
            }
            if (subagentTerminalChildren.has(taskId)) return;
            if (eventType === 'tool_call_started') {
                markTaskToolCall(taskId, 1, false, rawTs);
            } else if ((eventType === 'task_metrics_event' || eventType === 'task_eval') && Number.isFinite(Number(evt.tool_calls))) {
                markTaskToolCall(taskId, Number(evt.tool_calls), true, rawTs);
            } else if (
                eventType === 'tool_call_timeout'
                || eventType === 'tool_timeout'
                || eventType === 'llm_round_error'
                || eventType === 'llm_api_error'
                || (eventType === 'tool_call_finished' && evt.is_error)
            ) {
                forceTaskCard(taskId, rawTs);
            }
            const summary = summarizeChatLiveEvent(evt);
            if (!summary) return;
            const info = subagentChildParents.get(taskId);
            if (info) getSubagentCardRecord(taskId, info.parentId, info.role);
            attachTaskDetailReviews(taskId, evt);
            const presented = withTaskCostMeta(summary, evt, {
                replace: eventType === 'task_done' || eventType === 'task_cost_finalized',
                rawTs,
            });
            queueTaskLiveUpdate(presented, taskId, normalizeLogTs(rawTs), presented.dedupeKey || '', rawTs);
            return;
        }
        if (eventType === 'tool_call_started') {
            markTaskToolCall(taskId, 1, false, rawTs);
        } else if ((eventType === 'task_metrics_event' || eventType === 'task_eval') && Number.isFinite(Number(evt.tool_calls))) {
            markTaskToolCall(taskId, Number(evt.tool_calls), true, rawTs);
        } else if (
            eventType === 'tool_call_timeout'
            || eventType === 'tool_timeout'
            || eventType === 'llm_round_error'
            || eventType === 'llm_api_error'
            || (eventType === 'tool_call_finished' && evt.is_error)
        ) {
            forceTaskCard(taskId, rawTs);
        }
        attachTaskDetailReviews(taskId, evt);
        const summary = summarizeChatLiveEvent(evt);
        if (!summary) return;
        const presented = withTaskCostMeta(summary, evt, {
            replace: eventType === 'task_done' || eventType === 'task_cost_finalized',
            rawTs,
        });
        queueTaskLiveUpdate(presented, taskId, normalizeLogTs(rawTs), presented.dedupeKey || '', rawTs);
        updateSubagentCardFromEvent(evt, rawTs);
        if (eventType === 'task_done' && summary.terminal) {
            recordTerminalActivity(taskId);
            syncChatStatus();
            const taskState = getTaskUiState(taskId, false);
            revealBufferedCardIfNeeded(taskState, { rawTs });
        }
    }

    function addMessage(text, role, markdown = false, timestamp = null, isProgress = false, opts = {}) {
        const pending = !!opts.pending;
        const ephemeral = !!opts.ephemeral;
        const clientMessageId = opts.clientMessageId || '';
        const senderLabelOverride = opts.senderLabel || '';
        const senderSessionId = opts.senderSessionId || '';
        const source = opts.source || '';
        const systemType = opts.systemType || '';
        const taskId = opts.taskId || '';
        const projectId = opts.projectId || '';
        const projectName = opts.projectName || '';
        const ts = timestamp || new Date().toISOString();
        const messageKey = buildMessageKey(role, text, ts, {
            clientMessageId,
            systemType,
            isProgress,
            source,
            senderLabel: senderLabelOverride,
            senderSessionId,
            taskId,
        });
        if (messageKey && seenMessageKeys.has(messageKey)) return null;

        if (!isProgress && !ephemeral) {
            persistedHistory.push({
                text,
                role,
                ts,
                markdown: !!markdown,
                systemType,
                source,
                senderLabel: senderLabelOverride,
                senderSessionId,
                clientMessageId,
                taskId,
                projectId,
                projectName,
                skillReview: opts.skillReview || null,
            });
            // Mirror the sessionStorage slice(-200): the in-memory copy exists
            // only to feed that snapshot, so it obeys the same cap (P3).
            if (persistedHistory.length > 200) {
                persistedHistory.splice(0, persistedHistory.length - 200);
            }
            // perf2 P4.3: a rebuildAll replay serializes the sessionStorage
            // snapshot ONCE at the end of the batch, not per historical row.
            if (!_rebuildBatch) persistVisibleHistory();
        }

        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}` + (isProgress ? ' progress' : '');
        if (pending) bubble.classList.add('pending');
        if (ephemeral) bubble.dataset.ephemeral = '1';
        if (clientMessageId) bubble.dataset.clientMessageId = clientMessageId;
        if (systemType) bubble.dataset.systemType = systemType;
        if (senderSessionId) bubble.dataset.senderSessionId = senderSessionId;
        if (taskId) bubble.dataset.taskId = taskId;

        const sender = senderLabel(role, isProgress, systemType, {
            source, senderLabel: senderLabelOverride, senderSessionId,
        }, chatSessionId);
        const rendered = role === 'user'
            ? escapeHtml(text)
            : (role === 'system' && systemType === 'skill_review'
                ? renderSkillReviewDisclosure(text, opts.skillReview || null)
                : renderMarkdown(text));
        const timeFmt = formatMsgTime(ts);
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const pendingHtml = pending ? `<div class="msg-pending">Queued until reconnect</div>` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            <div class="message">${rendered}</div>
            ${pendingHtml}
            ${timeHtml}
        `;
        if (PROJECT_ROW_TYPES.has(systemType) && projectId) {
            const actions = document.createElement('div');
            actions.className = 'system-message-actions';
            actions.append(createSystemMessageAction({
                label: 'Open Project ↗',
                onClick: () => window.dispatchEvent(new CustomEvent('ouro:open-project', {
                    detail: { project: { id: projectId, name: projectName || 'Project' } },
                })),
            }));
            bubble.querySelector('.message')?.append(actions);
        }
        wireSkillReviewDisclosure(bubble, () => requestAnimationFrame(() => !destroyed && updateMessagesPadding({ preserveStickiness: true })));
        stampNodeTimestamp(bubble, ts);
        insertMessageNode(bubble, { forceStick: !!opts.forceStick });
        renderRoutingAnnotation(bubble, opts.chatAnnotation);
        rememberMessageKey(messageKey);
        if (pending && clientMessageId) pendingUserBubbles.set(clientMessageId, bubble);
        return bubble;
    }

    function renderRoutingAnnotation(bubble, annotation) {
        if (!bubble) return false;
        const text = routingAnnotationText(annotation);
        let note = bubble.querySelector('.msg-routing-annotation');
        if (!text) {
            note?.remove();
            delete bubble.dataset.chatAnnotationStatus;
            return false;
        }
        if (!note) {
            note = document.createElement('div');
            note.className = 'msg-routing-annotation';
            const time = bubble.querySelector('.msg-time');
            if (time) time.before(note);
            else bubble.append(note);
        }
        const status = String(annotation.status || '');
        note.textContent = text;
        note.dataset.annotationStatus = status;
        bubble.dataset.chatAnnotationStatus = status;
        return true;
    }

    function updateMessageAnnotation(clientMessageId, annotation) {
        const messageId = String(clientMessageId || '');
        if (!messageId) return false;
        // The journal copy carries the ack, so a re-render restores it too.
        const journalEntry = localEchoJournal.get(messageId);
        if (journalEntry) journalEntry.annotation = annotation || null;
        const bubble = Array.from(messagesDiv.querySelectorAll('.chat-bubble.user[data-client-message-id]'))
            .find((candidate) => candidate.dataset.clientMessageId === messageId);
        return renderRoutingAnnotation(bubble, annotation);
    }

    function clearTransientRoutingAnnotations() {
        for (const note of messagesDiv.querySelectorAll(
            '.msg-routing-annotation[data-annotation-status="pending"]',
        )) {
            const bubble = note.closest('.chat-bubble');
            if (bubble) delete bubble.dataset.chatAnnotationStatus;
            note.remove();
        }
    }

    function markPendingDelivered(clientMessageId) {
        const bubble = pendingUserBubbles.get(clientMessageId || '');
        if (!bubble) return;
        bubble.classList.remove('pending');
        bubble.querySelector('.msg-pending')?.remove();
        pendingUserBubbles.delete(clientMessageId);
    }

    function markPendingDropped(clientMessageId) {
        const bubble = pendingUserBubbles.get(clientMessageId || '');
        if (!bubble) return;
        const note = bubble.querySelector('.msg-pending');
        if (note) note.textContent = 'Not delivered — send again';
        pendingUserBubbles.delete(clientMessageId);
    }

    function ensureWelcomeMessage() {
        if (!isMain) return;
        if (welcomeShown) return;
        const hasRealBubbles = Array.from(messagesDiv.querySelectorAll('.chat-bubble')).some(
            bubble => !bubble.classList.contains('typing-bubble')
        );
        if (hasRealBubbles) return;
        welcomeShown = true;
        addMessage('Ouroboros has awakened', 'assistant', false, null, false, { ephemeral: true });
    }

    // Hydration triggers share one sticky request; reconnect/resync still refetch.
    function awaitInitialHydration({ includeUser = false } = {}) {
        if (initialHydrationPromise) return initialHydrationPromise;
        initialHydrationPromise = syncHistory({ includeUser });
        return initialHydrationPromise;
    }

    // Main briefly yields hydration to an opening Project, with a hard bound.
    const MAIN_HYDRATION_MAX_DEFER_MS = 3500;
    function waitForHydrationWindow() {
        if (!isMain) return Promise.resolve();
        if (hydrationGatePromise) return hydrationGatePromise;
        hydrationGatePromise = new Promise((resolve) => {
            const deadline = Date.now() + MAIN_HYDRATION_MAX_DEFER_MS;
            const scheduleIdle = (callback) => (typeof requestIdleCallback === 'function'
                ? requestIdleCallback(callback, { timeout: 1000 })
                : setTimeout(callback, 50));
            const attempt = () => {
                if (destroyed) {
                    resolve();
                    return;
                }
                if (Date.now() < deadline
                    && typeof isProjectOpening === 'function'
                    && isProjectOpening()) {
                    setTimeout(attempt, 200);
                    return;
                }
                resolve();
            };
            scheduleIdle(attempt);
        });
        return hydrationGatePromise;
    }

    // Apply deferred card/typing/storage work once after the replay batch mounts.
    function finalizeRebuildBatch(batch) {
        for (const record of batch.touched) {
            renderLiveCardMeta(record);
            updateLiveCardCount(record);
            syncLiveCardLayout(record);
        }
        if (batch.typingHidden) hideTypingIndicatorOnly();
        persistVisibleHistory();
    }

    async function syncHistory({ includeUser = false, fromReconnect = false, forceRebuild = false } = {}) {
        if (historySyncPromise) {
            // Preserve reconnect intent across an in-flight ordinary sync.
            if (fromReconnect) {
                pendingReconnectSync = true;
                return historySyncPromise.then(() => {
                    // One waiter consumes the queued rebuild; peers await it.
                    if (pendingReconnectSync) {
                        pendingReconnectSync = false;
                        return syncHistory({ includeUser: false, fromReconnect: true });
                    }
                    return historySyncPromise || lastHistorySyncSucceeded;
                });
            }
            return historySyncPromise;
        }
        historySyncPromise = (async () => {
            try {
                // Server defaults own first-load quotas; Load older overrides them.
                let historyUrl = `/api/chat/history${isMain ? '' : `?chat_id=${chatId}`}`;
                if (historyQuotaOverride) {
                    const sep = historyUrl.includes('?') ? '&' : '?';
                    historyUrl += `${sep}n_human=${historyQuotaOverride.n_human}`
                        + `&n_progress=${historyQuotaOverride.n_progress}`;
                }
                const resp = await apiFetch(historyUrl, { cache: 'no-store' });
                if (!resp.ok) {
                    lastHistorySyncSucceeded = false;
                    initialHydrationPromise = null;
                    return false;
                }
                const data = await resp.json();
                // A late continuation on a destroyed instance must not rebuild a
                // detached DOM subtree or repopulate the cleared collections.
                if (destroyed) {
                    lastHistorySyncSucceeded = false;
                    initialHydrationPromise = null;
                    return false;
                }
                const messages = Array.isArray(data.messages) ? data.messages : [];
                // perf2 P4.5: the server's window verdict (P3.2 additive field)
                // drives the Load-older button/notice after this sync lands.
                historyWindow = (data && typeof data.window === 'object' && data.window)
                    ? data.window
                    : null;
                const scrollBeforeSync = {
                    top: messagesDiv.scrollTop,
                    nearBottom: isNearBottom(),
                    anchor: captureVisibleTimelineAnchor(),
                };

                // First load/reconnect trusts server history and fully rebuilds the
                // feed; routine post-completion syncs only fold in new task cards.
                // perf2 P4: a Load-older refetch (forceRebuild) and the first
                // successful sync after an offline sessionStorage bootstrap
                // [GPT#17] rebuild fully too.
                const rebuildAll = !historyLoaded || fromReconnect || forceRebuild
                    || offlineBootstrapPainted;
                // On a soft reconnect the module (and its dedupe set)
                // survives: a plain re-sync would dedupe-drop every bubble.
                // Restore user text and rebuild from durable history on every
                // rebuild — incl. includeUser=false triggers (clean open /
                // 700ms resync), since offline-bootstrap cleared those too.
                const renderUser = includeUser || fromReconnect || offlineBootstrapPainted;
                if (!historyLoaded || fromReconnect) retiredTaskIds.clear();
                // The extra rebuild causes (Load-older / offline bootstrap)
                // replay everything too, so retirement resets with them.
                if (rebuildAll) retiredTaskIds.clear();

                // perf2 P4.3: the ENTIRE mutation below (clear -> pass 1 ->
                // pass 2 -> terminal resolution -> sweep) is one synchronous
                // closure. On rebuildAll it runs inside ONE outer
                // withStableViewport with a detached batch collecting the
                // top-level nodes; NO awaits may occur between the feed
                // clearing and the batch mount [GPT#14]. The routine path
                // (rebuildAll=false) calls it directly — unchanged behavior.
                const applySyncedMessages = () => {
                // Server-confirmed rows retire their journal copy; the rest
                // survive the rebuild below.
                const localEcho = partitionLocalEchoJournal(localEchoJournal, new Set(messages
                    .filter((m) => m.role === 'user' && m.client_message_id)
                    .map((m) => String(m.client_message_id))));
                for (const entry of localEcho.confirmed) {
                    localEchoJournal.delete(entry.clientMessageId);
                }
                if (rebuildAll) {
                    // The cards below are a new presentation generation. Keep
                    // hydrator single-flight/pending state, but let an already
                    // applied durable Plan revision attach to the rebuilt card.
                    reviewHydrator.invalidateApplied();
                    for (const record of liveCardRecords.values()) record.root?.remove();
                    liveCardRecords.clear();
                    for (const taskState of taskUiStates.values()) {
                        if (taskState?.cleanupTimer) clearTimeout(taskState.cleanupTimer);
                    }
                    taskUiStates.clear();
                    ephemeralDecisionTaskIds.clear();
                    // Rebuild replays the durable truth: stale name buffers and
                    // cancelable markers from the previous connection are dropped
                    // and re-learned from history rows (P3 growth caps).
                    pendingSuggestedNames.clear();
                    cancelableTaskIds.clear();
                    activeLiveGroupId = '';
                    // Atomically drop the standalone message bubbles and the dedupe
                    // state so the rebuild below cannot produce duplicates even if
                    // stale bubbles lingered in the DOM. Keep the typing indicator.
                    for (const bubble of Array.from(messagesDiv.querySelectorAll('.chat-bubble'))) {
                        if (!bubble.classList.contains('typing-bubble')) bubble.remove();
                    }
                    seenMessageKeys.clear();
                    messageKeyOrder.length = 0;
                    subagentChildParents.clear();
                    subagentTerminalChildren.clear();
                    for (const msg of messages) learnSubagentLineage(msg);
                }

                // First pass builds card state without DOM insertion.
                _syncPass1Active = true;
                try { for (const msg of messages) {
                    if (handleReviewReference(msg)) continue;
                    if (attachReviewFromRow(msg, msg.ts || '')) continue;
                    const taskId = msg.task_id || '';
                    if (!taskId) continue;
                    if (retiredTaskIds.has(taskId)) continue;
                    if (msg.is_progress) {
                        updateLiveCardFromProgressMessage(msg, { grantCancelAuthority: msg.project_mirror !== true });
                        continue;
                    }
                    if (msg.system_type === 'task_summary') {
                        // Historical cards only for non-trivial tasks.
                        const hadToolCalls = (msg.tool_calls || 0) > 0;
                        const hadMultipleRounds = (msg.rounds || 0) > 1;
                        const severity = taskOutcomeSeverity(msg);
                        const needsVisibleTerminal = severity === 'error' || severity === 'warn' || severity === 'cancelled';
                        if (hadToolCalls || hadMultipleRounds || needsVisibleTerminal) {
                            const taskState = getTaskUiState(taskId, true);
                            if (taskState) taskState.forceCard = true;
                        }
                        // Pass 2 inserts this in the right transcript position.
                        appendTaskSummaryToLiveCard(msg, { suppressDomInsert: true });
                    }
                } } finally { _syncPass1Active = false; }

                // Pass 2 inserts cards at the first visible task message, then finishes them.
                const insertedCardTaskIds = new Set();
                function reorderDirtyCardIfNeeded(rec) {
                    if (!rec?._anchorOrderDirty || rec.isSubagent || !rec.root?.isConnected) return;
                    insertMessageNode(rec.root, { reorderExisting: true });
                    rec._anchorOrderDirty = false;
                }
                function insertCardIfNeeded(taskId) {
                    if (!taskId || insertedCardTaskIds.has(taskId)) return;
                    insertedCardTaskIds.add(taskId);
                    const rec = liveCardRecords.get(taskId);
                    reorderDirtyCardIfNeeded(rec);
                    if (rec && rec.root && !rec.root.isConnected) {
                        if (rec.isSubagent) ensureLiveCardVisible(rec);
                        else insertMessageNode(rec.root);
                    }
                }
                for (const msg of messages) {
                    const taskId = msg.task_id || '';
                    // Owner-bound reviews attached in pass 1 are not terminal chat bubbles.
                    if (
                        handleReviewReference(msg)
                        || attachReviewFromRow(msg, msg.ts || '', true)
                    ) continue;
                    // Reconnect: a durably recorded submission must not stay
                    // `Sending...` — history + snapshot are the authorities
                    // (a live turn re-links via hydration / next typing frame).
                    if (fromReconnect && msg.role === 'user' && msg.client_message_id) {
                        pendingSubmissions.delete(String(msg.client_message_id));
                    }
                    if (!renderUser && msg.role === 'user') continue;
                    if (msg.is_progress) {
                        // Progress-only/failed tasks still anchor at their first event.
                        insertCardIfNeeded(taskId);
                        // Open post-task checkpoint replays as "Finalizing…".
                        if (msg.task_phase === 'finalizing') markLiveCardFinalizing(taskId);
                        continue;
                    }
                    if (msg.system_type === 'task_summary') continue;
                    if (PROJECT_ROW_TYPES.has(msg.system_type)) {
                        addMessage(msg.text, 'system', !!msg.markdown, msg.ts || null, false, {
                            systemType: msg.system_type,
                            taskId,
                            projectId: msg.project_id || '',
                            projectName: msg.project_name || '',
                        });
                        continue;
                    }
                    // Delivered media is a bubble, not a task-final
                    // message — render it BEFORE the taskId/finishLiveCard block so
                    // a mid-task delivery replayed while its task is still
                    // running does not falsely finalize that task's live card.
                    if (msg.msg_type === 'document' || msg.msg_type === 'photo' || msg.msg_type === 'video') {
                        if (msg.msg_type === 'document') appendDocumentBubble(msg);
                        else appendMediaBubble(msg);
                        continue;
                    }
                    // Replay conclusion: a typed terminal fact OR a plain
                    // untyped final (replay has no later task_done frame, so
                    // the bare final is the task's last word; marked rows —
                    // system_type/msg_type — still never conclude).
                    const plainUntypedFinal = !msg.system_type && !msg.msg_type;
                    if (
                        taskId
                        && (msg.role === 'assistant' || msg.role === 'system')
                        && (positiveTaskTerminalFact(msg) || plainUntypedFinal)
                        && !isNonTerminalMediaHistoryRow(msg)
                    ) {
                        if (subagentChildParents.has(taskId)) {
                            insertCardIfNeeded(taskId);
                            routeSubagentFinalMessageToCard(taskId, msg);
                            const taskState = getTaskUiState(taskId, false);
                            const record = liveCardRecords.get(taskId);
                            finishLiveCard(taskId, msg.task_terminal_status ? taskTerminalPhase(msg) : replayTerminalPhase(taskState, record));
                            continue;
                        }
                        insertCardIfNeeded(taskId);
                        // A replayed early final must not finalize the card.
                        if (msg.task_phase === 'finalizing') {
                            markLiveCardFinalizing(taskId);
                        } else {
                            const taskState = getTaskUiState(taskId, false);
                            const record = liveCardRecords.get(taskId);
                            finishLiveCard(taskId, msg.task_terminal_status ? taskTerminalPhase(msg) : replayTerminalPhase(taskState, record));
                        }
                    }
                    // A replayed durable routing receipt carries the same
                    // authority as its live WS frame: a receipt that landed
                    // while the socket was down still retires `Sending...`.
                    if (msg.chat_annotation && msg.client_message_id) {
                        pendingSubmissions.delete(String(msg.client_message_id));
                    }
                    addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                        systemType: msg.system_type || '',
                        source: msg.source || '',
                        senderLabel: msg.sender_label || '',
                        senderSessionId: msg.sender_session_id || '',
                        clientMessageId: msg.client_message_id || '',
                        taskId,
                        chatAnnotation: msg.chat_annotation || null,
                        skillReview: msg.system_type === 'skill_review' && msg.skill && msg.job_id
                            ? { skill: msg.skill, jobId: msg.job_id }
                            : null,
                    });
                }
                // Resolve cards whose task is already terminal on the server
                // (crash storm / hard timeout / cancellation write a terminal
                // status but no task_summary). Without this their progress-only
                // cards re-inflate as "Working" forever on reload/reconnect.
                const terminalTaskRecords = new Map();
                for (const msg of messages) {
                    const tid = msg.task_id || '';
                    if (tid && msg.task_terminal_status) {
                        terminalTaskRecords.set(tid, {
                            ...msg,
                            status: String(msg.task_terminal_status),
                        });
                    }
                }
                for (const [tid, terminalRecord] of terminalTaskRecords) {
                    if (!taskDoneIsTerminal(terminalRecord)) continue;
                    // Subagent terminal status resolves the child card, not the
                    // parent. Otherwise reload can revive a crashed/cancelled child.
                    if (subagentChildParents.has(tid)) {
                        routeSubagentTerminalToCard(tid, terminalRecord);
                        continue;
                    }
                    const rec = liveCardRecords.get(tid);
                    if (rec && !rec.finished) {
                        insertCardIfNeeded(tid);
                        if (terminalRecord.outcome_axes || terminalRecord.review_projection || terminalRecord.reason_code) {
                            appendTaskSummaryToLiveCard(terminalRecord);
                        } else {
                            // P5: shared terminal mapping — a cancelled root replays
                            // as "Cancelled", never as a generic "Done".
                            finishLiveCard(tid, taskTerminalPhase(terminalRecord));
                        }
                    }
                }

                // Append disconnected visible cards after mid-task reload; skip trivial placeholders.
                for (const [tid, rec] of liveCardRecords) {
                    reorderDirtyCardIfNeeded(rec);
                    if (rec && rec.root && !rec.root.isConnected && !retiredTaskIds.has(tid)) {
                        const ts = taskUiStates.get(tid);
                        if (ts && !ts.cardVisible && ts.completed) continue;
                        if (rec.isSubagent) ensureLiveCardVisible(rec);
                        else insertMessageNode(rec.root);
                    }
                }
                // A rebuild replays only what the server returned; re-render
                // owner rows a stale snapshot has not confirmed yet.
                if (rebuildAll) {
                    for (const entry of localEcho.unconfirmed) {
                        addMessage(entry.text, 'user', false, entry.ts, false, {
                            // A still-queued offline row keeps its pending mark.
                            pending: pendingUserBubbles.has(entry.clientMessageId),
                            source: 'web',
                            senderSessionId: chatSessionId,
                            clientMessageId: entry.clientMessageId,
                            chatAnnotation: entry.annotation,
                        });
                    }
                }
                };  // end applySyncedMessages

                // perf2 P4 follow-up (double-fetch fix): the replay below marks
                // historical cards finished; those transitions must not
                // schedule the post-completion resync (the rows just arrived
                // from this very fetch). The flag spans BOTH branches and is
                // dropped synchronously, so a real live completion frame can
                // never land while it is up.
                _historyReplayActive = true;
                try {
                    if (rebuildAll) {
                        // perf2 P4.3 [GPT#14]: one outer withStableViewport for
                        // the whole rebuild (inner wrappers collapse on the
                        // _viewportMutationDepth gate — no per-frame layout
                        // storm); one stable sort, one fragment mount, ONE
                        // persist, all synchronous: live frames never observe
                        // "records cleared, fragment not yet mounted".
                        _rebuildBatch = createRebuildBatch();
                        try {
                            withStableViewport(() => {
                                applySyncedMessages();
                                const batch = _rebuildBatch;
                                _rebuildBatch = null;
                                batch.mount(messagesDiv, messagesDiv.querySelector('.typing-bubble'));
                                finalizeRebuildBatch(batch);
                            });
                        } finally {
                            _rebuildBatch = null;
                        }
                    } else {
                        // Routine sync: the old per-row live-DOM path, untouched.
                        applySyncedMessages();
                    }
                } finally {
                    _historyReplayActive = false;
                }

                // After first load, sync status from live cards/active turns.
                syncChatStatus();

                // One-shot server recall seed includes other clients without resetting
                // ArrowUp during reconnect. Merge [server..., local...], newest wins.
                if (!inputHistorySeededFromServer) {
                    const serverTexts = [];
                    for (const msg of messages) {
                        if (msg.role !== 'user') continue;
                        let text = (msg.text || '').trim();
                        if (text) serverTexts.push(text);
                    }
                    const combined = [...serverTexts, ...inputHistory];
                    const deduped = [];
                    const seen = new Set();
                    for (let i = combined.length - 1; i >= 0; i--) {
                        if (!seen.has(combined[i])) {
                            deduped.unshift(combined[i]);
                            seen.add(combined[i]);
                        }
                    }
                    inputHistory.length = 0;
                    inputHistory.push(...deduped.slice(-50));
                    saveChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY, inputHistory);
                    inputHistoryIndex = inputHistory.length;
                    inputHistorySeededFromServer = true;
                }

                const wasFirstLoad = !historyLoaded;
                historyLoaded = true;
                lastHistorySyncSucceeded = true;
                // The durable rebuild superseded the offline fallback paint.
                offlineBootstrapPainted = false;
                // perf2 P4.1: ANY successful sync leaves the instance hydrated
                // — later hydration triggers ride this sticky promise.
                initialHydrationPromise = historySyncPromise;
                // perf2 P4.5: reflect the server's window verdict in the
                // Load-older control now that the feed matches this response.
                syncLoadOlderControl();
                // A recreated project instance restores its predecessor's stashed
                // mid-history position on first paint instead of pinning to newest.
                if (wasFirstLoad && _initialScrollPending) {
                    _initialScrollPending = false;
                    updateMessagesPadding({ preserveStickiness: false });
                    restoreScrollPosition();
                } else
                // First load jumps to latest; reconnect preserves older-message reading.
                if (wasFirstLoad || (fromReconnect ? scrollBeforeSync.nearBottom : isNearBottom())) {
                    updateMessagesPadding({ preserveStickiness: false });
                    scrollToBottomAfterLayout();
                } else if (fromReconnect) {
                    // Rebuild may add rows both ABOVE and BELOW the viewport; a
                    // scrollHeight delta cannot tell them apart and over-scrolls
                    // readers. Restore the first visible timestamped node to its
                    // prior visual offset instead (equal-ts ordinals keep
                    // arrival-order identity), after two RAF frames so async
                    // card heights above the anchor cannot move the reader.
                    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                    const restoredFromAnchor = restoreVisibleTimelineAnchor(scrollBeforeSync.anchor);
                    if (!restoredFromAnchor) messagesDiv.scrollTop = scrollBeforeSync.top;
                    updateScrollButton();
                }
                return messages.length > 0;
            } catch (err) {
                lastHistorySyncSucceeded = false;
                initialHydrationPromise = null;
                const socketState = ws?.ws?.readyState;
                const expectedDisconnect = socketState !== WebSocket.OPEN;
                if (expectedDisconnect && err instanceof TypeError) {
                    return false;
                }
                console.error('Failed to load chat history:', err);
                return false;
            } finally {
                historySyncPromise = null;
                // A reconnect caller waiting on the active promise owns replay of
                // pendingReconnectSync above, so its own promise resolves only after
                // the authoritative rebuild.
            }
        })();
        return historySyncPromise;
    }

    function cancelHistoryPaint() {
        historyPaintGeneration += 1;
    }

    async function refreshHistory({ revision = 0 } = {}) {
        const generation = ++historyPaintGeneration;
        const targetRevision = Math.max(0, Number(revision) || 0);
        // perf2 P4.1: only a NEW revision (or a never-hydrated instance)
        // forces a real fetch; otherwise the sticky hydration promise answers
        // and the paint receipt below still runs [GPT#12].
        if (targetRevision > lastLoadedHistoryRevision || !initialHydrationPromise) {
            await syncHistory({ includeUser: true });
        } else {
            await awaitInitialHydration({ includeUser: true });
        }
        if (lastHistorySyncSucceeded && targetRevision > lastLoadedHistoryRevision) {
            lastLoadedHistoryRevision = targetRevision;
        }
        if (destroyed || !lastHistorySyncSucceeded || generation !== historyPaintGeneration || page.hidden) {
            return { painted: false, revision: targetRevision };
        }
        // A successful fetch is not a read acknowledgement until the rebuilt
        // DOM has crossed an actual browser paint while this Project remains
        // visible. Two frames cover layout followed by paint/composite. A
        // destroyed page reports hidden===false, so the paint receipt must also
        // consult the lifecycle flag — a late paint on a torn-down instance
        // would otherwise acknowledge a revision that was never shown (GPT#15).
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return {
            painted: !destroyed && generation === historyPaintGeneration && !page.hidden,
            revision: targetRevision,
        };
    }

    (async () => {
        await loadUiPreferences();
        // perf2 P4.2: Main waits for the (bounded) idle hydration window;
        // project instances pass straight through. The sticky single-flight
        // below folds this trigger with the first socket open / refreshHistory.
        await waitForHydrationWindow();
        if (destroyed) return;
        if (await awaitInitialHydration({ includeUser: true })) return;
        try {
            const saved = JSON.parse(sessionStorage.getItem(storeKey(CHAT_STORAGE_KEY)) || '[]');
            for (const msg of saved) {
                addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                    systemType: msg.systemType || '',
                    source: msg.source || '',
                    senderLabel: msg.senderLabel || '',
                    senderSessionId: msg.senderSessionId || '',
                    clientMessageId: msg.clientMessageId || '',
                    taskId: msg.taskId || '',
                    projectId: msg.projectId || '',
                    projectName: msg.projectName || '',
                    skillReview: msg.skillReview || null,
                });
            }
        } catch {}
        historyLoaded = true;
        // GPT#17: this offline fallback sets historyLoaded=true, which would
        // make the first successful post-outage sync a NON-rebuilding routine
        // fold over stale sessionStorage bubbles. Flag it so that sync
        // rebuilds from durable history instead.
        if (!lastHistorySyncSucceeded) offlineBootstrapPainted = true;
        ensureWelcomeMessage();
    })();

    function rememberInput(text) {
        if (!text) return;
        if (inputHistory[inputHistory.length - 1] !== text) inputHistory.push(text);
        saveChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY, inputHistory);
        inputHistoryIndex = inputHistory.length;
        inputDraft = '';
    }

    function resizeChatInput({ preserveStickiness = false } = {}) {
        const caretAtEnd = input.selectionEnd >= input.value.length - 1;
        const previousScrollTop = input.scrollTop;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        input.scrollTop = caretAtEnd ? input.scrollHeight : previousScrollTop;
        updateMessagesPadding({ preserveStickiness });
    }

    function restoreInputHistory(step) {
        if (!inputHistory.length) return;
        if (step < 0) {
            if (input.selectionStart !== 0 || input.selectionEnd !== 0) return;
            if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
            inputHistoryIndex = Math.max(0, inputHistoryIndex - 1);
            input.value = inputHistory[inputHistoryIndex] || '';
        } else {
            if (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) return;
            inputHistoryIndex = Math.min(inputHistory.length, inputHistoryIndex + 1);
            input.value = inputHistoryIndex === inputHistory.length ? inputDraft : (inputHistory[inputHistoryIndex] || '');
        }
        resizeChatInput({ preserveStickiness: false });
        const cursor = input.value.length;
        input.setSelectionRange(cursor, cursor);
    }

    async function sendMessage(planMode = false) {
        if (sendBtn.disabled) return;  // guard against Enter re-entry during async upload
        let text = input.value.trim();
        // The owner's pure typed request (before attachment lines) — captured so a
        // live card spawned by this message can name a project from it on a "turn
        // into project" conversion even before the task records its objective (P1,
        // direct-chat case: the server has no title/objective/queue source yet).
        const objectiveText = text;
        const hasAttachments = pendingAttachments.length > 0;
        let uploadedAttachments = [];
        let attachmentMeta = [];
        if (!text && !pendingAttachments.length) return;
        if (pendingAttachments.length) {
            // Upload immediately before send; offline queueing would orphan files.
            if (ws.ws?.readyState !== WebSocket.OPEN) {
                showToast('Cannot attach file while offline. Reconnect and try again.', 'error');
                return;
            }
            const staged = [...pendingAttachments];
            const uploaded = [];
            setAttachmentUploadState(true);
            setSendBusy(true, staged.length > 1 ? 'Uploading files' : 'Uploading');
            try {
                for (const stagedItem of staged) {
                    if (ws.ws?.readyState !== WebSocket.OPEN) throw new Error('Connection closed during upload. Reconnect and try again.');
                    const formData = new FormData();
                    formData.append('file', stagedItem.file);
                    const resp = await apiFetch('/api/chat/upload', { method: 'POST', body: formData });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || !data.ok) {
                        throw new Error(data.error || resp.statusText);
                    }
                    uploaded.push({
                        filename: data.filename || '',
                        path: data.path || '',
                        display_name: data.display_name || stagedItem.display_name,
                        mime: data.mime || stagedItem.file?.type || '',
                    });
                }
                if (ws.ws?.readyState !== WebSocket.OPEN) throw new Error('Connection closed after upload. Reconnect and try again.');
                uploadedAttachments = uploaded;
                const attachmentLines = uploaded
                    .map((item) => `[Attached file: ${item.display_name} saved to ${item.path}]`)
                    .join('\n');
                text += (text ? '\n\n' : '') + attachmentLines;
                // Structured attachment metadata rides the WS frame so the
                // gateway can hand image uploads to the model as NATIVE image
                // blocks (vision models) instead of only a path label.
                attachmentMeta = uploaded.map((item) => ({
                    filename: item.filename,
                    display_name: item.display_name,
                    mime: item.mime || '',
                }));
            } catch (e) {
                await cleanupUploadedAttachments(uploaded);
                showToast('Upload error: ' + e.message, 'error');
                return;  // pending attachments and preview remain so the user can retry
            } finally {
                setAttachmentUploadState(false);
                setSendBusy(false);
            }
        }
        if (!text) return;
        const forcePlan = !!planMode && !text.startsWith('/');
        const result = ws.send({
            type: 'chat',
            content: text,
            sender_session_id: chatSessionId,
            force_plan: forcePlan,
            ...(isMain ? {} : { chat_id: chatId }),
            ...(projectId ? { project_id: projectId } : {}),
            ...(attachmentMeta.length ? { attachments: attachmentMeta } : {}),
            ...clientSurfaceField(),
        }, hasAttachments ? { queue: false } : undefined);
        if (hasAttachments && result?.status !== 'sent') {
            await cleanupUploadedAttachments(uploadedAttachments);
            showToast('Connection lost before send. Reconnect and try again.', 'error');
            return;
        }
        // One-shot: disarm Swarm now that the message is sent.
        if (planMode) setSwarm(false);
        // Hand the objective to the NEXT main-chat live card this message spawns.
        if (isMain && objectiveText) _pendingCardObjective = objectiveText;
        if (hasAttachments) {
            pendingAttachments = [];
            updateAttachmentPreview();
        }
        rememberInput(text);
        input.value = '';
        clearInputDraft();
        const sentTs = new Date().toISOString();
        addMessage(text, 'user', false, sentTs, false, {
            pending: result?.status === 'queued',
            source: 'web',
            senderSessionId: chatSessionId,
            clientMessageId: result?.clientMessageId || '',
            forceStick: true,
        });
        // ws.send always coins a client_message_id for chat frames; guard
        // only against a non-chat result shape.
        const pendingId = result?.clientMessageId || '';
        if (pendingId) {
            pendingSubmissions.set(pendingId, {
                clientMessageId: pendingId,
                timestamp: Date.now(),
            });
            recordLocalEcho(pendingId, text, sentTs);
        }
        syncChatStatus();
        resizeChatInput({ preserveStickiness: false });
        scrollToBottomAfterLayout();
    }

    // Send mode lives on DOM so CSS and click/Enter share one source.
    const sendGroup = page.querySelector('.chat-send-group');

    // Swarm is a one-shot arm: the next send goes through plan_task multi-model
    // brainstorm/planning, then the pill auto-disarms so it never sticks.
    const swarmBtn = byId('swarm');
    function swarmArmed() {
        return swarmBtn?.dataset.armed === 'true';
    }
    function setSwarm(armed) {
        if (swarmBtn) swarmBtn.dataset.armed = armed ? 'true' : 'false';
    }

    function setSendBusy(busy, label = '') {
        sendGroup.dataset.busy = busy ? '1' : '0';
        sendBtn.disabled = busy;
        if (busy) {
            sendBtn.textContent = label || 'Sending';
            sendBtn.title = label || 'Sending';
        } else {
            sendBtn.textContent = 'Send';
            sendBtn.title = 'Send message';
        }
    }

    swarmBtn?.addEventListener('click', () => setSwarm(!swarmArmed()));

    // Context-mode quick toggle: the owner endpoint hot-applies the setting
    // without a restart; Max -> Low is accepted only while Ouroboros is idle.
    const contextModeBtn = byId('context-mode');
    contextModeBtn?.addEventListener('click', async (event) => {
        const seg = event.target.closest('.chat-seg');
        if (!seg || contextModeBtn.dataset.disabled === 'true') return;
        const next = seg.dataset.mode === 'low' ? 'low' : 'max';
        const current = contextModeBtn.dataset.contextMode === 'low' ? 'low' : 'max';
        if (next === current) return;
        contextModeBtn.dataset.disabled = 'true';
        const postMode = (mode) => apiFetch('/api/owner/context-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });
        try {
            const resp = await postMode(next);
            if (resp.ok) {
                contextModeBtn.dataset.contextMode = next;
            } else {
                let message = 'Could not change context mode.';
                try { const p = await resp.json(); if (p?.error) message = p.error; } catch {}
                showToast(message, 'error');
            }
        } catch (e) {
            showToast(`Could not change context mode: ${e.message || e}`, 'error');
            /* leave the current value; /api/state refresh will resync */
        } finally {
            contextModeBtn.dataset.disabled = 'false';
            refreshHeaderControlState(true);
        }
    });

    // Arrow wrappers avoid MouseEvent leaking into sendMessage(planMode).
    sendBtn.addEventListener('click', () => sendMessage(swarmArmed()));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(swarmArmed());
            return;
        }
        if (e.key === 'ArrowUp' && !e.shiftKey) {
            restoreInputHistory(-1);
        } else if (e.key === 'ArrowDown' && !e.shiftKey) {
            restoreInputHistory(1);
        }
    });
    // Dynamic CSS reserve keeps the absolute composer from covering messages.
    function scrollToBottom() {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function scrollToBottomAfterLayout() {
        requestAnimationFrame(() => {
            if (destroyed) return;
            scrollToBottom();
            requestAnimationFrame(() => { if (!destroyed) scrollToBottom(); });
        });
    }

    // P7 — per-instance scroll memory. Switching tabs/opening a project panel used
    // to drop this thread back to the very top (the browser zeroes a hidden
    // column's scrollTop, and toggling .page display can reset it too). We
    // remember where the user was and restore it on show: pinned to the latest
    // message in the common case, or the exact spot they'd scrolled back to.
    messagesDiv?.addEventListener('scroll', () => {
        // Ignore the spurious scrollTop=0 a browser emits while the column is
        // hidden — that would erase the real position we want to restore.
        if (!isInstanceVisible()) return;
        // WebKit fires a scrollTop=0 event when a hidden column is re-shown, and
        // our own re-pin loop writes scrollTop too — neither is a real user
        // scroll, so during a restore pass we only refresh the button, never the
        // saved position (which would corrupt a mid-history restore to the top).
        if (_restoring) { updateScrollButton(); return; }
        _savedScrollTop = messagesDiv.scrollTop;
        _savedStick = isNearBottom();
        updateScrollButton();
    }, { passive: true });

    // Round glass "jump to newest" affordance — shown only when the user has
    // scrolled up away from the bottom, for both the main chat and panels.
    function updateScrollButton() {
        if (!scrollBottomBtn) return;
        scrollBottomBtn.classList.toggle('visible', isInstanceVisible() && !isNearBottom());
    }
    scrollBottomBtn?.addEventListener('click', () => {
        _savedStick = true;
        scrollToBottomAfterLayout();
        updateScrollButton();
    });

    function restoreScrollPosition() {
        if (!isInstanceVisible()) return;  // hidden column has no geometry yet
        // WebKit (the desktop WKWebView) leaves a freshly un-hidden flex column's
        // scrollTop pinned at 0 for a frame or two after the page is shown, so a
        // single/double rAF re-pin (which is enough in Chromium) lands the user at
        // the very top. Re-apply the target position across several frames until
        // the late relayout settles, then keep the button state in sync.
        _restoring = true;
        const targetStick = _savedStick;
        const targetTop = _savedScrollTop;
        let frames = 0;
        const apply = () => {
            if (destroyed || !isInstanceVisible()) { _restoring = false; return; }
            // scrollHeight is re-read each frame so a sticky thread tracks late
            // card-layout growth; a restored mid-history spot re-pins to the exact
            // saved offset (idempotent, so it isn't overridden).
            messagesDiv.scrollTop = targetStick ? messagesDiv.scrollHeight : targetTop;
            updateScrollButton();
            if (++frames < 12) requestAnimationFrame(apply);
            else _restoring = false;
        };
        requestAnimationFrame(apply);
    }

    function updateMessagesPadding(options = {}) {
        const preserveStickiness = options.preserveStickiness !== false;
        const shouldStick = preserveStickiness && isNearBottom();
        if (pageHeader && messagesDiv) {
            // The main header wraps to two rows on narrow viewports. Reserve its
            // REAL rendered height so scrollTop=0 never hides the first message
            // behind the absolute overlay; project panels have no overlay header.
            const headerReserve = Math.max(56, Math.ceil(pageHeader.offsetHeight || 0));
            page.style.setProperty('--chat-header-reserve', `${headerReserve}px`);
        }
        if (inputArea && messagesDiv) {
            const reserve = Math.max(92, Math.ceil(inputArea.offsetHeight || 0) + 16);
            // Set on the instance page root so it cascades to #chat-messages
            // (padding) AND the sibling scroll-to-bottom button (bottom offset).
            page.style.setProperty('--chat-input-reserve', `${reserve}px`);
        }
        if (shouldStick) scrollToBottomAfterLayout();
        updateScrollButton();
    }

    // Kept on the instance so destroy() can disconnect it (the observer was
    // previously an unreachable closure — the P3 lifecycle leak).
    let chatResizeObserver = null;

    function installChatResizeObservers() {
        if (typeof ResizeObserver !== 'function') return;
        let queued = false;
        const schedule = () => {
            if (queued) return;
            queued = true;
            requestAnimationFrame(() => {
                queued = false;
                if (destroyed) return;
                updateMessagesPadding({ preserveStickiness: true });
            });
        };
        chatResizeObserver = new ResizeObserver(schedule);
        if (pageHeader) chatResizeObserver.observe(pageHeader);
        if (inputArea) chatResizeObserver.observe(inputArea);
        if (messagesDiv) chatResizeObserver.observe(messagesDiv);
    }

    installChatResizeObservers();

    // Per-thread input draft (P3): destroy-on-close would otherwise lose typed
    // but unsent text. Saved on every input (cheap), restored at instance
    // creation, cleared on send.
    function saveInputDraft() {
        try {
            if (input.value) sessionStorage.setItem(storeKey(CHAT_DRAFT_KEY), input.value);
            else sessionStorage.removeItem(storeKey(CHAT_DRAFT_KEY));
        } catch {}
    }

    function clearInputDraft() {
        try { sessionStorage.removeItem(storeKey(CHAT_DRAFT_KEY)); } catch {}
    }

    try {
        const savedDraft = sessionStorage.getItem(storeKey(CHAT_DRAFT_KEY)) || '';
        if (savedDraft && !input.value) {
            input.value = savedDraft;
            inputDraft = savedDraft;
            resizeChatInput({ preserveStickiness: false });
        }
    } catch {}

    input.addEventListener('input', () => {
        if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
        resizeChatInput({ preserveStickiness: false });
        saveInputDraft();
    });

    headerActions?.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-chat-command]');
        if (!button) return;
        button.closest('details')?.removeAttribute('open');
        const command = button.dataset.chatCommand;
        if (command === 'evolve') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/evolve ${next ? 'start' : 'stop'}` });
            return;
        }
        if (command === 'bg') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/bg ${next ? 'start' : 'stop'}` });
            return;
        }
        if (command === 'review') {
            ws.send({ type: 'command', cmd: '/review' });
            return;
        }
        if (command === 'restart') {
            ws.send({ type: 'command', cmd: '/restart' });
            return;
        }
        if (command === 'panic') {
            // CRITICAL CONTROL: the whole confirm-and-send flow lives in the
            // node-tested confirmAndSendPanic (dialog options + strict
            // shouldFirePanic gate + the exact /panic command); this handler
            // only injects the real deps. Manual check on release: click
            // Panic → dialog → "Kill all workers" sends /panic;
            // Cancel/Escape/backdrop send nothing.
            await confirmAndSendPanic({ openConfirmDialog, ws });
        }
    });

    // The More menu is a native <details> (no auto-dismiss): collapse it when a
    // click/tap lands outside it, or on Escape, so it never stays stuck open.
    // Handler refs are kept so destroy() can remove them (P3 lifecycle).
    let documentClickHandler = null;
    let documentKeydownHandler = null;
    if (!asPanel) {
        const collapseHeaderMenus = (predicate) => {
            page.querySelectorAll('details.chat-header-more[open]').forEach((details) => {
                if (predicate(details)) details.removeAttribute('open');
            });
        };
        documentClickHandler = (event) => {
            collapseHeaderMenus((details) => !details.contains(event.target));
        };
        document.addEventListener('click', documentClickHandler);
        documentKeydownHandler = (event) => {
            if (event.key === 'Escape') collapseHeaderMenus(() => true);
        };
        document.addEventListener('keydown', documentKeydownHandler);
    }

    budgetPill?.addEventListener('click', () => {
        if (typeof openDashboardTab === 'function') openDashboardTab('costs');
        else if (typeof openSettingsTab === 'function') openSettingsTab('costs');
    });

    let headerControlInterval = null;
    if (asPanel) {
        // A panel has no global controls/budget to poll; seed the status from
        // the live socket so a late-created panel never gets stuck on
        // "Connecting…" (the one-shot WS `open` already fired before it existed;
        // future reconnects still update it via the shared `open` handler).
        if (ws.isConnected?.()) setStatus('online', 'Online');
        // 1A a panel created AFTER the socket opened missed the typing frame
        // and the `open`-driven refresh — hydrate in-flight turns once from
        // the snapshot (per-instance closure filters to this panel's chat_id).
        refreshHeaderControlState(true);
    } else {
        refreshHeaderControlState(true);
        headerControlInterval = setInterval(refreshHeaderControlState, 3000);
    }

    const typingEl = document.createElement('div');
    // Per-instance id (main stays 'typing-indicator'; panels get a unique id) so
    // multiple open chat columns never collide on a duplicate DOM id.
    typingEl.id = idPrefix === 'chat' ? 'typing-indicator' : `${idPrefix}-typing-indicator`;
    typingEl.className = 'chat-bubble assistant typing-bubble';
    typingEl.style.display = 'none';
    typingEl.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
    messagesDiv.appendChild(typingEl);

    // perf2 P4.5: "Load older" atop the feed. Server truth (window.complete /
    // truncated_by, P3.2) picks refetch button vs honest boundary notice;
    // excluded from viewport anchoring; mounted ONLY with content — a hidden
    // permanent node would break child-order consumers (ui-smoke chronology).
    const loadOlderEl = document.createElement('div');
    loadOlderEl.className = 'chat-load-older';
    const loadOlderBtn = document.createElement('button');
    loadOlderBtn.type = 'button';
    loadOlderBtn.className = 'chat-load-older-btn';
    loadOlderBtn.textContent = 'Load older messages';
    const loadOlderNote = document.createElement('span');
    loadOlderNote.className = 'chat-load-older-note';
    loadOlderNote.hidden = true;
    loadOlderEl.append(loadOlderBtn, loadOlderNote);
    loadOlderBtn.addEventListener('click', () => { loadOlderHistory(); });

    function syncLoadOlderControl() {
        const control = loadOlderControlState(historyWindow, historyQuotaOverride);
        if (control.mode === 'hidden') {
            loadOlderEl.remove();
            return;
        }
        if (!loadOlderEl.isConnected) messagesDiv.prepend(loadOlderEl);
        loadOlderBtn.hidden = control.mode !== 'button';
        loadOlderBtn.disabled = loadingOlderHistory;
        loadOlderBtn.textContent = loadingOlderHistory
            ? 'Loading…'
            : (control.mode === 'button' ? control.label : 'Load older messages');
        loadOlderNote.hidden = control.mode !== 'notice';
        if (control.mode === 'notice') loadOlderNote.textContent = control.label;
    }

    async function loadOlderHistory() {
        if (loadingOlderHistory) return;
        const next = nextQuotaEscalation(historyQuotaOverride);
        if (!next) return;
        loadingOlderHistory = true;
        syncLoadOlderControl();
        // Anchor the current first visible timestamped node (the control
        // itself is excluded from capture, like .typing-bubble) so the reader
        // does not drift when older rows land above the viewport [GPT#13].
        const anchor = _savedStick || isNearBottom() ? null : captureVisibleTimelineAnchor();
        const previousQuota = historyQuotaOverride;
        historyQuotaOverride = next;
        try {
            // Drain EVERY in-flight sync first: coalescing into one would
            // silently drop forceRebuild and the escalated window, and another
            // waiter can install a NEW promise right as the previous one
            // settles — so re-check until the slot is genuinely free. Only
            // then does syncHistory below start as OUR fetch (its head sees
            // historySyncPromise === null synchronously).
            while (historySyncPromise) {
                try { await historySyncPromise; } catch {}
                if (destroyed) return;
            }
            await syncHistory({ includeUser: true, forceRebuild: true });
            if (destroyed) return;
            if (!lastHistorySyncSucceeded) {
                historyQuotaOverride = previousQuota;
                return;
            }
            // Like the reconnect restore: wait two frames so late card layout
            // above the anchor cannot move the reader right after this call.
            await new Promise((resolve) => requestAnimationFrame(
                () => requestAnimationFrame(resolve)
            ));
            if (destroyed) return;
            if (anchor) restoreVisibleTimelineAnchor(anchor);
            updateScrollButton();
        } finally {
            loadingOlderHistory = false;
            if (!destroyed) syncLoadOlderControl();
        }
    }

    function hasActiveLiveCard() {
        return Array.from(liveCardRecords.values()).some(isForegroundLiveCard);
    }

    function deriveChatStatus() {
        let directCount = 0, managedActive = 0, managedQueued = 0;
        for (const entry of activeDirectActivities.values()) {
            if (String(entry?.kind || '') !== 'managed_task') directCount += 1;
            else if (String(entry?.phase || '') === 'queued') managedQueued += 1;
            else managedActive += 1;
        }
        return computeDerivedChatStatus({
            isConnected: ws.isConnected ? ws.isConnected() : true,
            hasActiveLiveCard: hasActiveLiveCard(),
            activeDirectCount: directCount,
            activeManagedCount: managedActive,
            queuedManagedCount: managedQueued,
            pendingSubmissionsCount: pendingSubmissions.size,
        });
    }

    function syncChatStatus() {
        const derived = deriveChatStatus();
        setStatus(derived.kind, derived.text);
        if (derived.showDots && !hasActiveLiveCard()) {
            typingEl.style.display = '';
            if (isNearBottom()) messagesDiv.scrollTop = messagesDiv.scrollHeight;
        } else {
            typingEl.style.display = 'none';
        }
    }

    function showTyping(activityId = '', meta = {}) {
        const actId = String(activityId || '').trim() || ('direct-' + chatId);
        // A typing frame after its turn's keyed final must not resurrect the
        // concluded turn — but it still carries the activity<->cmid link, so
        // it settles the linked submission (broadcasts are not ordered).
        if (concludedDirectActivities.has(actId)) {
            if (meta.clientMessageId && pendingSubmissions.delete(meta.clientMessageId)) {
                syncChatStatus();
            }
            return;
        }
        // Fresh live evidence outranks a task-detail read woken by an older
        // queue-loss snapshot. The in-flight read may finish, but cannot apply.
        missingManagedTaskIds.delete(actId);
        activeDirectActivities.set(actId, {
            activityId: actId,
            // '' = not registry-tracked (queued managed task): visible in the
            // active set but exempt from /api/state snapshot deletion.
            kind: meta.kind || '',
            phase: meta.phase || 'thinking',
            clientMessageId: meta.clientMessageId || '',
            startedAt: Date.now(),
        });
        markReviewAnchor(liveCardRecords.get(actId));
        if (meta.clientMessageId) {
            pendingSubmissions.delete(meta.clientMessageId);
        }
        syncChatStatus();
    }

    function hideTypingIndicatorOnly() {
        // perf2 P4.3: one typing-indicator write per replay batch.
        if (_rebuildBatch) {
            _rebuildBatch.typingHidden = true;
            return;
        }
        typingEl.style.display = 'none';
    }

    function revokeManagedTaskCancelAuthority(taskId) {
        cancelableTaskIds.delete(taskId);
        const record = liveCardRecords.get(taskId);
        if (!record) return;
        record.cancelable = false;
        syncCancelRunButton(record);
    }

    async function reconcileMissingManagedTask(taskId) {
        if (
            destroyed
            || managedTaskDetailReads.has(taskId)
            || concludedDirectActivities.has(taskId)
            || !missingManagedTaskIds.has(taskId)
        ) return;
        const record = liveCardRecords.get(taskId);
        if (subagentChildParents.has(taskId) || record?.isSubagent) {
            missingManagedTaskIds.delete(taskId);
            return;
        }
        managedTaskDetailReads.add(taskId);
        try {
            const detail = await fetchTaskDetail(taskId);
            if (destroyed || concludedDirectActivities.has(taskId)) return;
            const currentRecord = liveCardRecords.get(taskId);
            if (!currentRecord || currentRecord.isSubagent || subagentChildParents.has(taskId)) return;
            attachTaskDetailReviews(taskId, detail);
            const cancelPending = taskCancelPending(detail);
            if (cancelPending || String(detail?.status || '').trim()) {
                markReviewAnchor(currentRecord);
            }
            if (cancelPending) {
                reconcileCancelCardFromDetail(currentRecord, taskId, detail);
                return;
            }
            if (
                !missingManagedTaskIds.has(taskId)
                || activeDirectActivities.has(taskId)
            ) return;
            if (!isTerminalTaskDetail(detail)) {
                reconcileCancelCardFromDetail(currentRecord, taskId, detail);
                return;
            }
            recordTerminalActivity(taskId);
            appendTaskSummaryToLiveCard({ ...detail, task_id: taskId });
        } catch {
            // No terminal fact was proved. A later existing snapshot retries.
        } finally {
            managedTaskDetailReads.delete(taskId);
        }
    }

    function observeMissingManagedTask(taskId) {
        const id = String(taskId || '').trim();
        if (!id || concludedDirectActivities.has(id)) return;
        const record = liveCardRecords.get(id);
        if (subagentChildParents.has(id) || record?.isSubagent) return;
        missingManagedTaskIds.add(id);
        void reconcileMissingManagedTask(id);
    }

    function hydrateDirectActivities(turnsList, snapshotBarrierMs = Infinity, snapshotGeneration = 0) {
        if (!Array.isArray(turnsList)) return;
        const {
            activities: nextMap,
            departedManagedTaskIds,
            disappearedManagedTaskIds,
            globallyActiveActivityIds,
        } = reconcileHydratedDirectActivities(
            activeDirectActivities, turnsList, chatId, snapshotBarrierMs,
            concludedDirectActivities, snapshotGeneration,
        );
        activeDirectActivities.clear();
        for (const [k, v] of nextMap.entries()) {
            activeDirectActivities.set(k, v);
            markReviewAnchor(liveCardRecords.get(k));
            if (v.kind === 'managed_task') missingManagedTaskIds.delete(k);
            if (v.clientMessageId) {
                pendingSubmissions.delete(v.clientMessageId);
            }
        }
        for (const taskId of globallyActiveActivityIds) missingManagedTaskIds.delete(taskId);
        for (const taskId of departedManagedTaskIds) revokeManagedTaskCancelAuthority(taskId);
        for (const taskId of disappearedManagedTaskIds) observeMissingManagedTask(taskId);
        for (const taskId of unconfirmedForegroundCardIds(
            Array.from(liveCardRecords, ([id, r]) => ({
                id, finished: r.finished, isSubagent: r.isSubagent, connected: r.root?.isConnected,
            })),
            globallyActiveActivityIds,
        )) observeMissingManagedTask(taskId);
        for (const taskId of missingManagedTaskIds) {
            if (!activeDirectActivities.has(taskId)) void reconcileMissingManagedTask(taskId);
        }
        syncChatStatus();
    }

    const isKnownProjectFrame = (msg) => {
        const cid = Number(msg?.chat_id ?? 1);
        return state.projectChatIds instanceof Set && state.projectChatIds.has(cid);
    };

    function incrementUnreadIfNeeded(msg) {
        if (!isMain) return;  // the global unread badge tracks the main chat
        // Project visible_revision is the sole unread authority for a Project.
        // Project-owned frames never create a second Main unread.
        if (isKnownProjectFrame(msg)) return;
        if (state.activePage === 'chat') return;
        state.unreadCount++;
        updateUnreadBadge();
    }

    onWs('typing', (msg) => {
        if (!isMyThread(msg)) return;  // each column shows typing only for its own thread
        const actId = msg.activity_id || msg.task_id || ('direct-' + (msg.chat_id || chatId));
        const clientMsgId = msg.client_message_id || '';
        showTyping(actId, {
            clientMessageId: clientMsgId,
            phase: msg.phase || 'thinking',
            // Server-stamped for registry turns and RUNNING queue roots;
            // kind-less frames stay outside snapshot deletion authority.
            kind: msg.kind || '',
        });
    });

    const isMyThread = (msg) => {
        return chatThreadAccepts(msg, isMain, chatId, state.projectChatIds);
    };

    const isMyLogThread = (msg) => {
        return chatLogThreadAccepts(msg, isMain, chatId, state.projectChatIds);
    };

    onWs('chat', (msg) => {
        if (!isMyThread(msg)) return;
        if (msg.role === 'user') {
            const clientMessageId = msg.client_message_id || '';
            const senderSessionId = msg.sender_session_id || '';
            // 2A: the user echo is receipt of the user ROW, not turn start —
            // it settles the bubble but must NOT retire the `Sending...`
            // submission; that takes a linked typing frame / snapshot turn /
            // routing receipt or the turn's conclusion.
            if (senderSessionId === chatSessionId && clientMessageId) {
                markPendingDelivered(clientMessageId);
                syncChatStatus();
                return;
            }
            addMessage(msg.content, 'user', false, msg.ts || null, false, {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId,
                clientMessageId,
                taskId: msg.task_id || '',
            });
            incrementUnreadIfNeeded(msg);
            syncChatStatus();
            return;
        }

        if (msg.role === 'assistant' || msg.role === 'system') {
            const explicitTaskId = msg.task_id || '';
            if (handleReviewReference(msg)) {
                syncChatStatus();
                return;
            }
            if (attachReviewFromRow(msg, msg.ts || '', true)) {
                syncChatStatus();
                return;
            }
            if (PROJECT_ROW_TYPES.has(msg.system_type)) {
                addMessage(msg.content, 'system', msg.markdown, msg.ts || null, false, {
                    systemType: msg.system_type,
                    taskId: explicitTaskId,
                    projectId: msg.project_id || '',
                    projectName: msg.project_name || '',
                });
                incrementUnreadIfNeeded(msg);
                syncChatStatus();
                return;
            }
            learnSubagentLineage(msg);
            const ephemeralDecision = registerEphemeralDecisionFrame(msg);
            if (ephemeralDecision && explicitTaskId) {
                const existing = activeDirectActivities.get(explicitTaskId) || {};
                activeDirectActivities.set(explicitTaskId, {
                    activityId: explicitTaskId,
                    kind: 'ephemeral_decision',
                    phase: 'thinking',
                    startedAt: existing.startedAt || Date.now(),
                    clientMessageId: existing.clientMessageId || '',
                });
            }
            if (msg.is_progress) {
                showTaskIncidentToast(msg);
                if (ephemeralDecision) return;
                if (
                    msg.cancelable === true
                    && explicitTaskId
                    && !msg.subagent_event
                    && !subagentChildParents.has(explicitTaskId)
                ) {
                    showTyping(explicitTaskId, {
                        kind: 'managed_task', phase: msg.phase || 'working',
                    });
                }
                updateLiveCardFromProgressMessage(msg, { grantCancelAuthority: true });
                syncChatStatus();
                return;
            }

            // An early final (post-task still running) is NOT the turn's
            // conclusion; task_done or the queue snapshot concludes it.
            const finalizing = Boolean(explicitTaskId) && msg.task_phase === 'finalizing';
            const typedTerminal = positiveTaskTerminalFact(msg);
            const concludesTurn = !explicitTaskId || typedTerminal;
            if (!finalizing && concludesTurn) {
                if (explicitTaskId) {
                    // 4A (active set): a keyed final concludes ITS OWN turn —
                    // the finished activity + its linked pending — never a
                    // concurrent turn's state (2A keeps later `Sending...`).
                    const finished = activeDirectActivities.get(explicitTaskId);
                    activeDirectActivities.delete(explicitTaskId);
                    recordConcludedActivity(explicitTaskId);
                    if (finished?.clientMessageId) {
                        pendingSubmissions.delete(finished.clientMessageId);
                    }
                } else if (msg.system_type !== 'terminal_incident') {
                    // Unkeyed finals clear unscoped state; incidents are informational.
                    activeDirectActivities.clear();
                    pendingSubmissions.clear();
                }
            }

            if (msg.system_type === 'task_summary') {
                appendTaskSummaryToLiveCard(msg);
                if (!finalizing) markAssistantReply(explicitTaskId);
                incrementUnreadIfNeeded(msg);
                syncChatStatus();
                return;
            }
            if (explicitTaskId && subagentChildParents.has(explicitTaskId)) {
                routeSubagentFinalMessageToCard(explicitTaskId, msg);
                if (typedTerminal) markAssistantReply(explicitTaskId);
                incrementUnreadIfNeeded(msg);
                syncChatStatus();
                return;
            }
            if (finalizing) markLiveCardFinalizing(explicitTaskId);
            else if (explicitTaskId && typedTerminal) {
                finishLiveCard(explicitTaskId, taskTerminalPhase(msg));
            }
            if (!finalizing && typedTerminal) markAssistantReply(explicitTaskId);
            clearTransientRoutingAnnotations();
            addMessage(msg.content, msg.role, msg.markdown, msg.ts || null, false, {
                systemType: msg.system_type || '',
                source: msg.source || '',
                taskId: explicitTaskId,
            });
            incrementUnreadIfNeeded(msg);
            syncChatStatus();
        }
    });

    onWs('message_annotation', (msg) => {
        if (!isMyThread(msg)) return;
        if (msg.annotation_type !== 'routing_ack') return;
        updateMessageAnnotation(msg.client_message_id || '', msg);
        // Any routing receipt is the durable disposition of the submission and
        // ends its `Sending...` phase; further activity announces itself via
        // its own typing frame or task card.
        const receiptCid = String(msg.client_message_id || '');
        if (receiptCid && pendingSubmissions.delete(receiptCid)) {
            syncChatStatus();
        }
    });

    onWs('outbound_dropped', (msg) => {
        // Evicted from the offline queue: the submission will never reach
        // the server, so it can never earn a receipt, a turn, or a journal row.
        const cid = String(msg?.clientMessageId || '');
        if (!cid) return;
        markPendingDropped(cid);
        localEchoJournal.delete(cid);
        if (pendingSubmissions.delete(cid)) syncChatStatus();
    });

    onWs('log', (msg) => {
        if (!msg?.data) return;
        // Log frames carry the task's canonical Project chat_id, so the
        // Project panel alone builds/animates/finalizes that card. Legacy
        // frames without chat_id default to the main chat.
        if (!isMyLogThread(msg)) return;
        updateLiveCardFromLogEvent(msg.data);
    });

    // Cluster B: the proactive namer coined a project name for a fresh card — show it
    // as the card title up front (turn-into-project then reuses the same name). Not
    // thread-gated on chat_id: the broadcast carries only task_id, and applySuggestedName
    // no-ops unless THIS thread already holds that card.
    onWs('task_named', (msg) => {
        applySuggestedName(msg?.task_id || '', msg?.suggested_name || '');
    });

    onWs('outbound_sent', (evt) => {
        const cid = evt?.clientMessageId || '';
        if (cid) {
            // A socket write is not durable acceptance (2A): settle the
            // bubble only; `Sending...` retires on authoritative evidence.
            markPendingDelivered(cid);
            syncChatStatus();
        }
    });

    function buildMediaBubble(msg) {
        const type = msg.msg_type || msg.type;
        if (type !== 'photo' && type !== 'video') return null;
        const role = msg.role === 'user' ? 'user' : 'assistant';
        const sender = role === 'user'
            ? senderLabel('user', false, '', {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId: msg.sender_session_id || '',
            }, chatSessionId)
            : 'Ouroboros';
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const rawTs = msg.ts || new Date().toISOString();
        const timeFmt = formatMsgTime(rawTs);
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        const fallbackMime = type === 'photo' ? 'image/png' : 'video/mp4';
        const mimePattern = type === 'photo' ? /^image\/[a-z0-9.+-]+$/i : /^video\/[a-z0-9.+-]+$/i;
        const mime = mimePattern.test(String(msg.mime || '')) ? String(msg.mime) : fallbackMime;
        const base64Value = type === 'photo' ? msg.image_base64 : msg.video_base64;
        const mediaBase64 = /^[A-Za-z0-9+/=\s]+$/.test(String(base64Value || ''))
            ? String(base64Value || '').replace(/\s+/g, '')
            : '';
        const durableUrl = durableChatMediaUrl(msg.download_url);
        const mediaUrl = mediaBase64 ? `data:${mime};base64,${mediaBase64}` : durableUrl;
        if (!mediaUrl) return null;
        const mediaHtml = type === 'photo'
            ? `<img class="chat-photo" src="${escapeHtmlAttr(mediaUrl)}" alt="Photo attachment">`
            : `<video class="chat-video" src="${escapeHtmlAttr(mediaUrl)}" controls></video>`;
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            ${captionHtml}
            <div class="message">${mediaHtml}</div>
            ${timeHtml}
        `;
        const img = bubble.querySelector('.chat-photo');
        if (img) {
            img.addEventListener('click', () => window.open(mediaUrl, '_blank'));
        }
        stampNodeTimestamp(bubble, rawTs);
        return bubble;
    }

    function appendMediaBubble(msg) {
        const key = chatMediaMessageKey(msg);
        if (key && seenMessageKeys.has(key)) return false;
        const bubble = buildMediaBubble(msg);
        if (!bubble) return false;
        rememberMessageKey(key);
        insertMessageNode(bubble);
        return true;
    }

    for (const type of ['photo', 'video']) onWs(type, (msg) => {
        if (!isMyThread(msg)) return;
        // Media frames carry no activity identity: hide the dots row but leave
        // the authoritative active set intact; sync derives the header from it.
        hideTypingIndicatorOnly();
        syncChatStatus();
        if (appendMediaBubble(msg)) incrementUnreadIfNeeded(msg);
    });

    // Shared document-bubble builder for both live WS frames and history replay.
    // Download priority: a durable server download_url routed through
    // downloadViaHostBridge (desktop host-bridge saves to Downloads instead of
    // navigating the WKWebView fullscreen; browser falls back to fetch+blob),
    // else an in-memory base64 blob (live-only), else a disabled label.
    function buildDocumentBubble(msg) {
        const role = msg.role === 'user' ? 'user' : 'assistant';
        const sender = role === 'user'
            ? senderLabel('user', false, '', {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId: msg.sender_session_id || '',
            }, chatSessionId)
            : 'Ouroboros';
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const rawTs = msg.ts || new Date().toISOString();
        const timeFmt = formatMsgTime(rawTs);
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        const mime = /^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+$/.test(String(msg.mime || ''))
            ? String(msg.mime)
            : 'application/octet-stream';
        const fileBase64 = /^[A-Za-z0-9+/=\s]+$/.test(String(msg.file_base64 || ''))
            ? String(msg.file_base64 || '').replace(/\s+/g, '')
            : '';
        const downloadUrl = /^\/api\/files\/download\?/.test(String(msg.download_url || ''))
            ? String(msg.download_url)
            : '';
        const filename = String(msg.filename || 'file').replace(/[\r\n]+/g, ' ').slice(0, 200);
        const canDownload = Boolean(downloadUrl || fileBase64);
        // Body click = open in default OS app (external window); a separate ↓
        // button saves to ~/Downloads. Both degrade to a base64 blob when only
        // the live payload is present (no durable server URL to hand the bridge).
        const openHtml = canDownload
            ? `<button type="button" class="chat-file" data-open="1">📎 ${escapeHtml(filename)}</button>`
            : `<span class="chat-file chat-file-empty">📎 ${escapeHtml(filename)}</span>`;
        const downloadHtml = canDownload
            ? `<button type="button" class="chat-file-download" data-download="1" title="Download" aria-label="Download">↓</button>`
            : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            ${captionHtml}
            <div class="message"><div class="chat-file-row">${openHtml}${downloadHtml}</div></div>
            ${timeHtml}
        `;
        const saveBlobFallback = () => {
            const bytes = Uint8Array.from(atob(fileBase64), (c) => c.charCodeAt(0));
            const blobUrl = URL.createObjectURL(new Blob([bytes], { type: mime }));
            const tmp = document.createElement('a');
            Object.assign(tmp, { href: blobUrl, download: filename, rel: 'noopener' });
            document.body.appendChild(tmp);
            tmp.click();
            tmp.remove();
            setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
        };
        const openBtn = bubble.querySelector('.chat-file[data-open]');
        if (openBtn && canDownload) {
            openBtn.addEventListener('click', async () => {
                try {
                    if (downloadUrl) {
                        await openViaHostBridge(downloadUrl, filename);
                        return;
                    }
                    saveBlobFallback();
                } catch (err) {
                    showToast(`Could not open file: ${err && err.message ? err.message : err}`, 'error');
                }
            });
        }
        const dlBtn = bubble.querySelector('.chat-file-download[data-download]');
        if (dlBtn && canDownload) {
            dlBtn.addEventListener('click', async () => {
                try {
                    if (downloadUrl) {
                        await downloadViaHostBridge(downloadUrl, filename);
                        return;
                    }
                    saveBlobFallback();
                } catch (err) {
                    showToast(`Could not download file: ${err && err.message ? err.message : err}`, 'error');
                }
            });
        }
        stampNodeTimestamp(bubble, rawTs);
        return bubble;
    }

    function appendDocumentBubble(msg) {
        const key = documentMessageKey(msg);
        if (key && seenMessageKeys.has(key)) return false;
        rememberMessageKey(key);
        insertMessageNode(buildDocumentBubble(msg));
        return true;
    }

    onWs('document', (msg) => {
        if (!isMyThread(msg)) return;
        hideTypingIndicatorOnly();
        syncChatStatus();
        if (appendDocumentBubble(msg)) incrementUnreadIfNeeded(msg);
    });

    let wsHasConnectedOnce = false;

    onWs('open', (msg) => {
        // Reconnect drops kind-less entries (no snapshot source tracks them);
        // kind-stamped ones reconcile against the refreshed snapshot below.
        for (const [aid, entry] of activeDirectActivities) {
            if (!entry.kind) activeDirectActivities.delete(aid);
        }
        refreshHeaderControlState(true);
        syncChatStatus();
        // perf2 P4.1 [Gemini#3]: reconnect truth comes from the ws CLIENT
        // (previouslyConnected rides the open event) — a project instance
        // created while the socket was already open must still treat the next
        // open as a reconnect. The per-instance flag stays only as a fallback
        // for open events without a payload.
        const isReconnect = typeof msg?.previouslyConnected === 'boolean'
            ? msg.previouslyConnected
            : wsHasConnectedOnce;
        const reconnectBanner =
            pendingReconnectBannerText
            || (isReconnect ? '♻️ Reconnected' : '');
        const shouldClearReconnectParams = Boolean(pendingReconnectBannerText);
        pendingReconnectBannerText = '';
        wsHasConnectedOnce = true;
        updateMessagesPadding();
        loadUiPreferences()
            // Reconnect ALWAYS does a real fetch (a lost task_done is healed
            // only by refetching); the first clean open is a hydration trigger
            // and rides the sticky single-flight behind Main's idle gate.
            .then(() => (isReconnect
                ? syncHistory({ includeUser: !historyLoaded, fromReconnect: isReconnect })
                : waitForHydrationWindow().then(
                    () => awaitInitialHydration({ includeUser: !historyLoaded }),
                )))
            .then((hasMessages) => {
                if (!hasMessages) ensureWelcomeMessage();
                if (reconnectBanner) {
                    addMessage(reconnectBanner, 'system', false, null, false, { ephemeral: true, systemType: 'reconnect' });
                    if (shouldClearReconnectParams) clearPendingReconnectBanner();
                }
            })
            .catch(() => {
                if (reconnectBanner) {
                    addMessage(reconnectBanner, 'system', false, null, false, { ephemeral: true, systemType: 'reconnect' });
                    if (shouldClearReconnectParams) clearPendingReconnectBanner();
                }
            });
    });

    onWs('close', () => {
        hideTypingIndicatorOnly();
        syncChatStatus();
        syncHeaderControlState({ accounting: { available: false } });
    });

    return {
        page,
        chatId,
        projectId,
        // Called by app.js when this instance's panel is (re)shown so a project
        // thread restores its scroll position instead of jumping to the top (P7).
        restoreScrollPosition,
        refreshHistory,
        cancelHistoryPaint,
        // app.js fans its already-existing /api/state refresh to every open
        // thread; panels gain convergence without acquiring their own poll.
        hydrateStateSnapshot,
        // True once a history snapshot has actually been fetched and painted;
        // app.js uses it to decide whether a reopen needs a forced repaint.
        hasPaintedHistory: () => historyLoaded && lastHistorySyncSucceeded,
        // Unsendable client-side state (staged File objects / an in-flight
        // upload). app.js must hide, not destroy, an instance holding it.
        hasPendingWork: () => pendingAttachments.length > 0 || attachmentsUploading,
        // Viewport intent stash source for the single-live-panel policy.
        getScrollState: () => ({ scrollTop: _savedScrollTop, stick: _savedStick }),
        // Full teardown (P3): release every resource this instance acquired —
        // ws subscriptions, window/document listeners, the ResizeObserver, all
        // timers — then drop the buffered collections and remove the DOM last.
        // Idempotent; late rAF/async continuations no-op on `destroyed`.
        destroy() {
            if (destroyed) return;
            destroyed = true;
            cancelHistoryPaint();
            for (const dispose of wsDisposers) {
                try { dispose(); } catch {}
            }
            wsDisposers.length = 0;
            window.removeEventListener('ouro:page-shown', handlePageShown);
            document.removeEventListener('visibilitychange', handleVisibilityChange);
            if (documentClickHandler) document.removeEventListener('click', documentClickHandler);
            if (documentKeydownHandler) document.removeEventListener('keydown', documentKeydownHandler);
            chatResizeObserver?.disconnect();
            chatResizeObserver = null;
            historyResyncScheduler.cancel();
            if (_chatFreedTimer) { clearTimeout(_chatFreedTimer); _chatFreedTimer = null; }
            for (const taskState of taskUiStates.values()) {
                if (taskState?.cleanupTimer) clearTimeout(taskState.cleanupTimer);
            }
            if (headerControlInterval) { clearInterval(headerControlInterval); headerControlInterval = null; }
            liveCardRecords.clear();
            explicitCardExpansion.clear();
            reviewDisclosureByTask.clear();
            skillReviewDetailStore.clear();
            reviewHydrator.clear();
            taskUiStates.clear();
            pendingSuggestedNames.clear();
            subagentChildParents.clear();
            subagentTerminalChildren.clear();
            cancelableTaskIds.clear();
            missingManagedTaskIds.clear();
            managedTaskDetailReads.clear();
            ephemeralDecisionTaskIds.clear();
            retiredTaskIds.clear();
            pendingUserBubbles.clear();
            localEchoJournal.clear();
            seenMessageKeys.clear();
            messageKeyOrder.length = 0;
            persistedHistory.length = 0;
            try { page.remove(); } catch {}
        },
    };
}
