"""Durable interaction receipts, replay guards and explicit human reconciliation."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import uuid

import pytest

from parallax.assistant.actions import Action
from parallax.assistant.attempts import AttemptBlocked, AttemptJournal


def uid():
    return uuid.uuid4().hex


def test_unknown_effect_survives_new_instance_and_blocks_changed_elements(tmp_path):
    journal = AttemptJournal(tmp_path)
    group = uid()
    action = Action("click", "3", reason="PRIVATE reason")
    target = {"id": "3", "tag": "button", "label": "PRIVATE Send message"}
    page = "https://example.com/PRIVATE-path?token=PRIVATE-value"
    attempt = journal.begin(action, page, target, uid(), group)
    journal.settle(attempt, "unknown")
    assert journal.has_uncertain(group)
    recovered = AttemptJournal(tmp_path)
    with pytest.raises(AttemptBlocked) as failure:
        recovered.begin(Action("click", "99"), "https://example.com/other", {"label": "Different control"}, uid(), uid())
    assert not failure.value.duplicate
    assert failure.value.receipt["id"] == attempt
    assert b"PRIVATE" not in journal.path.read_bytes()
    assert "PRIVATE" not in json.dumps(recovered.uncertain())
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert journal.key_path.stat().st_mode & 0o777 == 0o600


def test_manual_resolution_is_single_use_and_not_automatic_verification(tmp_path):
    journal = AttemptJournal(tmp_path)
    original, inspecting = uid(), uid()
    action, target, page = Action("click", "1"), {"label": "Send"}, "https://example.com"
    attempt = journal.begin(action, page, target, uid(), original)
    journal.settle(attempt, "unknown")
    journal.resolve(attempt, occurred=True, group_id=inspecting)
    assert not journal.uncertain()
    assert journal.receipts(inspecting)[0]["status"] == "confirmed_by_user"
    for group in [original, inspecting]:
        with pytest.raises(AttemptBlocked) as failure:
            journal.begin(Action("click", "99"), page, target, uid(), group)
        assert failure.value.duplicate
    with pytest.raises(ValueError):
        journal.resolve(attempt, occurred=False)
    # A separately requested task, outside either task group, can repeat a known
    # operation only through the controller's normal authorization path.
    journal.begin(action, page, target, uid(), uid())


def test_confirmed_absence_allows_a_new_attempt_but_never_executes_it(tmp_path):
    journal, group = AttemptJournal(tmp_path), uid()
    action, page, target = Action("fill", "1", "PRIVATE field value"), "https://example.com", {"label": "Name"}
    attempt = journal.begin(action, page, target, uid(), group)
    journal.settle(attempt, "unknown")
    journal.resolve(attempt, occurred=False, group_id=group)
    assert journal.receipts(group)[0]["status"] == "not_applied_by_user"
    retry = journal.begin(action, page, target, uid(), group)
    assert retry != attempt
    journal.settle(retry, "returned")
    assert b"PRIVATE field value" not in journal.path.read_bytes()
    with pytest.raises(AttemptBlocked) as failure:
        journal.check(action, page, target, group)
    assert failure.value.duplicate


def test_concurrent_controllers_cannot_claim_the_same_origin(tmp_path):
    initial = AttemptJournal(tmp_path)
    attempt = initial.begin(Action("click", "1"), "https://other.example", {}, uid(), uid())
    initial.settle(attempt, "returned")

    def begin(_index):
        try:
            return AttemptJournal(tmp_path).begin(Action("click", "1"), "https://example.com", {}, uid(), uid())
        except AttemptBlocked:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(begin, range(2)))
    assert sum(item is not None for item in claimed) == 1
    assert len(initial.uncertain()) == 1


def test_inflight_process_cannot_be_resolved_until_it_has_exited(tmp_path):
    journal = AttemptJournal(tmp_path)
    own = journal.begin(Action("click", "1"), "https://live.example", {}, uid(), uid())
    with pytest.raises(ValueError, match="قيد التنفيذ"):
        journal.resolve(own, occurred=False)
    code = """import sys,uuid
from pathlib import Path
from parallax.assistant.actions import Action
from parallax.assistant.attempts import AttemptJournal
j=AttemptJournal(Path(sys.argv[1]))
print(j.begin(Action('click','1'),'https://crashed.example',{},uuid.uuid4().hex,uuid.uuid4().hex))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    orphaned = result.stdout.strip()
    recovered = AttemptJournal(tmp_path)
    with pytest.raises(AttemptBlocked):
        recovered.begin(Action("click", "2"), "https://crashed.example/new", {}, uid(), uid())
    recovered.resolve(orphaned, occurred=False)
    assert all(row["id"] != orphaned for row in recovered.uncertain())


def test_lost_journal_identity_fails_closed_instead_of_resetting_replay_history(tmp_path):
    journal = AttemptJournal(tmp_path)
    attempt = journal.begin(Action("click", "1"), "https://example.com", {}, uid(), uid())
    journal.settle(attempt, "returned")
    journal.key_path.unlink()
    with pytest.raises(ValueError, match="مفقودة"):
        journal.begin(Action("click", "1"), "https://example.com", {}, uid(), uid())
    assert not journal.key_path.exists()


def test_two_different_prepared_forms_are_not_mistaken_for_the_same_send(tmp_path):
    journal, group = AttemptJournal(tmp_path), uid()
    args = (Action("click", "1"), "https://example.com", {"label": "Send"}, uid(), group)
    first = journal.begin(*args, form_state=["first-form-hash"])
    journal.settle(first, "returned")
    second = journal.begin(*args, form_state=["second-form-hash"])
    journal.settle(second, "returned")
    with pytest.raises(AttemptBlocked):
        journal.check(args[0], args[1], args[2], group, form_state=["second-form-hash"])


def test_existing_receipts_migrate_without_losing_unknown_effects(tmp_path):
    journal = AttemptJournal(tmp_path)
    journal.key_path.write_bytes(os.urandom(32))
    identifier, group = uid(), uid()
    with sqlite3.connect(journal.path) as connection:
        connection.execute("""CREATE TABLE attempts (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, group_id TEXT NOT NULL, resolved_group_id TEXT,
            fingerprint TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
            owner_pid INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (identifier, uid(), group, None, "opaque", "click", "https://example.com", "unknown", os.getpid(), 1, 1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        recovered = list(pool.map(lambda _: AttemptJournal(tmp_path).uncertain(), range(2)))
    assert all(rows[0]["id"] == identifier and rows[0]["authority_id"] is None for rows in recovered)
    with pytest.raises(AttemptBlocked):
        journal.begin(Action("click", "1"), "https://example.com", {}, uid(), group, authority_id=uid())
