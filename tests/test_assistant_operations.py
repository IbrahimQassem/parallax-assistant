"""Exact form-operation authority, change detection and revocation."""
import asyncio
import copy
import hashlib
import json

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.browser import PersonalBrowser
from parallax.assistant.runtime import Assistant
from parallax.assistant.scoped_operations import OperationResults, ScopedOperation


URL = "https://example.com/profile"


def state_hash(value):
    return hashlib.sha256(json.dumps([value, False]).encode()).hexdigest()


def snapshot(values=None):
    values = values or {"Name": "Before", "Language": "en", "Save": ""}
    form = {"ref": "0", "dom_id": "form", "name": "preferences", "method": "post", "action": URL,
            "action_hash": "destination", "label": "Preferences", "controls": 3, "credentialed": False}
    elements = []
    for index, (label, tag, kind) in enumerate([("Name", "input", "text"), ("Language", "select", "select-one"), ("Save", "button", "submit")]):
        elements.append({"id": str(index), "tag": tag, "type": kind, "role": "", "name": label.lower(), "label": label,
            "frame": URL, "form": copy.deepcopy(form), "disabled": False, "sensitive": False, "multiple": False,
            "state_hash": state_hash(values[label]), "options": [{"value": "en", "label": "English"}, {"value": "ar", "label": "Arabic"}] if tag == "select" else []})
    return {"url": URL, "text": "Profile", "fingerprint": "initial", "elements": elements,
            "form_hashes": [state_hash(json.dumps(values))], "evidence_records": [
                {"frame": URL, "main": True, "fields": {"Account": "Demo", "Profile": "Personal", "Name": "Before", "Language": "English"}}]}


def proposal():
    return json.dumps({"description": "Change profile preferences", "identity": {"Account": "Demo", "Profile": "Personal"},
        "steps": [Action("fill", "0", "PRIVATE-NAME").to_dict(), Action("select", "1", "ar").to_dict(), Action("click", "2").to_dict()]})


def preview(action, observation):
    return PersonalBrowser.preview(None, action, observation)


def test_operation_is_single_use_and_rebinds_observed_ids_without_widening_values():
    before = snapshot()
    operation = ScopedOperation(proposal(), before, preview)
    with pytest.raises(ValueError):
        operation.next_action(before)
    operation.approve()
    assert operation.next_action(before).value == "PRIVATE-NAME"
    operation.advance()
    changed = snapshot({"Name": "PRIVATE-NAME", "Language": "en", "Save": ""})
    for i, element in enumerate(changed["elements"]):
        element["id"] = str(100+i)
        element["form"]["ref"] = "9"
        element["form"]["dom_id"] = "new-render-id"
    assert operation.next_action(changed).target == "101"
    operation.advance()
    changed["elements"][1]["state_hash"] = state_hash("ar")
    assert operation.next_action(changed).target == "102"
    operation.advance()
    assert operation.status == "consumed"
    with pytest.raises(ValueError):
        operation.next_action(changed)
    assert "PRIVATE" not in json.dumps(operation.receipt())


@pytest.mark.parametrize("mutation", ["account", "ambiguous", "value", "destination", "options", "disabled", "control", "page"])
def test_scope_change_revokes_eligibility_before_next_action(mutation):
    before = snapshot()
    operation = ScopedOperation(proposal(), before, preview)
    operation.approve()
    changed = copy.deepcopy(before)
    if mutation == "account": changed["evidence_records"][0]["fields"]["Account"] = "Other"
    if mutation == "ambiguous": changed["evidence_records"].append(copy.deepcopy(changed["evidence_records"][0]))
    if mutation == "value": changed["elements"][1]["state_hash"] = state_hash("unexpected")
    if mutation == "destination": changed["elements"][2]["form"]["action_hash"] = "different-submit-endpoint"
    if mutation == "options": changed["elements"][1]["options"][1]["label"] = "Not Arabic"
    if mutation == "disabled": changed["elements"][0]["disabled"] = True
    if mutation == "control": changed["elements"][0]["name"] = "another-input"
    if mutation == "page": changed["url"] = URL + "/other"
    with pytest.raises(ValueError): operation.next_action(changed)


@pytest.mark.parametrize("label", ["Delete", "Pay", "Publish", "Send", "حذف", "دفع", "Unknown action"])
def test_high_stakes_or_unknown_submitters_are_not_group_eligible(label):
    before = snapshot()
    before["elements"][2]["label"] = label
    with pytest.raises(ValueError): ScopedOperation(proposal(), before, preview)


