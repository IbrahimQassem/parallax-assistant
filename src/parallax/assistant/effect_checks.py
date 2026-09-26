"""Predeclared, bounded checks for effects; never execution authorization.

Checks are transient. A page assertion is evidence of a displayed state, not
proof of a server commit or of the model's interpretation of the user's goal.
"""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

from .actions import web_url


def normalise(value):
    return re.sub(r"\s+", " ", value).strip()


def contains(text, fragment):
    # Do not confuse ORDER-42 with ORDER-420, or "published" with "unpublished".
    return re.search(r"(?<!\w)" + re.escape(normalise(fragment)) + r"(?!\w)", normalise(text)) is not None


def origin(url):
    parsed = urlsplit(web_url(url))
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname, port


def parse_expectation(identifier, raw):
    def fields(value):
        if not isinstance(value, dict) or not 1 <= len(value) <= 6:
            raise ValueError()
        result = {}
        for label, expected in value.items():
            if (not isinstance(label, str) or not isinstance(expected, str)
                    or not label.strip() or not expected.strip() or len(label) > 100 or len(expected) > 600):
                raise ValueError()
            label = normalise(label)
            if label in result:
                raise ValueError()
            result[label] = normalise(expected)
        return result

    try:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", identifier):
            raise ValueError()
        data = json.loads(raw)
        required = {"description", "url", "subject", "outcome"}
        optional = {"label_aliases", "url_scope", "casefold_outcome"}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
            raise ValueError()
        for field, limit in (("description", 600), ("url", 2000)):
            if not isinstance(data[field], str) or not data[field].strip() or len(data[field]) > limit:
                raise ValueError()
            data[field] = data[field].strip()
        origin(data["url"])
        if data.get("url_scope", "page") not in ("page", "origin"):
            raise ValueError()
        if isinstance(data["subject"], str) and isinstance(data["outcome"], str):
            if "label_aliases" in data or "casefold_outcome" in data or data.get("url_scope") == "origin":
                raise ValueError()
            for field in ("subject", "outcome"):
                if not data[field].strip() or len(data[field]) > 300:
                    raise ValueError()
                data[field] = data[field].strip()
            if (normalise(data["subject"]) == normalise(data["outcome"])
                    or not contains(data["outcome"], data["subject"])):
                raise ValueError()
        else:
            data["subject"], data["outcome"] = fields(data["subject"]), fields(data["outcome"])
            if data["subject"].keys() & data["outcome"].keys():
                raise ValueError()
            if "casefold_outcome" in data:
                labels = data["casefold_outcome"]
                if (not isinstance(labels, list) or not 1 <= len(labels) <= len(data["outcome"])
                        or not all(isinstance(label, str) and label in data["outcome"] for label in labels)
                        or len(set(labels)) != len(labels)):
                    raise ValueError()
            if "label_aliases" in data:
                aliases = data["label_aliases"]
                labels = set(data["subject"]) | set(data["outcome"])
                if not isinstance(aliases, dict) or not aliases or not aliases.keys() <= labels:
                    raise ValueError()
                used = set(labels)
                clean = {}
                for label, alternatives in aliases.items():
                    if not isinstance(alternatives, list) or not 1 <= len(alternatives) <= 3:
                        raise ValueError()
                    clean[label] = []
                    for alternative in alternatives:
                        if not isinstance(alternative, str) or not alternative.strip() or len(alternative) > 100:
                            raise ValueError()
                        alternative = normalise(alternative)
                        if alternative in used:
                            raise ValueError()
                        used.add(alternative)
                        clean[label].append(alternative)
                data["label_aliases"] = clean
        return {"id": identifier, **data}
    except (ValueError, TypeError, RecursionError):
        raise ValueError("شرط التحقق غير صالح؛ حدد وصفًا وصفحة نتيجة وهوية عنصر وحالة متوقعة، كنصين أو كمجموعتي حقول منفصلتين.") from None


def matches_page(expectation, snapshot):
    if expectation.get("url_scope", "page") == "page":
        return snapshot.get("url") == expectation["url"]
    try:
        return origin(snapshot.get("url", "")) == origin(expectation["url"])
    except ValueError:
        return False


def matching_fields(expectation, record, fields, *, identity_candidates=False):
    for key, value in fields.items():
        alternatives = [key, *expectation.get("label_aliases", {}).get(key, [])]
        present = [label for label in alternatives if label in record["fields"]]
        if identity_candidates:
            if not any(normalise(record["fields"][label]) == value for label in present):
                return False
            continue
        # Two labels are ambiguous even when one contains the desired value.
        if len(present) != 1:
            return False
        actual = normalise(record["fields"][present[0]])
        if key in expectation.get("casefold_outcome", []):
            actual, value = actual.casefold(), value.casefold()
        if actual != value:
            return False
    return True


