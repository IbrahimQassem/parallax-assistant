"""Validate optional structured answers; evidence comes only from the controller."""
import json


def structured_result(raw, sources):
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
        return {
            "summary": text(data["summary"]), "scope": text(data.get("scope", "")),
            "work_done": text(data.get("work_done", "")), "findings": findings,
            "limitations": [text(v, 1000) for v in items(data.get("limitations", []), 10)],
            "followups": [text(v, 300) for v in items(data.get("followups", []), 3)],
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
