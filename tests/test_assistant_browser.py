"""Real Chromium tests, no model calls and no writes to external websites."""
from __future__ import annotations

import asyncio

import pytest

from parallax.assistant.actions import Action, needs_approval
from parallax.assistant.browser import PersonalBrowser


def test_browser_observation_actions_and_stale_forms(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True)
        try:
            await browser.start()
            await browser.page.set_content('''<h1>Test shop</h1>
              <label>Name<input id="name"></label><input type="password" value="SECRET" aria-label="Password">
              <select aria-label="Room"><option value="double">Double</option></select>
              <button onclick="document.querySelector('h1').textContent='Confirmed'">Reserve</button>''')
            first = await browser.observe()
            assert "SECRET" not in str(first)
            name = next(e for e in first["elements"] if e["label"] == "Name")
            password = next(e for e in first["elements"] if e["label"] == "Password")
            with pytest.raises(ValueError):
                browser.preview(Action("fill", password["id"], "anything"), first)
            await browser.execute(Action("fill", name["id"], "Test user"))
            assert await browser.page.locator("#name").input_value() == "Test user"
            changed = await browser.observe()
            assert changed["fingerprint"] != first["fingerprint"]
            assert "Test user" not in str(changed)
            button = next(e for e in changed["elements"] if e["label"] == "Reserve")
            browser.preview(Action("click", button["id"]), changed)
            await browser.execute(Action("click", button["id"]))
            assert "Confirmed" in (await browser.observe())["text"]
        finally:
            await browser.close()
    asyncio.run(run())


def test_existing_chrome_endpoint_validation(tmp_path):
    browser = PersonalBrowser(tmp_path / "unused", existing_chrome=True, chrome_data_dir=tmp_path)
    with pytest.raises(RuntimeError, match="Remote|remote"):
        browser._chrome_endpoint()
    for value in ["0\n/devtools/browser/abc", "9222\n//remote.example/", "secret", "65536\n/devtools/browser/abc"]:
        (tmp_path / "DevToolsActivePort").write_text(value)
        with pytest.raises(RuntimeError):
            browser._chrome_endpoint()
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/abc-123\n")
    assert browser._chrome_endpoint() == "ws://127.0.0.1:9222/devtools/browser/abc-123"


def test_existing_chrome_shares_session_and_disconnects_without_closing(tmp_path):
    from playwright.async_api import async_playwright

    async def run():
        async with async_playwright() as driver:
            profile = tmp_path / "external-profile"
            owner = await driver.chromium.launch_persistent_context(
                str(profile), headless=True, chromium_sandbox=True,
                args=["--remote-debugging-port=0"],
            )
            browser = PersonalBrowser(tmp_path / "unused", existing_chrome=True, chrome_data_dir=profile)
            try:
                original = owner.pages[0]
                await original.set_content("<h1>Unrelated private tab</h1>")
                await owner.add_cookies([{"name": "test_session", "value": "fake-session", "domain": "example.com", "path": "/"}])
                await browser.start()
                assert not (tmp_path / "unused").exists()
                assert any(c["name"] == "test_session" for c in await browser.context.cookies())
                await browser.page.set_content("<h1>Assistant tab</h1><input aria-label='Name'>")
                snapshot = await browser.observe()
                assert len(snapshot["tabs"]) == 1
                assert "Unrelated private tab" not in str(snapshot)
                name = snapshot["elements"][0]
                await browser.execute(Action("fill", name["id"], "Demo"))
                assert await browser.page.locator("input").input_value() == "Demo"
                await browser.close()
                assert not original.is_closed()
                assert len(owner.pages) == 2
                assert await original.inner_text("h1") == "Unrelated private tab"
                assert await owner.pages[1].locator("input").input_value() == "Demo"
                await browser.start()
                assert len((await browser.observe())["tabs"]) == 1
            finally:
                await browser.close()
                await owner.close()
    asyncio.run(run())


def test_browser_blocks_local_and_control_urls(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path)
        assert not await browser._allowed("http://127.0.0.1:1234/")
        assert not await browser._allowed("http://169.254.169.254/latest/meta-data")
        assert not await browser._allowed("file:///etc/passwd")
        browser.allow_local = True
        browser.blocked_origin = "http://127.0.0.1:8765"
        assert not await browser._allowed("http://127.0.0.1:8765/api/state")
        assert await browser._allowed("http://127.0.0.1:1234/")
    asyncio.run(run())


def test_real_dom_navigation_and_form_consent(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True)
        try:
            await browser.start()
            await browser.page.set_content('''
              <button aria-haspopup="menu" onclick="document.querySelector('#menu').hidden=false">Open profile menu</button>
              <div id="menu" role="menu" hidden><button role="menuitem">Settings</button></div>
              <button role="tab" aria-controls="panel">Personalization</button><div id="panel" role="tabpanel">Read settings</div>
              <form><button aria-haspopup="menu">Submit menu</button></form>
              <button aria-haspopup="menu">Save settings</button>
              <details><summary>Details</summary>Visible evidence</details>
            ''')
            snapshot = await browser.observe()
            targets = {e["label"]: e for e in snapshot["elements"]}
            for label in ["Open profile menu", "Personalization", "Details"]:
                assert not needs_approval(Action("click", targets[label]["id"]), targets[label])
            assert targets["Submit menu"]["in_form"]
            assert needs_approval(Action("click", targets["Save settings"]["id"]), targets["Save settings"])
            await browser.execute(Action("click", targets["Open profile menu"]["id"]))
            refreshed = await browser.observe()
            settings = next(e for e in refreshed["elements"] if e["label"] == "Settings")
            assert settings["role"] == "menuitem" and not needs_approval(Action("click", settings["id"]), settings)
        finally:
            await browser.close()
    asyncio.run(run())


def test_cleanup_stops_driver_after_browser_exits_on_sigint(tmp_path):
    stopped = []
    class ClosedContext:
        async def close(self):
            raise Exception("BrowserContext.close: Connection closed while reading from the driver")
    class Driver:
        async def stop(self):
            stopped.append(True)
    async def run():
        browser = PersonalBrowser(tmp_path)
        browser.context, browser.playwright = ClosedContext(), Driver()
        await browser.close()
        assert stopped == [True]
        assert browser.context is None and browser.playwright is None
    asyncio.run(run())
