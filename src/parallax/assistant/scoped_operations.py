"""One reviewed form operation, with exact values and no standing authority."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from .actions import Action, web_url
from .effect_checks import normalise


def descriptor(element):
    return tuple(element.get(key, "") for key in ("tag", "type", "role", "name", "label", "frame"))


def form_descriptor(form):
    return {key: value for key, value in form.items() if key not in {"ref", "dom_id"}}


def individual_review(element):
    label = unicodedata.normalize("NFKC", " ".join(str(element.get(key, "")) for key in ("label", "name", "type"))).casefold()
    label = "".join(c for c in label if unicodedata.category(c) not in {"Mn", "Cf"})
    return bool(element.get("sensitive") or re.search(
        r"\b(delete|remove|erase|reset|pay|payment|purchase|buy|checkout|transfer|bank|card|amount|password|security|owner|permission|recovery|publish|send)\b|"
        r"حذف|ازال|مسح|دفع|شراء|تحويل|مصرف|بنك|مبلغ|كلمة.?مرور|امان|ملكي|صلاح|استرداد|نشر|ارسال", label))


class ScopedOperation:
    """Transient exact-value delegation. A parsed proposal is not yet approved."""
    def __init__(self, raw, snapshot, preview):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"description", "identity", "steps"}:
                raise ValueError()
            if not isinstance(data["description"], str) or not 1 <= len(data["description"].strip()) <= 600:
                raise ValueError()
            identity = data["identity"]
            if not isinstance(identity, dict) or not 1 <= len(identity) <= 6:
                raise ValueError()
            if any(not isinstance(k, str) or not isinstance(v, str) or not k.strip() or not v.strip()
                   or len(k) > 100 or len(v) > 600 for k, v in identity.items()):
                raise ValueError()
            self.identity = {normalise(k): normalise(v) for k, v in identity.items()}
            if len(self.identity) != len(identity):
                raise ValueError()
            if not isinstance(data["steps"], list) or not 2 <= len(data["steps"]) <= 8:
                raise ValueError()
            self.steps = [Action.parse(json.dumps(step)) for step in data["steps"]]
            if self.steps[-1].kind != "click" or any(step.kind not in {"fill", "select"} for step in self.steps[:-1]):
                raise ValueError()
            self.page = web_url(snapshot["url"])
            self.description = data["description"].strip()
            targets = [preview(step, snapshot)["target"] for step in self.steps[:-1]]
            targets.append(next((element for element in snapshot["elements"] if element["id"] == self.steps[-1].target), None))
            if any(not target or target.get("frame") != self.page or not target.get("form") for target in targets):
                raise ValueError()
            form = targets[0]["form"]
            destination, page = urlsplit(web_url(form["action"])), urlsplit(self.page)
            if ((destination.scheme, destination.netloc) != (page.scheme, page.netloc)
                    or form.get("credentialed") or form.get("method") not in {"get", "post"}):
                raise ValueError()
            self.form = form_descriptor(form)
            if any(target["form"] != form for target in targets):
                raise ValueError()
            if targets[-1].get("tag") != "button" or targets[-1].get("type") != "submit":
                raise ValueError()
            if self.steps[-1].value or normalise(targets[-1].get("label", "")).casefold() not in {
                "save", "save changes", "update", "update profile", "apply",
                "حفظ", "حفظ التغييرات", "تحديث", "تحديث الملف", "تطبيق",
            }:
                raise ValueError()
            for step, target in zip(self.steps[:-1], targets[:-1]):
                if step.kind == "fill" and not (target.get("tag") == "textarea" or
                    (target.get("tag") == "input" and target.get("type") in {"text", "search", "email", "url", "tel", "number", "date", "time"})):
                    raise ValueError()
                if step.kind == "select" and (target.get("tag") != "select" or target.get("multiple")):
                    raise ValueError()
                if step.kind == "select" and any(individual_review({"label": f"{option['label']} {option['value']}"})
                        for option in target.get("options", []) if option["value"] == step.value):
                    raise ValueError()
            self.bindings = [descriptor(target) for target in targets]
            if len(set(self.bindings)) != len(self.bindings):
                raise ValueError()
            self.controls = self._controls(snapshot)
            if any(individual_review(element) for element in self.controls.values()):
                raise ValueError()
            if not all(element.get("state_hash") for element in self.controls.values()):
                raise ValueError()
            self.hashes = {key: element["state_hash"] for key, element in self.controls.items()}
            self.options = {descriptor(target): copy.deepcopy(target.get("options", [])) for target in targets if target.get("tag") == "select"}
            self.id = uuid.uuid4().hex
            self.position = 0
            self.approved_at = None
            self.expires_at = None
            self.deadline = None
            self.status = "proposed"
            self.basis = "user_operation_preview"
            self.context_revision = None
            self._check_identity(snapshot)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ValueError("العملية غير مؤهلة لاعتماد جماعي: يلزم نموذج واحد وهوية ظاهرة وقيم محددة وزر حفظ، دون حقول حساسة أو خطوات تحتاج مراجعة منفردة.") from None

    def _check_identity(self, snapshot):
        matches = [record for record in snapshot.get("evidence_records", [])
                   if record.get("main") is True and record.get("frame") == self.page
                   and all(normalise(record.get("fields", {}).get(key, "")) == value for key, value in self.identity.items())]
        if len(matches) != 1:
            raise ValueError("تغيرت هوية الوجهة أو أصبحت ملتبسة؛ أُلغي تفويض العملية.")

    def _controls(self, snapshot):
        if snapshot.get("url") != self.page:
            raise ValueError("تغيرت صفحة العملية؛ يلزم تقييم جديد.")
        controls = [element for element in snapshot.get("elements", [])
                    if element.get("frame") == self.page and element.get("form")
                    and form_descriptor(element["form"]) == self.form]
        if len(controls) != self.form.get("controls") or len({element["form"]["ref"] for element in controls}) != 1:
            raise ValueError("تغير النموذج أو أصبحت وجهته ملتبسة.")
        mapped = {descriptor(element): element for element in controls}
        if len(mapped) != len(controls):
            raise ValueError("لا يمكن تمييز حقول النموذج بثقة.")
        return mapped

    def validate_expectation(self, expectation):
        """Bind the result check to this identity and every reviewed field value."""
        subject, outcome = expectation.get("subject"), expectation.get("outcome")
        if (not isinstance(subject, dict) or not isinstance(outcome, dict)
                or any(subject.get(key) != value for key, value in self.identity.items())):
            raise ValueError("شرط التحقق لا يحدد هوية العملية المعروضة؛ يلزم شرط حقول للهوية نفسها.")
        labels = set()
        for label, allowed in self.result_fields():
            if (not label or label in labels or outcome.get(label) not in allowed
                    or label in expectation.get("casefold_outcome", [])):
                raise ValueError("شرط التحقق لا يغطي كل القيم المعتمدة؛ يلزم ربط كل حقل بقيمته أو تسمية خياره المحدد.")
            labels.add(label)

    def result_fields(self):
        fields = []
        for step, key in zip(self.steps[:-1], self.bindings[:-1]):
            values = {normalise(step.value)}
            if step.kind == "select":
                values.update(normalise(option["label"]) for option in self.options[key]
                              if option["value"] == step.value)
            fields.append((normalise(self.controls[key].get("label", "")), sorted(values)))
        return fields

    def approve(self, *, basis="user_operation_preview"):
        if self.status != "proposed":
            raise ValueError("انتهى طلب اعتماد العملية.")
        if basis not in {"user_operation_preview", "user_explicit_request"}:
            raise ValueError("مصدر تفويض العملية غير صالح.")
        self.basis = basis
        self.approved_at = time.time()
        self.expires_at = self.approved_at + 300
        self.deadline = time.monotonic() + 300
        self.status = "active"

    def next_action(self, snapshot):
        if self.status == "active" and time.monotonic() >= self.deadline:
            self.status = "expired"
        if self.status != "active":
            raise ValueError("انتهت صلاحية تفويض العملية.")
        self._check_identity(snapshot)
        controls = self._controls(snapshot)
        if controls.keys() != self.hashes.keys() or any(element.get("state_hash") != self.hashes[key] for key, element in controls.items()):
            raise ValueError("تغيرت قيم النموذج خارج الخطوات المعتمدة؛ أُلغي تفويض العملية.")
        if any(individual_review(element) for element in controls.values()) or controls[self.bindings[self.position]].get("disabled"):
            raise ValueError("تغيرت أهلية عناصر النموذج؛ أُلغي تفويض العملية.")
        if any(controls[key].get("options", []) != options for key, options in self.options.items()):
            raise ValueError("تغيرت خيارات النموذج؛ أُلغي تفويض العملية.")
        step = self.steps[self.position]
        return Action(step.kind, controls[self.bindings[self.position]]["id"], step.value, step.reason)

    def advance(self):
        step = self.steps[self.position]
        if step.kind in {"fill", "select"}:
            self.hashes[self.bindings[self.position]] = hashlib.sha256(json.dumps([step.value, False]).encode()).hexdigest()
        self.position += 1
        if self.position == len(self.steps) and self.status == "active":
            self.status = "consumed"

    def revoke(self):
        if self.status == "active":
            self.status = "revoked"

    def receipt(self):
        return {"id": self.id, "basis": self.basis, "status": self.status,
                "context_revision": self.context_revision,
                "approved_at": self.approved_at, "expires_at": self.expires_at,
                "steps": len(self.steps), "executed": self.position}

    def public(self):
        return {**self.receipt(), "description": self.description, "page": self.page, "identity": copy.deepcopy(self.identity),
                "changes": [{"kind": step.kind, "label": self.controls[key].get("label", ""), "value": step.value,
                             "choice_label": next((item["label"] for item in self.options.get(key, []) if item["value"] == step.value), None)}
                            for step, key in zip(self.steps, self.bindings)]}


class OperationResults:
    """Result obligations for parsed proposals, including ones never approved.

    These transient checks grant no authority. Values stay in memory; only check
    IDs, states and source references belong in reports. User revision retires
    the old intent, while the independent attempt/effect journals retain writes.
    """
    def __init__(self):
        self.entries = {}

    def register(self, operation, revision):
        page = urlsplit(operation.page)
        origin = (page.scheme, page.netloc)
        fields = operation.result_fields()
        identity = copy.deepcopy(operation.identity)
        key = json.dumps([revision, origin, identity, sorted(fields)], sort_keys=True)
        if key not in self.entries:
            self.entries[key] = {"id": f"O{len(self.entries) + 1}", "revision": revision,
                "origin": origin, "page": operation.page, "observed_url": None,
                "identity": identity, "fields": fields,
                "status": "unverified", "source_id": None}

    def observe(self, snapshot, source_id, revision):
        page = urlsplit(snapshot.get("url", ""))
        for entry in self.entries.values():
            if entry["revision"] != revision or entry["origin"] != (page.scheme, page.netloc):
                continue
            records = [record for record in snapshot.get("evidence_records", [])
                if record.get("main") is True and record.get("frame") == snapshot["url"]
                and all(normalise(record.get("fields", {}).get(key, "")) == value
                        for key, value in entry["identity"].items())]
            if not records and snapshot["url"] not in {entry["page"], entry["observed_url"]}:
                # Another page can be the next independent part of the task.
                # Recheck absence when returning to the operation/evidence page.
                continue
            labels = [label for label, _values in entry["fields"]]
            matched = (source_id is not None and len(records) == 1 and all(labels)
                and len(set(labels)) == len(labels)
                and all(label in records[0]["fields"]
                        and normalise(records[0]["fields"][label]) in values
                        for label, values in entry["fields"]))
            entry.update(status="matched" if matched else "unverified",
                         source_id=source_id if matched else None)
            if records:
                entry["observed_url"] = snapshot["url"]

    def summaries(self, revision):
        return [{key: entry[key] for key in ("id", "status", "source_id")}
                for entry in self.entries.values() if entry["revision"] == revision]

    def context(self, revision):
        return [{key: copy.deepcopy(entry[key]) for key in ("id", "page", "identity", "fields", "status", "source_id")}
                for entry in self.entries.values() if entry["revision"] == revision]
