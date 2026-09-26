"""Native form submission under one scoped approval, without a model or real account."""
import asyncio
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant


@pytest.mark.parametrize("mutation", [None, "account", "field", "destination"])
@pytest.mark.parametrize("from_request", [False, True])
def test_one_approval_fills_and_saves_real_form_and_rejects_unapproved_changes(tmp_path, mutation, from_request):
    class Profile(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            self.server.saved = {key: values[key][0] for key in ("name", "language")}
            self.server.posts += 1
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def do_GET(self):
            name = html.escape(self.server.saved["name"], quote=True)
            language = self.server.saved["language"]
            mutation_script = {
                None: "", "account": "document.querySelector('#account').textContent='Other';",
                "field": "document.querySelector('select').value='fr';",
                "destination": "document.querySelector('#save').setAttribute('formaction','/different');",
            }[mutation]
            options = "".join(f'<option value="{value}" {"selected" if value==language else ""}>{label}</option>'
                              for value, label in [("en", "English"), ("ar", "Arabic"), ("fr", "French")])
            body = f'''<title>Profile</title><h1>Profile</h1>
              <dl><dt>Account</dt><dd id="account">Demo</dd><dt>Profile</dt><dd>Personal</dd>
              <dt>Name</dt><dd>{name}</dd><dt>Language</dt><dd>{"Arabic" if language=="ar" else "English"}</dd></dl>
              <form method="post" id="original-form"><label>Name<input name="name" value="{name}" oninput="changed()"></label>
              <label>Language<select name="language">{options}</select></label><button id="save" disabled>Save</button></form>
              <script>function changed(){{
                document.querySelector('form').id='rerendered-form';
                if(!document.querySelector('#unrelated')){{const b=document.createElement('button');b.id='unrelated';b.textContent='Unrelated';document.body.prepend(b);}}
                document.querySelector('#save').disabled=false;{mutation_script}
              }}</script>'''
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    class Planner:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                return Action("expect", "profile", json.dumps({"description": "Update preferences", "url": snapshot["url"],
                    "subject": {"Account": "Demo", "Profile": "Personal"}, "outcome": {"Name": "Chosen name", "Language": "Arabic"}}))
            if self.calls == 2:
                targets = {element["label"]: element["id"] for element in snapshot["elements"]}
                return Action("operation", "profile", json.dumps({"description": "Update the displayed profile", "identity": {"Account": "Demo", "Profile": "Personal"},
                    "steps": [Action("fill", targets["Name"], "Chosen name").to_dict(),
                              Action("select", targets["Language"], "ar").to_dict(), Action("click", targets["Save"]).to_dict()]}))
            return Action("finish", value=json.dumps({"summary": "Profile result", "scope": "local profile", "work_done": "inspected the result", "findings": [],
                "limitations": [], "followups": [], "completion": {"status": "verified", "done": ["profile"], "remaining": [], "reason": "profile read",
                "evidence": [{"source_id": "S1", "claim": "profile inspected", "quote": "Profile"}]}}))

    async def run(url):
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        app = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
        try:
            await browser.start()
            await browser.page.goto(url)
            await app.start('Change Name to "Chosen name" and Language to Arabic for account Demo' if from_request else "Change my name and language", "codex")
            if not from_request:
                for _ in range(300):
                    pending = app.snapshot()["pending"]
                    if pending and pending["type"] == "approval":
                        assert len(pending["operation"]["changes"]) == 3
                        await app.control("approve", pending["token"])
                        break
                    if app.job.done(): raise AssertionError(app.snapshot())
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(app.job, 10)
            state = app.snapshot()
            assert state["approval_requests"] == (0 if from_request else 1)
            assert state["authority_receipts"][0]["basis"] == ("user_explicit_request" if from_request else "user_operation_preview")
            assert state["status"] == ("unverified" if mutation else "verified")
            assert server.posts == (0 if mutation else 1)
            assert state["authority_receipts"][0]["executed"] == (1 if mutation else 3)
            if not mutation: assert server.saved == {"name": "Chosen name", "language": "ar"}
        finally:
            await app.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Profile)
    server.posts, server.saved = 0, {"name": "Before", "language": "en"}
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(run(f"http://127.0.0.1:{server.server_port}/"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
