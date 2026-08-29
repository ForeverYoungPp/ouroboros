"""Focused browser proof for the selected skill-publish flow."""

from __future__ import annotations

import json

import pytest

from tests.test_ui_smoke_playwright import (
    direct_server_with_data as _direct_server_with_data,
)

direct_server_with_data = _direct_server_with_data


@pytest.mark.ui_browser
def test_ui_publish_stale_card_reaches_selected_preflight_and_task(
    direct_server_with_data,
):
    """A rendered card remains agent-repairable after its manifest disappears."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    data_dir = direct_server_with_data["data_dir"]
    url = direct_server_with_data["url"]
    settings_path = data_dir / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["GITHUB_TOKEN"] = "ui-smoke-github-token"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    skill_name = "publish-stale-card"
    skill_root = data_dir / "skills" / "external" / skill_name
    skill_root.mkdir(parents=True, exist_ok=True)
    manifest_path = skill_root / "SKILL.md"
    manifest_path.write_text(
        "---\n"
        f"name: {skill_name}\n"
        "type: instruction\n"
        "description: stale-card publish smoke\n"
        "version: 0.1.0\n"
        "---\n"
        f"# {skill_name}\n",
        encoding="utf-8",
    )
    direct_server_with_data["restart_server"]()

    posted_tasks = []

    def handle_tasks(route, request):
        if request.method == "POST" and request.url.rstrip("/").endswith("/api/tasks"):
            posted_tasks.append(request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "task_id": "publish-stale-task",
                        "status": "scheduled",
                    }
                ),
            )
            return
        route.continue_()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.route("**/api/tasks", handle_tasks)
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Opening the Skills page starts TWO reads: /api/skills paints
                # the cards, and the display-only OuroborosHub catalog repaints
                # the WHOLE list when it settles (renderSkillsList's
                # catalogSettled.then re-runs paint over container.innerHTML).
                # A repaint landing between the menu-trigger click and the
                # submit-hub click below recreates the card's <dialog> CLOSED,
                # so the click waits its full 30s for a hidden menu item. Gate
                # the flow on the catalog response (server-bounded at 15s) and
                # settle one frame so the deferred repaint has applied before
                # any interaction (same hydration-gate precedent as
                # tests/test_ui_smoke_status_attention.py).
                with page.expect_response(
                    lambda response: "/api/marketplace/ouroboroshub/catalog"
                    in response.url,
                    timeout=30_000,
                ) as catalog_hydration:
                    page.click('[data-nav-page="skills"]')
                catalog_hydration.value.finished()
                card = page.locator(f'.skills-card[data-skill="{skill_name}"]').first
                card.wait_for(state="visible", timeout=30_000)
                page.evaluate(
                    "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                assert card.locator(".skills-submit-hub").get_attribute("data-submit-disabled") == "false"

                # The passive catalogue no longer contains this row, but the selected
                # preflight still knows the clicked leaf and can return needs_attention.
                manifest_path.unlink()
                card.locator("[data-skill-menu-trigger]").click()
                with page.expect_request(
                    f"**/api/skills/{skill_name}/publish-preflight",
                    timeout=30_000,
                ) as preflight_request:
                    card.locator(".skills-submit-hub").click()
                assert preflight_request.value.method == "POST"

                dialog = page.locator(".confirm-dialog")
                dialog.wait_for(state="visible", timeout=30_000)
                assert "Ask Ouroboros to prepare this publication" in (
                    dialog.locator("#confirm-dialog-title").inner_text() or ""
                )
                dialog.locator(".confirm-dialog-details summary").click()
                dialog_text = dialog.inner_text()
                assert "Needs attention" in dialog_text
                assert "snapshot_manifest_missing" in dialog_text
                with page.expect_request("**/api/tasks", timeout=30_000):
                    dialog.locator("[data-confirm-ok]").click()
                page.wait_for_function("() => !document.querySelector('.confirm-dialog')", timeout=30_000)

                assert len(posted_tasks) == 1
                payload = posted_tasks[0]
                assert payload["type"] == "skill_publish"
                assert payload["metadata"]["skill_publish_target"] == {
                    "skill": skill_name,
                    "repository": "razzant/OuroborosHub",
                }
                assert "workspace_root" not in payload
                assert "acceptance_claims" not in payload
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