def test_expiry_revocation_and_foreign_destination_fail_closed():
    operation = ScopedOperation(proposal(), snapshot(), preview)
    operation.approve()
    operation.deadline = 0
    with pytest.raises(ValueError): operation.next_action(snapshot())
    assert operation.status == "expired"
    operation = ScopedOperation(proposal(), snapshot(), preview)
    operation.approve()
    operation.revoke()
    with pytest.raises(ValueError): operation.next_action(snapshot())
    before = snapshot()
    for element in before["elements"]: element["form"]["action"] = "https://other.example/submit"
    with pytest.raises(ValueError): ScopedOperation(proposal(), before, preview)


def test_high_stakes_selected_option_is_excluded_even_with_a_routine_field_label():
    before = snapshot()
    before["elements"][1]["options"][1]["label"] = "Delete account"
    with pytest.raises(ValueError): ScopedOperation(proposal(), before, preview)


class Browser:
    context = None
    def __init__(self, mutation=None, block=False):
        self.values = {"Name": "Before", "Language": "en", "Save": ""}
        self.executed = []
        self.mutation = mutation
        self.saved = False
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self): self.context = True
    async def close(self): self.context = None
    preview = staticmethod(preview)

    async def observe(self):
        result = snapshot(self.values)
        result["text"] = "Profile saved" if self.saved else "Profile"
        if self.saved:
            result["evidence_records"][0]["fields"].update(Name=self.values["Name"], Language="Arabic")
        if self.executed and self.mutation == "account":
            result["evidence_records"][0]["fields"]["Account"] = "Other"
        return result

    async def execute(self, action):
        self.executed.append(action)
        if action.kind == "fill": self.values["Name"] = action.value
        if action.kind == "select": self.values["Language"] = action.value
        if action.kind == "click": self.saved = True
        if self.block:
            self.started.set()
            await self.release.wait()


class Planner:
    def __init__(self, *_args): self.calls = 0
    async def propose(self, _task, observation, _history):
        self.calls += 1
        if self.calls == 1:
            return Action("expect", "preferences", json.dumps({"description": "Save preferences", "url": URL,
                "subject": {"Account": "Demo", "Profile": "Personal"}, "outcome": {"Name": "PRIVATE-NAME", "Language": "Arabic"}}))
        if self.calls == 2: return Action("operation", "preferences", proposal())
        return Action("finish", value=json.dumps({"summary": "Preferences result", "scope": "profile", "work_done": "inspected",
            "findings": [], "limitations": [], "followups": [], "completion": {"status": "verified", "done": ["preferences"],
            "remaining": [], "reason": "page read", "evidence": [{"source_id": "S1", "claim": "profile read", "quote": "Profile"}]}}))


async def pending(app, kind="approval"):
    for _ in range(200):
        value = app.snapshot()["pending"]
        if value and value["type"] == kind: return value
        if app.job.done(): raise AssertionError(app.snapshot())
        await asyncio.sleep(0.005)
    raise AssertionError("missing decision")


@pytest.mark.parametrize("mutation", ["account", "identity_missing", "field_missing", "value", "text_only"])
def test_mismatched_result_check_is_rejected_before_approval_or_execution(tmp_path, mutation):
    class MismatchedPlanner(Planner):
        async def propose(self, *args):
            action = await super().propose(*args)
            if action.kind == "expect":
                check = json.loads(action.value)
                if mutation == "account": check["subject"]["Account"] = "Other"
                if mutation == "identity_missing": del check["subject"]["Profile"]
                if mutation == "field_missing": del check["outcome"]["Language"]
                if mutation == "value": check["outcome"]["Language"] = "English"
                if mutation == "text_only": check.update(subject="Demo", outcome="Demo saved")
                return Action("expect", action.target, json.dumps(check))
            return action

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=MismatchedPlanner)
        await app.start("Change profile preferences", "codex")
        await app.job
        assert not browser.executed
        assert app.snapshot()["approval_requests"] == 0
        assert not app.snapshot()["authority_receipts"]
        assert app.snapshot()["status"] == "unverified"
        assert app.snapshot()["completion"]["operation_checks"] == [
            {"id": "O1", "status": "unverified", "source_id": None}]
        assert "PRIVATE-NAME" not in next(tmp_path.glob("*.json")).read_text()
    asyncio.run(run())


@pytest.mark.parametrize("language", ["ar", "Arabic"])
def test_result_check_accepts_selected_value_or_displayed_label(language):
    operation = ScopedOperation(proposal(), snapshot(), preview)
    operation.validate_expectation({"subject": {"Account": "Demo", "Profile": "Personal"},
                                    "outcome": {"Name": "PRIVATE-NAME", "Language": language}})


