"""Opt-in live CLI + Chromium smoke test against a synthetic local shop only.

    python scripts/smoke_assistant.py --provider codex
No real merchant, credentials, booking or payment is involved.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import secrets
import subprocess
import tempfile
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


class OfferPage(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_GET(self):
        offer = self.server.offer
        # All values originate in this synthetic fixture, not a remote page.
        body = (f'<html lang="en"><title>{offer["name"]}</title><h1>{offer["name"]}</h1>'
                f'<p>Monthly price: {offer["monthly"]} USD.</p>'
                f'<p>Annual price: {offer["annual"]} USD.</p></html>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


class RecordService(BaseHTTPRequestHandler):
    """Native form + persisted server record with a previously unknown receipt."""
    def log_message(self, *_args): pass

    def do_POST(self):
        self.server.submissions += 1
        self.server.reservations.append({"Item": "Blue notebook", "Account": "Demo",
            "Price": "5 USD", "Status": "Confirmed", "Receipt": secrets.token_hex(8)})
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self):
        self.server.reads += 1
        rows = "".join("<tr>" + "".join(f"<td>{value}</td>" for value in row.values()) + "</tr>"
                       for row in self.server.reservations)
        body = ("<html lang='en'><title>Local reservation service</title><h1>Demo account</h1>"
                "<p>Blue notebook costs 5 USD. This service is a synthetic local test with no payment.</p>"
                "<p>Accepted reservations appear below with Status Confirmed. The server assigns a new receipt number.</p>"
                "<form method='post'><button>Reserve Blue notebook for Demo (test only)</button></form>"
                "<table><caption>Reservation records</caption><tr><th>Item</th><th>Account</th><th>Price</th>"
                "<th>Status</th><th>Receipt</th></tr>" + rows + "</table></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


class SettingsService(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_POST(self):
        values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        self.server.saved = {key: values[key][0] for key in ("display_name", "language")}
        self.server.submissions += 1
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self):
        self.server.reads += 1
        name = html.escape(self.server.saved["display_name"], quote=True)
        language = self.server.saved["language"]
        options = "".join(f'<option value="{value}" {"selected" if value==language else ""}>{label}</option>'
                          for value, label in [("en", "English"), ("ar", "Arabic")])
        body = ("<html lang='en'><title>Local profile preferences</title><h1>Profile preferences</h1>"
                "<dl><dt>Account</dt><dd>Demo</dd><dt>Profile</dt><dd>Personal</dd>"
                f"<dt>Name</dt><dd>{name}</dd><dt>Language</dt><dd>{'Arabic' if language=='ar' else 'English'}</dd></dl>"
                f'<form method="post"><label>Name<input name="display_name" value="{name}"></label>'
                f'<label>Language<select name="language">{options}</select></label><button>Save</button></form></html>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


class FileService(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_GET(self):
        if self.path == "/report":
            self.server.deliveries += 1
            body = self.server.payload
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="report.csv"')
        else:
            body = b'<title>Reports</title><h1>Reports</h1><a href="/report" download>Download report</a>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UploadService(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        message = BytesParser(policy=default).parsebytes(
            ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw)
        part = next(part for part in message.walk() if part.get_filename())
        self.server.received.append((part.get_filename(), part.get_payload(decode=True)))
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def do_GET(self):
        receipt = ""
        if self.server.received:
            name, content = self.server.received[-1]
            receipt = (f"<dl><dt>Account</dt><dd>Demo</dd><dt>File</dt><dd>{html.escape(name)}</dd>"
                f"<dt>SHA256</dt><dd>{hashlib.sha256(content).hexdigest()}</dd><dt>Status</dt><dd>Received</dd></dl>")
        body = ('<title>Documents</title><h1>Documents for Demo</h1>' + receipt +
            '<form method="post" enctype="multipart/form-data" action="/upload">'
            f'<label>Document<input type="file" name="document" accept="{getattr(self.server, "accept", ".txt")}" onchange="this.form.requestSubmit()"></label></form>').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


def source_version():
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], text=True, capture_output=True, check=False
    ).stdout.strip() or "unknown"
    root = Path(__file__).resolve().parents[1]
    files = [Path(__file__).resolve()] + sorted(
        p for p in (root / "src/parallax/assistant").rglob("*")
        if p.is_file() and p.suffix in {".py", ".js", ".html", ".css"}
    )
    digest = hashlib.sha256()
    for file in files:
        digest.update(str(file.relative_to(root)).encode() + b"\0" + file.read_bytes() + b"\0")
    return {"revision": revision, "assistant_source_sha256": digest.hexdigest()}


def write_record(path, provider, state, error=None, *, scenario="reservation", started_source=None, fixture_evidence=None):
    if path is None:
        return
    ended_source = source_version()
    record = {
        "kind": "parallax-assistant-synthetic-smoke", "recorded_at": time.time(),
        **(started_source or ended_source),
        "source_changed_during_run": started_source is not None and started_source != ended_source,
        "ended_source": ended_source,
        "command": f".venv/bin/python scripts/smoke_assistant.py --provider {provider} --scenario {scenario}",
        "scenario": scenario,
        "provider": provider, "status": state.get("status") if state else "not_started",
        "step": state.get("step") if state else 0,
        "plan_revision": state.get("plan_revision", 0) if state else 0,
        "completion": state.get("completion") if state else None,
        "confirmation_expected": "TEST-RESERVATION-42" if scenario == "reservation" else None,
        # This runner uses only synthetic fixture data, never a personal site.
        "pending_kind": (state.get("pending") or {}).get("type") if state else None,
        "handoff_reason": (state.get("pending") or {}).get("message") if state else None,
        "fixture_evidence": fixture_evidence,
        "error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


async def exercise(root, provider, url, record=None, *, offers=None, record_service=None, settings_service=None, file_service=None, upload_service=None, request_service=None):
    started_source = source_version()
    scenario = "request" if request_service is not None else "upload" if upload_service is not None else "download" if file_service is not None else "settings" if settings_service is not None else "record" if record_service is not None else "research" if offers else "reservation"
    browser = PersonalBrowser(root / "browser", headless=True, allow_local=True)
    assistant = Assistant(browser, root / "reports", max_steps=12, task_timeout=600)
    task = ("This tab is a local synthetic test shop with no real payment. "
            "Approve and click the visible test-only reservation for the cheaper Blue notebook. "
            "Report TEST-RESERVATION-42 only after its confirmation text is visible. "
            "Do not use any other website. Reply in Arabic.")
    if offers:
        task = ("Compare these two synthetic services using their monthly prices only. "
                "My monthly budget is 10 USD. Read both sites and recommend the cheaper eligible service. "
                "Do not sign up, submit forms, or use other sites. No account is needed. "
                "Start the report summary with CHOICE=<exact recommended service name>, then explain in Arabic. "
                "Support the comparison with exact monthly-price evidence from BOTH read sources. "
                f"The two sites are {offers[0]['url']} and {offers[1]['url']}.")
    if record_service is not None:
        task = ("Reserve one Blue notebook for the Demo account on this local synthetic service, for 5 USD. "
                "There is no real payment. After submitting, reload the records page and verify the selected item, "
                "account, price, and Confirmed status. The receipt number is generated by the service; do not guess it. "
                "Use only this site and reply in Arabic.")
    if settings_service is not None:
        task = (f"Update the Demo account's Personal profile display name to {settings_service.requested_name} "
                "and its language to Arabic, then save. Reload the page and verify both saved values for that profile. "
                "Use only this synthetic local site. Reply in Arabic.")
    if file_service is not None:
        task = "Download the report linked on this synthetic local page and make it available as a local file for this task. Reply in Arabic."
    if upload_service is not None:
        task = ("Upload the document I will select using the task's local file picker to this synthetic Documents page for Demo. "
                "Verify the received file name and SHA256 against my selected file. Reply in Arabic.")
    if request_service is not None:
        task = "غيّر لغة هذا الحساب إلى العربية"
    state = None
    error = None
    conditions_cover_request = False
    selected_file = False
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
                if request_service is not None:
                    raise AssertionError("The explicit language request must require zero additional decisions")
                if upload_service is not None and pending["type"] == "handoff" and not selected_file:
                    await assistant.attach_file(state["id"], upload_service.filename, upload_service.payload)
                    selected_file = True
                    await assistant.control("resume", pending["token"])
                    continue
                if offers:
                    raise AssertionError("The fully specified read-only research task required unnecessary intervention")
                if pending["type"] != "approval" or urlsplit(pending["page"]).netloc != urlsplit(url).netloc:
                    raise AssertionError("Unexpected handoff or non-fixture destination")
                if settings_service is not None:
                    assert pending.get("operation") and not handled - {pending["token"]}, "Expected one complete operation preview"
                if file_service is not None:
                    assert pending["action"]["kind"] == "download" and len(handled) == 1, "Expected one explicit file download"
                if upload_service is not None:
                    assert selected_file and pending["action"]["kind"] == "upload" and state["approval_requests"] == 1
                action = pending["action"]
                print(f"approved fixture action: {action['kind']}", flush=True)
                await assistant.control("approve", pending["token"])
            await asyncio.sleep(0.25)
        state = assistant.snapshot()
        assert state["status"] == "verified", state["result"]
        assert state.get("task_plan") and state.get("plan_revision", 0) >= 1, "The planner did not define the task"
        if offers:
            eligible = [offer for offer in offers if offer["monthly"] <= 10]
            winner = min(eligible, key=lambda offer: offer["monthly"])
            assert state["report"]["summary"].startswith(f"CHOICE={winner['name']}"), "Incorrect monthly recommendation"
            source_ids = {s["id"] for s in state["sources"] if s["url"] in {o["url"] for o in offers}}
            evidence_ids = {e["source_id"] for e in state["completion"]["evidence"]}
            assert len(source_ids) == 2 and source_ids <= evidence_ids, "Both offers need read evidence"
            assert state["approval_requests"] == 0
        elif request_service is not None:
            assert request_service.submissions == 1 and request_service.reads >= 2
            assert request_service.saved == {"display_name": "Before", "language": "ar"}
            assert state["approval_requests"] == 0 and not handled
            authorities = state.get("authority_receipts", [])
            assert len(authorities) == 1 and authorities[0]["basis"] == "user_explicit_request"
            assert authorities[0]["status"] == "consumed" and authorities[0]["executed"] == 2
            assert {row["authority_id"] for row in state["operations"]} == {authorities[0]["id"]}
            checks = state["completion"].get("effect_checks", [])
            assert checks and all(check["status"] == "matched" for check in checks)
            matched_ids = {check["id"] for check in checks}
            for condition in assistant.effect_checks.context()["conditions"]:
                if isinstance(condition["subject"], dict) and isinstance(condition["outcome"], dict):
                    conditions_cover_request |= (condition["id"] in matched_ids
                        and condition["subject"].get("Account") == "Demo" and condition["outcome"].get("Language") == "Arabic")
            assert conditions_cover_request, "Expected independent proof of the account and saved language"
        elif upload_service is not None:
            assert upload_service.received == [(upload_service.filename, upload_service.payload)]
            assert state["approval_requests"] == 1 and len(handled) == 2
            assert state["completion"]["upload_checks"][0]["status"] == "selected"
            checks = state["completion"].get("effect_checks", [])
            assert checks and all(check["status"] == "matched" for check in checks)
            for condition in assistant.effect_checks.context()["conditions"]:
                if isinstance(condition["subject"], dict) and isinstance(condition["outcome"], dict):
                    fields = condition["subject"] | condition["outcome"]
                    fields = {name: value for label, value in fields.items()
                              for name in [label, *condition.get("label_aliases", {}).get(label, [])]}
                    folded = {name for label in condition.get("casefold_outcome", [])
                              for name in [label, *condition.get("label_aliases", {}).get(label, [])]}
                    identity = {"Account": "Demo", "File": upload_service.filename,
                                "SHA256": hashlib.sha256(upload_service.payload).hexdigest()}
                    receipt_state = fields.get("Status", "")
                    conditions_cover_request |= (
                        not folded.intersection(identity)
                        and all(fields.get(key) == value for key, value in identity.items())
                        and (receipt_state == "Received" or ("Status" in folded and receipt_state.casefold() == "received")))
            assert conditions_cover_request, "Expected file identity, digest, account and receipt status"
        elif file_service is not None:
            assert file_service.deliveries == state["approval_requests"] == 1
            assert len(state["artifacts"]) == 1
            artifact = state["artifacts"][0]
            metadata, content = assistant.artifacts.read(state["id"], artifact["id"])
            assert content == file_service.payload
            assert state["completion"]["file_checks"] == [{"id": artifact["id"], "status": "matched"}]
            conditions_cover_request = True
        elif settings_service is not None:
            assert settings_service.submissions == 1 and settings_service.reads >= 3
            assert settings_service.saved == {"display_name": settings_service.requested_name, "language": "ar"}
            authorities = state.get("authority_receipts", [])
            assert len(authorities) == 1 and authorities[0]["status"] == "consumed" and authorities[0]["executed"] == 3
            assert state["approval_requests"] == 1
            assert {row["authority_id"] for row in state["operations"]} == {authorities[0]["id"]}
            checks = state["completion"].get("effect_checks", [])
            assert checks and all(check["status"] == "matched" for check in checks)
            matched_ids = {check["id"] for check in checks}
            for condition in assistant.effect_checks.context()["conditions"]:
                conditions_cover_request |= (condition["id"] in matched_ids
                    and condition["subject"] == {"Account": "Demo", "Profile": "Personal"}
                    and condition["outcome"] == {"Name": settings_service.requested_name, "Language": "Arabic"})
            assert conditions_cover_request, "Expected verification of identity and both saved settings"
        elif record_service is not None:
            assert record_service.submissions == 1, "Expected exactly one synthetic submission"
            assert record_service.reads >= 3, "Expected a fresh read after the submission redirect"
            assert len(record_service.reservations) == 1
            receipt = record_service.reservations[0]
            assert {key: receipt[key] for key in ("Item", "Account", "Price", "Status")} == {
                "Item": "Blue notebook", "Account": "Demo", "Price": "5 USD", "Status": "Confirmed"}
            checks = state["completion"].get("effect_checks", [])
            assert checks and all(check["status"] == "matched" for check in checks), "Missing record-bound effect check"
            matched_ids = {check["id"] for check in checks if check["status"] == "matched"}
            for condition in assistant.effect_checks.context()["conditions"]:
                subject, outcome = condition["subject"], condition["outcome"]
                if condition["id"] not in matched_ids or not isinstance(subject, dict) or not isinstance(outcome, dict):
                    continue
                conditions_cover_request |= (
                    subject.get("Item") == "Blue notebook" and subject.get("Account") == "Demo"
                    and outcome.get("Status") == "Confirmed" and (subject | outcome).get("Price") == "5 USD")
            assert conditions_cover_request, "Predeclared condition did not cover requested identity, price and state"
        else:
            assert "TEST-RESERVATION-42" in state["result"], state["result"]
            assert "TEST-RESERVATION-42 confirmed" in (await browser.observe())["text"]
            checks = state["completion"].get("effect_checks", [])
            assert checks and all(check["status"] == "matched" for check in checks), "Missing object-bound effect check"
        print(json.dumps({"provider": provider, "status": "passed", "steps": state["step"],
                          "approved_actions": state["approval_requests"], "plan_revision": state["plan_revision"],
                          "scenario": scenario}))
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
        raise
    finally:
        write_record(record, provider, state or assistant.snapshot(), error,
                     scenario=scenario, started_source=started_source,
                     fixture_evidence={"submissions": record_service.submissions,
                                       "reads": record_service.reads,
                                       "conditions_cover_request": conditions_cover_request,
                                       "records": len(record_service.reservations)} if record_service is not None else
                         {"submissions": settings_service.submissions, "reads": settings_service.reads,
                          "conditions_cover_request": conditions_cover_request, "approvals": (state or {}).get("approval_requests"),
                          "authorities": (state or {}).get("authority_receipts", [])} if settings_service is not None else
                         {"deliveries": file_service.deliveries, "bytes_match": conditions_cover_request,
                          "artifacts": (state or {}).get("artifacts", [])} if file_service is not None else
                         {"uploads": len(upload_service.received), "bytes_match": upload_service.received == [(upload_service.filename, upload_service.payload)],
                          "conditions_cover_request": conditions_cover_request, "selected_file": selected_file,
                          "approvals": (state or {}).get("approval_requests")} if upload_service is not None else
                         {"submissions": request_service.submissions, "reads": request_service.reads,
                          "conditions_cover_request": conditions_cover_request, "approvals": (state or {}).get("approval_requests"),
                          "authorities": (state or {}).get("authority_receipts", [])} if request_service is not None else None)
        await assistant.close()


async def exercise_file_followup(root, provider, download, upload, record):
    """Two real planner tasks: download once, then upload the inherited version."""
    started_source = source_version()
    browser = PersonalBrowser(root / "browser", headless=True, allow_local=True)
    assistant = Assistant(browser, root / "reports", max_steps=12, task_timeout=600)
    download_url = f"http://127.0.0.1:{download.server_port}/"
    upload_url = f"http://127.0.0.1:{upload.server_port}/"
    states, error = [], None
    conditions_cover_request = False
    try:
        await browser.start()
        await browser.page.goto(download_url)
        for phase, task in enumerate([
            "نزّل التقرير من هذه الصفحة التجريبية المحلية وأتحه كملف لهذه المهمة.",
            f"ارفع التقرير الذي نزلته إلى صفحة المستندات التجريبية للحساب Demo في {upload_url} "
            "وتحقق من استلام الملف الصحيح بمطابقة اسمه وبصمته. استخدم الموقعين التجريبيين فقط.",
        ]):
            await assistant.start(task, provider, states[0]["id"] if phase else None)
            handled = set()
            while not assistant.job.done():
                state = assistant.snapshot()
                pending = state.get("pending")
                if pending and pending["token"] not in handled:
                    handled.add(pending["token"])
                    expected_kind, expected_url = ("upload", upload_url) if phase else ("download", download_url)
                    assert pending["type"] == "approval", "No picker or handoff is needed for an inherited file"
                    assert pending["action"]["kind"] == expected_kind and len(handled) == 1
                    assert urlsplit(pending["page"]).netloc == urlsplit(expected_url).netloc
                    if phase:
                        assert pending["upload"]["sha256"] == hashlib.sha256(download.payload).hexdigest()
                        assert pending["upload"]["name"] == "report.csv"
                    print(f"approved fixture action: {expected_kind}", flush=True)
                    await assistant.control("approve", pending["token"])
                await asyncio.sleep(0.25)
            state = assistant.snapshot()
            states.append(state)
            assert state["status"] == "verified", state["result"]
            assert state.get("task_plan") and state["plan_revision"] >= 1
            assert state["approval_requests"] == 1
            artifact, = state["artifacts"]
            assert assistant.artifacts.read(state["id"], artifact["id"])[1] == download.payload
            assert download.deliveries == 1
            if not phase:
                assert state["completion"]["file_checks"] == [{"id": artifact["id"], "status": "matched"}]
                assert not upload.received
                continue
            assert artifact["source_kind"] == "inherited" and artifact["task_id"] == state["id"]
            assert artifact["id"] != states[0]["artifacts"][0]["id"]
            assert not list(assistant.artifacts.directory(state["id"]).glob("*.data"))
            assert upload.received == [("report.csv", download.payload)]
            assert state["completion"]["upload_checks"][0]["status"] == "selected"
            matched = {check["id"] for check in state["completion"].get("effect_checks", []) if check["status"] == "matched"}
            for condition in assistant.effect_checks.context()["conditions"]:
                if condition["id"] not in matched or not all(isinstance(condition[key], dict) for key in ("subject", "outcome")):
                    continue
                fields = {name: value for label, value in (condition["subject"] | condition["outcome"]).items()
                          for name in [label, *condition.get("label_aliases", {}).get(label, [])]}
                folded = {name for label in condition.get("casefold_outcome", [])
                          for name in [label, *condition.get("label_aliases", {}).get(label, [])]}
                identity = {"Account": "Demo", "File": "report.csv", "SHA256": hashlib.sha256(download.payload).hexdigest()}
                status = fields.get("Status", "")
                conditions_cover_request |= (not folded.intersection(identity)
                    and all(fields.get(key) == value for key, value in identity.items())
                    and (status == "Received" or ("Status" in folded and status.casefold() == "received")))
            assert conditions_cover_request, "Receipt must verify the account, file version and received status"
        print(json.dumps({"provider": provider, "scenario": "file_followup", "status": "passed",
                          "steps": [state["step"] for state in states], "approvals": [state["approval_requests"] for state in states]}), flush=True)
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
        raise
    finally:
        write_record(record, provider, assistant.snapshot(), error, scenario="file_followup", started_source=started_source,
                     fixture_evidence={"deliveries": download.deliveries, "uploads": len(upload.received),
                         "bytes_match": upload.received == [("report.csv", download.payload)],
                         "conditions_cover_request": conditions_cover_request,
                         "tasks": [{key: state.get(key) for key in ("id", "parent_id", "status", "step", "approval_requests", "artifacts")} for state in states]})
        await assistant.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["codex", "antigravity"], default="codex")
    parser.add_argument("--scenario", choices=["reservation", "research", "record", "settings", "download", "upload", "request", "file_followup"], default="reservation")
    parser.add_argument("--record", type=Path, help="Write a non-secret JSON record of this synthetic run")
    args = parser.parse_args()
    servers, workers = [], []
    try:
        if args.scenario == "file_followup":
            for handler in (FileService, UploadService):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                server.deliveries, server.received, server.accept = 0, [], ".csv"
                server.payload = f"item,value\nReport-{secrets.token_hex(4)},42\n".encode()
                servers.append(server)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                workers.append(worker)
            with tempfile.TemporaryDirectory(prefix="parallax-followup-smoke-") as directory:
                asyncio.run(exercise_file_followup(Path(directory), args.provider, *servers, args.record))
            return
        offers = None
        if args.scenario == "research":
            offers = [
                {"name": "Cedar-" + secrets.token_hex(3), "monthly": 12 + secrets.randbelow(4), "annual": 40},
                {"name": "Maple-" + secrets.token_hex(3), "monthly": 5 + secrets.randbelow(4), "annual": 100},
            ]
        for offer in offers or [None]:
            handler = OfferPage if offer else {"record": RecordService, "settings": SettingsService, "request": SettingsService, "download": FileService, "upload": UploadService}.get(args.scenario, Shop)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            if args.scenario == "record":
                server.submissions, server.reservations, server.reads = 0, [], 0
            if args.scenario in {"settings", "request"}:
                server.submissions, server.reads = 0, 0
                server.saved = {"display_name": "Before", "language": "en"}
                server.requested_name = "Visitor-" + secrets.token_hex(3)
            if args.scenario == "download":
                server.deliveries = 0
                server.payload = f"item,value\nReport-{secrets.token_hex(4)},42\n".encode()
            if args.scenario == "upload":
                server.received = []
                server.filename = f"Document-{secrets.token_hex(3)}.txt"
                server.payload = f"SYNTHETIC-UPLOAD-{secrets.token_hex(8)}".encode()
            servers.append(server)
            url = f"http://127.0.0.1:{server.server_port}/"
            if offer:
                offer["url"] = url
                server.offer = offer
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            workers.append(worker)
        with tempfile.TemporaryDirectory(prefix="parallax-smoke-") as directory:
            asyncio.run(exercise(Path(directory), args.provider, offers[0]["url"] if offers else url,
                                 args.record, offers=offers, record_service=server if args.scenario == "record" else None,
                                 settings_service=server if args.scenario == "settings" else None,
                                 file_service=server if args.scenario == "download" else None,
                                 upload_service=server if args.scenario == "upload" else None,
                                 request_service=server if args.scenario == "request" else None))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    main()
