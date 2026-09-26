"""Real Chromium tests, no model calls and no writes to external websites."""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from parallax.assistant.actions import Action, needs_approval
from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant


def test_form_named_controls_cannot_hide_destination_or_expose_private_query(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True)
        try:
            await browser.start()
            await browser.page.set_content('''<form id="profile" name="preferences" method="post" action="https://example.com/save?token=SYNTHETIC-PRIVATE">
              <input type="hidden" name="action" value="PRIVATE-ACTION"><input type="hidden" name="method" value="PRIVATE-METHOD">
              <input type="hidden" name="name" value="PRIVATE-NAME"><label>Name<input name="display_name"></label><button>Save</button></form>''')
            first = await browser.observe()
            field = next(item for item in first["elements"] if item["label"] == "Name")
            assert field["form"]["action"] == "https://example.com/save"
            assert field["form"]["method"] == "post"
            assert field["form"]["name"] == "preferences"
            assert "PRIVATE" not in json.dumps(first)
            await browser.page.locator("form").evaluate("form=>form.setAttribute('action','https://example.com/save?token=DIFFERENT-PRIVATE')")
            changed = await browser.observe()
            new_field = next(item for item in changed["elements"] if item["label"] == "Name")
            assert new_field["form"]["action"] == field["form"]["action"]
            assert new_field["form"]["action_hash"] != field["form"]["action_hash"]
            assert "PRIVATE" not in json.dumps(changed)
        finally:
            await browser.close()
    asyncio.run(run())


def test_records_preserve_visible_field_boundaries_without_editable_values(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True)
        try:
            await browser.start()
            await browser.page.set_content('''
                <table><tr><th>Item</th><th>Status</th><th>Actions</th></tr>
                  <tr><td>Blue</td><td>Confirmed</td><td><button>Delete</button></td></tr>
                  <tr><td>Draft</td><td><input value="SECRET"></td><td></td></tr>
                  <tr hidden><td>Hidden</td><td>Confirmed</td><td></td></tr></table>
                <dl><div><dt>الحساب</dt><dd>تجربة</dd></div><div><dt>الحالة</dt><dd>مؤكد</dd></div></dl>
                <table><tr><th>Duplicate</th><th>Duplicate</th></tr><tr><td>First</td><td>Confirmed</td></tr></table>
                <form><dl><dt>Item</dt><dd>Unsaved</dd><dt>Status</dt><dd>Confirmed</dd></dl></form>
                <div contenteditable="true"><dl><dt>Item</dt><dd>Editable</dd><dt>Status</dt><dd>Confirmed</dd></dl></div>
            ''')
            snapshot = await browser.observe()
            fields = [record["fields"] for record in snapshot["evidence_records"]]
            assert {"Item": "Blue", "Status": "Confirmed"} in fields
            assert {"الحساب": "تجربة", "الحالة": "مؤكد"} in fields
            assert not any(record.get("Status") == "Confirmed" and record.get("Item") != "Blue" for record in fields)
            assert not any("Duplicate" in record for record in fields)
            assert "SECRET" not in str(snapshot)
            assert all(record["main"] for record in snapshot["evidence_records"])
        finally:
            await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize("wrong", [None, "account", "status", "item", "duplicate"])
