from __future__ import annotations

import pytest

pytest_plugins = ("tests.test_ui_smoke_playwright",)


@pytest.mark.ui_browser
@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 800), (390, 844)],
    ids=["desktop", "narrow"],
)
def test_failed_child_stays_local_while_root_keeps_working(
    direct_server_with_data,
    width,
    height,
):
    """A failed child stays factual and local while its root keeps working."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "chat.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "progress.jsonl").write_text("", encoding="utf-8")

    capture_socket = """() => {
        const NativeWebSocket = window.WebSocket;
        window.__statusAttentionTestSockets = [];
        window.WebSocket = class TestWebSocket extends NativeWebSocket {
            constructor(...args) {
                super(...args);
                window.__statusAttentionTestSockets.push(this);
            }
        };
    }"""

    def emit(page, frame):
        page.evaluate(
            """frame => {
                const socket = window.__statusAttentionTestSockets
                    ?.find(candidate => candidate.readyState === WebSocket.OPEN);
                if (!socket) throw new Error('test socket is not open');
                socket.dispatchEvent(new MessageEvent('message', {
                    data: JSON.stringify(frame),
                }));
            }""",
            frame,
        )
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            try:
                page.add_init_script(f"({capture_socket})()")
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(
                    "() => window.__statusAttentionTestSockets"
                    "?.some(socket => socket.readyState === WebSocket.OPEN)",
                    timeout=30_000,
                )

                emit(page, {
                    "type": "chat",
                    "role": "assistant",
                    "is_progress": True,
                    "chat_id": 1,
                    "task_id": "status-parent",
                    "content": "Parent continues the requested work",
                    "ts": "2026-08-24T12:00:00+00:00",
                })
                emit(page, {
                    "type": "chat",
                    "role": "assistant",
                    "is_progress": True,
                    "chat_id": 1,
                    "task_id": "status-child",
                    "delegation_role": "subagent",
                    "subagent_event": "scheduled",
                    "subagent_task_id": "status-child",
                    "parent_task_id": "status-parent",
                    "root_task_id": "status-parent",
                    "subagent_role": "route-checker",
                    "content": "Child checks the delegated route",
                    "status": "scheduled",
                    "ts": "2026-08-24T12:00:01+00:00",
                })
                emit(page, {
                    "type": "chat",
                    "role": "assistant",
                    "is_progress": True,
                    "chat_id": 1,
                    "task_id": "status-child",
                    "delegation_role": "subagent",
                    "subagent_event": "failed",
                    "subagent_task_id": "status-child",
                    "parent_task_id": "status-parent",
                    "root_task_id": "status-parent",
                    "subagent_role": "route-checker",
                    "content": "Child route finished unsuccessfully",
                    "status": "failed",
                    "reason_code": "delegated_route_unavailable",
                    "error": "The selected tool route was unavailable; the parent can continue.",
                    "ts": "2026-08-24T12:00:02+00:00",
                })

                parent = page.locator(
                    '.chat-live-card:not(.subagent)[data-task-id="status-parent"]'
                )
                child = page.locator(
                    '.chat-live-card.subagent[data-task-id="status-child"]'
                )
                parent.wait_for(state="visible", timeout=30_000)
                child.wait_for(state="visible", timeout=30_000)
                page.wait_for_function(
                    "() => document.querySelector('#chat-status')?.textContent === 'Working...'",
                    timeout=30_000,
                )

                assert child.evaluate(
                    "card => card.parentElement?.closest('.chat-live-card')?.dataset.taskId"
                ) == "status-parent"
                assert parent.get_attribute("data-finished") == "0"
                assert child.get_attribute("data-finished") == "1"
                assert parent.locator(
                    ":scope > [data-live-summary-button] [data-live-phase]"
                ).inner_text() == "Working"
                assert child.locator(
                    ":scope > [data-live-summary-button] [data-live-phase]"
                ).inner_text() == "Failed"
                assert "Failed" in child.inner_text()

                card_text = page.locator("#chat-messages").inner_text()
                assert "Issue" not in card_text
                assert "Notice" not in card_text
                assert page.locator("#chat-status").inner_text() == "Working..."
                assert "Attention" not in page.locator("#chat-status").inner_text()
                assert page.locator("#toast-stack .toast").count() == 0
                assert page.locator('[data-nav-page="chat"] .unread-badge').count() == 0

                geometry = page.evaluate(
                    """() => {
                        const facts = card => {
                            const summary = card.querySelector('[data-live-summary-button]');
                            const phase = summary.querySelector('[data-live-phase]');
                            const title = summary.querySelector('[data-live-title]');
                            const cardRect = card.getBoundingClientRect();
                            const summaryRect = summary.getBoundingClientRect();
                            return {
                                card: {
                                    left: cardRect.left,
                                    right: cardRect.right,
                                    width: cardRect.width,
                                },
                                summary: {
                                    left: summaryRect.left,
                                    right: summaryRect.right,
                                    width: summaryRect.width,
                                },
                                cardClientWidth: card.clientWidth,
                                cardScrollWidth: card.scrollWidth,
                                summaryClientWidth: summary.clientWidth,
                                summaryScrollWidth: summary.scrollWidth,
                                phaseClientWidth: phase.clientWidth,
                                phaseScrollWidth: phase.scrollWidth,
                                titleClientWidth: title.clientWidth,
                                titleScrollWidth: title.scrollWidth,
                            };
                        };
                        const parent = document.querySelector(
                            '.chat-live-card:not(.subagent)[data-task-id="status-parent"]'
                        );
                        const child = document.querySelector(
                            '.chat-live-card.subagent[data-task-id="status-child"]'
                        );
                        const status = document.querySelector('#chat-status').getBoundingClientRect();
                        const messages = document.querySelector('#chat-messages');
                        return {
                            viewportWidth: window.innerWidth,
                            documentClientWidth: document.documentElement.clientWidth,
                            documentScrollWidth: document.documentElement.scrollWidth,
                            messagesClientWidth: messages.clientWidth,
                            messagesScrollWidth: messages.scrollWidth,
                            statusLeft: status.left,
                            statusRight: status.right,
                            parent: facts(parent),
                            child: facts(child),
                        };
                    }"""
                )
                assert geometry["documentScrollWidth"] <= geometry["documentClientWidth"] + 1, geometry
                assert geometry["messagesScrollWidth"] <= geometry["messagesClientWidth"] + 1, geometry
                assert geometry["statusLeft"] >= -1, geometry
                assert geometry["statusRight"] <= geometry["viewportWidth"] + 1, geometry
                for card in (geometry["parent"], geometry["child"]):
                    assert card["card"]["left"] >= -1, geometry
                    assert card["card"]["right"] <= geometry["viewportWidth"] + 1, geometry
                    assert card["summary"]["left"] >= card["card"]["left"] - 1, geometry
                    assert card["summary"]["right"] <= card["card"]["right"] + 1, geometry
                    assert card["cardScrollWidth"] <= card["cardClientWidth"] + 1, geometry
                    assert card["summaryScrollWidth"] <= card["summaryClientWidth"] + 1, geometry
                    assert card["phaseScrollWidth"] <= card["phaseClientWidth"] + 1, geometry
                    assert card["titleScrollWidth"] <= card["titleClientWidth"] + 1, geometry
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
