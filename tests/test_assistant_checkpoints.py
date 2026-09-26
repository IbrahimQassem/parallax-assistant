import asyncio
import json
import pytest

from parallax.assistant.actions import Action
from parallax.assistant.attempts import AttemptBlocked
from parallax.assistant.checkpoints import CheckpointConflict
from parallax.assistant.runtime import Assistant


class Browser:
    def __init__(self): self.reads = 0; self.sends = 0
    async def start(self): pass
    async def close(self): pass
    async def observe(self):
        self.reads += 1
        return {"url": "https://example.com/", "text": "CURRENT-PAGE-PRIVATE", "fingerprint": "current"}
    def preview(self, action, snapshot):
        return {"action": action.to_dict(), "fingerprint": snapshot["fingerprint"],
                "target": {"tag": "button", "label": "Send", "in_form": True}}
    async def execute(self, action): self.sends += 1


class Planner:
    contexts = []
    def __init__(self, provider):
        if provider != "codex": raise ValueError("Unknown provider")
    async def propose(self, task, snapshot, *_args):
        self.contexts.append(snapshot["assistant_context"])
        return Action("click", "1") if task == "send" else Action("handoff", reason="Wait")


async def waiting(app):
    for _ in range(100):
        if app.snapshot()["pending"]: return app.snapshot()["pending"]
        await asyncio.sleep(.01)
    raise AssertionError(app.snapshot())


def test_selected_brief_survives_restart_without_replaying_prompt_approval_or_browser(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("RAW-ORIGINAL-PRIVATE", "codex", consent_mode="review")
        old_pending = await waiting(app)
        source = app.state["id"]
        with app.artifacts.staging(source) as stage:
            stage.write_bytes(b"SELECTED-FILE")
            saved = app.artifacts.commit(source, stage, "document.txt")
        await app.save_checkpoint(source, "continue selected work", 7)
        assert app.snapshot()["status"] == "cancelled"
        draft = app.checkpoints.load(source)
        assert draft["status"] == "ready" and draft["brief"] == "continue selected work"
        assert app.checkpoints.path(source).stat().st_mode & 0o077 == 0
        assert app.saved_reports.retention(source)["days"] == 7
        stored = app.checkpoints.path(source).read_text()
        assert "RAW-ORIGINAL-PRIVATE" not in stored and "CURRENT-PAGE-PRIVATE" not in stored
        assert old_pending["token"] not in stored and "task_plan" not in stored
        await app.close()
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert restored.job is None and restored.browser.reads == 0
        assert restored.checkpoints.load(source)["status"] == "ready"
        await restored.resume_checkpoint(source, "codex", 1)
        new_pending = await waiting(restored)
        assert new_pending["token"] != old_pending["token"]
        with pytest.raises(ValueError): await restored.control("resume", old_pending["token"])
        state = restored.snapshot()
        assert state["operation_group"] == source and state["parent_id"] == source
        assert state["consent_mode"] == "review"
        assert restored.browser.reads == 1
        assert restored.request_authority.request == "continue selected work" and not restored.request_authority.used
        assert state["task_plan"] is None and state["operation_authority"] is None
        assert Planner.contexts[-1]["previous_task_context"]["saved_continuation"] == "continue selected work"
        inherited, = state["artifacts"]
        assert inherited["id"] != saved["id"]
        assert restored.artifacts.read(state["id"], inherited["id"])[1] == b"SELECTED-FILE"
        await restored.control("stop")
        with pytest.raises(ValueError): await restored.resume_checkpoint(source, "codex", 1)
        await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["returned", "unknown"])
def test_resumed_checkpoint_keeps_completed_and_uncertain_effect_barriers(tmp_path, outcome):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        target = {"tag": "button", "label": "Send", "in_form": True}
        attempt = app.journal.begin(Action("click", "1"), "https://example.com/", target, source, source)
        app.journal.settle(attempt, outcome)
        await app.save_checkpoint(source, "send", 1)
        await app.close()
        resumed = Assistant(Browser(), tmp_path, planner_factory=Planner, max_steps=2)
        await resumed.resume_checkpoint(source, "codex", 1)
        if outcome == "unknown":
            assert (await waiting(resumed))["type"] == "handoff"
        else:
            await resumed.job
            assert resumed.snapshot()["status"] == "limited"
        # Neither an already returned step nor an uncertain one reaches approval.
        assert resumed.browser.sends == 0
        assert resumed.snapshot()["approval_requests"] == 0
        with pytest.raises(AttemptBlocked):
            resumed.journal.check(Action("click", "2"), "https://example.com/", target, source)
        await resumed.close()
    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_checkpoint_deletion_and_expiry_revoke_resume_and_remove_selected_content(tmp_path, expired):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        await app.save_checkpoint(source, "saved brief", 1)
        if expired:
            deadline = app.saved_reports.retention(source)["expires_at"]
            app.saved_reports.clock = lambda: deadline
            with pytest.raises(ValueError): await app.resume_checkpoint(source, "codex", 1)
            assert app.checkpoints.path(source).exists()  # Blocked before physical cleanup.
            await app.expire_reports()
        else:
            await app.delete_saved_task(source)
        assert not app.checkpoints.path(source).exists()
        with pytest.raises(ValueError): await app.resume_checkpoint(source, "codex", 1)
        assert app.browser.reads == 1
        await app.close()
    asyncio.run(run())