def test_reviewed_edited_values_cannot_use_casefold_verification():
    operation = ScopedOperation(proposal(), snapshot(), preview)
    with pytest.raises(ValueError):
        operation.validate_expectation({"subject": {"Account": "Demo", "Profile": "Personal"},
            "outcome": {"Name": "PRIVATE-NAME", "Language": "Arabic"}, "casefold_outcome": ["Name"]})


@pytest.mark.parametrize("fallback", [False, True])
def test_corrected_proposal_or_individual_actions_can_complete_rejected_intent(tmp_path, fallback):
    class RecoveringPlanner(Planner):
        async def propose(self, *args):
            action = await super().propose(*args)
            if self.calls in {1, 3}:
                check = {"description": "Save preferences", "url": URL,
                    "subject": {"Account": "Demo", "Profile": "Personal"},
                    "outcome": {"Name": "PRIVATE-NAME", "Language": "Arabic"}}
                if self.calls == 1: del check["outcome"]["Language"]
                return Action("expect", "preferences" if self.calls == 1 else "corrected", json.dumps(check))
            if self.calls == 4 and not fallback:
                return Action("operation", "corrected", proposal())
            if fallback and 4 <= self.calls <= 6:
                return Action.parse(json.dumps(json.loads(proposal())["steps"][self.calls - 4]))
            return action

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=RecoveringPlanner)
        await app.start("Change profile preferences", "codex")
        handled = set()
        while not app.job.done():
            decision = app.snapshot()["pending"]
            if decision and decision["token"] not in handled:
                assert decision["type"] == "approval"
                handled.add(decision["token"])
                await app.control("approve", decision["token"])
            await asyncio.sleep(0.005)
        assert len(handled) == (3 if fallback else 1)
        assert len(browser.executed) == 3
        assert app.snapshot()["status"] == "verified"
        assert app.snapshot()["completion"]["operation_checks"] == [
            {"id": "O1", "status": "matched", "source_id": "S1"}]
    asyncio.run(asyncio.wait_for(run(), 3))


def test_already_satisfied_values_are_verified_without_sending_again(tmp_path):
    async def run():
        browser = Browser()
        browser.values.update(Name="PRIVATE-NAME", Language="ar")
        browser.saved = True
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("Change profile preferences", "codex")
        await app.job
        assert not browser.executed
        assert app.snapshot()["approval_requests"] == 0
        assert app.snapshot()["status"] == "verified"
        assert app.snapshot()["completion"]["operation_checks"][0]["status"] == "matched"
    asyncio.run(run())


def test_user_revision_retires_unsent_intent_without_erasing_operation_history(tmp_path):
    class RejectedThenHandoff(Planner):
        async def propose(self, *args):
            action = await super().propose(*args)
            if self.calls == 1:
                check = json.loads(action.value)
                del check["outcome"]["Language"]
                return Action("expect", action.target, json.dumps(check))
            if self.calls == 3: return Action("handoff", reason="Clarify the intended task")
            if self.calls == 4:
                assert args[1]["assistant_context"]["operation_results"] == []
            return action

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=RejectedThenHandoff)
        await app.start("Change profile preferences", "codex")
        handoff = await pending(app, "handoff")
        await app.control("revise", app.snapshot()["id"], "اكتف بقراءة الملف، ولا تغيره")
        await app.control("resume", handoff["token"])
        await app.job
        assert not browser.executed
        assert app.snapshot()["completion"]["operation_checks"] == []
        assert app.snapshot()["status"] == "verified"
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [None, "field", "account", "duplicate", "split", "frame", "origin", "no_source"])
def test_proposed_result_requires_one_unique_record_for_all_requested_values(mutation):
    checks = OperationResults()
    checks.register(ScopedOperation(proposal(), snapshot(), preview), 0)
    result = snapshot()
    record = result["evidence_records"][0]
    record["fields"].update(Name="PRIVATE-NAME", Language="Arabic")
    if mutation == "field": record["fields"]["Language"] = "English"
    if mutation == "account": record["fields"]["Account"] = "Other"
    if mutation == "duplicate": result["evidence_records"].append(copy.deepcopy(record))
    if mutation == "split":
        result["evidence_records"].append({**record, "fields": {"Language": "Arabic"}})
        del record["fields"]["Language"]
    if mutation == "frame": record["main"] = False
    if mutation == "origin":
        result["url"] = record["frame"] = "https://other.example/profile"
    checks.observe(result, None if mutation == "no_source" else "S1", 0)
    assert checks.summaries(0)[0]["status"] == ("unverified" if mutation else "matched")
    assert "PRIVATE-NAME" not in json.dumps(checks.summaries(0))


