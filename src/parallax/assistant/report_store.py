"""Bounded saved results and durable deletion markers, independent of execution."""
import heapq
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from .actions import web_url
from .results import COMPLETION_STATES


PAGE_SIZE = 10
MAX_REPORT_BYTES = 200000
TERMINAL_STATES = COMPLETION_STATES | {"cancelled", "failed", "limited"}


def text(value):
    if not isinstance(value, str):
        raise ValueError("بيانات التقرير غير صالحة.")
    return value


def rows(value):
    if not isinstance(value, list):
        raise ValueError("بيانات التقرير غير صالحة.")
    return value


def timestamp(value):
    if value is not None and (type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 253402300799):
        raise ValueError("توقيت التقرير غير صالح.")
    return value


class ReportStore:
    def __init__(self, root):
        self.root = Path(root)
        self.clock = time.time

    def path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier) or self.root.is_symlink():
            raise ValueError("معرّف التقرير غير صالح.")
        return self.root / (identifier + ".json")

    def deletion_path(self, identifier):
        self.path(identifier)
        folder = self.root / ".deleted"
        if folder.is_symlink():
            raise ValueError("سجل الحذف غير صالح.")
        return folder / (identifier + ".json")

    def retention_path(self, identifier):
        self.path(identifier)
        folder = self.root / ".retention"
        if folder.is_symlink():
            raise ValueError("سجل مدة الاحتفاظ غير صالح.")
        return folder / (identifier + ".json")

    def retention(self, identifier):
        path = self.retention_path(identifier)
        if not path.exists() and not path.is_symlink():
            return {"days": None, "expires_at": None}
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError()
            value = json.loads(path.read_text())
            if (set(value) != {"id", "days", "expires_at"} or value["id"] != identifier
                    or type(value["days"]) is not int or value["days"] not in {1, 7, 30, 90}
                    or type(value["expires_at"]) not in {float, int} or timestamp(value["expires_at"]) is None):
                raise ValueError()
            return {key: value[key] for key in ("days", "expires_at")}
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError, RecursionError):
            raise ValueError("تعذّر التحقق من مدة الاحتفاظ؛ المحتوى محجوب حتى معالجة السياسة أو حذفه.") from None

    def is_revoked(self, identifier):
        if self.is_deleted(identifier):
            return True
        policy = self.retention(identifier)
        if policy["expires_at"] is None or self.clock() < policy["expires_at"]:
            return False
        # Persist revocation on first observation of expiry, even before the
        # cleaner runs. A later clock rollback cannot revive the content.
        try:
            self.mark_deleted(identifier)
        except OSError:
            pass
        return True

    def set_retention(self, identifier, days):
        if days is not None and (type(days) is not int or days not in {1, 7, 30, 90}):
            raise ValueError("مدة الاحتفاظ غير معروفة.")
        self.load(identifier)  # Expired/deleted content cannot be revived by extending its policy.
        if self.is_revoked(identifier):
            raise ValueError("انتهت مدة الاحتفاظ بالفعل.")
        path = self.retention_path(identifier)
        if days is None:
            path.unlink(missing_ok=True)
        else:
            self._write_metadata(path, {"id": identifier, "days": days, "expires_at": self.clock() + days * 86400})
        return self.retention(identifier)

    def expire_due(self):
        folder = self.retention_path("0" * 32).parent
        for path in folder.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            try:
                self.is_revoked(path.stem)
            except (OSError, ValueError):
                # An unreadable policy blocks access but is not authority to erase.
                continue

    def is_deleted(self, identifier):
        path = self.deletion_path(identifier)
        return path.exists() or path.is_symlink()

    def deletion(self, identifier):
        path = self.deletion_path(identifier)
        try:
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError()
            value = json.loads(path.read_text())
            if (set(value) != {"id", "pending", "report_mtime_ns"} or value.get("id") != identifier or type(value.get("pending")) is not bool
                    or type(value.get("report_mtime_ns")) is not int):
                raise ValueError()
            return value
        except (OSError, ValueError, AttributeError, TypeError):
            # A partial/corrupt marker still revokes access. Never restore an older
            # success merely because the latest deletion's timestamp was lost.
            try:
                modified = path.lstat().st_mtime_ns
            except OSError:
                modified = time.time_ns()
            return {"id": identifier, "pending": True, "report_mtime_ns": modified}

    def deletions(self):
        folder = self.deletion_path("0" * 32).parent
        for path in folder.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{32}", path.stem):
                yield self.deletion(path.stem)

    def _write_deletion(self, marker):
        path = self.deletion_path(marker["id"])
        self._write_metadata(path, marker)

    def _write_metadata(self, path, value):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            for directory in (path.parent, self.root):
                directory_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def mark_deleted(self, identifier):
        if self.is_deleted(identifier):
            self._write_deletion({**self.deletion(identifier), "pending": True})
            return
        info = self.path(identifier).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("التقرير غير صالح للحذف.")
        self._write_deletion({"id": identifier, "pending": True, "report_mtime_ns": info.st_mtime_ns})

    def finish_deletion(self, identifier):
        if not self.is_deleted(identifier):
            raise ValueError("لم يُطلب حذف هذا التقرير.")
        self.path(identifier).unlink(missing_ok=True)
        self.retention_path(identifier).unlink(missing_ok=True)
        self._write_deletion({**self.deletion(identifier), "pending": False})

    def entries(self, *, include_deleted=False):
        if self.root.is_symlink():
            raise ValueError("مجلد التقارير غير صالح.")
        for path in self.root.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue
            if self.is_deleted(path.stem):
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    yield info.st_mtime_ns, path.stem
            except OSError:
                continue
        for marker in self.deletions():
            if include_deleted or marker["pending"]:
                yield marker["report_mtime_ns"], marker["id"]

    def latest_id(self):
        latest = min(self.entries(include_deleted=True), key=lambda row: (-row[0], row[1]), default=None)
        return latest[1] if latest and not self.is_revoked(latest[1]) else None

    def load(self, identifier):
        path = self.path(identifier)
        if self.is_revoked(identifier):
            raise ValueError("حُذفت هذه النتيجة.")
        # O_NONBLOCK avoids waiting on a replaced FIFO; fstat checks the opened file.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
                raise ValueError("التقرير غير متاح أو تجاوز الحجم المسموح.")
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("تجاوز التقرير الحجم المسموح.")
        try:
            saved = json.loads(raw)
            if (not isinstance(saved, dict) or saved.get("status") not in TERMINAL_STATES
                    or saved.get("id", identifier) != identifier):
                raise ValueError("بيانات التقرير غير صالحة.")
            sources = [{"id": text(s["id"]), "title": text(s["title"]), "url": web_url(s["url"]),
                        "observed_at": timestamp(s["observed_at"])} for s in rows(saved.get("sources", []))]
            completion = saved.get("completion")
            if saved["status"] not in COMPLETION_STATES or not isinstance(completion, dict) or completion.get("status") not in COMPLETION_STATES:
                completion = {"status": "unverified", "done": [], "remaining": [], "evidence": [],
                              "reason": "نتيجة محفوظة؛ لا يوجد سجل تحقق مكتمل لهذه المهمة."}
            else:
                completion = {"status": completion["status"], "reason": text(completion.get("reason", "")),
                    "done": [text(v) for v in rows(completion.get("done", []))],
                    "remaining": [text(v) for v in rows(completion.get("remaining", []))],
                    "evidence": [{"source_id": text(e["source_id"]), "claim": text(e["claim"]),
                                  "observed_after_action": e.get("observed_after_action") is True}
                                 for e in rows(completion.get("evidence", []))],
                    **{key: [{k: text(v) for k, v in row.items() if k in {"id", "file_id", "status", "source_id"} and v is not None}
                             for row in rows(completion[key])]
                       for key in ("effect_checks", "operation_checks", "file_checks", "upload_checks") if key in completion}}
            report = saved.get("report")
            if report is not None:
                report = {"summary": text(report["summary"]), "scope": text(report.get("scope", "")),
                    "work_done": text(report.get("work_done", "")), "completion": completion,
                    "findings": [{"title": text(f["title"]), "detail": text(f["detail"]),
                        "metrics": [{"label": text(m["label"]), "value": text(m["value"])} for m in rows(f.get("metrics", []))],
                        "source_ids": [text(v) for v in rows(f.get("source_ids", []))]} for f in rows(report.get("findings", []))],
                    "limitations": [text(v) for v in rows(report.get("limitations", []))],
                    "followups": [text(v) for v in rows(report.get("followups", []))]}
            return {"id": identifier, "status": completion["status"] if saved["status"] in COMPLETION_STATES else saved["status"],
                    "result": text(saved["result"]), "report": report, "completion": completion, "sources": sources,
                    "started_at": timestamp(saved.get("started_at")), "finished_at": timestamp(saved.get("finished_at")),
                    "recorded_at": info.st_mtime, "retention": self.retention(identifier)}
        except (KeyError, TypeError, AttributeError, UnicodeError, OverflowError, RecursionError) as error:
            raise ValueError("بيانات التقرير غير صالحة.") from error

    def page(self, cursor=""):
        before = None
        if cursor:
            if not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,20}:[0-9a-f]{32}", cursor):
                raise ValueError("موضع السجل غير صالح.")
            modified, identifier = cursor.split(":")
            before = (-int(modified), identifier)
        candidates = (entry for entry in self.entries() if before is None or (-entry[0], entry[1]) > before)
        selected = heapq.nsmallest(PAGE_SIZE + 1, candidates, key=lambda entry: (-entry[0], entry[1]))
        items = []
        for modified, identifier in selected[:PAGE_SIZE]:
            item = {"id": identifier, "recorded_at": modified / 1e9, "available": False,
                    "status": "unavailable", "summary": "تعذّر قراءة هذا التقرير المحفوظ."}
            if self.is_deleted(identifier):
                item.update(status="deletion_pending", summary="حُجب التقرير؛ لم يكتمل حذف ملفاته المحلية.")
                items.append(item)
                continue
            try:
                report = self.load(identifier)
                item.update(available=True, status=report["status"],
                            summary=(report["report"]["summary"] if report["report"] else report["result"])[:180],
                            retention=report["retention"])
            except (OSError, ValueError):
                pass
            items.append(item)
        next_cursor = f"{selected[PAGE_SIZE - 1][0]}:{selected[PAGE_SIZE - 1][1]}" if len(selected) > PAGE_SIZE else None
        return {"items": items, "next_cursor": next_cursor}
