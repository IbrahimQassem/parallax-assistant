"""Loopback-only control UI, separated from Parallax's public sweep service."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .browser import PersonalBrowser
from .runtime import Assistant


STATIC = Path(__file__).with_name("static")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, assistant, loop, provider="codex"):
        self.assistant = assistant
        self.loop = loop
        self.provider = provider
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        assistant.browser.blocked_origin = self.origin

    def dispatch(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=15)


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, *_args):
        pass  # Request paths and task contents do not belong in terminal logs.

    def respond(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        for name, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        }.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, *, api=False, write=False):
        host = urlsplit(self.server.origin).netloc
        if self.headers.get("Host") != host:
            self.respond(403, {"error": "Host غير مسموح."})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "طلب من موقع آخر مرفوض."})
            return False
        if write and self.headers.get("Origin") != self.server.origin:
            self.respond(403, {"error": "Origin غير مسموح."})
            return False
        if api and not hmac.compare_digest(self.headers.get("X-Parallax-Token", "").encode(), self.server.token.encode()):
            self.respond(403, {"error": "أعد فتح واجهة المساعد المحلية."})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.allowed(api=path.startswith("/api/")):
            return
        if path == "/":
            html = (STATIC / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", self.server.token).replace("__PROVIDER__", self.server.provider)
            mode = "Chrome المفتوح • يستخدم جلسة تسجيل الدخول في تبويب مخصص للمساعد." if getattr(self.server.assistant.browser, "existing_chrome", False) else "جلسة مستقلة عن متصفحك المعتاد. تسجيل الدخول محفوظ على جهازك."
            html = html.replace("__BROWSER_MODE__", mode)
            self.respond(200, html.encode(), "text/html; charset=utf-8")
        elif path in {"/app.js", "/style.css"}:
            mime = "text/javascript" if path.endswith("js") else "text/css"
            self.respond(200, (STATIC / path[1:]).read_bytes(), mime + "; charset=utf-8")
        elif path == "/api/state":
            self.respond(200, self.server.assistant.snapshot())
        else:
            self.respond(404, {"error": "غير موجود."})

    def do_POST(self):
        if not self.allowed(api=True, write=True):
            return
        try:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("يلزم JSON.")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 64000:
                raise ValueError("حجم الطلب غير مسموح.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("طلب غير صالح.")
            if self.path == "/api/task":
                result = self.server.dispatch(self.server.assistant.start(
                    data.get("task"), data.get("provider", self.server.provider), data.get("parent_id"),
                    data.get("consent_mode", "browse")))
            elif self.path == "/api/control":
                result = self.server.dispatch(self.server.assistant.control(
                    data.get("command"), data.get("token", ""), data.get("note", "")))
            else:
                self.respond(404, {"error": "غير موجود."})
                return
            self.respond(200, result)
        except (ValueError, TypeError):
            self.respond(400, {"error": "طلب غير صالح أو انتهت صلاحية الخطوة. حدّث الحالة وحاول مجددًا."})
        except TimeoutError:
            self.respond(503, {"error": "الخادم مشغول. تحقق من الحالة قبل إعادة الطلب."})
        except Exception:
            self.respond(500, {"error": "تعذّر تنفيذ الطلب."})


def main(argv=None):
    parser = argparse.ArgumentParser(description="مساعد Parallax الشخصي بمتصفح محلي")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", choices=["codex", "antigravity"], default="codex")
    parser.add_argument("--data-dir", type=Path, default=Path(".parallax-assistant"))
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--headless", action="store_true", help="For automated tests; manual takeover requires a visible browser")
    parser.add_argument("--allow-local", action="store_true", help="Allow local test websites; control UI remains blocked")
    parser.add_argument("--existing-chrome", action="store_true", help="Use the signed-in Chrome 144+ session; enable chrome://inspect/#remote-debugging first")
    parser.add_argument("--chrome-data-dir", type=Path, help="Chrome data directory for discovering DevToolsActivePort; requires --existing-chrome")
    args = parser.parse_args(argv)
    if args.existing_chrome and args.headless:
        parser.error("--existing-chrome cannot be combined with --headless")
    if args.chrome_data_dir and not args.existing_chrome:
        parser.error("--chrome-data-dir requires --existing-chrome")
    if not 1 <= args.port <= 65535 or not 1 <= args.max_steps <= 100:
        parser.error("port must be 1–65535 and max-steps 1–100")
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    browser = PersonalBrowser(data / "browser", headless=args.headless, allow_local=args.allow_local,
                              existing_chrome=args.existing_chrome, chrome_data_dir=args.chrome_data_dir)
    assistant = Assistant(browser, data / "reports", max_steps=args.max_steps)
    server = LocalServer(("127.0.0.1", args.port), assistant, loop, args.provider)
    thread.start()
    print(f"Parallax Assistant: {server.origin}/", flush=True)
    print("متصفح شخصي محلي • Ctrl+C لإيقاف الخدمة", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.dispatch(assistant.close())
        finally:
            server.server_close()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
