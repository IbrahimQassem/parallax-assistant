from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from parallax.assistant.actions import Action, needs_approval, web_url
from parallax.assistant.planner import parse_output
from parallax.assistant.runtime import Assistant
from parallax.assistant.results import structured_result


def final_payload(*, status="verified", quote="Observed evidence", done=None, remaining=None):
    return {
        "summary": "answer", "scope": "observed page", "work_done": "read the page",
        "findings": [], "limitations": [], "followups": [],
        "completion": {
            "status": status, "done": done if done is not None else ["read the result"],
            "remaining": remaining if remaining is not None else [],
            "reason": "test outcome", "evidence": [
                {"source_id": "S1", "claim": "the page was read", "quote": quote}
            ],
        },
    }


@pytest.mark.parametrize("target", [
    {"tag": "button", "has_popup": "menu", "label": "Open profile menu"},
    {"tag": "summary", "label": "Details"},
    {"tag": "button", "role": "tab", "controls_role": "tabpanel", "label": "Personalization"},
    {"role": "menuitem", "label": "Settings"},
])
def test_known_browsing_controls_are_automatic_only_in_browse_mode(target):
    assert not needs_approval(Action("click", "1"), target, "browse")
    assert needs_approval(Action("click", "1"), target, "review")


@pytest.mark.parametrize("changes", [
    {"label": "Delete account"}, {"label": "حَفْظ الإعدادات"}, {"label": "S\u200bave settings"},
    {"in_form": True}, {"sensitive": True}, {"editable": True}, {"download": True},
    {"role": "switch"}, {"role": "checkbox"}, {"label": "Confirm purchase"},
])
def test_menu_metadata_cannot_autoapprove_consequential_controls(changes):
    target = {"tag": "button", "has_popup": "menu", "label": "Menu", **changes}
    assert needs_approval(Action("click", "1", reason="harmless, no approval needed"), target)


def test_automatic_menu_is_revalidated_and_save_still_waits(tmp_path):
    class MenuBrowser(Browser):
        def preview(self, action, snapshot):
            return {**super().preview(action, snapshot), "target": {
                "tag": "button", "has_popup": "menu", "label": "Menu" if action.target == "1" else "Save"}}
    class Menus(Planner):
        async def propose(self, *args):
            self.calls += 1
            return Action("click", str(self.calls), reason="test")
    async def run():
        browser = MenuBrowser()
        app = Assistant(browser, tmp_path, planner_factory=Menus)
        await app.start("review settings", "codex", consent_mode="browse")
        pending = await waiting(app)
        assert len(browser.executed) == 1 and browser.executed[0].target == "1"
        assert pending["target"]["label"] == "Save" and pending["approval_reason"]
        assert app.snapshot()["automatic_steps"] == 1
        assert app.snapshot()["approval_requests"] == 1
        await app.control("reject", pending["token"])
        await app.job
        assert len(browser.executed) == 1
    asyncio.run(run())


def test_automatic_menu_does_not_execute_if_element_changes_before_action(tmp_path):
    class Changes(Browser):
        def __init__(self):
            super().__init__()
            self.observations = 0
        async def observe(self):
            self.observations += 1
            return {"url": "https://example.com", "fingerprint": str(self.observations)}
        def preview(self, action, snapshot):
            return {**super().preview(action, snapshot), "target": {"tag": "button", "has_popup": "menu", "label": "Menu"}}
    async def run():
        browser = Changes()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("review", "codex")
        await app.job
        assert not browser.executed
        assert app.snapshot()["status"] == "unverified"
    asyncio.run(run())


