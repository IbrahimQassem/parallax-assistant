import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.artifacts import ArtifactStore
from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant
from parallax.assistant.file_transfer import retrieve_file


@pytest.fixture
def file_site():
    class Site(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            if self.path == "/same":
                self.send_response(302)
                self.send_header("Location", "/file")
                self.end_headers()
                return
            if self.path == "/slow":
                self.send_response(200)
                self.send_header("Content-Length", "32000")
                self.end_headers()
                try:
                    for _ in range(500):
                        self.wfile.write(b"x" * 64)
                        self.wfile.flush()
                        self.server.started.set()
                        time.sleep(0.01)
                except (BrokenPipeError, ConnectionResetError): pass
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/file")
                self.end_headers()
                return
            if self.path == "/auth" and self.headers.get("Cookie") != "session=synthetic-session":
                self.send_response(403)
                self.end_headers()
                return
            if self.path in {"/file", "/stream", "/auth"}:
                self.server.deliveries += 1
                body = b"requested report\n" * 10
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="../../report.txt"')
            else:
                body = b'<h1>Reports</h1><a href="/file" download>Download report</a><a href="/redirect">Redirected file</a><a href="/stream">Streamed report</a>'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            if self.path != "/stream": self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    server.deliveries = 0
    server.started = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield server, f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("scenario", ["public", "authenticated", "redirect"])
def test_download_is_approved_saved_verified_restored_and_never_executed(tmp_path, file_site, scenario):
    server, url = file_site
    class Planner:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                link = next(e for e in snapshot["elements"] if e["download"])
                return Action("download", link["id"], reason="Save the requested report")
            assert snapshot["assistant_context"]["artifacts"][0]["available"]
            return Action("finish", value=json.dumps({"summary": "Report downloaded", "findings": [],
                "completion": {"status": "verified", "done": ["downloaded report"], "remaining": [],
                    "reason": "Stored output and page read", "evidence": [{"source_id": "S1", "claim": "report link", "quote": "Reports"}]}}))

    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        app = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
        try:
            await browser.start()
            await browser.page.goto(url)
            if scenario == "authenticated":
                await browser.context.add_cookies([{"name": "session", "value": "synthetic-session", "url": url},
                    {"name": "unrelated", "value": "PRIVATE-OTHER-SITE", "url": "https://unrelated.example/"}])
                await browser.page.locator("a[download]").evaluate("el => el.href = '/auth'")
            if scenario == "redirect":
                await browser.page.locator("a[download]").evaluate("el => el.href = '/same'")
            await app.start("Download the report", "codex")
            while not app.snapshot()["pending"]:
                assert not app.job.done(), app.snapshot()
                await asyncio.sleep(0.01)
            decision = app.snapshot()["pending"]
            assert decision["download"]["url"] == url + {"public": "file", "authenticated": "auth", "redirect": "same"}[scenario]
            assert server.deliveries == 0
            await app.control("approve", decision["token"])
            await asyncio.wait_for(app.job, 40)
            state = app.snapshot()
            assert state["status"] == "verified", state
            assert server.deliveries == state["approval_requests"] == 1
            artifact = state["artifacts"][0]
            assert app.artifacts.read(state["id"], artifact["id"])[1] == b"requested report\n" * 10
            assert state["completion"]["file_checks"] == [{"id": artifact["id"], "status": "matched"}]
            restored = Assistant(browser, tmp_path / "reports", planner_factory=Planner)
            assert restored.snapshot()["artifacts"] == state["artifacts"]
            assert "synthetic-session" not in next((tmp_path / "reports").glob("*.json")).read_text()
        finally: await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["size", "stream_size", "changed", "redirect"])
def test_download_rejects_oversize_changed_link_and_cross_origin_redirect(tmp_path, file_site, case):
    server, url = file_site
    async def run():
        browser = PersonalBrowser(tmp_path / "profile", headless=True, allow_local=True)
        try:
            await browser.start()
            await browser.page.goto(url)
            snapshot = await browser.observe()
            label = {"redirect": "Redirected file", "stream_size": "Streamed report"}.get(case, "Download report")
            target = next(e for e in snapshot["elements"] if e["label"] == label)
            action = Action("download", target["id"])
            store = ArtifactStore(tmp_path / "files")
            with store.staging(uuid.uuid4().hex) as stage:
                expected_url = target["href"] + "/changed" if case == "changed" else target["href"]
                with pytest.raises((ValueError, TimeoutError)):
                    await asyncio.wait_for(browser.download_to(action, stage, 32, expected_url), 3)
            if case in {"changed", "redirect"}: assert server.deliveries == 0
            assert not list((tmp_path / "files").glob("*/.pending-*"))
        finally: await browser.close()
    asyncio.run(run())


def test_cancelling_stream_removes_partial_file_and_stops_worker(tmp_path, file_site):
    server, url = file_site
    async def run():
        async def allowed(_url): return True
        async def cookies(_urls): return []
        store = ArtifactStore(tmp_path / "files")
        task_id = uuid.uuid4().hex
        with store.staging(task_id) as stage:
            transfer = asyncio.create_task(retrieve_file(url + "slow", stage, 40000, allowed, cookies))
            assert await asyncio.to_thread(server.started.wait, 2)
            transfer.cancel()
            with pytest.raises(asyncio.CancelledError): await transfer
        assert store.list(task_id) == []
        assert not list(store.directory(task_id).iterdir())
    asyncio.run(run())
