"""Bounded natural-language authority for an already observed routine form.

Only a complete user command can match. Model reasons, page prose, previous
answers and partial quotations are never authority. Unrecognised requests use
the existing review path rather than acquiring broader permission.
"""
from __future__ import annotations

import copy
import re
import unicodedata

from .effect_checks import normalise
from .scoped_operations import descriptor, form_descriptor, individual_review


ACCOUNT_LABELS = {"account", "الحساب", "حساب"}
FIELD_LABELS = (
    {"language", "لغة", "اللغة"},
    {"name", "display name", "الاسم", "اسم", "اسم العرض"},
)
LANGUAGE_LABELS = ({"Arabic", "العربية", "عربي"}, {"English", "الإنجليزية", "الانجليزية"})


def alternatives(values, *, insensitive=False):
    escaped = "|".join(re.escape(value) for value in sorted(set(values)))
    return f"(?i:{escaped})" if insensitive else f"(?:{escaped})"


def has_clause_words(value):
    text = unicodedata.normalize("NFKC", value).casefold()
    text = "".join(c for c in text if unicodedata.category(c) not in {"Mn", "Cf"})
    return bool(re.search(
        r"\b(and|or|but|not|no|never|do|don['’]t|can['’]t|cannot|if|unless|until|when|once|"
        r"before|after|then|only|without|except|pending|approve|approval|ask|confirm|later|wait|"
        r"change|set|update|save|review|translate|for|in|on|of|to|account)\b|"
        r"\b(لا|ولا|لاتحفظ|ليس|لم|لن|لكن|ولكن|اذا|الا|قبل|بعد|ثم|فقط|دون|بدون|حتى|عندما|"
        r"او|و|الى|في|هذا|هذه|حساب|الحساب|غير|عدل|اضبط|حدث|احفظ|تحفظ|راجع|ترجم|موافقة)\b|"
        r"[;:،,\n\r]", text))


def parameter_pattern(value, *, insensitive=False):
    # One unquoted atom, or an explicitly delimited literal. Arbitrary multiword
    # page/model values must not swallow a qualifier or a second quoted value.
    forms = [left + value + right for left, right in [('"', '"'), ("'", "'"), ("«", "»")]
             if left not in value and right not in value and "\\" not in value]
    if re.fullmatch(r"[\w.-]+", value) and not has_clause_words(value):
        forms.append(value)
    return alternatives(forms, insensitive=insensitive) if forms else r"(?!)"


def value_pattern(step, control):
    patterns = [parameter_pattern(step.value)]
    if step.kind == "select":
        label = next(item["label"] for item in control["options"] if item["value"] == step.value)
        labels = {normalise(label)}
        if normalise(control["label"]).casefold() in FIELD_LABELS[0]:
            for names in LANGUAGE_LABELS:
                if label.casefold() in {name.casefold() for name in names}:
                    labels.update(names)
        patterns.extend(parameter_pattern(label, insensitive=True) for label in labels)
    return "(?:" + "|".join(patterns) + ")"


class RequestAuthority:
    def __init__(self, request):
        self.request = request.strip()
        self.bound = False
        self.page = None
        self.records = []
        self.elements = []
        self.used = False

    def bind(self, snapshot):
        if self.bound:
            return
        self.bound = True
        self.page = snapshot.get("url")
        self.records = copy.deepcopy([record for record in snapshot.get("evidence_records", [])
            if record.get("main") is True and record.get("frame") == self.page])
        self.elements = copy.deepcopy([element for element in snapshot.get("elements", []) if element.get("frame") == self.page])

    def invalidate(self):
        self.used = True

    def claim(self, operation, revision):
        # Revisions revoke this initial request, and one command cannot silently
        # authorize another sequence after cancellation or a partial execution.
        if self.used or revision != 0 or operation.page != self.page:
            return False
        if individual_review({"label": " ".join(str(operation.form.get(k, "")) for k in ("label", "name"))}):
            return False
        identity = operation.identity
        account_keys = [key for key in identity if key.casefold() in ACCOUNT_LABELS]
        if len(account_keys) != 1:
            return False
        account = identity[account_keys[0]]
        account_records = [r for r in self.records if any(k.casefold() in ACCOUNT_LABELS for k in r["fields"])]
        matching = [r for r in account_records if any(
            k.casefold() in ACCOUNT_LABELS and normalise(v) == account for k, v in r["fields"].items())]
        if len(matching) != 1 or not all(normalise(matching[0]["fields"].get(k, "")) == v for k, v in identity.items()):
            return False
        # Duplicate labels for account identity are ambiguous, even if values agree.
        if sum(k.casefold() in ACCOUNT_LABELS for k in matching[0]["fields"]) != 1:
            return False
        for binding in operation.bindings:
            initial = [element for element in self.elements if descriptor(element) == binding
                       and form_descriptor(element.get("form") or {}) == operation.form]
            if len(initial) != 1 or initial[0].get("options", []) != operation.controls[binding].get("options", []):
                return False
        name = parameter_pattern(account)
        english_scope = [f"(?i:(?:the )?account) {name}", f"(?i:the )?{name} (?i:account)"]
        arabic_scope = [f"(?:حساب|الحساب) {name}"]
        if len(account_records) == 1:
            english_scope.append("(?i:this account|(?:the )?current account)")
            arabic_scope.append("(?:هذا الحساب|الحساب الحالي)")
        english_scope = "(?:" + "|".join(english_scope) + ")"
        arabic_scope = "(?:" + "|".join(arabic_scope) + ")"
        fields = []
        for step, binding in zip(operation.steps[:-1], operation.bindings[:-1]):
            control = operation.controls[binding]
            label = normalise(control["label"])
            if not label:
                return False
            names = {label}
            field_pattern = parameter_pattern(label, insensitive=True)
            for aliases in FIELD_LABELS:
                if label.casefold() in aliases:
                    names.update(aliases)
                    field_pattern = alternatives(names, insensitive=True)
            candidates = [element for element in self.elements
                          if element.get("tag") in {"input", "select", "textarea"}
                          and normalise(element.get("label", "")).casefold() in {name.casefold() for name in names}]
            if len(candidates) != 1 or descriptor(candidates[0]) != binding:
                return False
            fields.append((field_pattern, value_pattern(step, control)))
        english_verb = "(?i:(?:please )?(?:change|set|update)|(?:can|could) you (?:please )?(?:change|set|update))"
        arabic_verb = "(?:(?:من فضلك )?(?:غيّر|غير|عدّل|عدل|اضبط|حدّث|حدث)|هل يمكنك (?:تغيير|تعديل|ضبط|تحديث))"
        english_changes = " (?i:and) ".join(f"(?i:the )?{label} (?i:to) {value}" for label, value in fields)
        arabic_changes = " و".join(f"{label} (?:إلى|الى) {value}" for label, value in fields)
        patterns = [
            f"{english_verb} {english_changes} (?i:for|in|on) {english_scope}(?i:,? then save)?",
            f"{arabic_verb} {arabic_changes} (?:في|لـ) {arabic_scope}(?:،? ثم احفظ)?",
        ]
        if len(fields) == 1:
            label, value = fields[0]
            patterns.extend([
                f"{english_verb} (?i:the )?{label} (?i:of|for|in) {english_scope} (?i:to) {value}(?i:,? then save)?",
                f"{arabic_verb} {label} {arabic_scope} (?:إلى|الى) {value}(?:،? ثم احفظ)?",
            ])
        if not any(re.fullmatch(pattern, self.request) for pattern in patterns):
            return False
        self.used = True
        return True