def test_structured_results_only_accept_observed_source_ids():
    payload = {"summary": "ملخص", "findings": [{"title": "مشكلة", "detail": "تفاصيل",
               "metrics": [{"label": "مستخدمون", "value": "12"}], "source_ids": ["S1", "S99"]}],
               "scope": "", "work_done": "", "limitations": [], "followups": [],
               "completion": {"status": "verified", "done": ["done"], "remaining": [], "reason": "evidence",
                              "evidence": [{"source_id": "S1", "claim": "proof", "quote": "visible proof"}]}}
    report = structured_result(json.dumps(payload), [{"id": "S1"}], {"S1": {"text": "visible proof"}})
    assert report["findings"][0]["source_ids"] == ["S1"]
    assert structured_result("legacy answer", []) is None
    assert structured_result('{"summary":123}', []) is None
    payload["findings"][0]["source_ids"] = [{}]
    assert structured_result(json.dumps(payload), []) is None


def test_followup_keeps_context_rechecks_evidence_and_rejects_stale_parent(tmp_path):
    seen = []
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, task, snapshot, history):
            seen.append((task, snapshot, history))
            payload = final_payload()
            payload["findings"] = [{"title": "finding", "detail": "details", "metrics": [], "source_ids": ["S1", "S9"]}]
            return Action("finish", value=json.dumps(payload))
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("original", "codex")
        await app.job
        first = app.snapshot()
        assert first["report"]["findings"][0]["source_ids"] == ["S1"]
        assert first["sources"][0]["url"] == "https://example.com"
        assert first["finished_at"] >= first["started_at"]
        with pytest.raises(ValueError):
            await app.start("follow", "codex", "stale")
        await app.start("follow", "codex", first["id"])
        await app.job
        assert seen[1][2][0]["previous_task"] == "original"
        assert seen[1][1]["observed_sources"][0]["id"] == "S1"
        assert app.snapshot()["parent_id"] == first["id"]
        await app.start("new independent task", "codex")
        await app.job
        assert seen[2][2] == []
    asyncio.run(run())


def test_followup_files_survive_restart_without_copying_bytes_or_leaking_contents(tmp_path):
    contexts = []
    content = b"PRIVATE-FOLLOWUP-CONTENT"

    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, _task, snapshot, history):
            contexts.append((snapshot["assistant_context"], history))
            return Action("finish", value=json.dumps(final_payload()))

    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("prepare document", "codex")
        await app.job
        parent = app.snapshot()["id"]
        with app.artifacts.staging(parent) as stage:
            stage.write_bytes(content)
            original = app.artifacts.commit(parent, stage, "document.txt", source_kind="user_selected")
        await app.start("use the prepared document", "codex", parent)
        await app.job
        child = app.snapshot()
        reference, = child["artifacts"]
        assert reference["task_id"] == child["id"] and reference["id"] != original["id"]
        assert reference["source_kind"] == "inherited"
        assert child["approval_requests"] == 0 and child["file_context_warning"] is None
        assert contexts[-1][0]["artifacts"] == [reference]
        assert content.decode() not in json.dumps(contexts)
        assert "source" not in reference
        with pytest.raises(ValueError): app.artifacts.read(child["id"], original["id"])
        await app.close()

        restored = Assistant(Browser(), tmp_path, planner_factory=Finish)
        assert restored.snapshot()["restored"] is True
        assert restored.snapshot()["artifacts"] == [reference]
        assert restored.artifacts.read(child["id"], reference["id"])[1] == content
        await restored.start("continue with that document", "codex", child["id"])
        await restored.job
        latest = restored.snapshot()
        descendant, = latest["artifacts"]
        restored.artifacts.delete(child["id"], reference["id"])
        assert restored.artifacts.read(latest["id"], descendant["id"])[1] == content
        assert len(list((tmp_path / "files").glob("*/*.data"))) == 1
        restored.artifacts.delete(parent, original["id"])
        assert restored.snapshot()["artifacts"][0]["available"] is False
        await restored.start("continue despite unavailable document", "codex", latest["id"])
        await restored.job
        assert restored.snapshot()["artifacts"] == []
        assert contexts[-1][0]["file_context_warning"]
        await restored.start("an unrelated request", "codex")
        await restored.job
        assert contexts[-1][0]["artifacts"] == []
        assert contexts[-1][0]["file_context_warning"] is None
        await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com", "https://example.com/\n"])
