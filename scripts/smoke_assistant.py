"""Opt-in live CLI + Chromium smoke test against a synthetic local shop only.

    python scripts/smoke_assistant.py --provider codex
No real merchant, credentials, booking or payment is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_GET(self):
        body = '''<html lang="en"><title>Local test shop</title><h1>Demo stationery shop</h1>
          <p>Blue notebook: 5 USD. Red notebook: 8 USD.</p>
          <button onclick="document.querySelector('#confirmation').textContent='TEST-RESERVATION-42 confirmed'">
            Reserve Blue notebook for 5 USD (test only)
          </button><p id="confirmation">No reservation yet.</p></html>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

def write_record(path, provider, state, error=None):
    if path is None:
        return
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False
    ).stdout.strip() or "unknown"
    record = {
        "kind": "parallax-assistant-synthetic-smoke", "recorded_at": time.time(),
        "revision": revision,
        "command": f".venv/bin/python scripts/smoke_assistant.py --provider {provider}",
        "provider": provider, "status": state.get("status") if state else "not_started",
        "step": state.get("step") if state else 0,
        "completion": state.get("completion") if state else None,
        "confirmation_expected": "TEST-RESERVATION-42",
        "error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


async def exercise(root, provider, url, record=None):
    browser = PersonalBrowser(root / "browser", headless=True, allow_local=True)
    assistant = Assistant(browser, root / "reports", max_steps=12, task_timeout=600)
    task = ("This tab is a local synthetic test shop with no real payment. "
            "Approve and click the visible test-only reservation for the cheaper Blue notebook. "
            "Report TEST-RESERVATION-42 only after its confirmation text is visible. "
            "Do not use any other website. Reply in Arabic.")
    state = None
    error = None
    try:
        await browser.start()
        await browser.page.goto(url)
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
                print(f"approved fixture action: {action['kind']}", flush=True)
                await assistant.control("approve", pending["token"])
            await asyncio.sleep(0.25)
        state = assistant.snapshot()
        assert state["status"] == "verified", state["result"]
        assert "TEST-RESERVATION-42" in state["result"], state["result"]
        assert "TEST-RESERVATION-42 confirmed" in (await browser.observe())["text"]
        print(json.dumps({"provider": provider, "status": "passed", "steps": state["step"],
                          "approved_actions": len(handled), "confirmation": "TEST-RESERVATION-42"}))
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
        raise
    finally:
        write_record(record, provider, state or assistant.snapshot(), error)
        await assistant.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["codex", "antigravity"], default="codex")
    parser.add_argument("--record", type=Path, help="Write a non-secret JSON record of this synthetic run")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="parallax-smoke-") as directory:
            asyncio.run(exercise(Path(directory), args.provider, f"http://127.0.0.1:{server.server_port}/", args.record))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
