from __future__ import annotations

import asyncio
import http.client
import json
import threading

from parallax.assistant.runtime import Assistant
from parallax.assistant.web import LocalServer


def test_local_api_rejects_foreign_hosts_origins_and_missing_token(tmp_path):
    class Browser:
        blocked_origin = ""
        async def close(self): pass
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    def request(method, path, headers=None, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        status, body = response.status, response.read()
        connection.close()
        return status, body
    try:
        assert request("GET", "/api/state")[0] == 403
        assert request("GET", "/", {"Host": "evil.example"})[0] == 403
        assert request("GET", "/", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request("GET", "/")[0] == 200
        headers = {"X-Parallax-Token": server.token, "Content-Type": "application/json"}
        assert request("GET", "/api/state", headers)[0] == 200
        assert request("POST", "/api/task", headers, '{}')[0] == 403
        headers["Origin"] = "https://evil.example"
        assert request("POST", "/api/task", headers, '{}')[0] == 403
        headers["Origin"] = server.origin
        assert request("POST", "/api/task", headers, '{bad')[0] == 400
        assert request("POST", "/api/task", headers, json.dumps({"task": "", "provider": "codex"}))[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()