def test_navigation_rejects_non_web_and_credentials(url):
    with pytest.raises(ValueError):
        web_url(url)


def test_model_cannot_supply_code_or_claim_a_click_is_safe():
    with pytest.raises(ValueError):
        Action.parse(json.dumps({"kind": "evaluate", "target": "", "value": "steal()", "reason": "safe"}))
    with pytest.raises(ValueError):
        Action.parse(json.dumps({"kind": "click", "target": "#pay", "value": "", "reason": "safe"}))
    assert needs_approval(Action("click", "1", reason="just a harmless search"))
    assert needs_approval(Action("press", "1", "Enter"))


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_provider_structured_response(provider):
    action = Action("finish", value="Observed result")
    if provider == "codex":
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(action.to_dict())}}
    else:
        event = {"event": "result", "result": {"status": "SUCCESS", "structured_output": action.to_dict()}}
    assert parse_output(provider, "non-json banner\n" + json.dumps(event)) == action


def test_provider_rejects_error_and_missing_output():
    with pytest.raises(RuntimeError):
        parse_output("codex", '{"type":"turn.failed"}')
    with pytest.raises(RuntimeError):
        parse_output("antigravity", '{"event":"result","result":{"status":"ERROR"}}')
    with pytest.raises(RuntimeError):
        parse_output("codex", '{}')


def test_latest_completed_result_survives_restart_without_persisting_task(tmp_path):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *args):
            return Action("finish", value=json.dumps(final_payload()))
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("PRIVATE ORIGINAL REQUEST", "codex")
        await app.job
        restored = Assistant(Browser(), tmp_path, planner_factory=Finish)
        state = restored.snapshot()
        assert state["restored"] and state["status"] == "verified"
        assert state["report"]["summary"] == "answer"
        assert "PRIVATE" not in next(tmp_path.glob("*.json")).read_text()
        await restored.start("follow up", "codex", state["id"])
        await restored.job
        assert not restored.snapshot()["restored"]
    asyncio.run(run())


def test_read_action_failure_reobserves_and_can_finish_without_handoff(tmp_path):
    class Read(Planner):
        async def propose(self, *args):
            self.calls += 1
            return Action("navigate", value="https://example.com") if self.calls == 1 else Action("finish", value="available result")
    async def run():
        browser = Browser()
        browser.fail = True
        app = Assistant(browser, tmp_path, planner_factory=Read)
        await app.start("read list", "codex")
        await app.job
        assert app.snapshot()["status"] == "unverified"
        assert app.snapshot()["result"] == "available result"
        assert app.snapshot()["last_error"]["action"] == "navigate"
        assert len(browser.executed) == 1
    asyncio.run(run())


def test_repeated_read_failure_hands_off_with_specific_reason(tmp_path):
    class Read:
        def __init__(self, *_args): pass
        async def propose(self, *args): return Action("navigate", value="https://example.com")
    async def run():
        browser = Browser()
        browser.fail = True
        app = Assistant(browser, tmp_path, planner_factory=Read)
        await app.start("read list", "codex")
        pending = await waiting(app)
        assert len(browser.executed) == 3
        assert pending["failure"] and "فتح الرابط" in pending["message"]
        assert "شراء" not in pending["message"]
        await app.control("stop")
    asyncio.run(run())


class Browser:
    def __init__(self):
        self.fingerprint = "first"
        self.executed = []
        self.fail = False

    async def start(self):
        pass

    async def observe(self):
        return {
            "url": "https://example.com", "title": "Example", "text": "Observed evidence",
            "fingerprint": self.fingerprint,
        }

    def preview(self, action, snapshot):
        return {"action": action.to_dict(), "fingerprint": snapshot["fingerprint"]}

    async def execute(self, action):
        self.executed.append(action)
        if self.fail:
            raise TimeoutError()

    async def close(self):
        pass


