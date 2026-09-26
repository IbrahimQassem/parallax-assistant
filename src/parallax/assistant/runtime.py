"""One task at a time; approval decisions are single-use and bound to a page."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from .actions import needs_approval, approval_reason, web_url
from .attempts import AttemptBlocked, AttemptJournal
from .artifacts import ArtifactStore, MAX_BYTES, upload_mime
from .effect_checks import EffectChecks, normalise
from .scoped_operations import OperationResults, ScopedOperation, form_descriptor
from .planner import CliPlanner, InvalidProposal
from .results import COMPLETION_STATES, structured_result
from .task_plan import parse_plan
from .request_authority import RequestAuthority
from .report_store import ReportStore
from .checkpoints import CheckpointStore


class RetentionExpired(RuntimeError):
    pass


def idle_state():
    return {"status": "idle", "events": [], "pending": None, "result": "", "page": "", "completion": None,
            "task_plan": None, "plan_revision": 0, "plan_stale": False, "context_revision": 0,
            "operation_group": None}


class Assistant:
    def __init__(self, browser, reports: Path, *, max_steps=40, task_timeout=1800,
                 planner_factory=CliPlanner):
        self.browser = browser
        self.reports = reports
        self.saved_reports = ReportStore(reports)
        self.checkpoints = CheckpointStore(self.saved_reports)
        self.checkpoint_busy = False
        self.session_id = uuid.uuid4().hex
        self.journal = AttemptJournal(reports)
        self.artifacts = ArtifactStore(reports / "files", is_deleted=self.saved_reports.is_revoked)
        self.downloaded_ids = []
        self.upload_requests = {}
        self.max_steps = max_steps
        self.task_timeout = task_timeout
        self.planner_factory = planner_factory
        self.lock = threading.RLock()
        self.state = idle_state()
        self.job = None
        self.maintenance_job = None
        self.retention_lock = asyncio.Lock()
        self.retention_stopped = False
        self.gate = None
        self.pause_requested = asyncio.Event()
        self.replan_requested = asyncio.Event()
        self.notes = []
        self.previous_context = None
        # Page text is evidence only while a task is running.  It is deliberately
        # not part of a saved report or a restored task.
        self.observations = {}
        self.last_interactive_action_at = None
        self.effect_checks = EffectChecks()
        self.operation = None
        self.used_operation_ids = set()
        self.operation_results = OperationResults()
        self.request_authority = None
        try:
            self.saved_reports.expire_due()
            self._cleanup_deletions()
        except (OSError, ValueError):
            self.event("تعذّر فحص مدة الاحتفاظ ببعض النتائج.", "error")
        self._restore_report()

    def ensure_maintenance(self):
        if self.maintenance_job is None or self.maintenance_job.done():
            self.maintenance_job = asyncio.create_task(self._maintain_retention())

    async def _maintain_retention(self):
        while True:
            try:
                await self.expire_reports()
            except (OSError, ValueError):
                pass  # Access checks still fail closed; the next tick retries cleanup.
            await asyncio.sleep(1)

    def _check_retention(self):
        try:
            dependencies = self.artifacts.source_tasks(self.state.get("id"))
            if self.state.get("parent_id"):
                dependencies.add(self.state["parent_id"])
            if any(self.saved_reports.is_revoked(identifier) for identifier in dependencies):
                raise ValueError()
        except (OSError, ValueError):
            self.retention_stopped = True
            raise RetentionExpired("توقفت المهمة لأن أحد مصادرها لم يعد متاحًا وفق سياسة الاحتفاظ. افحص أثر أي خطوة بدأت بالفعل.") from None

    async def expire_reports(self):
        async with self.retention_lock:
            self.saved_reports.expire_due()
            if self.job and not self.job.done():
                try:
                    self._check_retention()
                except RetentionExpired:
                    job = self.job
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    if self.job is job:
                        self.previous_context = None
            self._cleanup_deletions()

    async def set_retention(self, identifier, days):
        if self.job and not self.job.done():
            raise ValueError("انتظر انتهاء المهمة أو أوقفها قبل تغيير مدة الاحتفاظ.")
        policy = self.saved_reports.set_retention(identifier, days)
        self.ensure_maintenance()
        return policy

    def _cleanup_deleted_task(self, identifier):
        self.checkpoints.delete(identifier)
        self.artifacts.delete_task(identifier)
        self.saved_reports.finish_deletion(identifier)

    def _cleanup_deletions(self):
        try:
            for marker in self.saved_reports.deletions():
                if marker["pending"]:
                    self._forget_deleted_result(marker["id"])
                    try:
                        self._cleanup_deleted_task(marker["id"])
                    except (OSError, ValueError):
                        self.event("لم يكتمل حذف بعض الملفات المحلية؛ يمكن إعادة المحاولة من سجل النتائج.", "error")
        except (OSError, ValueError):
            self.event("تعذّر قراءة سجل الحذف المحلي.", "error")

    async def delete_saved_task(self, identifier):
        # This runs on the controller event loop with no await between the guard,
        # revocation and cleanup. It cannot interleave with start or a model step.
        if self.job and not self.job.done():
            raise ValueError("أوقف المهمة الجارية أو انتظر انتهاءها قبل حذف تقرير وملفاته.")
        marker_saved = True
        try:
            self.saved_reports.mark_deleted(identifier)
        except OSError:
            if not self.saved_reports.is_deleted(identifier):
                raise
            marker_saved = False
        self._forget_deleted_result(identifier)
        cleanup_pending = not marker_saved
        if marker_saved:
            try:
                self._cleanup_deleted_task(identifier)
            except (OSError, ValueError):
                cleanup_pending = self.saved_reports.deletion(identifier)["pending"]
        return {"deleted": True, "cleanup_pending": cleanup_pending, "state": self.snapshot()}

    def _forget_deleted_result(self, identifier):
        if self.state.get("id") == identifier:
            with self.lock:
                self.state = idle_state()
            self.notes = []
            self.previous_context = None
            self.observations = {}
            self.downloaded_ids = []
            self.upload_requests = {}
            self.effect_checks = EffectChecks()
            self.operation_results = OperationResults()
            self.request_authority = None
            self.operation = None
            self.used_operation_ids = set()
            self.job = None
        elif self.state.get("parent_id") == identifier:
            self.previous_context = None

    def _restore_report(self):
        # Restore only the latest report, never silently replace a failed run with
        # an older success. Prompts and browser snapshots remain unpersisted.
        try:
            identifier = self.saved_reports.latest_id()
            if identifier is None:
                return
            path = self.saved_reports.path(identifier)
            if path.stat().st_size > 200000:
                return
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("status") not in COMPLETION_STATES or not isinstance(report.get("result"), str):
                return
            sources = report.get("sources", [])
            if not isinstance(sources, list):
                return
            safe_sources = []
            for source in sources:
                if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                        or not isinstance(source.get("title"), str)
                        or not isinstance(source.get("observed_at"), (float, int))):
                    return
                web_url(source["url"])
                safe_sources.append(source)
            completion = report.get("completion")
            if not isinstance(completion, dict) or completion.get("status") not in COMPLETION_STATES:
                completion = {
                    "status": "unverified", "done": [], "remaining": [],
                    "reason": "هذه نتيجة محفوظة من إصدار سابق بلا دليل تحقق قابل لإعادة الفحص.", "evidence": [],
                }
            self.state.update(id=path.stem, status=completion["status"], result=report["result"],
                report=report.get("report"), sources=safe_sources, completion=completion,
                task="نتيجة محفوظة من آخر تشغيل", provider=report.get("provider", "codex"),
                events=[], step=report.get("step", 0), restored=True,
                consent_mode=report.get("consent_mode", "review"),
                automatic_steps=report.get("automatic_steps", 0), approval_requests=report.get("approval_requests", 0),
                started_at=report.get("started_at"), finished_at=report.get("finished_at"))
            group = report.get("operation_group", path.stem)
            self.state["operation_group"] = group if isinstance(group, str) and re.fullmatch(r"[0-9a-f]{32}", group) else path.stem
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
        if state.get("id") and state["status"] not in {"running", "waiting"}:
            try:
                hidden = self.saved_reports.is_revoked(state["id"])
            except (OSError, ValueError):
                hidden = True
            if hidden:
                state = idle_state()
        state["browser"] = {
            "mode": "chrome" if getattr(self.browser, "existing_chrome", False) else "isolated",
            "connected": getattr(self.browser, "context", None) is not None,
        }
        try:
            state["uncertain_actions"] = self.journal.uncertain()
            state["operations"] = self.journal.receipts(state.get("operation_group")) if state.get("operation_group") else []
            state["journal_error"] = None
        except (RuntimeError, ValueError):
            # Keep stop/takeover available even when persistence is unavailable.
            # Sending still fails closed in journal.begin().
            state.update(uncertain_actions=[], operations=[], journal_error="تعذّر قراءة سجل العمليات؛ لا يمكن إرسال خطوات جديدة حتى إصلاحه.")
        try:
            state["artifacts"] = self.artifacts.list(state.get("id"))
            state["artifacts_error"] = None
        except (OSError, ValueError):
            state.update(artifacts=[], artifacts_error="تعذّر قراءة ملفات المهمة.")
        return state

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def event(self, message, kind="info"):
        with self.lock:
            self.state["events"].append({"time": time.time(), "kind": kind, "message": message})
            self.state["events"] = self.state["events"][-160:]

    async def start(self, task: str, provider: str, parent_id=None, consent_mode="browse", *, checkpoint=None):
        if self.checkpoint_busy or (self.job and not self.job.done()):
            raise ValueError("توجد مهمة قيد التنفيذ. أوقفها أو انتظر اكتمالها.")
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise ValueError("اكتب طلبًا بين 1 و12000 حرف.")
        previous = self.snapshot()
        if consent_mode not in {"browse", "review"}:
            raise ValueError("وضع التدخل غير معروف.")
        history = []
        previous_context = None
        if checkpoint is not None:
            previous = {"id": checkpoint["id"], "operation_group": checkpoint["group_id"],
                        "artifacts": self.artifacts.list(checkpoint["id"])}
            if any(not row["available"] for row in previous["artifacts"]):
                raise ValueError("أحد ملفات نقطة المتابعة غير متاح؛ افحصه قبل الاستئناف.")
            previous_context = {"saved_continuation": checkpoint["brief"],
                "notice": "User-selected continuation. Observe the current session, destination and account before acting. Prior receipts are not proof of current success. Never replay completed or uncertain effects. No previous approval or DOM target is valid."}
            history = [previous_context]
        if parent_id is not None:
            if (not isinstance(parent_id, str) or parent_id != previous.get("id")
                    or previous["status"] not in COMPLETION_STATES or self.saved_reports.is_revoked(parent_id)):
                raise ValueError("المتابعة تخص آخر نتيجة مكتملة فقط.")
            history = [{"previous_task": previous["task"], "previous_answer": previous["result"],
                        "notice": "Prior answer is context only. Recheck facts in current pages."}]
            previous_context = {**history[0], "previous_plan": previous.get("task_plan"),
                                "previous_user_updates": copy.deepcopy(self.notes)}
        planner = self.planner_factory(provider)
        self.request_authority = RequestAuthority(task)
        self.retention_stopped = False
        self.pause_requested.clear()
        self.replan_requested.clear()
        self.notes = []
        self.previous_context = previous_context
        identifier = checkpoint["continuation"]["task_id"] if checkpoint else uuid.uuid4().hex
        unavailable_files = 0
        if parent_id is not None or checkpoint is not None:
            for source in previous.get("artifacts", []):
                try:
                    if not source["available"]:
                        raise ValueError("الملف السابق غير متاح.")
                    self.artifacts.inherit(identifier, source)
                except (OSError, ValueError):
                    if checkpoint is not None:
                        raise ValueError("تعذرت إتاحة أحد ملفات نقطة المتابعة؛ لم يبدأ التنفيذ.") from None
                    unavailable_files += 1
        self.update(id=identifier, operation_group=(previous.get("operation_group") or parent_id) if parent_id or checkpoint else identifier,
                    task=task, provider=provider, status="running",
                    events=[], pending=None, result="", report=None, completion=None, sources=[], page="", step=0,
                    parent_id=checkpoint["id"] if checkpoint else parent_id, started_at=time.time(), finished_at=None,
                    task_plan=None, plan_revision=0, plan_stale=False, context_revision=0,
                    file_context_warning="تعذرت إتاحة بعض ملفات المهمة السابقة؛ الملفات المتاحة فقط ظاهرة هنا." if
                        parent_id is not None and (unavailable_files or previous.get("artifacts_error")) else None)
        self.update(restored=False, phase="جارٍ الاتصال بالمتصفح", last_error=None,
                    consent_mode=consent_mode, automatic_steps=0, approval_requests=0)
        self.observations = {}
        self.last_interactive_action_at = None
        self.effect_checks = EffectChecks()
        self.operation = None
        self.used_operation_ids = set()
        self.operation_results = OperationResults()
        self.update(operation_authority=None, authority_receipts=[])
        self.downloaded_ids = []
        self.upload_requests = {}
        self.job = asyncio.create_task(self._run(task.strip(), planner, history))
        self.ensure_maintenance()
        return self.snapshot()

    async def save_checkpoint(self, identifier, brief, days):
        brief = self.checkpoints.brief(brief)
        if type(days) is not int or days not in {1, 7, 30, 90}:
            raise ValueError("اختر مدة احتفاظ لنقطة المتابعة.")
        if self.checkpoint_busy or identifier != self.state.get("id") or not identifier:
            raise ValueError("نقطة المتابعة تخص المهمة الحالية فقط.")
        if self.job and not self.job.done() and self.state["status"] != "waiting":
            raise ValueError("تسلّم المتصفح وانتظر توقف التنفيذ قبل حفظ نقطة المتابعة.")
        path = self.checkpoints.path(identifier)
        if path.exists() or path.is_symlink():
            raise ValueError("توجد نقطة متابعة لهذه المهمة بالفعل.")
        self._check_retention()
        self.checkpoint_busy = True
        try:
            if self.job and not self.job.done():
                self._revoke_operation()
                self.job.cancel()
                await asyncio.gather(self.job, return_exceptions=True)
            self.saved_reports.set_retention(identifier, days)
            self.checkpoints.save(identifier, brief, self.state["operation_group"], self.state.get("consent_mode", "browse"))
            self.ensure_maintenance()
            return {"id": identifier, "state": self.snapshot()}
        finally:
            self.checkpoint_busy = False

    async def update_checkpoint(self, identifier, brief, revision):
        if self.checkpoint_busy or (self.job and not self.job.done()):
            raise ValueError("انتظر انتهاء المهمة الحالية أو أوقفها قبل تعديل نقطة المتابعة.")
        # Mutations and resume claims run on the controller loop without yielding.
        return self.checkpoints.update(identifier, brief, revision)

    async def resume_checkpoint(self, identifier, provider, revision):
        if self.checkpoint_busy or (self.job and not self.job.done()):
            raise ValueError("انتظر انتهاء المهمة الحالية أو أوقفها.")
        checkpoint = self.checkpoints.review(identifier, revision)
        # Validate provider and all file references before consuming the resume.
        self.planner_factory(provider)
        files = self.artifacts.list(identifier)
        if any(not row["available"] for row in files):
            raise ValueError("أحد ملفات نقطة المتابعة غير متاح؛ افحصه قبل الاستئناف.")
        self.journal.receipts(checkpoint["group_id"])
        checkpoint = self.checkpoints.claim(identifier, revision, {
            "task_id": uuid.uuid4().hex, "owner_pid": os.getpid(), "owner_session": self.session_id})
        # A crash after this durable claim must not auto-replay the continuation.
        return await self.start(checkpoint["brief"], provider, consent_mode=checkpoint["consent_mode"], checkpoint=checkpoint)

    def checkpoint_recovery(self, checkpoint):
        continuation = checkpoint.get("continuation")
        result = {"can_prepare": False, "task_id": continuation["task_id"] if continuation else None,
                  "report_available": False, "uncertain": False,
                  "reason": "لا يوجد رابط لمحاولة سابقة؛ لا يمكن إعادة تجهيز نقطة مستهلكة قديمة تلقائيًا."}
        if continuation is None:
            return result
        identifier = continuation["task_id"]
        try:
            if self.saved_reports.is_revoked(identifier):
                result["reason"] = "حُذفت المحاولة السابقة أو انتهت إتاحتها؛ لا تعاد من هذه النقطة."
                return result
            try:
                report = self.saved_reports.load(identifier)
            except FileNotFoundError:
                report = None
            result["report_available"] = report is not None
            result["uncertain"] = self.journal.has_uncertain(checkpoint["group_id"])
            if checkpoint["status"] != "claimed":
                result["reason"] = "النقطة جاهزة للمراجعة؛ تقرير المحاولة السابقة متاح إن حُفظ."
                return result
            if self.checkpoint_busy or (self.job and not self.job.done()):
                result["reason"] = "انتظر انتهاء المهمة الحالية أو أوقفها قبل إعادة تجهيز النقطة."
                return result
            if (report and report["status"] in COMPLETION_STATES) or (
                    self.state.get("id") == identifier and self.state["status"] in COMPLETION_STATES):
                result["reason"] = "للمحاولة نتيجة نهائية؛ راجعها واطلب متابعة جديدة لما بقي."
                return result
            if report is None and not (continuation["owner_pid"] == os.getpid()
                                      and continuation["owner_session"] == self.session_id):
                try:
                    os.kill(continuation["owner_pid"], 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    result["reason"] = "تعذّر التأكد من توقف التشغيل السابق؛ لم تُتح إعادة تجهيز النقطة."
                    return result
                else:
                    result["reason"] = "التشغيل السابق ما زال موجودًا ولا يتوفر تقرير نهائي؛ لا تبدأ متابعة أخرى."
                    return result
            result.update(can_prepare=True, reason=(
                "المحاولة متوقفة دون نتيجة مكتملة. افحص الأثر، ثم يمكنك إعادة تجهيز النص المحفوظ للمراجعة دون تشغيل المتصفح."
                if report else "لا يوجد تقرير نهائي والتشغيل السابق متوقف. قد يكون ترك أثرًا؛ افحصه قبل إعادة تجهيز النقطة."))
        except (OSError, ValueError, RuntimeError):
            result.update(can_prepare=False, reason="تعذّر فحص المحاولة السابقة أو سجل الأثر؛ لم تُتح إعادة تجهيز النقطة.")
        return result

    def inspect_checkpoint(self, identifier):
        checkpoint = self.checkpoints.public(identifier)
        if checkpoint is None:
            return None
        recovery = self.checkpoint_recovery(checkpoint)
        return {**checkpoint, "continuation": {"task_id": recovery["task_id"]} if recovery["task_id"] else None,
                "recovery": recovery}

    async def prepare_checkpoint_again(self, identifier, revision):
        checkpoint = self.checkpoints.version(identifier, revision)
        recovery = self.checkpoint_recovery(checkpoint)
        if not recovery["can_prepare"]:
            raise ValueError(recovery["reason"])
        # User decision changes eligibility only; it never starts observation or
        # resolves an uncertain operation. Resume still needs the new revision.
        return self.checkpoints.prepare_again(identifier, revision)

    async def attach_file(self, task_id, name, content):
        if (not self.job or self.job.done() or task_id != self.state.get("id")
                or len(self.notes) >= 20 or not isinstance(name, str) or not name.strip()
                or len(name) > 500 or not isinstance(content, bytes) or len(content) > MAX_BYTES):
            raise ValueError("اختر ملفًا للمهمة النشطة ضمن الحد المسموح.")
        self.artifacts.check_capacity(task_id)
        with self.artifacts.staging(task_id) as stage:
            stage.write_bytes(content)
            self.artifacts.commit(task_id, stage, name, source_kind="user_selected")
        # This is a user action, not instructions extracted from the file/name.
        return await self.control("revise", task_id, "أضفت ملفًا للمهمة؛ راجع ملفات المهمة المتاحة قبل المتابعة.")

    async def control(self, command, token="", note=""):
        if self.checkpoint_busy:
            raise ValueError("جارٍ حفظ نقطة المتابعة وإيقاف المهمة.")
        if command in {"effect_occurred", "effect_absent"}:
            self.journal.resolve(token, occurred=command == "effect_occurred",
                                 group_id=self.state.get("operation_group"))
            self.event("سُجل فحصك اليدوي لأثر الإجراء؛ هذا ليس تحققًا آليًا ولا يعيد الإرسال أو يستأنف المتصفح.")
            return self.snapshot()
        if not self.job or self.job.done():
            raise ValueError("لا توجد مهمة نشطة.")
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError("الملاحظة طويلة جدًا.")
        if command in {"revise", "resume", "approve"} and note.strip() and len(self.notes) >= 20:
            raise ValueError("وصلت المهمة إلى الحد الأقصى لتحديثات التوجيه.")
        if command == "stop":
            self._revoke_operation()
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        elif command == "pause":
            self._revoke_operation()
            self.pause_requested.set()
            if self.gate and not self.gate.done():
                self.gate.set_result(("pause", ""))
        elif command == "revoke_operation":
            if self.operation is None or token != self.operation.id:
                raise ValueError("انتهى هذا الاعتماد؛ حدّث الحالة قبل المحاولة.")
            self._revoke_operation()
            self.pause_requested.set()
            self.event("أُلغي اعتماد الخطوات المتبقية. الخطوة التي بدأت قد يكون لها أثر؛ افحص الصفحة قبل الاستئناف.")
        elif command == "revise":
            if token != self.snapshot().get("id"):
                raise ValueError("انتهت المهمة التي يخصها هذا التوجيه؛ حدّث الحالة قبل المحاولة.")
            if not note.strip() or len(self.notes) >= 20:
                raise ValueError("اكتب توجيهًا غير فارغ؛ الحد الأقصى عشرون تحديثًا للمهمة.")
            self._remember_note(note)
            pending = self.snapshot()["pending"]
            # A new instruction invalidates a pending step, but never resumes
            # observation while the user owns the browser during a handoff.
            if pending and pending["type"] == "approval" and self.gate and not self.gate.done():
                self.gate.set_result(("revise", ""))
            self.event("وصل توجيهك؛ سأعيد تقييم الخطة والخطوات التالية. ما نُفّذ بالفعل لا يُلغى تلقائيًا.")
        elif command in {"approve", "reject", "resume"}:
            if command != "reject":
                try:
                    self._check_retention()
                except RetentionExpired as error:
                    raise ValueError(str(error)) from error
            pending = self.snapshot()["pending"]
            if (not pending or token != pending["token"] or not self.gate
                    or self.gate.done()):
                raise ValueError("انتهى هذا الطلب؛ حدّث الصفحة لعرض الحالة الحالية.")
            if command == "resume" and pending["type"] != "handoff":
                raise ValueError("هذا الطلب يحتاج اعتمادًا أو رفضًا.")
            if command == "approve" and pending["type"] != "approval":
                raise ValueError("أكمل التدخل اليدوي ثم استأنف.")
            self.gate.set_result((command, note))
        else:
            raise ValueError("أمر غير معروف.")
        return self.snapshot()

    def _remember_note(self, note):
        if note.strip():
            self._revoke_operation()
            self.effect_checks.invalidate()
            self.notes.append({"user_update": note.strip()})
            self.update(context_revision=self.snapshot()["context_revision"] + 1, plan_stale=True)
            self.replan_requested.set()

    def _sync_operation(self, operation):
        with self.lock:
            receipts = [receipt for receipt in self.state.get("authority_receipts", []) if receipt["id"] != operation.id]
            self.state.update(operation_authority=operation.public(), authority_receipts=[*receipts, operation.receipt()])

    def _revoke_operation(self):
        if self.request_authority is not None and not self.request_authority.bound:
            # A takeover before the first observation loses the original scope;
            # resuming must not reinterpret "this account" as a different page.
            self.request_authority.invalidate()
        if self.operation is not None:
            self.operation.revoke()
            self._sync_operation(self.operation)
            self.operation = None
            self.effect_checks.invalidate()

    async def _perform_operation(self, operation, revision):
        attempted = 0
        for _ in operation.steps:
            self._check_retention()
            if self.operation is not operation or self.pause_requested.is_set() or revision != self.snapshot()["context_revision"]:
                break
            fresh = await self.browser.observe()
            self._check_retention()
            if self.operation is not operation or self.pause_requested.is_set() or revision != self.snapshot()["context_revision"]:
                break
            action = operation.next_action(fresh)
            preview = self.browser.preview(action, fresh)
            attempt = self.journal.begin(action, fresh["url"], preview.get("target"),
                self.state["id"], self.state["operation_group"], form_state=fresh.get("form_hashes"), authority_id=operation.id)
            self.effect_checks.sent(attempt)
            attempted += 1
            self.update(step=self.snapshot()["step"] + 1, phase="جارٍ تنفيذ خطوة ضمن العملية المعتمدة")
            try:
                await asyncio.wait_for(self.browser.execute(action), timeout=35)
            except asyncio.CancelledError:
                self.journal.settle(attempt, "unknown")
                raise
            except Exception:
                self.journal.settle(attempt, "unknown")
                self.last_interactive_action_at = time.time()
                self._revoke_operation()
                await self._handoff("تعذر تأكيد أثر خطوة من العملية. أُلغي اعتماد البقية؛ افحص الأثر قبل المتابعة.", failure=True)
                return attempted
            self.journal.settle(attempt, "returned")
            self.last_interactive_action_at = time.time()
            if operation.basis == "user_explicit_request":
                self.update(automatic_steps=self.snapshot()["automatic_steps"] + 1)
            operation.advance()
            self._sync_operation(operation)
            self.event("عاد المتصفح من خطوة ضمن اعتماد العملية المحددة؛ التحقق من النتيجة مستقل.", "receipt")
        if operation.status == "consumed" and self.operation is operation:
            self.operation = None
        return attempted

    async def _wait(self, kind, detail):
        self.gate = asyncio.get_running_loop().create_future()
        self.update(status="waiting", pending={"type": kind, "token": uuid.uuid4().hex, **detail})
        try:
            return await self.gate
        finally:
            self.gate = None
            self.update(pending=None, status="running")

    async def _handoff(self, message, **detail):
        self._revoke_operation()
        self.pause_requested.clear()
        decision, note = await self._wait("handoff", {"message": message, **detail})
        if decision in {"reject", "pause"}:
            raise asyncio.CancelledError
        if note:
            self._remember_note(note)
        self.event("انتهى التدخل اليدوي؛ سأقرأ الصفحة مجددًا.")

    async def _plan(self, planner, task, snapshot, history):
        if self.pause_requested.is_set() or self.replan_requested.is_set():
            return None
        planning = asyncio.create_task(planner.propose(task, snapshot, history))
        paused = asyncio.create_task(self.pause_requested.wait())
        revised = asyncio.create_task(self.replan_requested.wait())
        try:
            done, _ = await asyncio.wait({planning, paused, revised}, return_when=asyncio.FIRST_COMPLETED)
            if paused in done or revised in done:
                return None
            return await planning
        finally:
            for job in (planning, paused, revised):
                if not job.done():
                    job.cancel()
            await asyncio.gather(planning, paused, revised, return_exceptions=True)

    def _record_source(self, snapshot):
        try:
            url = web_url(snapshot["url"])
        except (KeyError, ValueError):
            return None
        with self.lock:
            sources = self.state["sources"]
            existing = next((source for source in sources if source["url"] == url), None)
            if existing is None:
                existing = {"id": f"S{len(sources) + 1}", "url": url}
                sources.append(existing)
            observed_at = time.time()
            existing.update(title=str(snapshot.get("title") or "صفحة تمت قراءتها")[:300], observed_at=observed_at)
            text = snapshot.get("text")
            if isinstance(text, str):
                self.observations[existing["id"]] = {
                    "text": text,
                    "after_action": bool(self.last_interactive_action_at and observed_at >= self.last_interactive_action_at),
                }
            return existing["id"]

    async def _run(self, task, planner, history=None):
        history = list(history or [])
        read_failures = 0
        proposal_failures = 0
        operation_steps = 0
        try:
            async with asyncio.timeout(self.task_timeout):
                await self.browser.start()
                self.event("المتصفح جاهز. يمكنك إيقاف المهمة أو تسلّم المتصفح في أي وقت.")
                for step in range(1, self.max_steps + 1):
                    self._check_retention()
                    step += operation_steps
                    if step > self.max_steps:
                        break
                    self.update(step=step)
                    if self.pause_requested.is_set():
                        await self._handoff("المتصفح تحت تحكمك الآن. اضغط استئناف بعد الانتهاء.")
                    self.replan_requested.clear()
                    revision = self.snapshot()["context_revision"]
                    self.update(phase="جارٍ قراءة الصفحة")
                    snapshot = await self.browser.observe()
                    self._check_retention()
                    if self.pause_requested.is_set() or revision != self.snapshot()["context_revision"]:
                        continue
                    source_id = self._record_source(snapshot)
                    self.request_authority.bind(snapshot)
                    self.effect_checks.observe(snapshot, source_id)
                    self.operation_results.observe(snapshot, source_id, revision)
                    snapshot["observed_sources"] = self.snapshot()["sources"]
                    state = self.snapshot()
                    snapshot["assistant_context"] = {
                        "original_request": task, "user_updates": copy.deepcopy(self.notes),
                        "task_plan": state["task_plan"], "plan_revision": state["plan_revision"],
                        "plan_stale": state["plan_stale"], "context_revision": revision,
                        "previous_task_context": self.previous_context,
                        "operation_receipts": state["operations"],
                        "uncertain_actions": state["uncertain_actions"],
                        "effect_checks": self.effect_checks.context(),
                        "consent_mode": state["consent_mode"],
                        "operation_authority": state.get("operation_authority"),
                        "operation_results": self.operation_results.context(revision),
                        "artifacts": state["artifacts"],
                        "file_context_warning": state.get("file_context_warning"),
                        "downloads_supported": not getattr(self.browser, "existing_chrome", False),
                    }
                    self.update(page=snapshot["url"])
                    self.event(f"قراءة الصفحة والتخطيط للخطوة {step}…")
                    self.update(phase="جارٍ تحليل البيانات وتحديد الخطوة التالية")
                    try:
                        action = await self._plan(planner, task, snapshot, history + self.notes)
                        self._check_retention()
                    except InvalidProposal:
                        proposal_failures += 1
                        if proposal_failures > 2:
                            raise RuntimeError("أعاد المحرك خطوات بصيغة غير صالحة ثلاث مرات؛ توقفت المهمة دون تنفيذ هذه المقترحات.") from None
                        history.append({"controller_error": "Your last response was not ONE valid JSON action. No action was executed. Return exactly one object with kind, target, value, reason matching the action schema, without Markdown, commentary or multiple objects."})
                        self.event("لم تكن صيغة خطوة المحرك صالحة؛ سأعيد طلبها قبل تنفيذ أي إجراء.")
                        continue
                    if action is None or revision != self.snapshot()["context_revision"]:
                        continue
                    proposal_failures = 0
                    if action.kind == "operation":
                        try:
                            operation = ScopedOperation(action.value, snapshot, self.browser.preview)
                            self.operation_results.register(operation, revision)
                            self.operation_results.observe(snapshot, source_id, revision)
                            if self.snapshot().get("consent_mode") == "review":
                                raise ValueError("وضع مراجعة كل خطوة لا يسمح باعتماد مجموعة؛ اقترح الخطوات منفردة.")
                            if action.target != self.effect_checks.active or action.target in self.used_operation_ids:
                                raise ValueError("يلزم شرط تحقق نشط وجديد للعملية قبل طلب اعتمادها.")
                            if len(operation.steps) > self.max_steps - step:
                                raise ValueError("لا تكفي ميزانية الخطوات لتنفيذ العملية كاملة.")
                            effect_check = self.effect_checks.preview(snapshot)
                            operation.validate_expectation(effect_check)
                            first = self.browser.preview(operation.steps[0], snapshot)
                            self.journal.check(operation.steps[0], snapshot["url"], first.get("target"), self.state["operation_group"],
                                               form_state=snapshot.get("form_hashes"))
                        except (ValueError, AttemptBlocked) as error:
                            history.append({"controller_error": str(error)})
                            self.event(str(error), "error")
                            continue
                        from_request = self.request_authority.claim(operation, revision)
                        if from_request:
                            decision, _note = "approve", ""
                            self.event("العملية تطابق التغيير الصريح في طلبك والحساب المرصود؛ سأُنفذها ضمن هذا التفويض ثم أتحقق من النتيجة.")
                        else:
                            self.update(approval_requests=self.snapshot()["approval_requests"] + 1)
                            decision, _note = await self._wait("approval", {"action": action.to_dict(), "page": snapshot["url"],
                                "operation": operation.public(), "effect_check": effect_check,
                                "approval_reason": "اعتماد واحد للقيم والخطوات المعروضة فقط، مرة واحدة ولمدة خمس دقائق بعد الموافقة."})
                        if decision == "reject":
                            self.update(status="cancelled", result="رُفضت العملية؛ لم تُنفذ خطواتها.")
                            return
                        if decision == "pause":
                            self.pause_requested.set()
                        if decision == "approve" and _note:
                            self._remember_note(_note)
                        if decision != "approve" or revision != self.snapshot()["context_revision"]:
                            continue
                        operation.approve(basis="user_explicit_request" if from_request else "user_operation_preview")
                        operation.context_revision = revision
                        self.operation = operation
                        self.used_operation_ids.add(action.target)
                        self._sync_operation(operation)
                        before = self.snapshot()["step"]
                        try:
                            await self._perform_operation(operation, revision)
                        except AttemptBlocked as error:
                            self._revoke_operation()
                            history.append({"controller_error": str(error)})
                            if not error.duplicate:
                                await self._handoff(str(error), failure=True)
                        except ValueError as error:
                            self._revoke_operation()
                            history.append({"controller_error": str(error)})
                            self.event(str(error), "error")
                        finally:
                            operation_steps += self.snapshot()["step"] - before
                        history.append({"operation_receipt": operation.receipt(), "notice": "Inspect the site before deciding completion; never replay completed steps."})
                        continue
                    if action.kind == "expect":
                        try:
                            self.effect_checks.register(action.target, action.value, snapshot)
                        except ValueError as error:
                            history.append({"controller_error": str(error)})
                            self.event(str(error), "error")
                            continue
                        self.event("حُدد شرط تحقق للعملية؛ سيظهر مع معاينة الخطوات ولا يمنح إذنًا بتنفيذها.")
                        continue
                    if action.kind == "plan":
                        try:
                            if action.target != str(self.snapshot()["plan_revision"]):
                                raise ValueError("تغيرت نسخة الخطة؛ أعد التخطيط باستخدام النسخة الحالية.")
                            task_plan = parse_plan(action.value)
                        except ValueError as error:
                            history.append({"controller_error": str(error)})
                            self.event(str(error), "error")
                            continue
                        self.update(task_plan=task_plan, plan_revision=self.snapshot()["plan_revision"] + 1,
                                    plan_stale=False)
                        self.event("حُدّثت خطة المهمة؛ تقدم الخطة لا يُعد دليلًا على تحقق النتيجة.")
                        continue
                    if action.kind == "finish":
                        report = structured_result(
                            action.value, self.snapshot()["sources"], self.observations,
                            requires_post_action_evidence=self.last_interactive_action_at is not None,
                        )
                        if report is None:
                            completion = {
                                "status": "unverified", "done": [], "remaining": [],
                                "reason": "انتهى المحرك من الإجابة دون بنية ودليل يمكن للمتحكم التحقق منه.",
                                "evidence": [],
                            }
                        else:
                            completion = report["completion"]
                        completion["effect_checks"] = self.effect_checks.summaries()
                        completion["file_checks"] = []
                        completion["upload_checks"] = [{key: entry[key] for key in ("file_id", "status")}
                            for entry in self.upload_requests.values() if entry["revision"] == revision]
                        if any(check["status"] != "selected" for check in completion["upload_checks"]):
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            completion["reason"] = "لم يُسلّم أحد الملفات المقترحة إلى حقل الموقع. " + completion["reason"]
                            completion["remaining"] = ["مراجعة رفع الملف الذي لم يُنفذ", *completion["remaining"]][:12]
                        for identifier in self.downloaded_ids:
                            try:
                                self.artifacts.read(state["id"], identifier)
                                valid = True
                            except (OSError, ValueError):
                                valid = False
                            completion["file_checks"].append({"id": identifier, "status": "matched" if valid else "unavailable"})
                        if any(check["status"] != "matched" for check in completion["file_checks"]):
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            completion["reason"] = "أحد الملفات المنزلة غير متاح أو تغير محتواه. " + completion["reason"]
                            completion["remaining"] = ["فحص ملفات المهمة المحلية", *completion["remaining"]][:12]
                        completion["operation_checks"] = self.operation_results.summaries(revision)
                        if any(check["status"] != "matched" for check in completion["operation_checks"]):
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            completion["reason"] = "لم تثبت نتيجة كل التغييرات المقترحة ضمن التوجيه الحالي. " + completion["reason"]
                            completion["remaining"] = ["إثبات القيم المطلوبة للعملية التي لم تكتمل أو تصحيح نطاق الطلب", *completion["remaining"]][:12]
                        if any(receipt["status"] != "consumed" and receipt.get("context_revision") == revision
                               for receipt in self.snapshot().get("authority_receipts", [])):
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            completion["reason"] = "لم تكتمل خطوات العملية المعتمدة ضمن التوجيه الحالي. " + completion["reason"]
                            completion["remaining"] = ["مراجعة ما تبقى من العملية المتوقفة", *completion["remaining"]][:12]
                        if self.effect_checks.missing():
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            completion["reason"] = "لم تتحقق شروط الأثر لكل الخطوات المؤثرة، أو لم تُحدد قبل تنفيذها. " + completion["reason"]
                            completion["remaining"] = ["إثبات حالة كل عنصر متأثر في وجهته", *completion["remaining"]][:12]
                        journal_unavailable = False
                        try:
                            uncertain = self.journal.has_uncertain(self.state["operation_group"])
                        except (RuntimeError, ValueError):
                            uncertain = True
                            journal_unavailable = True
                        if uncertain:
                            if completion["status"] == "verified":
                                completion["status"] = "unverified"
                            reason = "تعذّر فحص سجل العمليات. " if journal_unavailable else "بقي أثر إرسال غير مؤكد في سجل المهمة. "
                            remaining = "إصلاح سجل العمليات وفحص محاولات الإرسال" if journal_unavailable else "فحص أثر محاولات الإرسال غير المؤكدة"
                            completion["reason"] = reason + completion["reason"]
                            completion["remaining"] = [remaining, *completion["remaining"]][:12]
                        self.update(status=completion["status"], result=action.value, report=report, completion=completion)
                        labels = {
                            "verified": "النتيجة متحقق منها بالأدلة المعروضة.",
                            "partial": "النتيجة جزئية؛ راجع ما أُنجز وما بقي.",
                            "unverified": "انتهى المحرك من الإجابة، لكن النتيجة غير متحقق منها.",
                        }
                        self.event(labels[completion["status"]])
                        return
                    if action.kind == "handoff":
                        await self._handoff(action.reason or action.value)
                        history.append({"handoff": "User returned control; observe the new page."})
                        continue
                    try:
                        preview = self.browser.preview(action, snapshot)
                        requires_effect_check = action.kind != "download" and needs_approval(action, preview.get("target"), "browse")
                        if action.kind == "download":
                            self.artifacts.check_capacity(self.state["id"])
                            preview["download"] = {"url": preview["target"]["href"],
                                "directory": str(self.artifacts.root), "max_bytes": MAX_BYTES}
                        if action.kind == "upload":
                            self.artifacts.identifier(action.value)
                            upload_key = json.dumps([revision, action.value, snapshot["url"],
                                {key: preview["target"].get(key) for key in ("tag", "type", "name", "label", "frame")},
                                form_descriptor(preview["target"].get("form") or {})], sort_keys=True)
                            self.upload_requests.setdefault(upload_key, {"revision": revision,
                                "file_id": action.value, "status": "not_sent"})
                            metadata, _content = self.artifacts.read(self.state["id"], action.value)
                            del _content
                            mime = upload_mime(metadata["name"], preview["target"].get("accept", ""))
                            preview["upload"] = {key: metadata[key] for key in ("id", "task_id", "name", "size", "sha256")}
                            preview["upload"]["mime"] = mime
                        if requires_effect_check:
                            preview["effect_check"] = self.effect_checks.preview(snapshot)
                            check = preview["effect_check"]
                            if action.kind == "upload" and check and any(
                                    check["outcome"][label].casefold() in {normalise(metadata["name"]).casefold(), metadata["sha256"].casefold()}
                                    for label in check.get("casefold_outcome", [])):
                                raise ValueError("اسم الملف وبصمته يحتاجان مطابقة دقيقة؛ لا تهمل حالة الأحرف في هذين الحقلين.")
                    except ValueError as error:
                        history.append({"controller_error": str(error)})
                        self.event(str(error), "error")
                        continue
                    policy_reason = approval_reason(action, preview.get("target"), self.snapshot().get("consent_mode", "browse"))
                    effect_page = preview["target"]["href"] if action.kind == "download" else snapshot["url"]
                    requires_approval = policy_reason is not None
                    if requires_approval:
                        try:
                            self.journal.check(action, effect_page, preview.get("target"), self.state["operation_group"],
                                               form_state=snapshot.get("form_hashes"))
                        except AttemptBlocked as blocked:
                            history.append({"controller_error": str(blocked), "operation_receipt": blocked.receipt})
                            if blocked.duplicate:
                                self.event(str(blocked))
                            else:
                                await self._handoff(str(blocked) + " سجل نتيجة الفحص في قائمة الآثار غير المؤكدة ثم استأنف.", failure=True)
                            continue
                        preview["approval_reason"] = policy_reason
                        self.update(approval_requests=self.snapshot()["approval_requests"] + 1)
                        decision, note = await self._wait("approval", preview)
                        if decision == "revise":
                            continue
                        if decision == "pause":
                            self.pause_requested.set()
                            continue
                        if decision == "reject":
                            self.update(status="cancelled", result="رُفضت الخطوة؛ لم تُنفّذ.")
                            self.event("رُفضت الخطوة وانتهت المهمة.")
                            return
                        # Approval binds the exact action AND observed state. A user editing
                        # the browser while reading the preview invalidates that preview.
                        fresh = await self.browser.observe()
                        if fresh["fingerprint"] != preview["fingerprint"]:
                            history.append({"controller_error": "Page changed; approval invalidated. Replan."})
                            self.event("تغيّرت الصفحة؛ أُلغي الاعتماد السابق دون تنفيذ.")
                            continue
                        self.browser.preview(action, fresh)
                        if note:
                            self._remember_note(note)
                    elif action.kind == "click":
                        # A model round-trip can outlive the original DOM. Recheck both
                        # the page fingerprint and the navigation classification.
                        fresh = await self.browser.observe()
                        if fresh["fingerprint"] != preview["fingerprint"]:
                            self.event("تغيرت الصفحة قبل خطوة التصفح؛ سأقرأها مجددًا.")
                            continue
                        fresh_preview = self.browser.preview(action, fresh)
                        if needs_approval(action, fresh_preview.get("target"), self.snapshot().get("consent_mode", "browse")):
                            self.event("تغير نوع العنصر؛ أُلغيت خطوة التصفح التلقائية.")
                            continue
                    if self.pause_requested.is_set() or revision != self.snapshot()["context_revision"]:
                        continue
                    upload = None
                    self._check_retention()
                    if action.kind == "upload":
                        try:
                            metadata, content = self.artifacts.read(self.state["id"], action.value)
                            if any(metadata[key] != preview["upload"][key] for key in ("id", "task_id", "name", "size", "sha256")):
                                raise ValueError("تغيرت نسخة الملف بعد المعاينة؛ يلزم اعتماد جديد.")
                            upload = (metadata["name"], preview["upload"]["mime"], content)
                        except (OSError, ValueError):
                            message = "الملف تغير أو حُذف بعد المعاينة؛ لم يُرفع، ويلزم اختيار نسخة متاحة واعتمادها من جديد."
                            self.event(message, "error")
                            history.append({"controller_error": message})
                            continue
                    attempt = None
                    if requires_approval:
                        try:
                            attempt = self.journal.begin(action, effect_page, preview.get("target"),
                                                         self.state["id"], self.state["operation_group"],
                                                         form_state=snapshot.get("form_hashes"))
                        except AttemptBlocked as blocked:
                            history.append({"controller_error": str(blocked), "operation_receipt": blocked.receipt})
                            self.event(str(blocked))
                            continue
                    if attempt and requires_effect_check:
                        self.effect_checks.sent(attempt)
                    try:
                        self.update(phase="جارٍ تنفيذ الخطوة والتحقق من نتيجتها")
                        if action.kind == "download":
                            with self.artifacts.staging(self.state["id"]) as stage:
                                name = await asyncio.wait_for(self.browser.download_to(
                                    action, stage, MAX_BYTES, preview["target"]["href"]), timeout=35)
                                artifact = self.artifacts.commit(self.state["id"], stage, name)
                                self.downloaded_ids.append(artifact["id"])
                        elif upload is not None:
                            await asyncio.wait_for(self.browser.upload_file(action, *upload), timeout=35)
                            self.upload_requests[upload_key]["status"] = "selected"
                        else:
                            await asyncio.wait_for(self.browser.execute(action), timeout=35)
                    except asyncio.CancelledError:
                        if attempt:
                            self.journal.settle(attempt, "unknown")
                        raise
                    except Exception as error:
                        if attempt:
                            self.journal.settle(attempt, "unknown")
                        # A timeout might occur AFTER a purchase/send succeeded. Never retry
                        # a consequential action implicitly when its outcome is unknown.
                        # Do not surface raw exception text: it can contain form values.
                        category = type(error).__name__
                        timed_out = isinstance(error, TimeoutError) or category == "TimeoutError"
                        reason = "انتهت مهلة تنفيذ الخطوة" if timed_out else "تعذّر تنفيذ الخطوة في المتصفح"
                        label = {"navigate": "فتح الرابط", "click": "النقر", "fill": "تعبئة الحقل",
                                 "select": "اختيار القيمة", "press": "الضغط على المفتاح",
                                 "scroll": "التمرير", "switch_tab": "تبديل التبويب", "wait": "الانتظار", "download": "تنزيل الملف", "upload": "رفع الملف"}[action.kind]
                        self.update(last_error={"action": action.kind, "category": category, "message": reason})
                        self.event(f"{reason} أثناء {label}.", "error")
                        if action.kind in {"navigate", "scroll", "switch_tab", "wait"}:
                            read_failures += 1
                            history.append({"controller_error": f"{action.kind} failed ({category}). Observe current state; do not assume success. Produce the best supported answer if enough data is already visible."})
                            if read_failures <= 2:
                                self.event("سأقرأ الحالة الحالية مجددًا لاستكمال الطلب بالبيانات المتاحة.")
                                continue
                            message = f"{reason} أثناء {label}. لم تتوفر نتيجة نهائية بعد. افحص أن الصفحة المطلوبة مفتوحة، ثم استأنف لقراءة البيانات المتاحة."
                        else:
                            message = f"{reason} أثناء {label}. لم يمكن تأكيد أثر هذه الخطوة؛ لن تتكرر تلقائيًا. افحص الصفحة ثم استأنف. يمكنك طلب الاكتفاء بالبيانات المعروضة في الملاحظة."
                            # A later finish needs fresh page evidence even if the user
                            # takes over.  A browser timeout does not erase the chance
                            # that the site acted after the controller lost certainty.
                            self.last_interactive_action_at = time.time()
                        await self._handoff(message, failure=True)
                        history.append({"action": action.to_dict(), "outcome": "Unknown; user inspected. Read site before deciding."})
                        continue
                    if attempt:
                        self.journal.settle(attempt, "returned")
                    read_failures = 0
                    if action.kind in {"click", "fill", "select", "press", "download", "upload"}:
                        self.last_interactive_action_at = time.time()
                    if not requires_approval:
                        self.update(automatic_steps=self.snapshot()["automatic_steps"] + 1)
                    description = {"navigate": "فتح الرابط", "wait": "انتظار تحميل الصفحة",
                                   "click": "النقر على العنصر المعتمد", "fill": "تعبئة الحقل المعتمد",
                                   "select": "اختيار القيمة المعتمدة", "press": "الضغط على المفتاح المعتمد",
                                   "scroll": "تمرير الصفحة", "switch_tab": "تبديل التبويب", "download": "حفظ الملف في مخرجات المهمة", "upload": "تسليم الملف المحدد لحقل الموقع"}
                    receipt = "بموافقتك" if requires_approval else "تلقائيًا ضمن التصفح"
                    label = "فتح عنصر التصفح" if action.kind == "click" and not requires_approval else description.get(action.kind, 'تنفيذ الخطوة')
                    self.event(f"تم {label} {receipt}؛ جارٍ التحقق من الصفحة.", "receipt")
                    history.append({"action": action.to_dict(), "outcome": "Browser action returned; verify site state."})
                self.update(status="limited", result="وصلت المهمة إلى الحد الأقصى للخطوات. راجع النتيجة قبل بدء طلب متابعة.")
        except RetentionExpired as error:
            self.update(status="cancelled", result=str(error))
            self.previous_context = None
        except asyncio.CancelledError:
            message = "أُوقفت المهمة لانتهاء إتاحة أحد مصادرها وفق سياسة الاحتفاظ؛ افحص أثر أي خطوة بدأت بالفعل." if self.retention_stopped else "أُوقفت المهمة. المتصفح متاح للفحص اليدوي."
            self.update(status="cancelled", result=message)
            self.event("أُوقفت المهمة؛ الإجراءات التي اكتملت سابقًا لم تُلغَ.")
        except TimeoutError:
            self.update(status="limited", result="انتهت مهلة المهمة أو المحرك. راجع المتصفح قبل المتابعة.")
        except Exception as error:
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else "تعذّر إكمال المهمة. تحقق من المتصفح وتثبيت Chromium."
            self.update(status="failed", result=message)
            self.event(message, "error")
        finally:
            self._revoke_operation()
            self.update(pending=None, finished_at=time.time())
            try:
                self._save_report()
            except OSError:
                self.event("تعذّر حفظ التقرير على القرص؛ النتيجة متاحة في الواجهة.", "error")

    def _save_report(self):
        self.reports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.reports.chmod(0o700)
        state = self.snapshot()
        if not state.get("id") or self.saved_reports.is_revoked(state["id"]):
            return
        # Do not persist prompts, page snapshots, filled form values or CLI stderr.
        report = {k: state.get(k) for k in ("id", "status", "provider", "step", "result", "events",
                                           "report", "completion", "sources", "started_at", "finished_at",
                                           "consent_mode", "automatic_steps", "approval_requests", "last_error",
                                           "operation_group", "operations", "authority_receipts")}
        path = self.reports / f'{state["id"]}.json'
        with path.open("x", encoding="utf-8") as file:
            path.chmod(0o600)
            json.dump(report, file, ensure_ascii=False, indent=2)

    async def close(self):
        if self.maintenance_job:
            self.maintenance_job.cancel()
            await asyncio.gather(self.maintenance_job, return_exceptions=True)
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.browser.close()
