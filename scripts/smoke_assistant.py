"""Opt-in live CLI + Chromium smoke test against a synthetic local shop only.

    python scripts/smoke_assistant.py --provider codex
No real merchant, credentials, booking or payment is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_GET(self):
        if self.path == "/confirm":
            body = '<html><h1>TEST-RESERVATION-42 confirmed</h1><p>Blue notebook reserved for Demo User. Total 5 USD. No real payment.</p></html>'
        else:
            body = '''<html lang="en"><title>Local test shop</title><h1>Demo stationery shop</h1>
              <p>Blue notebook: 5 USD. Red notebook: 8 USD.</p>
              <form action="/confirm" method="post"><label>Name <input name="name" required></label>
              <button>Reserve Blue notebook for 5 USD (test only)</button></form></html>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(303)
        self.send_header("Location", "/confirm")
        self.end_headers()


async def exercise(root, provider, url):
    browser = PersonalBrowser(root / "browser", headless=True, allow_local=True)
    assistant = Assistant(browser, root / "reports", max_steps=12, task_timeout=600)
    task = (f"Open {url} (local synthetic test shop). Compare the two notebook prices. "
            "Reserve the cheaper Blue notebook for the synthetic customer Demo User using the form. "
            "This is a local test with no real purchase. Report the exact confirmation ID after you see it. "
            "Do not use any other website. Reply in Arabic.")
    try:
        await assistant.start(task, provider)
        handled = set()
        while not assistant.job.done():
            state = assistant.snapshot()
            pending = state["pending"]
            if pending and pending["token"] not in handled:
                handled.add(pending["token"])
                if pending["type"] != "approval" or urlsplit(pending["page"]).netloc != urlsplit(url).netloc:
                    raise AssertionError("Unexpected handoff or non-fixture destination")
                action = pending["action"]
                if action["kind"] == "fill" and action["value"] != "Demo User":
                    raise AssertionError("Unexpected form value")
                print(f"approved fixture action: {action['kind']}", flush=True)
                await assistant.control("approve", pending["token"])
            await asyncio.sleep(0.25)
        state = assistant.snapshot()
        assert state["status"] == "completed", state["result"]
        assert "TEST-RESERVATION-42" in state["result"], state["result"]
        assert browser.page.url.endswith("/confirm"), browser.page.url
        print(json.dumps({"provider": provider, "status": "passed", "steps": state["step"],
                          "approved_actions": len(handled), "confirmation": "TEST-RESERVATION-42"}))
    finally:
        await assistant.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["codex", "antigravity"], default="codex")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="parallax-smoke-") as directory:
            asyncio.run(exercise(Path(directory), args.provider, f"http://127.0.0.1:{server.server_port}/"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