class Planner:
    def __init__(self, *_args):
        self.calls = 0

    async def propose(self, *args):
        self.calls += 1
        return Action("click", "1", reason="submit test") if self.calls == 1 else Action("finish", value="done")


async def waiting(app):
    for _ in range(200):
        if app.snapshot().get("pending"):
            return app.snapshot()["pending"]
        if app.job.done():
            raise AssertionError(app.snapshot())
        await asyncio.sleep(0.005)
    raise AssertionError("did not wait for user")


def test_rejection_means_no_browser_action(tmp_path):
    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("test", "codex")
        pending = await waiting(app)
        assert not browser.executed
        await app.control("reject", pending["token"])
        await app.job
        assert not browser.executed
        assert app.snapshot()["status"] == "cancelled"
    asyncio.run(run())


def test_approval_single_use_and_no_concurrent_tasks(tmp_path):
    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("test", "codex")
        pending = await waiting(app)
        with pytest.raises(ValueError):
            await app.start("second", "codex")
        with pytest.raises(ValueError):
            await app.control("approve", "wrong-token")
        await app.control("approve", pending["token"])
        with pytest.raises(ValueError):
            await app.control("approve", pending["token"])
        await app.job
        assert len(browser.executed) == 1
        assert app.snapshot()["status"] == "unverified"
    asyncio.run(run())


def test_stale_approval_cannot_execute(tmp_path):
    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("test", "codex")
        pending = await waiting(app)
        browser.fingerprint = "changed"
        await app.control("approve", pending["token"])
        await app.job
        assert not browser.executed
        assert any("أُلغي" in e["message"] for e in app.snapshot()["events"])
    asyncio.run(run())


def test_stop_during_planning_cancels_model(tmp_path):
    cancelled = []
    class Slow:
        def __init__(self, *_args): pass
        async def propose(self, *args):
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.append(True)
    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Slow)
        await app.start("test", "codex")
        await asyncio.sleep(0.02)
        await app.control("stop")
        assert cancelled and not browser.executed
        assert app.snapshot()["status"] == "cancelled"
    asyncio.run(run())


def test_unknown_submission_result_hands_off_without_retry(tmp_path):
    async def run():
        browser = Browser()
        browser.fail = True
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("test", "codex")
        pending = await waiting(app)
        await app.control("approve", pending["token"])
        for _ in range(100):
            await asyncio.sleep(0.005)
            pending = app.snapshot()["pending"]
            if pending and pending["type"] == "handoff":
                break
        assert pending["type"] == "handoff"
        assert len(browser.executed) == 1
        await app.control("stop")
        assert len(browser.executed) == 1
    asyncio.run(run())


def test_report_excludes_prompt_and_form_values(tmp_path):
    async def run():
        class Fill(Planner):
            async def propose(self, *args):
                self.calls += 1
                return Action("fill", "1", "PRIVATE-FORM-VALUE") if self.calls == 1 else Action("finish", value="done")
        app = Assistant(Browser(), tmp_path, planner_factory=Fill)
        await app.start("PRIVATE-PROMPT", "codex")
        pending = await waiting(app)
        await app.control("approve", pending["token"])
        await app.job
        report = next(tmp_path.glob("*.json"))
        assert "PRIVATE" not in report.read_text()
        assert report.stat().st_mode & 0o777 == 0o600
    asyncio.run(run())


def test_finish_with_matching_browser_evidence_is_verified(tmp_path):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *_args):
            return Action("finish", value=json.dumps(final_payload()))
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("read the page", "codex")
        await app.job
        state = app.snapshot()
        assert state["status"] == "verified"
        assert state["completion"]["done"] == ["read the result"]
        assert state["completion"]["evidence"][0]["source_id"] == "S1"
    asyncio.run(run())


def test_early_plain_finish_is_unverified_not_completed(tmp_path):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *_args):
            return Action("finish", value="The purchase succeeded.")
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("buy", "codex")
        await app.job
        state = app.snapshot()
        assert state["status"] == "unverified"
        assert "دون بنية ودليل" in state["completion"]["reason"]
    asyncio.run(run())


