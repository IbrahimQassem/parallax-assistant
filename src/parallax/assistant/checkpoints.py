"""User-selected continuation briefs; lifetime belongs to the source report.

No pending action, DOM identifier, prompt, page snapshot or approval is saved.
A durable claim prevents a stale resume request from launching twice.
"""
import json
import os
import re
import stat


class CheckpointConflict(ValueError):
    def __init__(self):
        super().__init__("تغيّرت نقطة المتابعة بعد عرضها. أعد فتح التقرير وراجع النص الحالي قبل الحفظ أو الاستئناف.")


class CheckpointStore:
    def __init__(self, reports):
        self.reports = reports

    def path(self, identifier):
        self.reports.path(identifier)
        folder = self.reports.root / ".checkpoints"
        if folder.is_symlink():
            raise ValueError("مجلد نقاط المتابعة غير صالح.")
        return folder / (identifier + ".json")

    @staticmethod
    def brief(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
            raise ValueError("اكتب نص المتابعة الذي تريد حفظه، بحد 4000 حرف.")
        # Reject common explicit credentials; free prose still needs user review.
        if re.search(r"-----BEGIN .*PRIVATE KEY|(?:password|token|secret|api[_ -]?key|otp|كلمة المرور|رمز التحقق)\s*[:=]\s*\S|https?://[^\s/]+@|[?&#](?:token|code|key|secret|password|access_token|refresh_token)=", value, re.I):
            raise ValueError("أزل كلمات المرور والرموز وروابط الاسترداد السرية من النص المحفوظ.")
        return value.strip()

    def load(self, identifier):
        self.reports.load(identifier)  # Revocation/expiry applies before reading the brief.
        path = self.path(identifier)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64000:
                raise ValueError("نقطة المتابعة غير صالحة.")
            raw = stream.read(64001)
        try:
            value = json.loads(raw)
            # Existing briefs remain readable; the first mutation writes a revision.
            legacy_fields = {"id", "brief", "group_id", "status", "consent_mode"}
            if isinstance(value, dict):
                if set(value) == legacy_fields:
                    value["revision"] = 1
                if set(value) == legacy_fields | {"revision"}:
                    value["continuation"] = None
            if (len(raw) > 64000 or not isinstance(value, dict)
                    or set(value) != legacy_fields | {"revision", "continuation"}
                    or value["id"] != identifier or value["status"] not in {"ready", "claimed"}
                    or value["consent_mode"] not in {"browse", "review"}
                    or type(value["revision"]) is not int or not 1 <= value["revision"] <= 2147483647):
                raise ValueError()
            self.reports.path(value["group_id"])
            value["brief"] = self.brief(value["brief"])
            self.validate_continuation(value["continuation"])
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("نقطة المتابعة غير صالحة.") from None

    def public(self, identifier):
        try:
            return self.load(identifier)
        except (OSError, ValueError):
            return None

    def validate_continuation(self, value):
        if value is None:
            return
        if (not isinstance(value, dict) or set(value) != {"task_id", "owner_pid", "owner_session"}
                or type(value["owner_pid"]) is not int or not 1 <= value["owner_pid"] <= 2147483647):
            raise ValueError("رابط محاولة المتابعة غير صالح.")
        self.reports.path(value["task_id"])
        self.reports.path(value["owner_session"])

    def save(self, identifier, brief, group_id, consent_mode):
        self.reports.load(identifier)
        self.reports.path(group_id)
        if consent_mode not in {"browse", "review"}:
            raise ValueError("وضع التدخل غير صالح.")
        value = {"id": identifier, "brief": self.brief(brief), "group_id": group_id,
                 "status": "ready", "consent_mode": consent_mode, "revision": 1, "continuation": None}
        path = self.path(identifier)
        if path.exists() or path.is_symlink():
            raise ValueError("توجد نقطة متابعة لهذه المهمة بالفعل.")
        self.reports._write_metadata(path, value)
        return value

    def version(self, identifier, revision):
        if type(revision) is not int or not 1 <= revision <= 2147483647:
            raise ValueError("يلزم رقم النسخة المعروضة لنقطة المتابعة.")
        value = self.load(identifier)
        if value["revision"] != revision:
            raise CheckpointConflict()
        return value

    def review(self, identifier, revision):
        value = self.version(identifier, revision)
        if value["status"] != "ready":
            raise ValueError("سبق بدء متابعة هذه النقطة؛ راجع المهمة والسجل قبل أي طلب جديد.")
        return value

    def update(self, identifier, brief, revision):
        brief = self.brief(brief)
        value = self.review(identifier, revision)
        if brief == value["brief"]:
            return value
        if revision == 2147483647:
            raise ValueError("وصلت نقطة المتابعة إلى حد التعديلات.")
        value = {**value, "brief": brief, "revision": revision + 1}
        self.reports._write_metadata(self.path(identifier), value)
        return value

    def claim(self, identifier, revision, continuation=None):
        value = self.review(identifier, revision)
        self.validate_continuation(continuation)
        value = {**value, "status": "claimed", "continuation": continuation}
        self.reports._write_metadata(self.path(identifier), value)
        return value

    def prepare_again(self, identifier, revision):
        value = self.version(identifier, revision)
        if value["status"] != "claimed" or value["continuation"] is None:
            raise ValueError("لا توجد محاولة مرتبطة يمكن إعادة تجهيز نقطتها.")
        if revision == 2147483647:
            raise ValueError("وصلت نقطة المتابعة إلى حد التعديلات.")
        value = {**value, "status": "ready", "revision": revision + 1}
        self.reports._write_metadata(self.path(identifier), value)
        return value

    def delete(self, identifier):
        self.path(identifier).unlink(missing_ok=True)