def test_generated_receipt_matches_the_requested_record_after_reload(tmp_path, wrong):
    class Service(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def do_POST(self):
            self.server.posts += 1
            record = {"Item": "Blue", "Account": "Demo", "Status": "Confirmed", "Receipt": uuid.uuid4().hex}
            if wrong in {"account", "status", "item"}:
                record[{"account": "Account", "status": "Status", "item": "Item"}[wrong]] = "Other"
            self.server.records.append(record)
            if wrong == "duplicate":
                self.server.records.append({**record, "Status": "Pending", "Receipt": uuid.uuid4().hex})
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self):
            self.server.reads += 1
            rows = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in record.values()) + "</tr>"
                           for record in self.server.records)
            body = ("<h1>Records</h1><form method='post'><button>Reserve Blue</button></form>"
                    "<table><tr><th>Item</th><th>Account</th><th>Status</th><th>Receipt</th></tr>" + rows + "</table>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    class Planner:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                return Action("expect", "reservation", json.dumps({"description": "Reserve Blue for Demo",
                    "url": snapshot["url"], "subject": {"Item": "Blue", "Account": "Demo"},
                    "outcome": {"Status": "Confirmed"}}))
            if self.calls == 2:
                button = next(item for item in snapshot["elements"] if item["label"] == "Reserve Blue")
                return Action("click", button["id"])
            if self.calls == 3:
                return Action("navigate", value=snapshot["url"])
            return Action("finish", value=json.dumps({"summary": "Reservation submitted", "scope": "local service",
                "work_done": "submitted and reloaded records", "findings": [], "limitations": [], "followups": [],
                "completion": {"status": "verified", "done": ["reservation"], "remaining": [], "reason": "records read",
                               "evidence": [{"source_id": "S1", "claim": "records were read", "quote": "Records"}]}}))

    async def run(url):
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        app = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
        try:
            await browser.start()
            await browser.page.goto(url)
            await app.start("Reserve Blue for Demo", "codex")
            for _ in range(300):
                pending = app.snapshot()["pending"]
                if pending and pending["type"] == "approval":
                    assert pending["effect_check"]["subject"] == {"Item": "Blue", "Account": "Demo"}
                    await app.control("approve", pending["token"])
                    break
                await asyncio.sleep(0.01)
            await asyncio.wait_for(app.job, 10)
            assert app.snapshot()["status"] == ("unverified" if wrong else "verified")
            assert server.posts == 1 and server.reads >= 3
            assert len(server.records[0]["Receipt"]) == 32
        finally:
            await app.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
    server.posts, server.reads, server.records = 0, 0, []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(run(f"http://127.0.0.1:{server.server_port}/"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


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


@pytest.mark.parametrize("result_text,expected_status", [
    ("TEST-ORDER-42 confirmed", "verified"),
    ("TEST-ORDER-43 confirmed", "unverified"),
    ("TEST-ORDER-42 not confirmed", "unverified"),
])
def test_real_chromium_confirmation_produces_verified_completion(tmp_path, result_text, expected_status):
    """Chromium fixture: the proof is page text after the approved test action."""
    class Shop(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass

        def do_GET(self):
            body = f"""<title>Fixture shop</title><h1>Blue notebook</h1>
              <button onclick=\"document.querySelector('#confirmation').textContent='{result_text}'\">Place test order</button>
              <p id=\"confirmation\">No order yet</p>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                return Action("expect", "order", json.dumps({"description": "Create TEST-ORDER-42",
                    "url": snapshot["url"], "subject": "TEST-ORDER-42", "outcome": "TEST-ORDER-42 confirmed"}))
            if self.calls == 2:
                button = next(item for item in snapshot["elements"] if item["label"] == "Place test order")
                return Action("click", button["id"], reason="run local fixture action")
            return Action("finish", value=json.dumps({
                "summary": "The local fixture confirms TEST-ORDER-42.",
                "scope": "local Chromium fixture", "work_done": "clicked the approved fixture button",
                "findings": [], "limitations": [], "followups": [],
                "completion": {
                    "status": "verified", "done": ["created the synthetic test order"],
                    "remaining": [], "reason": "confirmation text appeared after the action",
                    "evidence": [{"source_id": "S1", "claim": "fixture confirmation", "quote": result_text}],
                },
            }))

    async def run(url):
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        app = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
        try:
            await browser.start()
            await browser.page.goto(url)
            await app.start("place a synthetic order", "codex")
            for _ in range(200):
                pending = app.snapshot()["pending"]
                if pending and pending["type"] == "approval":
                    assert pending["effect_check"]["subject"] == "TEST-ORDER-42"
                    await app.control("approve", pending["token"])
                    break
                await asyncio.sleep(0.01)
            await app.job
            state = app.snapshot()
            assert state["status"] == expected_status
            assert state["completion"]["evidence"][0]["observed_after_action"]
            assert state["completion"]["effect_checks"][0]["status"] == ("matched" if expected_status == "verified" else "unverified")
        finally:
            await app.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(run(f"http://127.0.0.1:{server.server_port}/"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