def identity_candidates(expectation, snapshot):
    return [record for record in snapshot.get("evidence_records", [])
            if record.get("frame") == snapshot["url"] and record.get("main") is True
            and matching_fields(expectation, record, expectation["subject"], identity_candidates=True)]


def matching_blocks(expectation, snapshot):
    if not matches_page(expectation, snapshot):
        return []
    if isinstance(expectation["subject"], dict):
        candidates = identity_candidates(expectation, snapshot)
        return candidates if (len(candidates) == 1 and matching_fields(expectation, candidates[0], expectation["subject"])
                              and matching_fields(expectation, candidates[0], expectation["outcome"])) else []
    candidates = [block for block in snapshot.get("evidence_blocks", [])
                  if block.get("frame") == expectation["url"] and block.get("main") is True
                  and contains(block.get("text", ""), expectation["subject"])]
    return candidates if len(candidates) == 1 and normalise(candidates[0]["text"]) == normalise(expectation["outcome"]) else []


class EffectChecks:
    def __init__(self):
        self.entries = {}
        self.active = None
        self.uncovered = []

    def register(self, identifier, raw, snapshot):
        expectation = parse_expectation(identifier, raw)
        if identifier in self.entries or len(self.entries) >= 12:
            raise ValueError("استخدم معرّف تحقق جديدًا؛ لا يمكن تعديل شرط سبق تسجيله، والحد اثنا عشر شرطًا.")
        page = origin(snapshot["url"])
        if page != origin(expectation["url"]):
            raise ValueError("سجل شرط التحقق من وجهة العملية نفسها؛ صفحة النتيجة يجب أن تكون على أصل الموقع الحالي.")
        self.entries[identifier] = {"expectation": expectation, "attempts": [], "status": "not_started",
                                    "source_id": None, "execution_origin": page, "evidence_url": None}
        self.active = identifier

    def invalidate(self):
        # A changed user instruction cannot silently reuse an old proposal.
        self.active = None

    def preview(self, snapshot):
        if self.active is None:
            return None
        entry = self.entries[self.active]
        if origin(snapshot["url"]) != entry["execution_origin"]:
            raise ValueError("تغيرت وجهة العملية؛ سجل شرطًا جديدًا للوجهة الحالية قبل التنفيذ.")
        if matching_blocks(entry["expectation"], snapshot):
            raise ValueError("الحالة المتوقعة ظاهرة بالفعل؛ افحص النتيجة دون إعادة تنفيذ العملية.")
        return copy.deepcopy(entry["expectation"])

    def sent(self, attempt):
        if self.active is None:
            self.uncovered.append(attempt)
            return
        entry = self.entries[self.active]
        entry["attempts"].append(attempt)
        entry.update(status="unverified", source_id=None)

    def observe(self, snapshot, source_id):
        for identifier, entry in self.entries.items():
            expectation = entry["expectation"]
            if not entry["attempts"] or not matches_page(expectation, snapshot):
                continue
            if (expectation.get("url_scope") == "origin" and entry["evidence_url"]
                    and snapshot["url"] != entry["evidence_url"] and not identity_candidates(expectation, snapshot)):
                # Unrelated pages do not erase proof; returning to the evidence
                # page or seeing this identity elsewhere requires revalidation.
                continue
            # One record must contain both identity and state. Ambiguous repeated
            # records, other frames and separate snippets never establish a match.
            matched = source_id is not None and len(matching_blocks(expectation, snapshot)) == 1
            entry.update(status="matched" if matched else "unverified", source_id=source_id if matched else None)
            if matched:
                entry["evidence_url"] = snapshot["url"]
            if matched and self.active == identifier:
                # A completed condition cannot absorb subsequent operations.
                self.active = None

    def summaries(self):
        # Persist no expected text, full URL or raw page block in result reports.
        return [{"id": identifier, "status": entry["status"], "source_id": entry["source_id"],
                 "attempt_count": len(entry["attempts"])} for identifier, entry in self.entries.items()
                if entry["attempts"]] + [
                    {"id": attempt, "status": "no_condition", "source_id": None, "attempt_count": 1}
                    for attempt in self.uncovered]

    def missing(self):
        return any(item["status"] != "matched" for item in self.summaries())

    def context(self):
        return {"active": self.active, "conditions": [copy.deepcopy(entry["expectation"])
                for entry in self.entries.values()], "results": self.summaries()}
