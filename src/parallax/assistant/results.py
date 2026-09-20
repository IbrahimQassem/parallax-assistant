"""Validate final answers against evidence captured by the browser controller."""
import json
import re


COMPLETION_STATES = {"verified", "partial", "unverified"}


def _normalise(value):
    return re.sub(r"\s+", " ", value).strip()


def structured_result(raw, sources, observations=None, *, requires_post_action_evidence=False):
    """Return a report only when its claimed evidence is controller-observed.

    ``observations`` stays in memory: reports retain source URLs and the result,
    but never a page snapshot.  A quotation is accepted only if it appeared in
    the observed text for its declared source.  This makes a model's completion
    label a proposal rather than its own proof of success.
    """
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        def text(value, limit=4000):
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError()
            return value
        def items(value, limit):
            if not isinstance(value, list) or len(value) > limit:
                raise ValueError()
            return value
        known = {source["id"] for source in sources}
        observations = observations or {}
        findings = []
        for item in items(data.get("findings", []), 12):
            refs = items(item.get("source_ids", []), 12)
            if not all(isinstance(ref, str) for ref in refs):
                raise ValueError()
            findings.append({
                "title": text(item["title"], 300), "detail": text(item["detail"]),
                "metrics": [{"label": text(m["label"], 150), "value": text(m["value"], 200)}
                            for m in items(item.get("metrics", []), 8)],
                "source_ids": list(dict.fromkeys(ref for ref in refs if ref in known)),
            })
        completion = data.get("completion")
        if not isinstance(completion, dict) or set(completion) != {
            "status", "done", "remaining", "reason", "evidence"
        }:
            return None
        proposed_status = completion["status"]
        if proposed_status not in COMPLETION_STATES:
            return None
        done = [text(value, 600) for value in items(completion["done"], 12)]
        remaining = [text(value, 600) for value in items(completion["remaining"], 12)]
        reason = text(completion["reason"], 1000)
        evidence = []
        for item in items(completion["evidence"], 12):
            if not isinstance(item, dict) or set(item) != {"source_id", "claim", "quote"}:
                raise ValueError()
            source_id = text(item["source_id"], 40)
            claim = text(item["claim"], 600)
            quote = text(item["quote"], 500)
            observed = observations.get(source_id, {})
            observed_text = observed.get("text")
            if source_id not in known or not isinstance(observed_text, str):
                raise ValueError()
            if not _normalise(quote) or _normalise(quote) not in _normalise(observed_text):
                raise ValueError()
            evidence.append({
                "source_id": source_id,
                "claim": claim,
                "observed_after_action": bool(observed.get("after_action")),
            })

        # A successful click/fill/select/press needs a page observation after it.
        # A model cannot turn its own prose or an earlier page into confirmation.
        independently_verified = bool(evidence) and (
            not requires_post_action_evidence
            or any(item["observed_after_action"] for item in evidence)
        )
        status = proposed_status
        if proposed_status == "verified" and not independently_verified:
            status = "unverified"
            reason = "لا يوجد دليل صفحة مقروء مناسب للتحقق المستقل من النتيجة. " + reason
        if proposed_status == "partial" and not (done and remaining):
            raise ValueError()
        if proposed_status == "unverified" and not reason:
            raise ValueError()

        return {
            "summary": text(data["summary"]), "scope": text(data.get("scope", "")),
            "work_done": text(data.get("work_done", "")), "findings": findings,
            "limitations": [text(v, 1000) for v in items(data.get("limitations", []), 10)],
            "followups": [text(v, 300) for v in items(data.get("followups", []), 3)],
            "completion": {
                "status": status, "done": done, "remaining": remaining,
                "reason": reason, "evidence": evidence,
            },
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
