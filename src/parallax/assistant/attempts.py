"""Private write-ahead receipts for browser interactions with possible effects.

Only opaque identities, keyed fingerprints, origins and outcomes are persisted.
Task text, element labels, full URLs and field values never enter the database.
This prevents local replay; it does not promise exactly-once delivery by a site.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from urllib.parse import urlsplit
import uuid

from .actions import Action, web_url


UNCERTAIN = {"attempting", "unknown"}
PUBLIC_FIELDS = "id, task_id, group_id, kind, origin, status, created_at, updated_at, authority_id"


class AttemptBlocked(RuntimeError):
    def __init__(self, receipt, *, duplicate=False):
        self.receipt = receipt
        self.duplicate = duplicate
        super().__init__("هذه الخطوة سبق إرسالها ضمن المهمة؛ افحص أثرها قبل أي تكرار." if duplicate else
                         "توجد محاولة على هذه الوجهة لم يتأكد أثرها؛ افحصها قبل تنفيذ خطوة أخرى.")


class AttemptJournal:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "operations.sqlite3"
        self.key_path = directory / "operations.key"

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("معرّف العملية غير صالح.")
        return value

    def _key(self):
        if self.path.exists() and not self.key_path.exists():
            raise ValueError("هوية سجل العمليات مفقودة؛ لا يمكن إنشاء هوية بديلة فوق سجل سابق.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.key_path, flags, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        if self.key_path.is_symlink() or not self.key_path.is_file():
            raise ValueError("ملف هوية سجل العمليات غير صالح.")
        self.key_path.chmod(0o600)
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("ملف هوية سجل العمليات غير صالح.")
        return key

    @contextmanager
    def _connection(self):
        connection = None
        try:
            self._key()
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            if self.path.is_symlink() or not stat.S_ISREG(self.path.stat().st_mode):
                raise ValueError("ملف سجل العمليات غير صالح.")
            self.path.chmod(0o600)
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, group_id TEXT NOT NULL,
                resolved_group_id TEXT, fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
                owner_pid INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, authority_id TEXT
            )""")
            if "authority_id" not in {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if "authority_id" not in {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}:
                        connection.execute("ALTER TABLE attempts ADD COLUMN authority_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS attempts_fingerprint ON attempts(fingerprint, group_id)")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS attempts_uncertain_origin ON attempts(origin)
                WHERE status IN ('attempting', 'unknown')""")
            yield connection
        except (OSError, sqlite3.Error):
            raise RuntimeError("تعذّر الوصول إلى سجل العمليات؛ لم يُسمح بإرسال خطوة جديدة.") from None
        finally:
            if connection is not None:
                connection.close()

    def identity(self, action: Action, page: str, target: dict | None, form_state=None):
        parsed = urlsplit(web_url(page))
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        target = target or {}
        # Observed numeric element IDs and page fingerprints change after a
        # rerender; neither is a durable identity for a possibly repeated effect.
        descriptor = {key: target.get(key, "") for key in ("tag", "type", "role", "label", "frame", "href")}
        value = json.dumps({"kind": action.kind, "value": action.value, "page": page,
                            "target": descriptor, "form_state": form_state or []},
                           sort_keys=True, ensure_ascii=False).encode()
        digest = hmac.new(self._key(), value, hashlib.sha256).hexdigest()
        return origin, digest

    @staticmethod
    def _blocked(connection, origin, fingerprint, group_id):
        row = connection.execute(f"""SELECT {PUBLIC_FIELDS} FROM attempts
            WHERE origin=? AND status IN ('attempting','unknown') LIMIT 1""", (origin,)).fetchone()
        if row is not None:
            raise AttemptBlocked(dict(row))
        row = connection.execute(f"""SELECT {PUBLIC_FIELDS} FROM attempts
            WHERE fingerprint=? AND (group_id=? OR resolved_group_id=?)
            AND status IN ('returned','confirmed_by_user') LIMIT 1""",
            (fingerprint, group_id, group_id)).fetchone()
        if row is not None:
            raise AttemptBlocked(dict(row), duplicate=True)

    def check(self, action, page, target, group_id, *, form_state=None):
        if not self.path.exists():
            return
        origin, fingerprint = self.identity(action, page, target, form_state)
        with self._connection() as connection:
            self._blocked(connection, origin, fingerprint, self._identifier(group_id))

    def begin(self, action, page, target, task_id, group_id, *, form_state=None, authority_id=None):
        if action.kind not in {"click", "fill", "select", "press", "download", "upload"}:
            raise ValueError("نوع الخطوة لا يحتاج سجل إرسال.")
        task_id, group_id = self._identifier(task_id), self._identifier(group_id)
        if authority_id is not None:
            self._identifier(authority_id)
        origin, fingerprint = self.identity(action, page, target, form_state)
        identifier, now = uuid.uuid4().hex, time.time()
        with self._connection() as connection:
            with connection:
                # Serialization covers the check and reservation together.
                connection.execute("BEGIN IMMEDIATE")
                self._blocked(connection, origin, fingerprint, group_id)
                connection.execute("""INSERT INTO attempts
                    (id,task_id,group_id,fingerprint,kind,origin,status,owner_pid,created_at,updated_at,authority_id)
                    VALUES (?,?,?,?,?,?,'attempting',?,?,?,?)""",
                    (identifier, task_id, group_id, fingerprint, action.kind, origin, os.getpid(), now, now, authority_id))
        return identifier

    def settle(self, identifier, status):
        if status not in {"returned", "unknown"}:
            raise ValueError("حالة محاولة التنفيذ غير صالحة.")
        with self._connection() as connection:
            with connection:
                changed = connection.execute("UPDATE attempts SET status=?,updated_at=? WHERE id=? AND status='attempting'",
                    (status, time.time(), self._identifier(identifier))).rowcount
                if changed != 1:
                    raise ValueError("انتهت محاولة التنفيذ أو تغيرت حالتها.")

    def uncertain(self):
        if not self.path.exists():
            return []
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(f"""SELECT {PUBLIC_FIELDS} FROM attempts
                WHERE status IN ('attempting','unknown') ORDER BY created_at LIMIT 100""")]

    def receipts(self, group_id):
        if not self.path.exists():
            return []
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(f"""SELECT {PUBLIC_FIELDS} FROM attempts
                WHERE group_id=? OR resolved_group_id=? ORDER BY created_at LIMIT 200""", (group_id, group_id))]

    def has_uncertain(self, group_id):
        if not self.path.exists():
            return False
        with self._connection() as connection:
            return connection.execute("""SELECT 1 FROM attempts
                WHERE (group_id=? OR resolved_group_id=?) AND status IN ('attempting','unknown') LIMIT 1""",
                (group_id, group_id)).fetchone() is not None

    def resolve(self, identifier, *, occurred, group_id=None):
        self._identifier(identifier)
        if group_id is not None:
            self._identifier(group_id)
        if type(occurred) is not bool:
            raise ValueError("يلزم تحديد نتيجة الفحص اليدوي.")
        with self._connection() as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT status,owner_pid FROM attempts WHERE id=?", (identifier,)).fetchone()
                if row is None or row["status"] not in UNCERTAIN:
                    raise ValueError("تغيرت حالة المحاولة؛ حدّث القائمة قبل تسجيل الفحص.")
                if row["status"] == "attempting":
                    try:
                        os.kill(row["owner_pid"], 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        raise ValueError("قد يكون الإجراء قيد التنفيذ في عملية أخرى؛ انتظر انتهاءها.") from None
                    else:
                        raise ValueError("قد يكون الإجراء قيد التنفيذ؛ أوقف المهمة أو انتظر انتهاءها قبل تسجيل الفحص.")
                connection.execute("UPDATE attempts SET status=?,resolved_group_id=?,updated_at=? WHERE id=?",
                    ("confirmed_by_user" if occurred else "not_applied_by_user", group_id, time.time(), identifier))
