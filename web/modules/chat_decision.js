// Owner decision cards: the typed quiz card (question + option buttons +
// stake + assumption) and its lifecycle states. The card is fire-and-continue
// UI: the asking task keeps working under the stated assumption, so the card
// must read correctly both as "you can redirect me" (open) and as a record of
// what happened (answered / expired). The routing picker (#198) joins this
// module when it lands — one decision-card family, one answer contract.
import { MAX_QUIZ_OPTIONS } from './api_types.js';

const QUIZ_STATUS_TEXT = {
    open: 'Awaiting answer',
    answered: 'Answered',
    expired_terminal: 'Task finished — question expired',
    superseded: 'Superseded by a retry',
};

export function createChatDecision({
    apiFetch,
    frameNode,
    renderMarkdown,
    enhanceMarkdown,
    showToast,
}) {
    function normalizeQuiz(msg) {
        const nested = msg && typeof msg.quiz === 'object' && msg.quiz ? msg.quiz : null;
        const src = nested || msg || {};
        // Strict per-card validation: ONE corrupt option refuses THIS card
        // (buildQuizCard -> null), never the whole history hydration pass.
        // Filtering instead would silently shift option_index against the
        // producer's original list — a wrong answer, not a degraded card.
        const raw = Array.isArray(src.options) ? src.options : [];
        const normalized = raw.map((option) => (typeof option === 'string' ? { label: option } : option));
        const corrupt = normalized.some(
            (option) => !option || typeof option !== 'object' || !String(option.label || '').trim());
        const options = corrupt ? [] : normalized.slice(0, MAX_QUIZ_OPTIONS);
        return {
            quizId: String(src.quiz_id || ''),
            question: String((nested ? msg.text : src.question) || ''),
            options,
            stake: String(src.stake || ''),
            assumption: String(src.assumption || ''),
            state: String(src.state || 'open'),
            taskId: String(msg.task_id || ''),
            ts: msg.ts || null,
            answeredIndex: Number.isInteger(src.answered_index) ? src.answered_index : null,
        };
    }

    function statusText(state) {
        // Unknown states read as settled, never as an open invitation.
        return QUIZ_STATUS_TEXT[state] || 'Closed';
    }

    async function submitAnswer(card, quiz, index) {
        if (card.dataset.pending === '1') return;
        card.dataset.pending = '1';
        // STABLE per-card idempotency key: a retry after a transient failure
        // must replay the SAME request, or the server-side first-wins latch
        // reads the retry as a competing second answer.
        if (!card.dataset.requestId) {
            card.dataset.requestId = (crypto.randomUUID && crypto.randomUUID()) || `q-${Date.now()}`;
        }
        try {
            const res = await apiFetch('/api/decisions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: card.dataset.requestId,
                    decision_id: `quiz:${quiz.taskId}:${quiz.quizId}`,
                    option_index: index,
                }),
            });
            let body = null;
            try { body = res && res.json ? await res.json() : null; } catch (parseErr) { body = null; }
            if (res && res.ok) {
                const answered = body && Number.isInteger(body.answered_index) ? body.answered_index : index;
                setCardState(card, 'answered', answered);
                return;
            }
            const status = res ? res.status : 0;
            if (status === 409 && body && body.state) {
                // The refusal body carries the card's TRUE lifecycle state —
                // an already-answered quiz settles as answered (with the
                // winning option when known), never as a false expiry.
                const answered = Number.isInteger(body.answered_index) ? body.answered_index : null;
                setCardState(card, body.state, answered);
                showToast(body.state === 'answered'
                    ? 'Already answered.' : 'This question is no longer open.', 'error');
                return;
            }
            if (status === 409 && card.dataset.state === 'open') {
                setCardState(card, 'expired_terminal', null);
                showToast('This question is no longer open.', 'error');
                return;
            }
            showToast(`Could not record the answer (${status || 'network error'}).`, 'error');
        } catch (err) {
            showToast('Could not record the answer (network error).', 'error');
        } finally {
            delete card.dataset.pending;
        }
    }

    function setCardState(card, state, answeredIndex) {
        if (!card) return;
        card.dataset.state = state;
        const status = card.querySelector('.chat-quiz-status-text');
        if (status) status.textContent = statusText(state);
        const buttons = card.querySelectorAll('.chat-quiz-option');
        buttons.forEach((btn, i) => {
            btn.disabled = state !== 'open';
            btn.classList.toggle('chosen', answeredIndex !== null && i === answeredIndex);
        });
    }

    function buildQuizCard(msg) {
        const quiz = normalizeQuiz(msg);
        if (!quiz.quizId || !quiz.taskId || !quiz.question || quiz.options.length < 2) return null;

        const card = document.createElement('div');
        card.className = 'chat-quiz-card';
        card.dataset.quizId = quiz.quizId;

        const head = document.createElement('div');
        head.className = 'chat-quiz-head';
        const chip = document.createElement('span');
        chip.className = 'chat-quiz-chip';
        chip.textContent = 'Question';
        const status = document.createElement('span');
        status.className = 'chat-quiz-status';
        const dot = document.createElement('span');
        dot.className = 'chat-quiz-dot';
        const statusLabel = document.createElement('span');
        statusLabel.className = 'chat-quiz-status-text';
        status.append(dot, statusLabel);
        head.append(chip, status);
        card.append(head);

        // DRY with the chat surface (owner requirement): question and stake go
        // through the SAME sanitizing markdown pipeline as assistant bubbles,
        // so chat rendering improvements reach the card automatically.
        const question = document.createElement('div');
        question.className = 'chat-quiz-question';
        if (renderMarkdown) question.innerHTML = renderMarkdown(quiz.question);
        else question.textContent = quiz.question;
        card.append(question);

        if (quiz.stake) {
            const stake = document.createElement('div');
            stake.className = 'chat-quiz-stake';
            if (renderMarkdown) stake.innerHTML = renderMarkdown(`At stake: ${quiz.stake}`);
            else stake.textContent = `At stake: ${quiz.stake}`;
            card.append(stake);
        }

        const optionsBox = document.createElement('div');
        optionsBox.className = 'chat-quiz-options';
        quiz.options.forEach((option, index) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'chat-quiz-option';
            const label = document.createElement('span');
            label.className = 'chat-quiz-option-label';
            label.textContent = String(option.label || '');
            btn.append(label);
            const detailText = String(option.detail || '');
            if (detailText) {
                const detail = document.createElement('span');
                detail.className = 'chat-quiz-option-detail';
                detail.textContent = detailText;
                btn.append(detail);
            }
            btn.addEventListener('click', () => {
                if (card.dataset.state !== 'open') return;
                submitAnswer(card, quiz, index);
            });
            optionsBox.append(btn);
        });
        card.append(optionsBox);

        // The signature line: what the agent keeps doing while the owner has
        // not answered — and, once the card settles, the record of the path
        // it took by default.
        if (quiz.assumption) {
            const assumption = document.createElement('div');
            assumption.className = 'chat-quiz-assumption';
            assumption.textContent = `Continuing meanwhile: ${quiz.assumption}`;
            card.append(assumption);
        }

        setCardState(card, quiz.state, quiz.answeredIndex);
        const framed = frameNode(msg, card);
        if (enhanceMarkdown && renderMarkdown) enhanceMarkdown(card);
        return framed;
    }

    function applyQuizStateFrame(rootNode, frame) {
        // Live lifecycle update for an already-rendered card (WS "quiz_state").
        // The card is found by identity, never appended: state changes must
        // not create a second card (the quiz frame dedupe is id+ts keyed).
        const quizId = String(frame && frame.quiz_id || '');
        if (!quizId || !rootNode) return false;
        const card = rootNode.querySelector(`.chat-quiz-card[data-quiz-id="${CSS.escape(quizId)}"]`);
        if (!card) return false;
        const index = Number.isInteger(frame.answered_index) ? frame.answered_index : null;
        setCardState(card, String(frame.state || ''), index);
        return true;
    }

    return { buildQuizCard, setCardState, applyQuizStateFrame };
}
