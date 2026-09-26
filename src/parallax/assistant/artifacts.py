"""Private, bounded downloaded outputs; model text is never a filesystem path."""
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
import uuid


MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 5
PUBLIC_FIELDS = ("id", "task_id", "name", "size", "sha256", "created_at", "source_kind", "available")
VERSION_FIELDS = ("name", "size", "sha256")


def upload_mime(name, accept):
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    choices = [item.strip().lower() for item in accept.split(",") if item.strip()]
    if choices and not any((item.startswith(".") and name.lower().endswith(item))
            or item == mime or (item.endswith("/*") and mime.startswith(item[:-1])) for item in choices):
        raise ValueError("نوع الملف لا يطابق الأنواع المعلنة في حقل الموقع.")
    return mime


def safe_name(value):
    value = str(value).replace("\\", "/").split("/")[-1]
    value = "".join(c for c in value if unicodedata.category(c)[0] != "C" and c not in '<>:"|?*')
    value = value.strip(" .")[:120].strip(" .")
    return value or "download.bin"


class ArtifactStore:
    def __init__(self, root, *, is_deleted=None):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.is_deleted = is_deleted or (lambda _identifier: False)

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("معرّف الملف غير صالح.")
        return value

    def directory(self, task_id, *, create=False, allow_deleted=False):
        folder = self.root / self.identifier(task_id)
        if not allow_deleted and self.is_deleted(task_id):
            raise ValueError("حُذفت ملفات هذه المهمة.")
        if self.root.is_symlink() or folder.is_symlink():
            raise ValueError("مجلد المخرجات غير صالح.")
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            folder.mkdir(exist_ok=True, mode=0o700)
            folder.chmod(0o700)
        return folder

    def list(self, task_id):
        if task_id is None:
            return []
        if self.is_deleted(task_id):
            return []
        folder = self.directory(task_id)
        result = []
        with self.lock:
            for path in sorted(folder.glob("*.json")):
                try:
                    row = self._metadata(task_id, path.stem)
                    try:
                        payload = self._payload(row)
                        row["available"] = not payload.is_symlink() and payload.is_file() and payload.stat().st_size == row["size"]
                    except (OSError, ValueError):
                        row["available"] = False
                    result.append({key: row[key] for key in PUBLIC_FIELDS})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return result

    def _metadata(self, task_id, identifier):
        path = self.directory(task_id) / (self.identifier(identifier) + ".json")
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError("بيانات الملف غير صالحة.")
        row = json.loads(path.read_text())
        if not isinstance(row, dict):
            raise ValueError("بيانات الملف غير صالحة.")
        row.setdefault("source_kind", "download")
        if (row.get("id") != identifier or row.get("task_id") != task_id
                or not isinstance(row.get("name"), str) or row["name"] != safe_name(row["name"])
                or type(row.get("size")) is not int or not 0 <= row["size"] <= MAX_BYTES
                or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or type(row.get("created_at")) not in {int, float}
                or row.get("source_kind") not in ("download", "user_selected", "inherited")):
            raise ValueError("بيانات الملف غير صالحة.")
        if row["source_kind"] == "inherited":
            source = row.get("source")
            if not isinstance(source, dict) or set(source) != {"task_id", "id"}:
                raise ValueError("مرجع الملف غير صالح.")
            self.identifier(source["task_id"])
            self.identifier(source["id"])
            if source["task_id"] == task_id:
                raise ValueError("مرجع الملف غير صالح.")
        return row

    def _payload(self, row):
        owner = row
        if row["source_kind"] == "inherited":
            try:
                owner = self._metadata(row["source"]["task_id"], row["source"]["id"])
            except (OSError, ValueError):
                raise ValueError("أصل الملف غير متاح.") from None
            # References always point directly to bytes, never another reference.
            if owner["source_kind"] == "inherited" or any(owner[key] != row[key] for key in VERSION_FIELDS):
                raise ValueError("تغير أصل الملف؛ المرجع يخص النسخة السابقة فقط.")
        return self.directory(owner["task_id"]) / (owner["id"] + ".data")

    def inherit(self, task_id, source):
        """Create a task-local ID for one pinned version; never copy file bytes."""
        with self.lock:
            if task_id == source["task_id"]:
                raise ValueError("اختر مهمة متابعة مختلفة.")
            self.check_capacity(task_id)
            original = self._metadata(source["task_id"], source["id"])
            if any(original[key] != source[key] for key in VERSION_FIELDS):
                raise ValueError("تغير الملف السابق قبل بدء المتابعة.")
            payload = self._payload(original)
            if payload.is_symlink() or not payload.is_file() or payload.stat().st_size != original["size"]:
                raise ValueError("أصل الملف غير متاح.")
            owner = original["source"] if original["source_kind"] == "inherited" else {key: original[key] for key in ("task_id", "id")}
            folder = self.directory(task_id, create=True)
            identifier = uuid.uuid4().hex
            data_path = folder / (identifier + ".data")
            if data_path.exists() or data_path.is_symlink():
                raise FileExistsError("معرّف الملف مستخدم.")
            row = {"id": identifier, "task_id": task_id, **{key: original[key] for key in VERSION_FIELDS},
                   "created_at": time.time(), "source_kind": "inherited", "source": dict(owner)}
            path = folder / (identifier + ".json")
            created = False
            try:
                with path.open("x") as stream:
                    created = True
                    path.chmod(0o600)
                    json.dump(row, stream, ensure_ascii=False)
            except BaseException:
                if created:
                    path.unlink(missing_ok=True)
                raise
            return {key: value for key, value in {**row, "available": True}.items() if key in PUBLIC_FIELDS}

    def check_capacity(self, task_id):
        if len(self.list(task_id)) >= MAX_FILES:
            raise ValueError("وصلت المهمة إلى خمسة ملفات؛ أزل ملفًا أو مرجعًا من المهمة قبل إضافة آخر.")

    @contextmanager
    def staging(self, task_id):
        folder = self.directory(task_id, create=True)
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=folder)
        os.close(fd)
        try:
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)

    def commit(self, task_id, stage, name, *, source_kind="download"):
        if source_kind not in {"download", "user_selected"}:
            raise ValueError("مصدر الملف غير صالح.")
        with self.lock:
            self.check_capacity(task_id)
            folder = self.directory(task_id, create=True)
            if stage.parent != folder or stage.is_symlink() or not stage.name.startswith(".pending-"):
                raise ValueError("ملف التنزيل المؤقت غير صالح.")
            if stage.stat().st_size > MAX_BYTES:
                raise ValueError("تجاوز الملف حد 20 ميغابايت.")
            data = stage.read_bytes()
            if len(data) > MAX_BYTES:
                raise ValueError("تجاوز الملف حد 20 ميغابايت.")
            identifier = uuid.uuid4().hex
            payload, metadata = folder / (identifier + ".data"), folder / (identifier + ".json")
            row = {"id": identifier, "task_id": task_id, "name": safe_name(name), "size": len(data),
                   "sha256": hashlib.sha256(data).hexdigest(), "created_at": time.time(), "source_kind": source_kind}
            created = []
            try:
                with payload.open("xb") as stream:
                    created.append(payload)
                    payload.chmod(0o600)
                    stream.write(data)
                with metadata.open("x") as stream:
                    created.append(metadata)
                    metadata.chmod(0o600)
                    json.dump(row, stream, ensure_ascii=False)
            except BaseException:
                for path in created:
                    path.unlink(missing_ok=True)
                raise
            return {**row, "available": True}

    def read(self, task_id, identifier):
        self.identifier(identifier)
        with self.lock:
            row = next((row for row in self.list(task_id) if row["id"] == identifier), None)
            if not row or not row["available"]:
                raise ValueError("الملف غير متاح.")
            path = self._payload(self._metadata(task_id, identifier))
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("تغير محتوى الملف؛ لا يمكن تسليم نسخة غير مطابقة.")
            return row, data

    def delete(self, task_id, identifier):
        self.identifier(identifier)
        with self.lock:
            folder = self.directory(task_id)
            # Fixed opaque paths only; unlink never follows a payload symlink.
            (folder / (identifier + ".json")).unlink(missing_ok=True)
            (folder / (identifier + ".data")).unlink(missing_ok=True)

    def delete_task(self, task_id):
        """Remove owned local bytes/manifests only; never follow inherited sources."""
        with self.lock:
            folder = self.directory(task_id, allow_deleted=True)
            if not folder.exists():
                return
            for path in folder.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}\.(json|data)", path.name) or path.name.startswith(".pending-"):
                    path.unlink(missing_ok=True)
            # An unexpected directory/file is not recursively erased. Keep the
            # deletion pending so the user is not told that cleanup succeeded.
            folder.rmdir()

    def source_tasks(self, task_id):
        """Original owners of references, including owners no longer available."""
        if task_id is None:
            return set()
        with self.lock:
            owners = set()
            for path in self.directory(task_id).glob("*.json"):
                row = self._metadata(task_id, path.stem)
                if row["source_kind"] == "inherited":
                    owners.add(row["source"]["task_id"])
            return owners
