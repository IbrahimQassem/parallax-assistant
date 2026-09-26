"""User-selected bytes, exact approval binding, and a real local upload endpoint."""
import asyncio
from email.parser import BytesParser
from email.policy import default
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

import pytest

from parallax.assistant.actions import Action, needs_approval
from parallax.assistant.artifacts import upload_mime
from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant


CONTENT = b"SYNTHETIC-PRIVATE-FILE-CONTENT"


async def pending(app, kind):
    for _ in range(500):
        value = app.snapshot()["pending"]
        if value and value["type"] == kind: return value
        assert not app.job.done(), app.snapshot()
        await asyncio.sleep(0.01)
    raise AssertionError("Missing decision")


@pytest.mark.parametrize("inherited,mutation", [
    *[(False, value) for value in [None, "delete", "bytes", "metadata", "destination", "foreign", "wrong_type", "casefold_name", "casefold_digest"]],
    *[(True, value) for value in [None, "delete", "bytes", "metadata", "foreign"]],
])
def test_selected_file_is_bound_to_approval_and_verified_by_server(tmp_path, inherited, mutation):
    foreign_identifier = None
    class Site(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser(policy=default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw)
            part = next(part for part in message.walk() if part.get_filename())
            self.server.received.append((part.get_filename(), part.get_payload(decode=True)))
            self.send_response(303)
            self.send_header("Location", self.server.receipt_path)
            self.end_headers()
        def do_GET(self):
            record = ""
            if self.server.received:
                name, body = self.server.received[-1]
                record = (f"<dl><dt>Account</dt><dd>Demo</dd><dt>File</dt><dd>{html.escape(name)}</dd>"
                    f"<dt>SHA256</dt><dd>{hashlib.sha256(body).hexdigest()}</dd><dt>Status</dt><dd>Received</dd></dl>")
            body = ("<h1>Documents</h1>" + record + '<form method="post" enctype="multipart/form-data" action="/upload">'
                    '<label>Document<input type="file" name="document" accept=".txt" onchange="this.form.requestSubmit()"></label></form>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

    class Planner:
        def __init__(self, *_args): self.calls = 1 if inherited else 0
        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1: return Action("handoff", reason="Choose the local file for this task")
            if self.calls == 2:
                artifact = snapshot["assistant_context"]["artifacts"][0]
                assert artifact["source_kind"] == ("inherited" if inherited else "user_selected")
                return Action("expect", "upload_result", json.dumps({"description": "Receive the selected document", "url": snapshot["url"],
                    "subject": {"Account": "Demo"}, "outcome": {"Filename": artifact["name"].upper() if mutation == "casefold_name" else artifact["name"],
                        "SHA256": artifact["sha256"].upper() if mutation == "casefold_digest" else artifact["sha256"], "Status": "received"},
                    "label_aliases": {"Filename": ["File", "File name"]}, "url_scope": "origin",
                    "casefold_outcome": ["Status", *(["Filename"] if mutation == "casefold_name" else ["SHA256"] if mutation == "casefold_digest" else [])]}))
            if self.calls == 3:
                target = next(e for e in snapshot["elements"] if e["type"] == "file")
                return Action("upload", target["id"], foreign_identifier if mutation == "foreign" else snapshot["assistant_context"]["artifacts"][0]["id"])
            source_id = next(s["id"] for s in snapshot["observed_sources"] if s["url"] == snapshot["url"])
            return Action("finish", value=json.dumps({"summary": "Document result", "findings": [],
                "completion": {"status": "verified", "done": ["document"], "remaining": [], "reason": "Observed the site",
                    "evidence": [{"source_id": source_id, "claim": "documents page", "quote": "Documents"}]}}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.received = []
    server.receipt_path = "/receipt/" + uuid.uuid4().hex
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def run():
        nonlocal foreign_identifier
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        app = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
        try:
            await browser.start()
            await browser.page.goto(f"http://127.0.0.1:{server.server_port}/")
            if inherited:
                class Finish:
                    def __init__(self, *_args): pass
                    async def propose(self, *_args): return Action("finish", value="Prepared document")
                app.planner_factory = Finish
                await app.start("Prepare my document", "codex")
                await app.job
                owner = app.snapshot()["id"]
                with app.artifacts.staging(owner) as stage:
                    stage.write_bytes(CONTENT)
                    original = app.artifacts.commit(owner, stage, "document.txt", source_kind="user_selected")
                foreign_identifier = original["id"]
                app.planner_factory = Planner
                await app.start("Upload that document", "codex", owner)
                reference, = app.snapshot()["artifacts"]
                assert reference["id"] != original["id"]
                assert not list(app.artifacts.directory(reference["task_id"]).glob("*.data"))
            else:
                await app.start("Upload my document", "codex")
                handoff = await pending(app, "handoff")
                await app.attach_file(app.snapshot()["id"], "document.pdf" if mutation == "wrong_type" else "document.txt", CONTENT)
                assert app.snapshot()["pending"]["token"] == handoff["token"]
                assert not server.received
                await app.control("resume", handoff["token"])
            if mutation == "foreign" and not inherited:
                other_task = uuid.uuid4().hex
                with app.artifacts.staging(other_task) as stage:
                    stage.write_bytes(b"other task")
                    foreign_identifier = app.artifacts.commit(other_task, stage, "other.txt")["id"]
            if mutation in {"foreign", "wrong_type", "casefold_name", "casefold_digest"}:
                await asyncio.wait_for(app.job, 5)
                assert app.snapshot()["status"] == "unverified"
                assert app.snapshot()["approval_requests"] == 0
                assert not server.received
                return
            approval = await pending(app, "approval")
            assert approval["upload"]["sha256"] == hashlib.sha256(CONTENT).hexdigest()
            assert approval["effect_check"]["label_aliases"] == {"Filename": ["File", "File name"]}
            assert approval["effect_check"]["url_scope"] == "origin"
            assert approval["effect_check"]["casefold_outcome"] == ["Status"]
            assert not server.received
            task_id, identifier = app.snapshot()["id"], approval["upload"]["id"]
            if inherited:
                # Change the original after review, not the child's reference.
                task_id, identifier = owner, original["id"]
            folder = app.artifacts.directory(task_id)
            if mutation == "delete": app.artifacts.delete(task_id, identifier)
            if mutation in {"bytes", "metadata"}:
                changed = b"X" * len(CONTENT)
                (folder / (identifier + ".data")).write_bytes(changed)
                if mutation == "metadata":
                    path = folder / (identifier + ".json")
                    metadata = json.loads(path.read_text())
                    metadata["sha256"] = hashlib.sha256(changed).hexdigest()
                    path.write_text(json.dumps(metadata))
            if mutation == "destination":
                await browser.page.locator("form").evaluate("el => el.action='/other'")
            await app.control("approve", approval["token"])
            await asyncio.wait_for(app.job, 5)
            state = app.snapshot()
            assert state["status"] == ("unverified" if mutation else "verified"), state
            assert server.received == ([] if mutation else [("document.txt", CONTENT)])
            assert state["approval_requests"] == 1
            assert CONTENT.decode() not in next((tmp_path / "reports").glob("*.json")).read_text()
            assert len(state["operations"]) == (0 if mutation else 1)
            assert state["completion"]["upload_checks"][0]["status"] == ("not_sent" if mutation else "selected")
            if not mutation:
                source_id = state["completion"]["effect_checks"][0]["source_id"]
                assert next(s for s in state["sources"] if s["id"] == source_id)["url"].endswith(server.receipt_path)
        finally: await app.close()
    try: asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_upload_grammar_types_and_sensitive_fields():
    identifier = uuid.uuid4().hex
    assert needs_approval(Action.parse(json.dumps(Action("upload", "0", identifier).to_dict())))
    with pytest.raises(ValueError): Action.parse(json.dumps(Action("upload", "0", "/private/file.txt").to_dict()))
    assert upload_mime("document.txt", ".txt,text/plain") == "text/plain"
    assert upload_mime("photo.png", "image/*") == "image/png"
    with pytest.raises(ValueError): upload_mime("program.exe", ".pdf,image/*")
    for kind in ["password", "text", "hidden"]:
        with pytest.raises(ValueError):
            PersonalBrowser.preview(None, Action("upload", "0", identifier), {"elements": [
                {"id": "0", "tag": "input", "type": kind, "disabled": False, "sensitive": True}], "url": "https://example.com/", "fingerprint": "x"})


def test_observation_detects_file_selection_without_disclosing_names_or_bytes(tmp_path):
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True)
        try:
            await browser.start()
            await browser.page.set_content('<label>Document<input type="file" accept=".txt"></label>')
            before = await browser.observe()
            await browser.page.locator("input").set_input_files({"name": "PRIVATE-NAME.txt", "mimeType": "text/plain", "buffer": CONTENT})
            after = await browser.observe()
            assert before["fingerprint"] != after["fingerprint"]
            assert after["elements"][0]["selected_files"] == 1
            assert after["elements"][0]["file_state_hash"] != before["elements"][0]["file_state_hash"]
            assert "PRIVATE-NAME" not in json.dumps(after)
            assert CONTENT.decode() not in json.dumps(after)
        finally: await browser.close()
    asyncio.run(run())
