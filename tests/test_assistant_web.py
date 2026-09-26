from __future__ import annotations

import asyncio
import http.client
import json
import threading
import uuid

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
        status, body, response_headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, body, response_headers
    try:
        assert request("GET", "/api/state")[0] == 403
        assert request("GET", "/", {"Host": "evil.example"})[0] == 403
        assert request("GET", "/", {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert request("GET", "/")[0] == 200
        headers = {"X-Parallax-Token": server.token, "Content-Type": "application/json"}
        assert request("GET", "/api/state", headers)[0] == 200
        report_id = uuid.uuid4().hex
        (tmp_path / (report_id + ".json")).write_text(json.dumps({
            "id": report_id, "status": "failed", "result": "Previous failure", "task": "PRIVATE-REQUEST"}))
        before = app.snapshot()
        assert request("GET", "/api/history")[0] == 403
        assert request("GET", f"/api/history/{report_id}")[0] == 403
        assert request("GET", "/api/history", {**headers, "Host": "evil.example"})[0] == 403
        assert request("GET", "/api/history", {**headers, "Sec-Fetch-Site": "cross-site"})[0] == 403
        status, body, _ = request("GET", "/api/history", headers)
        assert status == 200 and json.loads(body)["items"][0]["status"] == "failed"
        status, body, _ = request("GET", f"/api/history/{report_id}", headers)
        assert status == 200 and json.loads(body)["result"] == "Previous failure"
        assert "PRIVATE" not in body.decode() and app.snapshot() == before
        app.artifacts.root.mkdir(exist_ok=True)
        invalid_folder = app.artifacts.root / report_id
        invalid_folder.symlink_to(tmp_path, target_is_directory=True)
        status, body, _ = request("GET", f"/api/history/{report_id}", headers)
        assert status == 200 and json.loads(body)["result"] == "Previous failure"
        assert json.loads(body)["artifacts"] == [] and json.loads(body)["artifacts_error"]
        invalid_folder.unlink()
        assert request("GET", "/api/history/../private", headers)[0] == 404
        assert request("GET", "/api/history?cursor=bad", headers)[0] == 400
        assert request("GET", "/api/history?cursor=&cursor=", headers)[0] == 400
        assert request("GET", "/api/history?unexpected=1", headers)[0] == 400
        assert request("POST", "/api/task", headers, '{}')[0] == 403
        headers["Origin"] = "https://evil.example"
        assert request("POST", "/api/task", headers, '{}')[0] == 403
        headers["Origin"] = server.origin
        for route in ["/api/checkpoints/save", "/api/checkpoints/resume", "/api/checkpoints/update"]:
            assert request("POST", route, {}, '{}')[0] == 403
            assert request("POST", route, {**headers, "Origin": "https://evil.example"}, '{}')[0] == 403
            assert request("POST", route, headers, json.dumps({"id": "../private"}))[0] == 400
        app.checkpoints.save(report_id, "original brief", report_id, "browse")
        checkpoint_update = {"id": report_id, "brief": "edited brief", "revision": 1}
        status, body, _ = request("POST", "/api/checkpoints/update", headers, json.dumps(checkpoint_update))
        assert status == 200 and json.loads(body)["revision"] == 2
        assert json.loads(body)["brief"] == "edited brief"
        assert request("POST", "/api/checkpoints/resume", headers, json.dumps({"id": report_id}))[0] == 400
        for route, data in [("/api/checkpoints/update", checkpoint_update),
                            ("/api/checkpoints/resume", {"id": report_id, "revision": 1})]:
            status, body, _ = request("POST", route, headers, json.dumps(data))
            assert status == 409 and json.loads(body)["code"] == "checkpoint_conflict"
        assert app.checkpoints.load(report_id)["status"] == "ready"
        policy = json.dumps({"id": report_id, "days": 7})
        assert request("POST", "/api/history/retention", {}, policy)[0] == 403
        assert request("POST", "/api/history/retention", {**headers, "Origin": "https://evil.example"}, policy)[0] == 403
        assert request("POST", "/api/history/retention", headers, json.dumps({"id": report_id}))[0] == 400
        assert request("POST", "/api/history/retention", headers, json.dumps({"id": report_id, "days": True}))[0] == 400
        status, body, _ = request("POST", "/api/history/retention", headers, policy)
        assert status == 200 and json.loads(body)["days"] == 7
        assert request("POST", "/api/history/retention", headers, json.dumps({"id": report_id, "days": None}))[0] == 200
        assert request("POST", "/api/task", headers, '{bad')[0] == 400
        assert request("POST", "/api/task", headers, json.dumps({"task": "", "provider": "codex"}))[0] == 400
        task_id = uuid.uuid4().hex
        with app.artifacts.staging(task_id) as stage:
            stage.write_bytes(b"local output")
            saved = app.artifacts.commit(task_id, stage, "تقرير.html")
        url = f"/api/files/{task_id}/{saved['id']}"
        assert request("GET", url)[0] == 403
        status, body, response_headers = request("GET", url, headers)
        assert status == 200 and body == b"local output"
        assert response_headers["Content-Type"] == "application/octet-stream"
        assert response_headers["Content-Disposition"].startswith("attachment;")
        assert response_headers["X-Content-Type-Options"] == "nosniff"
        assert request("GET", "/api/files/../../private", headers)[0] == 404
        bad_origin = {**headers, "Origin": "https://evil.example"}
        deletion = json.dumps({"task_id": task_id, "id": saved["id"]})
        assert request("POST", "/api/files/delete", bad_origin, deletion)[0] == 403
        assert request("POST", "/api/files/delete", headers, deletion)[0] == 200
        assert request("GET", url, headers)[0] == 404
        file_headers = {**headers, "Content-Type": "application/octet-stream",
                        "X-Parallax-Task": task_id, "X-Parallax-Filename": "sample.txt"}
        assert request("POST", "/api/files/add", {}, b"sample")[0] == 403
        assert request("POST", "/api/files/add", {**file_headers, "Origin": "https://evil.example"}, b"sample")[0] == 403
        assert request("POST", "/api/files/add", {**file_headers, "Content-Length": str(20*1024*1024 + 1)}, b"")[0] == 400
        assert request("POST", "/api/files/add", file_headers, b"sample")[0] == 400  # No active task.
        deletion = json.dumps({"id": report_id})
        assert request("POST", "/api/history/delete", {}, deletion)[0] == 403
        assert request("POST", "/api/history/delete", bad_origin, deletion)[0] == 403
        assert request("POST", "/api/history/delete", headers, json.dumps({"id": "../private"}))[0] == 400
        status, body, _ = request("POST", "/api/history/delete", headers, deletion)
        assert status == 200 and json.loads(body)["deleted"] and not json.loads(body)["cleanup_pending"]
        assert request("GET", f"/api/history/{report_id}", headers)[0] == 404
        assert request("POST", "/api/history/delete", headers, deletion)[0] == 200
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()