def test_invalid_brief_provider_or_changed_file_does_not_consume_ready_checkpoint(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        for brief in ["", "x" * 4001, "password=private", "https://example.com/reset?token=private"]:
            with pytest.raises(ValueError): await app.save_checkpoint(source, brief, 7)
        assert app.state["status"] == "waiting" and not app.checkpoints.path(source).exists()
        with app.artifacts.staging(source) as stage:
            stage.write_bytes(b"ORIGINAL")
            saved = app.artifacts.commit(source, stage, "file.txt")
        await app.save_checkpoint(source, "continue", 7)
        with pytest.raises(ValueError): await app.resume_checkpoint(source, "bad", 1)
        assert app.checkpoints.load(source)["status"] == "ready"
        (tmp_path / "files" / source / (saved["id"] + ".data")).write_bytes(b"CHANGED")
        with pytest.raises(ValueError): await app.resume_checkpoint(source, "codex", 1)
        assert app.checkpoints.load(source)["status"] == "ready"
        await app.close()
    asyncio.run(run())


def test_checkpoint_claim_survives_crash_boundary_and_corruption_cannot_launch(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        await app.save_checkpoint(source, "continue", 7)
        app.checkpoints.claim(source, 1)  # Process loss before a new task is created.
        await app.close()
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        with pytest.raises(ValueError): await restored.resume_checkpoint(source, "codex", 1)
        assert restored.browser.reads == 0
        app.checkpoints.path(source).write_text("{broken")
        with pytest.raises(ValueError): await restored.resume_checkpoint(source, "codex", 1)
        assert restored.checkpoints.public(source) is None
        await restored.delete_saved_task(source)
        await restored.close()
    asyncio.run(run())


def test_expiry_of_checkpoint_stops_its_live_continuation_without_browser_reads(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        await app.save_checkpoint(source, "continue", 1)
        await app.resume_checkpoint(source, "codex", 1)
        pending = await waiting(app)
        reads = app.browser.reads
        deadline = app.saved_reports.retention(source)["expires_at"]
        app.saved_reports.clock = lambda: deadline
        with pytest.raises(ValueError): await app.control("resume", pending["token"])
        await app.expire_reports()
        assert app.state["status"] == "cancelled" and app.browser.reads == reads
        assert not app.checkpoints.path(source).exists() and app.previous_context is None
        await app.close()
    asyncio.run(run())


def test_claim_persistence_failure_never_starts_a_browser_or_task(tmp_path, monkeypatch):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        await app.save_checkpoint(source, "continue", 7)
        await app.close()
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        def fail(*_args): raise OSError("Synthetic write failure")
        monkeypatch.setattr(restored.saved_reports, "_write_metadata", fail)
        with pytest.raises(OSError): await restored.resume_checkpoint(source, "codex", 1)
        assert restored.browser.reads == 0 and restored.job is None
        assert restored.checkpoints.load(source)["status"] == "ready"
        await restored.close()
    asyncio.run(run())


def test_saving_requires_a_stable_task_and_does_not_replace_an_existing_checkpoint(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        source = app.state["id"]
        with pytest.raises(ValueError): await app.save_checkpoint(source, "too early", 7)
        await waiting(app)
        with pytest.raises(ValueError): await app.save_checkpoint("0" * 32, "wrong task", 7)
        await app.save_checkpoint(source, "chosen", 7)
        with pytest.raises(ValueError): await app.save_checkpoint(source, "replacement", 7)
        assert app.checkpoints.load(source)["brief"] == "chosen"
        await app.close()
    asyncio.run(run())


def test_maximum_unicode_brief_fits_serialized_storage_and_remains_readable(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await app.start("original", "codex")
        await waiting(app)
        source = app.state["id"]
        brief = "📝" * 4000
        await app.save_checkpoint(source, brief, 1)
        assert app.checkpoints.load(source)["brief"] == brief
        await app.close()
    asyncio.run(run())


async def saved_checkpoint(tmp_path):
    app = Assistant(Browser(), tmp_path, planner_factory=Planner)
    await app.start("original", "codex", consent_mode="review")
    await waiting(app)
    source = app.state["id"]
    await app.save_checkpoint(source, "original brief", 7)
    return app, source


def test_edit_survives_restart_preserves_lifetime_files_and_receipts_and_resumes_reviewed_text(tmp_path):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        policy = app.saved_reports.retention(source)
        report = app.saved_reports.path(source).read_bytes()
        with app.artifacts.staging(source) as stage:
            stage.write_bytes(b"SELECTED-FILE")
            app.artifacts.commit(source, stage, "document.txt")
        attempt = app.journal.begin(Action("click", "1"), "https://example.com/", {"label": "Send"}, source, source)
        app.journal.settle(attempt, "returned")
        changed = await app.update_checkpoint(source, "new constraints and remaining work", 1)
        assert changed["revision"] == 2 and changed["status"] == "ready"
        assert app.saved_reports.retention(source) == policy
        assert app.saved_reports.path(source).read_bytes() == report
        assert "original brief" not in app.checkpoints.path(source).read_text()
        assert app.checkpoints.path(source).stat().st_mode & 0o077 == 0
        await app.close()
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert restored.browser.reads == 0
        with pytest.raises(CheckpointConflict): await restored.resume_checkpoint(source, "codex", 1)
        assert restored.job is None and restored.checkpoints.load(source)["status"] == "ready"
        await restored.resume_checkpoint(source, "codex", 2)
        await waiting(restored)
        state = restored.snapshot()
        assert state["task"] == "new constraints and remaining work" and state["consent_mode"] == "review"
        assert state["operation_group"] == source and state["operations"][0]["id"] == attempt
        reference, = state["artifacts"]
        assert restored.artifacts.read(state["id"], reference["id"])[1] == b"SELECTED-FILE"
        await restored.control("stop")
        with pytest.raises(ValueError): await restored.update_checkpoint(source, "too late", 2)
        await restored.close()
    asyncio.run(run())


def test_competing_edits_and_resume_keep_only_the_reviewed_revision(tmp_path):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        results = await asyncio.gather(app.update_checkpoint(source, "first edit", 1),
                                       app.update_checkpoint(source, "stale edit", 1), return_exceptions=True)
        assert results[0]["revision"] == 2 and isinstance(results[1], CheckpointConflict)
        results = await asyncio.gather(app.update_checkpoint(source, "second edit", 2),
                                       app.resume_checkpoint(source, "codex", 2), return_exceptions=True)
        assert results[0]["revision"] == 3 and isinstance(results[1], CheckpointConflict)
        assert app.browser.reads == 1 and app.checkpoints.load(source)["status"] == "ready"
        results = await asyncio.gather(app.resume_checkpoint(source, "codex", 3),
                                       app.update_checkpoint(source, "after claim", 3), return_exceptions=True)
        assert results[0]["task"] == "second edit" and isinstance(results[1], ValueError)
        await waiting(app)
        assert app.checkpoints.load(source)["brief"] == "second edit"
        await app.close()
    asyncio.run(run())


def test_noop_edit_preserves_revision_but_reverting_text_does_not_revive_old_review(tmp_path):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        assert (await app.update_checkpoint(source, "original brief", 1))["revision"] == 1
        await app.update_checkpoint(source, "different brief", 1)
        assert (await app.update_checkpoint(source, "original brief", 2))["revision"] == 3
        with pytest.raises(CheckpointConflict): await app.resume_checkpoint(source, "codex", 1)
        assert app.browser.reads == 1
        await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("claimed", [False, True])
def test_legacy_checkpoint_revision_is_readable_without_reviving_a_used_checkpoint(tmp_path, claimed):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        value = app.checkpoints.load(source)
        value.pop("revision")
        value.pop("continuation")
        value["status"] = "claimed" if claimed else "ready"
        app.checkpoints.path(source).write_text(json.dumps(value))
        assert app.checkpoints.load(source)["revision"] == 1
        if claimed:
            with pytest.raises(ValueError): await app.update_checkpoint(source, "edit", 1)
            with pytest.raises(ValueError): await app.resume_checkpoint(source, "codex", 1)
        else:
            await app.update_checkpoint(source, "legacy edit", 1)
            assert json.loads(app.checkpoints.path(source).read_text())["revision"] == 2
        await app.close()
    asyncio.run(run())


def test_invalid_edits_or_revision_and_write_failure_leave_saved_brief_available(tmp_path, monkeypatch):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        original = app.checkpoints.path(source).read_bytes()
        for revision in [None, True, False, 0, -1, "1", 1.0, 2**40]:
            with pytest.raises(ValueError): await app.update_checkpoint(source, "edit", revision)
            with pytest.raises(ValueError): await app.resume_checkpoint(source, "codex", revision)
        for brief in ["", "x" * 4001, "token=private", "https://example.com/?code=private"]:
            with pytest.raises(ValueError): await app.update_checkpoint(source, brief, 1)
        def fail(*_args): raise OSError("Synthetic write failure")
        monkeypatch.setattr(app.saved_reports, "_write_metadata", fail)
        with pytest.raises(OSError): await app.update_checkpoint(source, "edit", 1)
        assert app.checkpoints.path(source).read_bytes() == original and app.browser.reads == 1
        await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_deleted_or_expired_checkpoint_cannot_be_recreated_by_a_stale_editor(tmp_path, expired):
    async def run():
        app, source = await saved_checkpoint(tmp_path)
        if expired:
            deadline = app.saved_reports.retention(source)["expires_at"]
            app.saved_reports.clock = lambda: deadline
        else:
            await app.delete_saved_task(source)
        with pytest.raises(ValueError): await app.update_checkpoint(source, "stale edit", 1)
        await app.expire_reports()
        assert not app.checkpoints.path(source).exists() and app.browser.reads == 1
        await app.close()
    asyncio.run(run())
