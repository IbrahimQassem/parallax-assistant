"""One task at a time; approval decisions are single-use and bound to a page."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path

from .actions import needs_approval, approval_reason, web_url
from .planner import CliPlanner
from .results import COMPLETION_STATES, structured_result


class Assistant:
    def __init__(self, browser, reports: Path, *, max_steps=40, task_timeout=1800,
                 planner_factory=CliPlanner):
        self.browser = browser
        self.reports = reports
        self.max_steps = max_steps
        self.task_timeout = task_timeout
        self.planner_factory = planner_factory
        self.lock = threading.RLock()
        self.state = {"status": "idle", "events": [], "pending": None, "result": "", "page": "", "completion": None}
        self.job = None
        self.gate = None
        self.pause_requested = asyncio.Event()
        self.notes = []
        # Page text is evidence only while a task is running.  It is deliberately
        # not part of a saved report or a restored task.
        self.observations = {}
        self.last_interactive_action_at = None
        self._restore_report()

    def _restore_report(self):
        # Restore only the latest report, never silently replace a failed run with
        # an older success. Prompts and browser snapshots remain unpersisted.
        try:
            paths = [p for p in self.reports.glob("*.json")
                     if re.fullmatch(r"[0-9a-f]{32}\.json", p.name) and not p.is_symlink()]
            if not paths:
                return
            path = max(paths, key=lambda p: p.stat().st_mtime_ns)
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
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
        state["browser"] = {
            "mode": "chrome" if getattr(self.browser, "existing_chrome", False) else "isolated",
            "connected": getattr(self.browser, "context", None) is not None,
        }
        return state

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def event(self, message, kind="info"):
        with self.lock:
            self.state["events"].append({"time": time.time(), "kind": kind, "message": message})
            self.state["events"] = self.state["events"][-160:]

    async def start(self, task: str, provider: str, parent_id=None, consent_mode="browse"):
        if self.job and not self.job.done():
            raise ValueError("توجد مهمة قيد التنفيذ. أوقفها أو انتظر اكتمالها.")
        if not isinstance(task, str) or not task.strip() or len(task) > 12000:
            raise ValueError("اكتب طلبًا بين 1 و12000 حرف.")
        previous = self.snapshot()
        if consent_mode not in {"browse", "review"}:
            raise ValueError("وضع التدخل غير معروف.")
        history = []
        if parent_id is not None:
            if (not isinstance(parent_id, str) or parent_id != previous.get("id")
                    or previous["status"] not in COMPLETION_STATES):
                raise ValueError("المتابعة تخص آخر نتيجة مكتملة فقط.")
            history = [{"previous_task": previous["task"], "previous_answer": previous["result"],
                        "notice": "Prior answer is context only. Recheck facts in current pages."}]
        planner = self.planner_factory(provider)
        self.pause_requested.clear()
        self.notes = []
        self.update(id=uuid.uuid4().hex, task=task, provider=provider, status="running",
                    events=[], pending=None, result="", report=None, completion=None, sources=[], page="", step=0,
                    parent_id=parent_id, started_at=time.time(), finished_at=None)
        self.update(restored=False, phase="جارٍ الاتصال بالمتصفح", last_error=None,
                    consent_mode=consent_mode, automatic_steps=0, approval_requests=0)
        self.observations = {}
        self.last_interactive_action_at = None
        self.job = asyncio.create_task(self._run(task.strip(), planner, history))
        return self.snapshot()

    async def control(self, command, token="", note=""):
        if not self.job or self.job.done():
            raise ValueError("لا توجد مهمة نشطة.")
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError("الملاحظة طويلة جدًا.")
        if command == "stop":
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        elif command == "pause":
            self.pause_requested.set()
            if self.gate and not self.gate.done():
                self.gate.set_result(("pause", ""))
        elif command in {"approve", "reject", "resume"}:
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

    async def _wait(self, kind, detail):
        self.gate = asyncio.get_running_loop().create_future()
        self.update(status="waiting", pending={"type": kind, "token": uuid.uuid4().hex, **detail})
        try:
            return await self.gate
        finally:
            self.gate = None
            self.update(pending=None, status="running")

    async def _handoff(self, message, **detail):
        self.pause_requested.clear()
        decision, note = await self._wait("handoff", {"message": message, **detail})
        if decision in {"reject", "pause"}:
            raise asyncio.CancelledError
        if note:
            self.notes.append({"user_update": note})
        self.event("انتهى التدخل اليدوي؛ سأقرأ الصفحة مجددًا.")

    async def _plan(self, planner, task, snapshot, history):
        planning = asyncio.create_task(planner.propose(task, snapshot, history))
        paused = asyncio.create_task(self.pause_requested.wait())
        try:
            done, _ = await asyncio.wait({planning, paused}, return_when=asyncio.FIRST_COMPLETED)
            if paused in done:
                return None
            return await planning
        finally:
            for job in (planning, paused):
                if not job.done():
                    job.cancel()
            await asyncio.gather(planning, paused, return_exceptions=True)

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
        try:
            async with asyncio.timeout(self.task_timeout):
                await self.browser.start()
                self.event("المتصفح جاهز. يمكنك إيقاف المهمة أو تسلّم المتصفح في أي وقت.")
                for step in range(1, self.max_steps + 1):
                    self.update(step=step)
                    if self.pause_requested.is_set():
                        await self._handoff("المتصفح تحت تحكمك الآن. اضغط استئناف بعد الانتهاء.")
                    self.update(phase="جارٍ قراءة الصفحة")
                    snapshot = await self.browser.observe()
                    self._record_source(snapshot)
                    snapshot["observed_sources"] = self.snapshot()["sources"]
                    self.update(page=snapshot["url"])
                    self.event(f"قراءة الصفحة والتخطيط للخطوة {step}…")
                    self.update(phase="جارٍ تحليل البيانات وتحديد الخطوة التالية")
                    action = await self._plan(planner, task, snapshot, history + self.notes)
                    if action is None:
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
                    except ValueError as error:
                        history.append({"controller_error": str(error)})
                        self.event(str(error), "error")
                        continue
                    policy_reason = approval_reason(action, preview.get("target"), self.snapshot().get("consent_mode", "browse"))
                    requires_approval = policy_reason is not None
                    if requires_approval:
                        preview["approval_reason"] = policy_reason
                        self.update(approval_requests=self.snapshot()["approval_requests"] + 1)
                        decision, note = await self._wait("approval", preview)
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
                            self.notes.append({"user_update": note})
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
                    if self.pause_requested.is_set():
                        continue
                    try:
                        self.update(phase="جارٍ تنفيذ الخطوة والتحقق من نتيجتها")
                        await asyncio.wait_for(self.browser.execute(action), timeout=35)
                    except Exception as error:
                        # A timeout might occur AFTER a purchase/send succeeded. Never retry
                        # a consequential action implicitly when its outcome is unknown.
                        # Do not surface raw exception text: it can contain form values.
                        category = type(error).__name__
                        timed_out = isinstance(error, TimeoutError) or category == "TimeoutError"
                        reason = "انتهت مهلة تنفيذ الخطوة" if timed_out else "تعذّر تنفيذ الخطوة في المتصفح"
                        label = {"navigate": "فتح الرابط", "click": "النقر", "fill": "تعبئة الحقل",
                                 "select": "اختيار القيمة", "press": "الضغط على المفتاح",
                                 "scroll": "التمرير", "switch_tab": "تبديل التبويب", "wait": "الانتظار"}[action.kind]
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
                    read_failures = 0
                    if action.kind in {"click", "fill", "select", "press"}:
                        self.last_interactive_action_at = time.time()
                    if not requires_approval:
                        self.update(automatic_steps=self.snapshot()["automatic_steps"] + 1)
                    description = {"navigate": "فتح الرابط", "wait": "انتظار تحميل الصفحة",
                                   "click": "النقر على العنصر المعتمد", "fill": "تعبئة الحقل المعتمد",
                                   "select": "اختيار القيمة المعتمدة", "press": "الضغط على المفتاح المعتمد",
                                   "scroll": "تمرير الصفحة", "switch_tab": "تبديل التبويب"}
                    receipt = "بموافقتك" if requires_approval else "تلقائيًا ضمن التصفح"
                    label = "فتح عنصر التصفح" if action.kind == "click" and not requires_approval else description.get(action.kind, 'تنفيذ الخطوة')
                    self.event(f"تم {label} {receipt}؛ جارٍ التحقق من الصفحة.", "receipt")
                    history.append({"action": action.to_dict(), "outcome": "Browser action returned; verify site state."})
                self.update(status="limited", result="وصلت المهمة إلى الحد الأقصى للخطوات. راجع النتيجة قبل بدء طلب متابعة.")
        except asyncio.CancelledError:
            self.update(status="cancelled", result="أُوقفت المهمة. المتصفح متاح للفحص اليدوي.")
            self.event("أُوقفت المهمة؛ الإجراءات التي اكتملت سابقًا لم تُلغَ.")
        except TimeoutError:
            self.update(status="limited", result="انتهت مهلة المهمة أو المحرك. راجع المتصفح قبل المتابعة.")
        except Exception as error:
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else "تعذّر إكمال المهمة. تحقق من المتصفح وتثبيت Chromium."
            self.update(status="failed", result=message)
            self.event(message, "error")
        finally:
            self.update(pending=None, finished_at=time.time())
            try:
                self._save_report()
            except OSError:
                self.event("تعذّر حفظ التقرير على القرص؛ النتيجة متاحة في الواجهة.", "error")

    def _save_report(self):
        self.reports.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.reports.chmod(0o700)
        state = self.snapshot()
        # Do not persist prompts, page snapshots, filled form values or CLI stderr.
        report = {k: state.get(k) for k in ("id", "status", "provider", "step", "result", "events",
                                           "report", "completion", "sources", "started_at", "finished_at",
                                           "consent_mode", "automatic_steps", "approval_requests", "last_error")}
        path = self.reports / f'{state["id"]}.json'
        with path.open("x", encoding="utf-8") as file:
            path.chmod(0o600)
            json.dump(report, file, ensure_ascii=False, indent=2)

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.browser.close()
