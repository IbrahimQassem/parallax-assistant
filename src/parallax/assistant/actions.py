"""The model can propose data, never JavaScript or a shell command."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


KINDS = ("navigate", "click", "fill", "select", "press", "scroll", "switch_tab",
         "wait", "handoff", "finish")
SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["kind", "target", "value", "reason"],
    "additionalProperties": False,
}


def web_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or any(ord(c) < 33 for c in value)):
        raise ValueError("يسمح بروابط HTTP/HTTPS فقط، دون كلمات مرور في الرابط.")
    return value


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""
    value: str = ""
    reason: str = ""

    @classmethod
    def parse(cls, raw: str) -> Action:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != set(SCHEMA["required"]):
            raise ValueError("صيغة خطوة المحرك غير صحيحة.")
        if not all(isinstance(v, str) and len(v) <= 12000 for v in data.values()):
            raise ValueError("تجاوزت الخطوة الحجم المسموح أو احتوت قيمة غير نصية.")
        action = cls(**data)
        if action.kind not in KINDS:
            raise ValueError("نوع الخطوة غير مسموح.")
        if action.kind == "navigate":
            web_url(action.value)
        if action.kind in {"click", "fill", "select", "press", "switch_tab"}:
            if not action.target.isdigit():
                raise ValueError("يجب اختيار عنصر من الصفحة الحالية.")
        if action.kind == "press" and action.value not in {"Enter", "Tab", "Escape"}:
            raise ValueError("المفتاح غير مسموح.")
        if action.kind == "scroll" and action.value not in {"up", "down"}:
            raise ValueError("اتجاه التمرير غير صحيح.")
        return action

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def approval_reason(action: Action, target: dict | None = None, mode="browse") -> str | None:
    """Bounded navigation delegation using observed DOM, never model risk claims.

    DOM semantics are a heuristic, not proof of a site's behavior. Unknown controls,
    forms and changes stay gated; do not infer authority from action.reason.
    """
    if action.kind not in {"click", "fill", "select", "press"}:
        return None
    if mode == "review":
        return "اخترت مراجعة كل خطوة تفاعلية قبل تنفيذها."
    if action.kind != "click":
        return "قد تغيّر هذه الخطوة بيانات أو ترسلها؛ راجع العنصر والقيمة أولًا."
    target = target or {}
    label = unicodedata.normalize("NFKC", str(target.get("label", ""))).casefold()
    label = "".join(c for c in label if unicodedata.category(c) not in {"Mn", "Cf"})
    consequential = re.search(
        r"\b(save|send|submit|delete|remove|buy|pay|purchase|checkout|book|reserve|confirm|accept|agree|allow|grant|enable|disable|subscribe|unsubscribe|sign|log|logout|reset|clear|upload|download|publish|share|connect|disconnect|archive|restore)\b|"
        r"حفظ|احفظ|ارسال|ارسل|حذف|احذف|ازال|شراء|دفع|حجز|تاكيد|موافق|سماح|تفعيل|تعطيل|اشتراك|تسجيل|خروج|مسح|نشر|مشارك|تحميل|ارشف|استعاد", label)
    if (consequential or target.get("sensitive") or target.get("in_form")
            or target.get("editable") or target.get("download") or target.get("disabled")
            or target.get("role") in {"switch", "checkbox", "radio", "menuitemcheckbox", "menuitemradio"}):
        return "قد تؤثر هذه الخطوة في البيانات أو الإعدادات أو الموافقات؛ تحتاج مراجعتك."
    role = target.get("role", "")
    if target.get("tag") == "summary":
        return None
    if role == "tab" and target.get("controls_role") == "tabpanel":
        return None
    if (target.get("tag") == "button" or role == "button") and target.get("has_popup") == "menu":
        return None
    if target.get("expanded") in {"true", "false"} and target.get("controls_role") in {"menu", "navigation", "tablist"}:
        return None
    if role == "menuitem" and label.strip() in {"settings", "personalization", "preferences", "الإعدادات", "الاعدادات", "التخصيص", "التفضيلات"}:
        return None
    return "أثر هذا العنصر غير واضح بما يكفي للتنفيذ التلقائي؛ راجعه مرة واحدة."


def needs_approval(action: Action, target: dict | None = None, mode="browse") -> bool:
    return approval_reason(action, target, mode) is not None