def test_results_for_two_pages_survive_navigation_but_recheck_on_return():
    checks = OperationResults()
    checks.register(ScopedOperation(proposal(), snapshot(), preview), 0)
    other = snapshot()
    other["url"] = URL + "/other"
    other["evidence_records"][0]["frame"] = other["url"]
    other["evidence_records"][0]["fields"]["Account"] = "Other"
    for element in other["elements"]:
        element["frame"] = element["form"]["action"] = other["url"]
    other_proposal = json.loads(proposal())
    other_proposal["identity"]["Account"] = "Other"
    checks.register(ScopedOperation(json.dumps(other_proposal), other, preview), 0)
    first_result = snapshot()
    first_result["evidence_records"][0]["fields"].update(Name="PRIVATE-NAME", Language="Arabic")
    other["evidence_records"][0]["fields"].update(Name="PRIVATE-NAME", Language="Arabic")
    checks.observe(first_result, "S1", 0)
    checks.observe(other, "S2", 0)
    assert [row["status"] for row in checks.summaries(0)] == ["matched", "matched"]
    assert [row["source_id"] for row in checks.summaries(0)] == ["S1", "S2"]
    checks.observe(snapshot(), "S1", 0)
    assert [row["status"] for row in checks.summaries(0)] == ["unverified", "matched"]


@pytest.mark.parametrize("mutation", [None, "account"])
def test_one_approval_runs_exact_values_and_account_change_stops_remaining_steps(tmp_path, mutation):
    async def run():
        browser = Browser(mutation)
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("Change profile preferences", "codex")
        decision = await pending(app)
        assert decision["operation"]["changes"][0]["value"] == "PRIVATE-NAME"
        await app.control("approve", decision["token"])
        await app.job
        state = app.snapshot()
        assert state["approval_requests"] == 1
        assert len(browser.executed) == (1 if mutation else 3)
        assert state["status"] == ("unverified" if mutation else "verified")
        authority = state["authority_receipts"][0]
        assert authority["status"] == ("revoked" if mutation else "consumed")
        assert {receipt["authority_id"] for receipt in state["operations"]} == {authority["id"]}
        assert "PRIVATE-NAME" not in next(tmp_path.glob("*.json")).read_text()
    asyncio.run(run())


@pytest.mark.parametrize("stop", [False, True])
def test_revoke_or_stop_during_a_step_never_starts_the_remaining_steps(tmp_path, stop):
    async def run():
        browser = Browser(block=True)
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("Change profile preferences", "codex")
        decision = await pending(app)
        await app.control("approve", decision["token"])
        await asyncio.wait_for(browser.started.wait(), 1)
        authority = app.snapshot()["operation_authority"]
        if stop:
            await app.control("stop")
            assert app.snapshot()["uncertain_actions"][0]["status"] == "unknown"
        else:
            await app.control("revoke_operation", authority["id"])
            browser.release.set()
            await pending(app, "handoff")
            assert app.snapshot()["operation_authority"]["status"] == "revoked"
            await app.control("stop")
        assert len(browser.executed) == 1
        assert app.snapshot()["authority_receipts"][0]["status"] == "revoked"
    asyncio.run(run())


def test_review_mode_and_insufficient_budget_do_not_accept_grouped_execution(tmp_path):
    async def run():
        for mode, limit in [("review", 10), ("browse", 4)]:
            browser = Browser()
            app = Assistant(browser, tmp_path / mode, planner_factory=Planner, max_steps=limit)
            await app.start("Change profile preferences", "codex", consent_mode=mode)
            await app.job
            assert not browser.executed
            assert app.snapshot()["approval_requests"] == 0
    asyncio.run(run())


def test_new_user_direction_invalidates_remaining_operation_steps(tmp_path):
    async def run():
        browser = Browser(block=True)
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("Change profile preferences", "codex")
        decision = await pending(app)
        await app.control("approve", decision["token"])
        await asyncio.wait_for(browser.started.wait(), 1)
        await app.control("revise", app.snapshot()["id"], "اكتف بفحص ما حدث ولا تحفظ النموذج")
        browser.release.set()
        await app.job
        state = app.snapshot()
        assert len(browser.executed) == 1 and not browser.saved
        assert state["context_revision"] == 1
        assert state["authority_receipts"][0]["status"] == "revoked"
        assert state["approval_requests"] == 1
        assert len(state["operations"]) == 1
        assert state["completion"]["effect_checks"][0]["attempt_count"] == 1
        assert state["status"] == "unverified"
    asyncio.run(run())
