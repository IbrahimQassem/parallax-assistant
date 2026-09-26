"""Bounded, descriptive task plans. A plan grants no execution authority."""
from __future__ import annotations

import json
import re


STEP_STATUSES = {"pending", "in_progress", "done", "blocked"}


def parse_plan(raw: str) -> dict:
    """Validate a topologically ordered plan without trusting claimed progress.

    Plans are transient model context, not verified results or permission records.
    Reject unexpected fields so model-supplied authority cannot enter this shape.
    """
    def text(value, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError()
        return value.strip()

    def texts(value, *, required=False):
        if not isinstance(value, list) or len(value) > 12 or (required and not value):
            raise ValueError()
        return [text(item, 600) for item in value]

    try:
        if not isinstance(raw, str) or len(raw) > 12000:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"goal", "constraints", "success_criteria", "steps"}:
            raise ValueError()
        goal = text(data["goal"], 1000)
        constraints = texts(data["constraints"])
        criteria = texts(data["success_criteria"], required=True)
        if not isinstance(data["steps"], list) or not 1 <= len(data["steps"]) <= 12:
            raise ValueError()
        steps, known = [], {}
        for item in data["steps"]:
            if not isinstance(item, dict) or set(item) != {"id", "title", "depends_on", "status"}:
                raise ValueError()
            identifier = text(item["id"], 32)
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", identifier) or identifier in known:
                raise ValueError()
            dependencies = texts(item["depends_on"])
            status = text(item["status"], 32)
            if (status not in STEP_STATUSES or len(set(dependencies)) != len(dependencies)
                    or any(dep not in known for dep in dependencies)):
                raise ValueError()
            if status in {"in_progress", "done"} and any(known[dep] != "done" for dep in dependencies):
                raise ValueError()
            known[identifier] = status
            steps.append({"id": identifier, "title": text(item["title"], 600),
                          "depends_on": dependencies, "status": status})
        if sum(step["status"] == "in_progress" for step in steps) > 1:
            raise ValueError()
        return {"goal": goal, "constraints": constraints, "success_criteria": criteria, "steps": steps}
    except (ValueError, TypeError, KeyError):
        # Never echo rejected model content; it may contain personal information.
        raise ValueError("خطة المهمة غير صالحة؛ يلزم هدف ومعايير إنجاز وخطوات مترابطة بلا تعارض.") from None