def test_verified_claim_without_matching_page_evidence_is_unverified(tmp_path):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *_args):
            return Action("finish", value=json.dumps(final_payload(quote="invented confirmation")))
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("buy", "codex")
        await app.job
        assert app.snapshot()["status"] == "unverified"
    asyncio.run(run())


def test_declared_partial_result_shows_done_and_remaining_work(tmp_path):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *_args):
            payload = final_payload(status="partial", done=["compared one offer"], remaining=["open the second offer"])
            payload["completion"]["evidence"] = []
            return Action("finish", value=json.dumps(payload))
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)
        await app.start("compare offers", "codex")
        await app.job
        state = app.snapshot()
        assert state["status"] == "partial"
        assert state["completion"]["remaining"] == ["open the second offer"]
    asyncio.run(run())


def test_ambiguous_consequential_action_cannot_become_verified_from_early_answer(tmp_path):
    class Ambiguous:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, *_args):
            self.calls += 1
            if self.calls == 1:
                return Action("click", "1", reason="submit test")
            return Action("finish", value="The purchase succeeded.")
    async def run():
        browser = Browser()
        browser.fail = True
        app = Assistant(browser, tmp_path, planner_factory=Ambiguous)
        await app.start("buy", "codex")
        approval = await waiting(app)
        await app.control("approve", approval["token"])
        handoff = None
        for _ in range(100):
            await asyncio.sleep(0.005)
            candidate = app.snapshot()["pending"]
            if candidate and candidate["type"] == "handoff":
                handoff = candidate
                break
        assert handoff is not None
        assert handoff["type"] == "handoff" and handoff["failure"]
        await app.control("resume", handoff["token"])
        await app.job
        state = app.snapshot()
        assert state["last_error"]["action"] == "click"
        assert state["status"] == "unverified"
        assert len(browser.executed) == 1
    asyncio.run(run())


def test_unknown_effect_cannot_be_verified_by_unrelated_fresh_page_text(tmp_path):
    class Proposals:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, *_args):
            self.calls += 1
            return Action("click", "1") if self.calls == 1 else Action("finish", value=json.dumps(final_payload()))

    async def run():
        browser = Browser()
        browser.fail = True
        app = Assistant(browser, tmp_path, planner_factory=Proposals)
        await app.start("send once", "codex")
        approval = await waiting(app)
        await app.control("approve", approval["token"])
        handoff = await waiting_after(app, approval["token"])
        assert handoff["type"] == "handoff"
        await app.control("resume", handoff["token"])
        await app.job
        state = app.snapshot()
        assert state["status"] == state["report"]["completion"]["status"] == "unverified"
        assert state["uncertain_actions"][0]["status"] == "unknown"
        assert "إرسال غير مؤكد" in state["completion"]["reason"]
        restored = Assistant(Browser(), tmp_path, planner_factory=Proposals)
        assert restored.snapshot()["operations"][0]["id"] == state["operations"][0]["id"]
    asyncio.run(run())


async def waiting_after(app, previous_token):
    for _ in range(200):
        value = app.snapshot()["pending"]
        if value and value["token"] != previous_token:
            return value
        if app.job.done():
            raise AssertionError(app.snapshot())
        await asyncio.sleep(0.005)
    raise AssertionError("no new pending decision")


