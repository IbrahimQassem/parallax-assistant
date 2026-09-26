"""General task planning and user steering, without model calls or external writes."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.planner import InvalidProposal, parse_output
from parallax.assistant.runtime import Assistant
from parallax.assistant.task_plan import parse_plan


def plan():
    return {
        "goal": "Compare two services and prepare a request",
        "constraints": ["Use monthly prices", "Do not submit the request"],
        "success_criteria": ["Both prices have read sources", "The draft includes the cheaper option"],
        "steps": [
            {"id": "read", "title": "Read the offers", "depends_on": [], "status": "in_progress"},
            {"id": "draft", "title": "Prepare a draft", "depends_on": ["read"], "status": "pending"},
        ],
    }


def plan_action(value=None, revision=0):
    return Action("plan", str(revision), json.dumps(value or plan()), "Organize the requested work")


class Browser:
    def __init__(self):
        self.executed = []
        self.reads = 0

    async def start(self): pass

    async def observe(self):
        self.reads += 1
        return {"url": "https://example.com/offers", "text": "Two offers", "fingerprint": "same"}

    def preview(self, action, snapshot):
        return {"action": action.to_dict(), "fingerprint": snapshot["fingerprint"],
                "target": {"tag": "button", "label": "Send"}}

    async def execute(self, action):
        self.executed.append(action)

    async def close(self): pass


async def pending(app):
    for _ in range(200):
        value = app.snapshot()["pending"]
        if value:
            return value
        if app.job.done():
            raise AssertionError(app.snapshot())
        await asyncio.sleep(0.005)
    raise AssertionError("no pending user decision")


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(approved=True),
    lambda p: p.update(goal=" "),
    lambda p: p.update(success_criteria=[]),
    lambda p: p.update(steps=[]),
    lambda p: p["steps"][0].update(depends_on=["draft"]),
    lambda p: p["steps"][0].update(depends_on=["read"]),
    lambda p: p["steps"][1].update(depends_on=["unknown"]),
    lambda p: p["steps"][1].update(depends_on=["read", "read"]),
    lambda p: p["steps"][1].update(id="read"),
    lambda p: p["steps"][1].update(status="done"),
    lambda p: p["steps"][1].update(status="in_progress"),
    lambda p: p["steps"][0].update(status="verified"),
    lambda p: p["steps"][0].update(selector="#pay"),
    lambda p: p.update(constraints=["x" * 601]),
    lambda p: p.update(success_criteria=[42]),
])
def test_plan_rejects_conflicting_dependencies_and_authority_fields(mutation):
    payload = plan()
    mutation(payload)
    with pytest.raises(ValueError, match="خطة المهمة غير صالحة"):
        parse_plan(json.dumps(payload))


@pytest.mark.parametrize("raw", ["null", "[]", "bad json", '"text"', "x" * 12001])
def test_invalid_plan_has_a_safe_error(raw):
    with pytest.raises(ValueError, match="خطة المهمة غير صالحة"):
        parse_plan(raw)


@pytest.mark.parametrize("provider", ["codex", "antigravity"])
def test_plan_uses_same_data_grammar_for_each_provider(provider):
    action = plan_action()
    if provider == "codex":
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(action.to_dict())}}
    else:
        event = {"event": "result", "result": {"status": "SUCCESS", "structured_output": action.to_dict()}}
    parsed = parse_output(provider, json.dumps(event))
    assert parsed == action
    assert parse_plan(parsed.value) == plan()
    for target in ["", "-1", "0.5", "١"]:
        with pytest.raises(ValueError):
            Action.parse(json.dumps({**action.to_dict(), "target": target}))


def test_plan_survives_steps_but_is_not_authority_evidence_or_saved_prompt(tmp_path):
    seen = []

    class Planner:
        def __init__(self, *_args): pass

        async def propose(self, task, snapshot, history):
            seen.append(copy.deepcopy(snapshot["assistant_context"]))
            if len(seen) == 1:
                payload = plan()
                payload["goal"] = "PRIVATE-PLAN-INTENT"
                return plan_action(payload)
            if len(seen) == 2:
                return Action("click", "1", reason="This plan already authorized sending")
            updated = plan()
            for step in updated["steps"]:
                step["status"] = "done"
            if len(seen) == 3:
                return plan_action(updated, revision=1)
            return Action("finish", value="All done according to my plan")

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("PRIVATE-ORIGINAL-REQUEST", "codex")
        approval = await pending(app)
        assert approval["type"] == "approval"
        assert not browser.executed
        assert app.snapshot()["task_plan"]["goal"] == "PRIVATE-PLAN-INTENT"
        await app.control("approve", approval["token"])
        await app.job
        assert len(browser.executed) == 1
        assert app.snapshot()["plan_revision"] == 2
        assert app.snapshot()["status"] == "unverified"
        assert seen[2]["task_plan"]["constraints"] == plan()["constraints"]
        saved = next(tmp_path.glob("*.json")).read_text()
        assert "PRIVATE-" not in saved
        assert "task_plan" not in json.loads(saved)
    asyncio.run(run())


def test_stale_or_invalid_plan_is_replanned_without_browser_execution(tmp_path):
    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, history):
            self.calls += 1
            if self.calls == 1:
                return plan_action(revision=99)
            if self.calls == 2:
                assert history[-1].get("controller_error")
                return Action("plan", "0", '{"authorization":"PRIVATE-INVALID-PLAN"}')
            if self.calls == 3:
                return plan_action()
            assert snapshot["assistant_context"]["plan_revision"] == 1
            return Action("finish", value="No browser effect")

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("general request", "codex")
        await app.job
        assert not browser.executed
        assert app.snapshot()["plan_revision"] == 1
        assert "PRIVATE-INVALID-PLAN" not in next(tmp_path.glob("*.json")).read_text()
    asyncio.run(run())


def test_user_revision_cancels_old_planning_and_supplies_original_constraints(tmp_path):
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()
        seen = []

        class Planner:
            def __init__(self, *_args): self.calls = 0

            async def propose(self, task, snapshot, history):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()
                seen.append(copy.deepcopy(snapshot["assistant_context"]))
                if self.calls == 2:
                    return plan_action()
                return Action("finish", value="Replanned")

        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("compare with original budget", "codex")
        await asyncio.wait_for(started.wait(), 1)
        await app.control("revise", app.snapshot()["id"], note="PRIVATE-UPDATE: monthly plans only")
        await asyncio.wait_for(app.job, 1)
        assert cancelled.is_set() and not browser.executed
        assert seen[0]["original_request"] == "compare with original budget"
        assert seen[0]["user_updates"] == [{"user_update": "PRIVATE-UPDATE: monthly plans only"}]
        assert seen[0]["context_revision"] == 1 and seen[0]["plan_stale"]
        assert not app.snapshot()["plan_stale"]
        assert "PRIVATE-UPDATE" not in next(tmp_path.glob("*.json")).read_text()
    asyncio.run(run())


def test_revision_invalidates_pending_approval_without_executing_it(tmp_path):
    seen = []

    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                return Action("click", "1", reason="Send the request")
            seen.append(snapshot["assistant_context"])
            return Action("finish", value="Kept as draft")

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("prepare a request", "codex")
        approval = await pending(app)
        await app.control("revise", app.snapshot()["id"], note="Do not send, keep a draft")
        with pytest.raises(ValueError):
            await app.control("approve", approval["token"])
        await app.job
        assert not browser.executed
        assert seen[0]["user_updates"][0]["user_update"] == "Do not send, keep a draft"
    asyncio.run(run())


def test_revision_during_manual_handoff_does_not_resume_observation(tmp_path):
    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            if self.calls == 1:
                return Action("handoff", reason="Enter the code in the browser")
            assert snapshot["assistant_context"]["user_updates"] == [{"user_update": "Use the second account"}]
            return Action("finish", value="Reviewed new state")

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("recover my account", "codex")
        handoff = await pending(app)
        reads = browser.reads
        await app.control("revise", app.snapshot()["id"], note="Use the second account")
        await asyncio.sleep(0)
        assert app.snapshot()["pending"]["token"] == handoff["token"]
        assert browser.reads == reads and not app.job.done()
        await app.control("resume", handoff["token"])
        await app.job
        assert browser.reads > reads
    asyncio.run(run())


def test_revision_during_inflight_action_preserves_its_outcome(tmp_path):
    async def run():
        sending, finish_send = asyncio.Event(), asyncio.Event()

        class InflightBrowser(Browser):
            async def execute(self, action):
                sending.set()
                await finish_send.wait()
                await super().execute(action)

        class Planner:
            def __init__(self, *_args): self.calls = 0

            async def propose(self, _task, snapshot, history):
                self.calls += 1
                if self.calls == 1:
                    return Action("click", "1", reason="Send")
                assert snapshot["assistant_context"]["user_updates"]
                assert any(item.get("action", {}).get("kind") == "click" for item in history)
                return Action("finish", value="The sent action was not undone")

        browser = InflightBrowser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("send once", "codex")
        approval = await pending(app)
        await app.control("approve", approval["token"])
        await asyncio.wait_for(sending.wait(), 1)
        await app.control("revise", app.snapshot()["id"], note="Do not send any more requests")
        finish_send.set()
        await asyncio.wait_for(app.job, 1)
        assert len(browser.executed) == 1
        assert app.snapshot()["status"] == "unverified"
    asyncio.run(run())


def test_followup_keeps_prior_plan_context_but_new_task_does_not(tmp_path):
    contexts = []

    class Planner:
        def __init__(self, *_args): self.calls = 0

        async def propose(self, _task, snapshot, _history):
            self.calls += 1
            contexts.append(copy.deepcopy(snapshot["assistant_context"]))
            return plan_action() if self.calls == 1 else Action("finish", value="Context only")

    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("compare", "codex")
        await app.job
        await app.start("prepare", "codex", app.snapshot()["id"])
        await app.job
        assert contexts[2]["previous_task_context"]["previous_plan"] == plan()
        assert contexts[2]["task_plan"] is None
        await app.start("independent request", "codex")
        await app.job
        assert contexts[4]["previous_task_context"] is None
    asyncio.run(run())


def test_old_task_revision_is_rejected_and_update_limit_never_blocks_stop(tmp_path):
    class Planner:
        def __init__(self, *_args): pass

        async def propose(self, *_args):
            return Action("handoff", reason="Manual check")

    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("first request", "codex")
        await pending(app)
        previous_id = app.snapshot()["id"]
        await app.control("stop")
        await app.start("new request", "codex")
        await pending(app)
        with pytest.raises(ValueError):
            await app.control("revise", previous_id, note="Late update for old request")
        assert app.notes == [] and app.snapshot()["context_revision"] == 0
        for i in range(20):
            await app.control("revise", app.snapshot()["id"], note=f"Constraint {i}")
        with pytest.raises(ValueError):
            await app.control("revise", app.snapshot()["id"], note="Beyond the bound")
        await app.control("stop", note="Text left in a form")
        assert app.snapshot()["status"] == "cancelled"
    asyncio.run(run())


def test_pause_during_observation_never_sends_that_snapshot_to_planner(tmp_path):
    async def run():
        reading, finish_read = asyncio.Event(), asyncio.Event()
        calls = []

        class SlowBrowser(Browser):
            async def observe(self):
                reading.set()
                await finish_read.wait()
                return await super().observe()

        class Planner:
            def __init__(self, *_args): pass

            async def propose(self, *_args):
                calls.append(True)
                return Action("finish", value="Observed after resume")

        app = Assistant(SlowBrowser(), tmp_path, planner_factory=Planner)
        await app.start("read", "codex")
        await asyncio.wait_for(reading.wait(), 1)
        await app.control("pause")
        finish_read.set()
        handoff = await pending(app)
        assert handoff["type"] == "handoff" and not calls
        await app.control("resume", handoff["token"])
        await app.job
        assert calls == [True]
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    "PRIVATE response text", '```json\n{}\n```',
    json.dumps(plan_action().to_dict()) + '\n{"kind":"click"}',
    {"kind": "evaluate", "target": "", "value": "PRIVATE script", "reason": ""},
    [],
])
def test_provider_rejects_malformed_or_multiple_actions_without_exposing_text(response):
    envelope = {"event": "result", "result": {"status": "SUCCESS", "structured_output": response}}
    with pytest.raises(InvalidProposal, match="خطوة JSON صالحة") as failure:
        parse_output("antigravity", json.dumps(envelope))
    assert "PRIVATE" not in str(failure.value)


@pytest.mark.parametrize("always_invalid", [False, True])
def test_bad_proposal_is_retried_with_feedback_and_a_bound_before_any_action(tmp_path, always_invalid):
    calls = []

    class Planner:
        def __init__(self, *_args): pass

        async def propose(self, _task, _snapshot, history):
            calls.append(True)
            if len(calls) > 1:
                assert "No action was executed" in history[-1]["controller_error"]
            if always_invalid or len(calls) == 1:
                raise InvalidProposal("PRIVATE discarded provider response")
            return Action("finish", value="No action was needed")

    async def run():
        browser = Browser()
        app = Assistant(browser, tmp_path, planner_factory=Planner)
        await app.start("read", "codex")
        await app.job
        assert not browser.executed
        assert len(calls) == (3 if always_invalid else 2)
        assert app.snapshot()["status"] == ("failed" if always_invalid else "unverified")
        assert "PRIVATE" not in next(tmp_path.glob("*.json")).read_text()
    asyncio.run(run())
