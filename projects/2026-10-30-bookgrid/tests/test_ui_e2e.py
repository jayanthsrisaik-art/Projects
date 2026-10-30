"""Browser test of the built UI against a live server.

Skipped unless the frontend is built (``npm run build``) and Python
Playwright with a Chromium browser is available.
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pw = pytest.importorskip("playwright.sync_api")
if not (ROOT / "frontend" / "dist" / "src" / "app.js").exists():
    pytest.skip("frontend not built", allow_module_level=True)

import uvicorn  # noqa: E402

from bookgrid.api import create_app  # noqa: E402
from bookgrid.seed import load_seed  # noqa: E402


@pytest.fixture
def server(tmp_path):
    app = create_app(str(tmp_path / "ui.db"))
    load_seed(app.state.store, ROOT / "samples" / "seed.json")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


def test_book_conflict_and_cancel_in_browser(server, tmp_path):
    with pw.sync_playwright() as p:
        # BOOKGRID_CHROMIUM points at an existing Chrome/Chromium binary when
        # Playwright's own bundled browser is not installed.
        exe = os.environ.get("BOOKGRID_CHROMIUM")
        try:
            browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{server}/?week=2026-11-02")
        # Seeded standup (Mon-Fri) + fortnightly design review on Wed 4 Nov.
        page.wait_for_selector(".block")
        assert page.locator(".block", has_text="Platform standup").count() == 5
        assert page.locator(".block", has_text="Design review").count() == 1

        page.fill("input[name=title]", "Retro")
        page.fill("input[name=organizer]", "ben")
        page.fill("input[name=date]", "2026-11-02")
        page.fill("input[name=start_time]", "13:30")
        page.fill("input[name=duration]", "60")
        page.select_option("select[name=freq]", "WEEKLY")
        for d in ("MO", "WE", "FR"):
            page.check(f"input[name=day][value={d}]")
        page.fill("input[name=count]", "3")
        assert page.inner_text("#rule-text") == "Every week on Mon, Wed, Fri, 3 times"

        page.click("button[type=submit]")
        err = page.wait_for_selector("#form-result .err")
        assert 'clashes with "Design review"' in err.inner_text()
        assert page.locator(".block", has_text="Retro").count() == 0  # reject = nothing written

        page.select_option("select[name=on_conflict]", "skip")
        page.click("button[type=submit]")
        ok = page.wait_for_selector("#form-result .ok")
        assert "Booked 2 occurrence(s)" in ok.inner_text()
        page.wait_for_function("document.querySelectorAll('.block').length === 8")
        page.screenshot(path=str(tmp_path / "week.png"), full_page=True)

        page.locator(".block", has_text="Retro").first.click()
        page.click("#details button[data-scope=series]")
        page.wait_for_selector("#d-result .ok")
        assert "Cancelled 2" in page.inner_text("#d-result")
        page.wait_for_function("document.querySelectorAll('.block').length === 6")
        assert errors == []
        browser.close()
