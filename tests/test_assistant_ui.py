"""Exercise the real Arabic UI with a deterministic planner and no external site."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path

import pytest

from playwright.async_api import async_playwright, expect

from parallax.assistant.actions import Action
from parallax.assistant.attempts import AttemptJournal
from parallax.assistant.runtime import Assistant
from parallax.assistant.web import LocalServer


def test_history_review_export_and_original_file_deletion_leave_active_task_unchanged(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    original_id = None
    for index in range(11):
        identifier = uuid.uuid4().hex
        row = {"id": identifier, "status": "failed", "result": f"توقف قديم {index}"}
        if index == 0:
            original_id = identifier
            row.update(status="verified", result="OLD-RESULT", report={"summary": "ملخص قديم",
                       "findings": [{"title": "ملاحظة محفوظة", "detail": '<img src=x onerror="alert(1)">'}]},
                       completion={"status": "verified", "reason": "دليل محفوظ", "done": ["تقرير سابق"],
                                   "remaining": [], "evidence": []})
        path = reports / (identifier + ".json")
        path.write_text(json.dumps(row))
        os.utime(path, (1000 + index, 1000 + index))

    class Browser:
        context = None
        reads = 0
        async def start(self): self.context = True
        async def observe(self):
            self.reads += 1
            return {"url": "https://example.com/", "text": "Current task", "fingerprint": "same"}
        async def close(self): self.context = None
    class Planner:
        def __init__(self, *_args): pass
        async def propose(self, *_args): return Action("handoff", reason="تدخل للمهمة الحالية")

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), reports, planner_factory=Planner)
    with app.artifacts.staging(original_id) as stage:
        stage.write_bytes(b"OLD-FILE-BYTES")
        original = app.artifacts.commit(original_id, stage, "تقرير قديم.txt")
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill("ACTIVE-REQUEST")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                before = app.snapshot()
                reads = app.browser.reads
                app.artifacts.inherit(before["id"], original)
                await page.locator("#history > summary").click()
                await expect(page.locator("#history-list li")).to_have_count(10)
                await page.get_by_role("button", name="توقف قديم 10", exact=True).click()
                await expect(page.locator("#history-result")).to_contain_text("تعذّرت")
                await page.get_by_role("button", name="النتائج الأقدم").click()
                await expect(page.locator("#history-list li")).to_have_count(1)
                await page.get_by_role("button", name="ملخص قديم", exact=True).focus()
                await page.keyboard.press("Enter")
                await expect(page.locator("#history-result")).to_contain_text("لم يُعد التحقق منها الآن")
                await page.locator("#history-list li").get_by_role("button", name="حذف هذه النتيجة").click()
                await expect(page.locator("#history-delete-confirm")).to_be_disabled()
                await expect(page.locator("#history-delete-wait")).to_contain_text("أوقف المهمة الجارية")
                await page.get_by_role("button", name="الاحتفاظ بالنتيجة").click()
                assert not app.saved_reports.is_deleted(original_id)
                await expect(page.locator("#history-result")).to_contain_text('<img src=x onerror="alert(1)">')
                assert await page.locator("#history-result img").count() == 0
                async with page.expect_download() as incoming:
                    await page.get_by_role("button", name="تنزيل التقرير المحفوظ").click()
                download = await incoming.value
                exported = Path(await download.path()).read_text()
                assert "ملخص قديم" in exported and "ACTIVE-REQUEST" not in exported
                assert "لم يُعد التحقق منها الآن" in exported
                async with page.expect_download() as incoming:
                    await page.locator("#history-files").get_by_role("button", name="الحصول على نسخة").click()
                assert Path(await (await incoming.value).path()).read_bytes() == b"OLD-FILE-BYTES"
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path=str(tmp_path / "history.png"), full_page=True)
                await page.locator("#history-files").get_by_role("button", name="حذف النسخة المحلية").click()
                await expect(page.locator("#history-files button")).to_have_count(0)
                await expect(page.locator("#artifact-list")).to_contain_text("الملف غير متاح")
                await page.get_by_role("button", name="النتائج الأحدث").click()
                await expect(page.locator("#history-list li")).to_have_count(10)
                await page.get_by_role("button", name="تحديث السجل").focus()
                await page.keyboard.press("Escape")
                await expect(page.locator("#history")).not_to_have_attribute("open", "")
                await expect(page.locator("#current-task")).to_have_text("ACTIVE-REQUEST")
                await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                after = app.snapshot()
                assert after["id"] == before["id"] and after["pending"]["token"] == before["pending"]["token"]
                assert after["context_revision"] == before["context_revision"]
                assert app.browser.reads == reads and after["operations"] == []
                assert not errors
            finally: await browser.close()
    try: asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


@pytest.mark.parametrize("expire", [False, True])
def test_history_deletion_can_be_cancelled_and_retried_without_revealing_revoked_content(tmp_path, monkeypatch, expire):
    class Browser:
        context = None
        async def start(self): pass
        async def observe(self): return {"url": "https://example.com/", "text": "Result", "fingerprint": "same"}
        async def close(self): pass
    class Planner:
        def __init__(self, *_args): pass
        async def propose(self, *_args): return Action("finish", value="نتيجة للحذف")

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    reports = tmp_path / "reports"
    reports.mkdir()
    corrupt = reports / (uuid.uuid4().hex + ".json")
    corrupt.write_text("{broken")
    app = Assistant(Browser(), reports, planner_factory=Planner)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill("أعد النتيجة")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                await expect(page.locator("#result")).to_contain_text("نتيجة للحذف")
                task = app.snapshot()["id"]
                with app.artifacts.staging(task) as stage:
                    stage.write_bytes(b"DELETE-MY-LOCAL-COPY")
                    saved = app.artifacts.commit(task, stage, "local.txt")
                actual_delete = app.artifacts.delete_task
                def fail(_identifier): raise OSError("Synthetic cleanup interruption")
                monkeypatch.setattr(app.artifacts, "delete_task", fail)
                await page.locator("#history > summary").click()
                await page.get_by_role("button", name="نتيجة للحذف", exact=True).click()
                await expect(page.locator("#retention-days")).to_have_value("")
                await page.get_by_label("الاحتفاظ بهذه النتيجة وملفاتها").select_option("1")
                await page.get_by_role("button", name="حفظ مدة الاحتفاظ").click()
                await expect(page.locator("#retention-status")).to_contain_text("الحذف التلقائي في")
                deadline = app.saved_reports.retention(task)["expires_at"]
                assert app.saved_reports.retention_path(task).stat().st_mode & 0o077 == 0
                await page.reload()
                await page.locator("#history > summary").click()
                await page.get_by_role("button", name="نتيجة للحذف", exact=True).click()
                await expect(page.locator("#retention-days")).to_have_value("1")
                if expire:
                    app.saved_reports.clock = lambda: deadline + 1
                    await page.evaluate("milliseconds => {Date.now = () => milliseconds;}", (deadline + 1) * 1000)
                    await expect(page.locator("#history-detail")).to_be_hidden()
                    await expect(page.locator("#result")).to_be_hidden()
                    await expect(page.get_by_role("button", name="استكمال الحذف")).to_be_visible()
                    assert (reports / "files" / task / (saved["id"] + ".data")).exists()
                    with pytest.raises(ValueError): app.artifacts.read(task, saved["id"])
                    monkeypatch.setattr(app.artifacts, "delete_task", actual_delete)
                    for _ in range(300):
                        if not (reports / (task + ".json")).exists(): break
                        await asyncio.sleep(0.01)
                    assert not (reports / (task + ".json")).exists()
                    assert not (reports / "files" / task).exists()
                    await page.get_by_role("button", name="تحديث السجل").click()
                    await expect(page.locator("#history-list li")).to_have_count(1)
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    return
                await page.get_by_label("الاحتفاظ بهذه النتيجة وملفاتها").select_option("")
                await page.get_by_role("button", name="حفظ مدة الاحتفاظ").click()
                await expect(page.locator("#retention-status")).to_contain_text("لا يوجد حذف تلقائي")
                row = page.locator("#history-list li").filter(has_text="نتيجة للحذف")
                await row.get_by_role("button", name="حذف هذه النتيجة").click()
                await expect(page.locator("#history-delete-target")).to_have_text("نتيجة للحذف")
                await page.get_by_role("button", name="الاحتفاظ بالنتيجة").click()
                assert not app.saved_reports.is_deleted(task)
                assert app.artifacts.read(task, saved["id"])[1] == b"DELETE-MY-LOCAL-COPY"
                await row.get_by_role("button", name="حذف هذه النتيجة").click()
                await page.get_by_role("button", name="حذف التقرير وملفاته نهائيًا").click()
                await expect(page.get_by_role("button", name="استكمال الحذف")).to_be_visible()
                await expect(page.locator("#history-detail")).to_be_hidden()
                await expect(page.locator("#result")).to_be_hidden()
                await expect(page.locator("#followup")).to_be_hidden()
                assert app.snapshot()["status"] == "idle"
                assert (reports / "files" / task / (saved["id"] + ".data")).exists()
                response = await page.request.get(server.origin + f"/api/files/{task}/{saved['id']}", headers={"X-Parallax-Token": server.token})
                assert response.status == 404
                monkeypatch.setattr(app.artifacts, "delete_task", actual_delete)
                await page.get_by_role("button", name="استكمال الحذف").click()
                await page.get_by_role("button", name="حذف التقرير وملفاته نهائيًا").click()
                await expect(page.locator("#history-list li")).to_have_count(1)
                assert not (reports / "files" / task).exists()
                await page.get_by_role("button", name="حذف هذه النتيجة").click()
                await page.get_by_role("button", name="حذف التقرير وملفاته نهائيًا").click()
                await expect(page.locator("#history-list li")).to_have_count(0)
                assert not corrupt.exists()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally: await browser.close()
    try: asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


@pytest.mark.parametrize("conflict_action", ["resume", "edit"])
def test_checkpoint_ui_saves_selected_brief_and_requires_explicit_single_resume(tmp_path, conflict_action):
    class Browser:
        context = None
        reads = 0
        async def start(self): pass
        async def observe(self):
            self.reads += 1
            return {"url": "https://example.com/", "text": "Current page", "fingerprint": "current"}
        async def close(self): pass
    class Planner:
        def __init__(self, *_args): pass
        async def propose(self, *_args): return Action("handoff", reason="راجع الصفحة")
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path, planner_factory=Planner)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill("طلب خام لا يُنسخ إلى نقطة المتابعة")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                await expect(page.locator("#status")).to_have_text("بانتظارك")
                source = app.state["id"]
                await page.locator("#checkpoint-save > summary").click()
                await expect(page.locator("#checkpoint-brief")).to_have_value("")
                await page.get_by_label("النص الذي تريد حفظه للمتابعة").fill("أكمل المقارنة وفق الميزانية المختارة")
                await page.locator("#checkpoint-submit").click()
                await expect(page.locator("#checkpoint-status")).to_contain_text("حُفظت نقطة المتابعة")
                await expect(page.locator("#status")).to_have_text("متوقفة")
                assert app.checkpoints.load(source)["status"] == "ready"
                reads = app.browser.reads
                await page.reload()
                await page.locator("#history > summary").click()
                await page.get_by_role("button", name="نقطة متابعة: أكمل المقارنة وفق الميزانية المختارة", exact=True).click()
                await expect(page.locator("#checkpoint-text")).to_have_text("أكمل المقارنة وفق الميزانية المختارة")
                assert app.browser.reads == reads
                policy = app.saved_reports.retention(source)
                second = await browser.new_page(viewport={"width": 390, "height": 844})
                await second.goto(server.origin)
                await second.locator("#history > summary").click()
                await second.get_by_role("button", name="نقطة متابعة: أكمل المقارنة وفق الميزانية المختارة", exact=True).click()
                await expect(second.locator("#checkpoint-text")).to_have_text("أكمل المقارنة وفق الميزانية المختارة")
                if conflict_action == "edit":
                    await second.locator("#checkpoint-editor > summary").click()
                    await second.get_by_label("النص المعدّل للمتابعة").fill("تعديل من نافذة قديمة")
                await page.locator("#checkpoint-editor > summary").click()
                await page.get_by_label("النص المعدّل للمتابعة").fill("أكمل المقارنة للخطة الشهرية فقط")
                await expect(page.locator("#checkpoint-resume")).to_be_disabled()
                await page.get_by_role("button", name="حفظ تعديل النقطة").click()
                await expect(page.locator("#checkpoint-text")).to_have_text("أكمل المقارنة للخطة الشهرية فقط")
                assert app.checkpoints.load(source)["revision"] == 2
                assert app.saved_reports.retention(source) == policy
                if conflict_action == "edit":
                    await second.get_by_role("button", name="حفظ تعديل النقطة").click()
                    await expect(second.locator("#checkpoint-edit-status")).to_contain_text("تغيّرت نقطة المتابعة")
                    await expect(second.get_by_label("النص المعدّل للمتابعة")).to_have_value("تعديل من نافذة قديمة")
                else:
                    await second.locator("#checkpoint-resume").click()
                    await expect(second.locator("#checkpoint-resume-status")).to_contain_text("تغيّرت نقطة المتابعة")
                assert app.browser.reads == reads and app.checkpoints.load(source)["status"] == "ready"
                await expect(second.locator("#checkpoint-resume")).to_be_disabled()
                await second.locator("#checkpoint-reload").click()
                await expect(second.locator("#checkpoint-text")).to_have_text("أكمل المقارنة للخطة الشهرية فقط")
                await second.locator("#checkpoint-editor > summary").click()
                await second.get_by_label("النص المعدّل للمتابعة").fill("تعديل غير محفوظ")
                await second.locator("#checkpoint-editor > summary").click()
                await expect(second.locator("#checkpoint-resume")).to_be_disabled()
                await second.locator("#checkpoint-reload").click()
                await expect(second.locator("#checkpoint-resume")).to_be_enabled()
                await expect(second.locator("#checkpoint-edit-brief")).to_have_value("أكمل المقارنة للخطة الشهرية فقط")
                await second.close()
                await expect(page.locator("#checkpoint-resume")).to_be_enabled()
                await page.locator("#checkpoint-resume").click()
                await expect(page.locator("#status")).to_have_text("بانتظارك")
                await expect(page.locator("#checkpoint-resume-status")).to_contain_text("سبق بدء متابعة")
                assert app.state["parent_id"] == source and app.state["id"] != source
                assert app.state["task"] == "أكمل المقارنة للخطة الشهرية فقط"
                await expect(page.locator("#checkpoint-resume")).to_be_disabled()
                await page.get_by_role("button", name="إيقاف", exact=True).click()
                await expect(page.locator("#status")).to_have_text("متوقفة")
                await expect(page.locator("#checkpoint-resume")).to_be_disabled()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally: await browser.close()
    try: asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown(); server.server_close(); serving.join()
        loop.call_soon_threadsafe(loop.stop); worker.join(); loop.close()


def test_local_file_picker_keeps_handoff_and_never_sends_bytes_to_the_planner(tmp_path):
    payload = b"SYNTHETIC-PRIVATE-ATTACHMENT"
    class Browser:
        context = None
        async def start(self): self.context = True
        async def observe(self): return {"url": "https://example.com/", "text": "Documents", "fingerprint": "same"}
        async def close(self): self.context = None
    class Planner:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, _task, snapshot, history):
            self.calls += 1
            assert payload.decode() not in json.dumps([snapshot, history])
            if self.calls == 1: return Action("handoff", reason="اختر الملف من ملفات المهمة ثم استأنف")
            assert snapshot["assistant_context"]["artifacts"][0]["source_kind"] == "user_selected"
            return Action("finish", value="تم اختيار ملف محلي فقط")

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path / "reports", planner_factory=Planner)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill("ارفع الملف الذي سأختاره")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                token = app.snapshot()["pending"]["token"]
                await page.get_by_label("اختيار ملف لهذه المهمة").set_input_files({"name": "تقرير.txt", "mimeType": "text/plain", "buffer": payload})
                await page.get_by_role("button", name="إضافة الملف للمهمة").click()
                await expect(page.locator("#artifact-list")).to_contain_text("تقرير.txt")
                state = app.snapshot()
                assert state["pending"]["token"] == token and state["context_revision"] == 1
                assert state["approval_requests"] == 0 and not state["operations"]
                assert app.artifacts.read(state["id"], state["artifacts"][0]["id"])[1] == payload
                await page.get_by_role("button", name="انتهيت، استأنف المهمة").click()
                await expect(page.locator("#file-form")).to_be_hidden()
                response = await page.request.post(server.origin + "/api/files/add", headers={
                    "X-Parallax-Token": server.token, "Origin": server.origin,
                    "X-Parallax-Task": state["id"], "X-Parallax-Filename": "late.txt",
                    "Content-Type": "application/octet-stream"}, data=payload)
                assert response.status == 400
                assert len(app.artifacts.list(state["id"])) == 1
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally: await browser.close()
    try: asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


@pytest.mark.parametrize("inherited", [False, True])
def test_saved_file_can_be_downloaded_and_deleted_through_local_ui(tmp_path, inherited):
    class Browser:
        context = None
        async def start(self): pass
        async def observe(self): return {"url": "https://example.com/", "text": "Documents", "fingerprint": "same"}
        async def close(self): pass

    class Planner:
        def __init__(self, *_args): pass
        async def propose(self, _task, snapshot, _history):
            assert snapshot["assistant_context"]["artifacts"][0]["source_kind"] == "inherited"
            return Action("finish", value="الملف متاح للمتابعة")

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path / "reports", planner_factory=Planner)
    task_id = uuid.uuid4().hex
    app.update(id=task_id, status="unverified", task="تنزيل تقرير", result="ملف محفوظ")
    with app.artifacts.staging(task_id) as stage:
        stage.write_bytes(b"saved output")
        original = app.artifacts.commit(task_id, stage, "تقرير.txt")
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await expect(page.locator("#artifacts")).to_contain_text("تقرير.txt")
                if inherited:
                    await page.locator("#followup-task").fill("تابع العمل على التقرير")
                    await page.get_by_role("button", name="إرسال المتابعة").click()
                    await expect(page.locator("#artifact-list")).to_contain_text("مرجع لنسخة محددة من مهمة سابقة")
                    assert app.snapshot()["parent_id"] == task_id
                    assert app.snapshot()["approval_requests"] == 0
                async with page.expect_download() as incoming:
                    await page.get_by_role("button", name="الحصول على نسخة").click()
                download = await incoming.value
                assert download.suggested_filename == "تقرير.txt"
                assert Path(await download.path()).read_bytes() == b"saved output"
                await page.get_by_role("button", name="إزالة من المتابعة" if inherited else "حذف النسخة المحلية").click()
                await expect(page.locator("#artifacts")).to_be_hidden()
                assert app.artifacts.list(app.snapshot()["id"]) == []
                if inherited:
                    assert app.artifacts.read(task_id, original["id"])[1] == b"saved output"
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally: await browser.close()
    try: asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


@pytest.mark.parametrize("structured", [False, True])
def test_general_plan_and_user_revision_through_local_ui(tmp_path, structured):
    class Browser:
        context = None
        reads = 0
        executed = []

        async def start(self): self.context = True

        async def observe(self):
            self.reads += 1
            return {"url": "https://example.com/service", "fingerprint": "same", "text": "Service options"}

        def preview(self, action, snapshot):
            return {"action": action.to_dict(), "page": snapshot["url"], "fingerprint": "same",
                    "target": {"tag": "button", "label": "Send"}}

        async def execute(self, action): self.executed.append(action)
        async def close(self): self.context = None

    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls in {1, 4}:
                changed = self.calls == 4
                if changed:
                    assert snapshot["assistant_context"]["user_updates"] == [{"user_update": "لا ترسل الطلب"}]
                return Action("plan", "1" if changed else "0", json.dumps({
                    "goal": '<img src=x onerror="window.planInjected=true">' if changed else "قارن الخدمات وجهز الطلب",
                    "constraints": ["لا ترسل الطلب"],
                    "success_criteria": ["عرض مقارنة بمصادر مقروءة"],
                    "steps": [{"id": "read", "title": "قراءة الخيارات", "depends_on": [], "status": "in_progress"}],
                }))
            if self.calls == 2:
                return Action("expect", "request", json.dumps({"description": "إرسال الطلب التجريبي",
                    "url": snapshot["url"], "subject": {"الطلب": "REQUEST-42"} if structured else "REQUEST-42",
                    "outcome": {"الحالة": "مرسل"} if structured else "REQUEST-42 sent",
                    **({"label_aliases": {"الحالة": ["حالة الطلب"]}, "url_scope": "origin",
                        "casefold_outcome": ["الحالة"]} if structured else {})}))
            if self.calls == 3:
                return Action("click", "1", reason="إرسال الطلب التجريبي")
            if self.calls == 5:
                return Action("handoff", reason="حدد الخيار المطلوب ثم استأنف")
            return Action("finish", value="النتيجة التجريبية دون إرسال الطلب")

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path / "reports", planner_factory=Planner)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill("قارن الخدمات ثم جهز مسودة")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                await expect(page.locator("#task-plan")).to_be_visible()
                await expect(page.locator("#plan-content")).to_contain_text("عرض مقارنة بمصادر مقروءة")
                await expect(page.locator("#pending-title")).to_have_text("قرار واحد قبل المتابعة")
                await expect(page.locator("#details")).to_contain_text("الحالة: مرسل" if structured else "REQUEST-42 sent")
                if structured:
                    await expect(page.locator("#details")).to_contain_text("تسميات بديلة لحقل الحالة")
                    await expect(page.locator("#details")).to_contain_text("حالة الطلب")
                    await expect(page.locator("#details")).to_contain_text("صفحة لاحقة على أصل الموقع نفسه")
                    await expect(page.locator("#details")).to_contain_text("حقول لا تؤثر حالة الأحرف في قيمها")
                await page.get_by_text("تعديل التوجيه أثناء العمل", exact=True).click()
                await page.get_by_label("ما الذي تريد تغييره أو توضيحه؟").fill("لا ترسل الطلب")
                await page.locator("#revision-note").press("Tab")
                await expect(page.locator("#revision-submit")).to_be_focused()
                await page.locator("#revision-submit").press("Enter")
                await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                await expect(page.locator("#plan-content")).to_contain_text("<img src=x")
                assert await page.locator("#plan-content img").count() == 0
                assert await page.evaluate("window.planInjected") is None
                assert not app.browser.executed
                reads = app.browser.reads
                await page.get_by_label("ما الذي تريد تغييره أو توضيحه؟").fill("اكتف بالقراءة")
                await page.get_by_role("button", name="تحديث التوجيه", exact=True).click()
                await expect(page.locator("#plan-notice")).to_contain_text("بانتظار إعادة التقييم")
                assert app.browser.reads == reads
                assert app.snapshot()["pending"]["type"] == "handoff"
                assert await page.locator("html").get_attribute("dir") == "rtl"
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.screenshot(path=str(tmp_path / "general-plan-mobile.png"), full_page=True)
                await page.get_by_role("button", name="انتهيت، استأنف المهمة").click()
                await expect(page.locator("#status")).to_have_text("غير متحقق منه")
                await expect(page.locator("#result")).to_contain_text("النتيجة التجريبية دون إرسال الطلب")
                assert not app.browser.executed
            finally:
                await browser.close()

    try:
        asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


def test_recovered_effect_can_be_checked_in_idle_ui_without_starting_browser(tmp_path):
    class Browser:
        context = None
        async def close(self): pass

    journal = AttemptJournal(tmp_path)
    identifier = journal.begin(Action("click", "1"), "https://example.com/private?secret=hidden",
                               {"label": "PRIVATE-SUBMIT"}, uuid.uuid4().hex, uuid.uuid4().hex)
    journal.settle(identifier, "unknown")
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(), tmp_path)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await expect(page.locator("#uncertain-actions")).to_be_visible()
                await expect(page.get_by_role("link", name="فتح الوجهة للفحص")).to_have_attribute("href", "https://example.com/")
                assert "PRIVATE-SUBMIT" not in await page.locator("#uncertain-actions").inner_text()
                await page.get_by_role("button", name="تحققت أن الإجراء تم", exact=True).click()
                await expect(page.locator("#uncertain-actions")).to_be_hidden()
                assert app.job is None and app.browser.context is None
                assert app.snapshot()["status"] == "idle"
                assert not journal.uncertain()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                await browser.close()

    try:
        asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()


@pytest.mark.parametrize("outcome", ["revoke", "decline", "finish", "invalid", "request", "request_revoke"])
def test_operation_preview_revocation_and_results_through_local_ui(tmp_path, outcome):
    from test_assistant_operations import Browser, Planner

    class SelectedPlanner(Planner):
        async def propose(self, *args):
            action = await super().propose(*args)
            if outcome == "invalid" and action.kind == "expect":
                check = json.loads(action.value)
                del check["outcome"]["Language"]
                return Action("expect", action.target, json.dumps(check))
            return action

    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    app = Assistant(Browser(block=outcome in {"revoke", "request_revoke"}), tmp_path, planner_factory=SelectedPlanner)
    server = LocalServer(("127.0.0.1", 0), app, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    async def exercise():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(server.origin)
                await page.get_by_label("ما النتيجة التي تريدها؟").fill(
                    "غيّر الاسم إلى PRIVATE-NAME واللغة إلى العربية في حساب Demo" if outcome.startswith("request") else "Update my profile preferences")
                await page.get_by_role("button", name="ابدأ المهمة").click()
                if outcome.startswith("request"):
                    await expect(page.locator("#operation-authority")).to_be_visible()
                    await expect(page.locator("#authority-expiry")).to_contain_text("تفويض من طلبك الصريح")
                    assert app.snapshot()["approval_requests"] == 0
                    if outcome == "request":
                        await expect(page.locator("#status")).to_have_text("متحقق منه")
                        assert len(app.browser.executed) == 3
                    else:
                        await page.get_by_role("button", name="إلغاء اعتماد الخطوات المتبقية والتسلّم").click()
                        loop.call_soon_threadsafe(app.browser.release.set)
                        await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                        assert len(app.browser.executed) == 1
                        await page.get_by_role("button", name="إيقاف", exact=True).click()
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    return
                if outcome == "invalid":
                    await expect(page.locator("#status")).to_have_text("غير متحقق منه")
                    await expect(page.locator("#result")).to_contain_text("لم تثبت جميع القيم المطلوبة")
                    assert not app.browser.executed
                    assert app.snapshot()["approval_requests"] == 0
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    return
                await expect(page.locator("#details")).to_contain_text("PRIVATE-NAME")
                await expect(page.locator("#details")).to_contain_text("خمس دقائق")
                await expect(page.locator("#details")).to_contain_text("Account: Demo")
                assert await page.locator("#pending").get_attribute("role") == "group"
                if outcome == "decline":
                    await page.locator("#reject").focus()
                    await page.locator("#reject").press("Escape")
                    await expect(page.locator("#status")).to_have_text("متوقفة")
                    assert not app.browser.executed
                else:
                    await page.get_by_role("button", name="اعتماد التغييرات وحفظها مرة واحدة").click()
                    if outcome == "finish":
                        await expect(page.locator("#result")).to_contain_text("ظهرت جميع القيم المطلوبة للسجل المحدد")
                        assert app.snapshot()["status"] == "verified"
                        assert len(app.browser.executed) == 3
                        return
                    await expect(page.locator("#operation-authority")).to_be_visible()
                    await expect(page.locator("#authority-status")).to_contain_text("نشط")
                    await page.get_by_role("button", name="إلغاء اعتماد الخطوات المتبقية والتسلّم").click()
                    loop.call_soon_threadsafe(app.browser.release.set)
                    await expect(page.locator("#authority-status")).to_contain_text("أُلغي اعتماد البقية")
                    await expect(page.locator("#pending-title")).to_have_text("خطوة تحتاج تدخلك")
                    assert len(app.browser.executed) == 1
                    await expect(page.locator("#revoke-operation")).to_be_disabled()
                    await page.get_by_role("button", name="إيقاف", exact=True).click()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            finally:
                await browser.close()

    try:
        asyncio.run(exercise())
    finally:
        server.dispatch(app.close())
        server.shutdown()
        server.server_close()
        serving.join()
        loop.call_soon_threadsafe(loop.stop)
        worker.join()
        loop.close()
