"""Deletion revokes access before cleanup, without resetting the operation journal."""
import asyncio
import json
import time
import uuid

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.attempts import AttemptBlocked
from parallax.assistant.runtime import Assistant


class Browser:
    async def start(self): pass
    async def observe(self):
        return {"url": "https://example.com/", "text": "Saved evidence", "fingerprint": "same"}
    async def close(self): pass


class Planner:
    def __init__(self, *_args): pass
    async def propose(self, task, *_args):
        return Action("handoff", reason="User step") if task == "wait" else Action("finish", value="PRIVATE-SAVED-ANSWER")


def file(app, task_id):
    with app.artifacts.staging(task_id) as stage:
        stage.write_bytes(b"PRIVATE-CONTENT")
        return app.artifacts.commit(task_id, stage, "document.txt")


async def completed(app):
    await app.start("finish", "codex")
    await app.job
    return app.snapshot()["id"]


def test_delete_latest_clears_memory_blocks_stale_followup_and_does_not_restore_older_success(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        old = await completed(app)
        latest = await completed(app)
        saved = file(app, latest)
        attempt = app.journal.begin(Action("click", "1"), "https://example.com/", {"label": "Send"}, latest, latest)
        app.journal.settle(attempt, "unknown")
        journal_before = (tmp_path / "operations.key").read_bytes()
        outcome = await app.delete_saved_task(latest)
        assert outcome["deleted"] and not outcome["cleanup_pending"]
        assert outcome["state"]["status"] == "idle" and not outcome["state"].get("id")
        assert "PRIVATE" not in json.dumps(outcome)
        assert not (tmp_path / (latest + ".json")).exists()
        assert not (tmp_path / "files" / latest).exists()
        assert app.saved_reports.load(old)["result"] == "PRIVATE-SAVED-ANSWER"
        assert (tmp_path / "operations.key").read_bytes() == journal_before
        assert (tmp_path / "operations.sqlite3").exists()
        assert app.saved_reports.latest_id() is None
        assert len(app.saved_reports.page()["items"]) == 1
        with pytest.raises(ValueError): app.artifacts.read(latest, saved["id"])
        with pytest.raises(ValueError): await app.start("stale followup", "codex", latest)
        app._save_report()
        assert not (tmp_path / (latest + ".json")).exists()
        again = await app.delete_saved_task(latest)
        assert not again["cleanup_pending"]
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert restored.snapshot()["status"] == "idle" and not restored.snapshot().get("id")
        assert restored.journal.uncertain()[0]["id"] == attempt
        with pytest.raises(AttemptBlocked):
            restored.journal.begin(Action("click", "2"), "https://example.com/", {"label": "Send"}, uuid.uuid4().hex, uuid.uuid4().hex)
        fresh = await completed(restored)
        assert restored.saved_reports.latest_id() == fresh
        await app.close()
        await restored.close()
    asyncio.run(run())


def test_interrupted_cleanup_revokes_original_and_inherited_bytes_before_restart(tmp_path, monkeypatch):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        original_task = await completed(app)
        original = file(app, original_task)
        await app.start("follow", "codex", original_task)
        await app.job
        child = app.snapshot()
        reference, = child["artifacts"]
        def fail(_identifier): raise OSError("Synthetic cleanup interruption")
        monkeypatch.setattr(app.artifacts, "delete_task", fail)
        outcome = await app.delete_saved_task(original_task)
        assert outcome["cleanup_pending"] and outcome["state"]["id"] == child["id"]
        assert (tmp_path / "files" / original_task / (original["id"] + ".data")).exists()
        assert not app.snapshot()["artifacts"][0]["available"]
        with pytest.raises(ValueError): app.saved_reports.load(original_task)
        with pytest.raises(ValueError): app.artifacts.read(child["id"], reference["id"])
        with pytest.raises(ValueError): app.artifacts.inherit(uuid.uuid4().hex, original)
        with pytest.raises(ValueError): file(app, original_task)
        pending = next(row for row in app.saved_reports.page()["items"] if row["id"] == original_task)
        assert pending["status"] == "deletion_pending" and not pending["available"]
        marker = app.saved_reports.deletion_path(original_task)
        assert "PRIVATE" not in marker.read_text() and marker.stat().st_mode & 0o077 == 0
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert not (tmp_path / "files" / original_task).exists()
        assert not (tmp_path / (original_task + ".json")).exists()
        assert not restored.saved_reports.deletion(original_task)["pending"]
        assert restored.snapshot()["id"] == child["id"]
        assert not restored.snapshot()["artifacts"][0]["available"]
        await app.close()
        await restored.close()
    asyncio.run(run())


def test_deleting_a_reference_task_leaves_original_and_other_followup_references(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        parent = await completed(app)
        original = file(app, parent)
        await app.start("follow", "codex", parent)
        await app.job
        child = app.snapshot()["id"]
        await app.start("follow again", "codex", child)
        await app.job
        descendant = app.snapshot()
        await app.delete_saved_task(child)
        assert app.artifacts.read(parent, original["id"])[1] == b"PRIVATE-CONTENT"
        assert app.artifacts.read(descendant["id"], descendant["artifacts"][0]["id"])[1] == b"PRIVATE-CONTENT"
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert restored.snapshot()["id"] == descendant["id"]
        await app.close()
        await restored.close()
    asyncio.run(run())


@pytest.mark.parametrize("fail_on", [1, 2])
def test_marker_write_error_after_revocation_does_not_leave_followup_eligible(tmp_path, monkeypatch, fail_on):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        task = await completed(app)
        saved = file(app, task)
        original = app.saved_reports._write_deletion
        writes = []
        def fail_after_write(marker):
            original(marker)
            writes.append(marker)
            if len(writes) == fail_on:
                raise OSError("Synthetic sync failure after rename")
        monkeypatch.setattr(app.saved_reports, "_write_deletion", fail_after_write)
        outcome = await app.delete_saved_task(task)
        assert outcome["cleanup_pending"] == (fail_on == 1) and not outcome["state"].get("id")
        assert (tmp_path / "files" / task / (saved["id"] + ".data")).exists() == (fail_on == 1)
        with pytest.raises(ValueError): await app.start("follow", "codex", task)
        monkeypatch.setattr(app.saved_reports, "_write_deletion", original)
        assert not (await app.delete_saved_task(task))["cleanup_pending"]
        await app.close()
    asyncio.run(run())


def test_corrupt_deletion_marker_is_recovered_without_restoring_content_or_blocking_future_results(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await completed(app)
        latest = await completed(app)
        file(app, latest)
        app.saved_reports.mark_deleted(latest)
        app.saved_reports.deletion_path(latest).write_text("{interrupted")
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert not restored.snapshot().get("id")
        assert not (tmp_path / "files" / latest).exists()
        assert not restored.saved_reports.deletion(latest)["pending"]
        fresh = await completed(restored)
        assert restored.saved_reports.latest_id() == fresh
        await app.close()
        await restored.close()
    asyncio.run(run())


def test_deleting_while_task_is_active_does_not_revoke_or_change_pending_decision(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        old = await completed(app)
        saved = file(app, old)
        await app.start("wait", "codex")
        for _ in range(100):
            if app.snapshot()["pending"]: break
            await asyncio.sleep(0.01)
        before = app.snapshot()
        assert before["pending"]
        with pytest.raises(ValueError): await app.delete_saved_task(old)
        assert app.snapshot()["pending"] == before["pending"]
        assert not app.saved_reports.is_deleted(old)
        assert app.artifacts.read(old, saved["id"])[1] == b"PRIVATE-CONTENT"
        await app.close()
    asyncio.run(run())


@pytest.mark.parametrize("unexpected", ["file", "symlink"])
def test_cleanup_does_not_follow_or_recursively_remove_unowned_paths(tmp_path, unexpected):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        task = await completed(app)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "keep.txt"
        secret.write_text("KEEP")
        folder = app.artifacts.directory(task, create=True)
        if unexpected == "file":
            (folder / "keep.txt").write_text("KEEP")
        else:
            folder.rmdir()
            folder.symlink_to(outside, target_is_directory=True)
        outcome = await app.delete_saved_task(task)
        assert outcome["cleanup_pending"]
        assert secret.read_text() == "KEEP"
        assert app.saved_reports.is_deleted(task)
        with pytest.raises(ValueError): app.saved_reports.load(task)
        await app.close()
    asyncio.run(run())


def test_expiry_revokes_pinned_files_at_boundary_before_cleanup_and_cannot_be_extended(tmp_path, monkeypatch):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        parent = await completed(app)
        original = file(app, parent)
        assert app.saved_reports.retention(parent) == {"days": None, "expires_at": None}
        policy = await app.set_retention(parent, 1)
        assert policy["expires_at"] == now[0] + 86400
        await app.start("follow", "codex", parent)
        await app.job
        child = app.snapshot()
        reference, = child["artifacts"]
        now[0] = policy["expires_at"] - 0.01
        assert app.artifacts.read(child["id"], reference["id"])[1] == b"PRIVATE-CONTENT"
        now[0] = policy["expires_at"]
        with pytest.raises(ValueError): app.artifacts.read(child["id"], reference["id"])
        assert (tmp_path / "files" / parent / (original["id"] + ".data")).exists()
        with pytest.raises(ValueError): await app.set_retention(parent, 90)
        with pytest.raises(ValueError): await app.set_retention(parent, None)
        # Once expiry is observed, a backward clock adjustment cannot revive it.
        now[0] -= 86400
        with pytest.raises(ValueError): app.saved_reports.load(parent)
        await app.expire_reports()
        assert not (tmp_path / "files" / parent).exists()
        assert not app.saved_reports.retention_path(parent).exists()
        assert app.snapshot()["id"] == child["id"]
        await app.close()
    asyncio.run(run())


def test_retention_can_be_changed_or_cancelled_before_expiry(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        task = await completed(app)
        original = file(app, task)
        first = await app.set_retention(task, 1)
        now[0] += 100
        extended = await app.set_retention(task, 7)
        assert extended["expires_at"] > first["expires_at"]
        assert await app.set_retention(task, None) == {"days": None, "expires_at": None}
        now[0] = extended["expires_at"] + 1
        await app.expire_reports()
        assert app.artifacts.read(task, original["id"])[1] == b"PRIVATE-CONTENT"
        for invalid in [True, False, 0, -1, 2, 1.5, "7"]:
            with pytest.raises(ValueError): await app.set_retention(task, invalid)
        await app.close()
    asyncio.run(run())


def test_expired_result_is_cleaned_after_service_was_offline_without_restoring_an_older_result(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        await completed(app)
        task = await completed(app)
        file(app, task)
        app.saved_reports.clock = lambda: time.time() - 2 * 86400
        await app.set_retention(task, 1)
        await app.close()
        restored = Assistant(Browser(), tmp_path, planner_factory=Planner)
        assert restored.snapshot()["status"] == "idle" and not restored.snapshot().get("id")
        assert not (tmp_path / (task + ".json")).exists()
        assert not (tmp_path / "files" / task).exists()
        assert restored.saved_reports.is_deleted(task)
        await restored.close()
    asyncio.run(run())


def test_corrupt_policy_blocks_use_but_is_not_authority_to_delete(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        task = await completed(app)
        saved = file(app, task)
        await app.set_retention(task, 1)
        app.saved_reports.retention_path(task).write_text("{broken")
        await app.expire_reports()
        assert not app.saved_reports.is_deleted(task)
        assert (tmp_path / "files" / task / (saved["id"] + ".data")).exists()
        with pytest.raises(ValueError): app.artifacts.read(task, saved["id"])
        with pytest.raises(ValueError): app.saved_reports.load(task)
        assert not app.snapshot().get("id")
        assert not (await app.delete_saved_task(task))["cleanup_pending"]
        await app.close()
    asyncio.run(run())


def test_background_cleaner_runs_without_ui_polling_and_stops_on_close(tmp_path):
    async def run():
        app = Assistant(Browser(), tmp_path, planner_factory=Planner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        task = await completed(app)
        file(app, task)
        policy = await app.set_retention(task, 1)
        now[0] = policy["expires_at"] + 1
        for _ in range(300):
            if not (tmp_path / (task + ".json")).exists(): break
            await asyncio.sleep(0.01)
        assert not (tmp_path / (task + ".json")).exists()
        assert app.state["status"] == "idle"
        await app.close()
        assert app.maintenance_job.done()
    asyncio.run(run())


def test_expiry_cancels_a_waiting_followup_without_resuming_or_observing_browser(tmp_path):
    class CountingBrowser(Browser):
        reads = 0
        async def observe(self):
            self.reads += 1
            return await super().observe()
    async def run():
        app = Assistant(CountingBrowser(), tmp_path, planner_factory=Planner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        parent = await completed(app)
        policy = await app.set_retention(parent, 1)
        await app.start("wait", "codex", parent)
        for _ in range(100):
            if app.snapshot()["pending"]: break
            await asyncio.sleep(0.01)
        pending = app.snapshot()["pending"]
        reads = app.browser.reads
        with pytest.raises(ValueError): await app.set_retention(parent, None)
        now[0] = policy["expires_at"]
        with pytest.raises(ValueError): await app.control("resume", pending["token"])
        await app.expire_reports()
        assert app.snapshot()["status"] == "cancelled"
        assert "الاحتفاظ" in app.snapshot()["result"] and app.snapshot()["pending"] is None
        assert app.previous_context is None and app.browser.reads == reads
        await app.close()
    asyncio.run(run())


def test_expiry_during_effect_keeps_unknown_journal_and_never_replays(tmp_path):
    class SendingBrowser(Browser):
        def __init__(self): self.entered = asyncio.Event(); self.sends = 0
        def preview(self, action, snapshot):
            return {"action": action.to_dict(), "fingerprint": snapshot["fingerprint"], "target": {"tag": "button", "label": "Send"}}
        async def execute(self, _action):
            self.sends += 1
            self.entered.set()
            await asyncio.Future()
    class SendingPlanner(Planner):
        async def propose(self, task, *args):
            return Action("click", "1") if task == "send" else await super().propose(task, *args)
    async def run():
        app = Assistant(SendingBrowser(), tmp_path, planner_factory=SendingPlanner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        parent = await completed(app)
        policy = await app.set_retention(parent, 1)
        await app.start("send", "codex", parent)
        for _ in range(100):
            if app.snapshot()["pending"]: break
            await asyncio.sleep(0.01)
        await app.control("approve", app.snapshot()["pending"]["token"])
        await asyncio.wait_for(app.browser.entered.wait(), 2)
        now[0] = policy["expires_at"]
        await app.expire_reports()
        assert app.snapshot()["status"] == "cancelled" and app.browser.sends == 1
        assert len(app.journal.uncertain()) == 1
        assert not (tmp_path / (parent + ".json")).exists()
        with pytest.raises(AttemptBlocked):
            app.journal.begin(Action("click", "1"), "https://example.com/", {}, uuid.uuid4().hex, uuid.uuid4().hex)
        await app.close()
    asyncio.run(run())


def test_late_planner_proposal_after_expiry_cannot_reach_approval_or_execution(tmp_path):
    entered, release = None, None
    class SlowPlanner(Planner):
        async def propose(self, task, *args):
            if task == "late":
                entered.set()
                await release.wait()
                return Action("click", "1")
            return await super().propose(task, *args)
    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        app = Assistant(Browser(), tmp_path, planner_factory=SlowPlanner)
        now = [time.time()]
        app.saved_reports.clock = lambda: now[0]
        parent = await completed(app)
        policy = await app.set_retention(parent, 1)
        await app.start("late", "codex", parent)
        await asyncio.wait_for(entered.wait(), 2)
        now[0] = policy["expires_at"]
        release.set()
        await asyncio.wait_for(app.job, 2)
        state = app.snapshot()
        assert state["status"] == "cancelled" and state["pending"] is None
        assert state["approval_requests"] == 0 and not state["operations"]
        await app.close()
    asyncio.run(run())