@pytest.mark.parametrize("occurred", [True, False])
def test_recovered_unknown_write_requires_manual_reconciliation_before_retry(tmp_path, occurred):
    import uuid

    class Proposals:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, *_args):
            self.calls += 1
            return Action("click", "1") if self.calls < 3 else Action("finish", value="available report")

    async def run():
        old = Assistant(Browser(), tmp_path, planner_factory=Proposals)
        attempt = old.journal.begin(Action("click", "1"), "https://example.com", None,
                                    uuid.uuid4().hex, uuid.uuid4().hex)
        old.journal.settle(attempt, "unknown")
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Proposals)
        await app.start("inspect and continue the request", "codex")
        handoff = await waiting(app)
        assert handoff["type"] == "handoff" and not browser.executed
        assert app.snapshot()["approval_requests"] == 0
        await app.control("effect_occurred" if occurred else "effect_absent", attempt)
        assert app.snapshot()["pending"]["token"] == handoff["token"]
        assert not browser.executed
        await app.control("resume", handoff["token"])
        if not occurred:
            approval = await waiting_after(app, handoff["token"])
            assert approval["type"] == "approval" and not browser.executed
            await app.control("approve", approval["token"])
        await app.job
        assert len(browser.executed) == (0 if occurred else 1)
        assert not app.snapshot()["uncertain_actions"]
    asyncio.run(run())


def test_cancelled_inflight_action_is_persisted_as_unknown_and_can_be_checked_when_idle(tmp_path):
    async def run():
        sending = asyncio.Event()

        class SlowBrowser(Browser):
            async def execute(self, action):
                sending.set()
                await asyncio.Future()

        app = Assistant(SlowBrowser(), tmp_path, planner_factory=Planner)
        await app.start("send", "codex")
        approval = await waiting(app)
        await app.control("approve", approval["token"])
        await asyncio.wait_for(sending.wait(), 1)
        attempt = app.snapshot()["operations"][0]["id"]
        with pytest.raises(ValueError, match="قيد التنفيذ"):
            await app.control("effect_absent", attempt)
        await app.control("stop")
        assert app.snapshot()["status"] == "cancelled"
        assert app.snapshot()["uncertain_actions"][0]["status"] == "unknown"
        recovered = Assistant(Browser(), tmp_path)
        assert recovered.snapshot()["status"] == "idle"
        await recovered.control("effect_occurred", attempt)
        assert not recovered.snapshot()["uncertain_actions"]
    asyncio.run(run())


def test_journal_must_be_durable_before_browser_execution(tmp_path, monkeypatch):
    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("تعذّر حفظ محاولة الإرسال")

        monkeypatch.setattr(app.journal, "begin", unavailable)
        await app.start("send", "codex")
        approval = await waiting(app)
        await app.control("approve", approval["token"])
        await app.job
        assert app.snapshot()["status"] == "failed"
        assert not browser.executed
    asyncio.run(run())


def test_unavailable_journal_preserves_answer_without_claiming_verification(tmp_path, monkeypatch):
    class Finish:
        def __init__(self, *_args): pass
        async def propose(self, *_args):
            return Action("finish", value=json.dumps(final_payload()))

    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Finish)

        def unavailable(*_args):
            raise RuntimeError("storage unavailable")

        monkeypatch.setattr(app.journal, "has_uncertain", unavailable)
        await app.start("read the page", "codex")
        await app.job
        state = app.snapshot()
        assert state["status"] == state["report"]["completion"]["status"] == "unverified"
        assert state["report"]["summary"] == "answer"
        assert "تعذّر فحص سجل" in state["completion"]["reason"]
        assert not app.browser.executed
    asyncio.run(run())


def test_successful_browser_return_without_predeclared_condition_is_unverified(tmp_path):
    class Proposals:
        def __init__(self, *_args): self.calls = 0
        async def propose(self, *_args):
            self.calls += 1
            return Action("click", "1") if self.calls == 1 else Action("finish", value=json.dumps(final_payload()))

    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Proposals)
        await app.start("submit and verify", "codex")
        approval = await waiting(app)
        assert approval["effect_check"] is None
        await app.control("approve", approval["token"])
        await app.job
        state = app.snapshot()
        assert len(app.browser.executed) == 1
        assert state["status"] == "unverified"
        assert state["completion"]["evidence"][0]["observed_after_action"]
        assert state["completion"]["effect_checks"][0]["status"] == "no_condition"
    asyncio.run(run())
