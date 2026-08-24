import { taskPresentation } from './log_events.js';

// Pure desired-chip projection. Terminal truth wins; while unfinished, an
// owner stop/finalization hold stays sticky across ordinary progress frames.
export function desiredLiveCardPhase(record = {}, terminalPhase = 'done') {
    if (record.finished) {
        const presentation = taskPresentation(terminalPhase || 'done');
        return {
            phase: presentation.phase,
            text: presentation.headline,
            className: `chat-live-phase ${presentation.phase}`,
        };
    }
    if (record.cancelPendingPolicy) {
        return {
            phase: 'working',
            text: record.cancelPendingPolicy === 'finalize' ? 'Finalizing…' : 'Cancelling…',
            className: 'chat-live-phase working cancelling',
        };
    }
    if (record.finalizingHold) {
        return {
            phase: 'working',
            text: 'Finalizing…',
            className: 'chat-live-phase working finalizing',
        };
    }
    return { phase: 'working', text: 'Working', className: 'chat-live-phase working' };
}

// One writer for the stable factual task/subagent phase chip. Technical
// nonterminal diagnostics stay in the card timeline/details.
export function setLiveCardPhase(record, phase = 'working', text = '', className = '') {
    if (!record?.phaseEl) return;
    const activePhase = String(phase || 'working');
    const activeText = String(text || taskPresentation(activePhase).headline);
    const activeClassName = className || `chat-live-phase ${activePhase}`;
    const activeLabel = `${record.isSubagent ? 'Subagent' : 'Task'} status: ${activeText}`;
    const phaseEl = record.phaseEl;
    if (phaseEl.dataset.phase !== activePhase) phaseEl.dataset.phase = activePhase;
    if (phaseEl.className !== activeClassName) phaseEl.className = activeClassName;
    // Do not make a polite live region re-announce identical routine progress.
    if (phaseEl.textContent !== activeText) phaseEl.textContent = activeText;
    if (phaseEl.getAttribute('role') !== 'status') phaseEl.setAttribute('role', 'status');
    if (phaseEl.getAttribute('aria-live') !== 'polite') phaseEl.setAttribute('aria-live', 'polite');
    if (phaseEl.getAttribute('aria-atomic') !== 'true') phaseEl.setAttribute('aria-atomic', 'true');
    if (phaseEl.getAttribute('aria-label') !== activeLabel) phaseEl.setAttribute('aria-label', activeLabel);
}
