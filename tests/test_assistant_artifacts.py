import json
from pathlib import Path
import uuid

import pytest

from parallax.assistant.actions import Action, needs_approval
from parallax.assistant.artifacts import ArtifactStore, MAX_BYTES, MAX_FILES, safe_name


def save(store, task, content=b"report", name="report.txt"):
    with store.staging(task) as stage:
        stage.write_bytes(content)
        return store.commit(task, stage, name)


def test_outputs_are_private_isolated_and_never_use_the_filename_as_a_path(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    task = uuid.uuid4().hex
    first = save(store, task, name="../../private\\report\r\n.txt")
    second = save(store, task, content=b"other", name=first["name"])
    assert first["id"] != second["id"]
    assert first["name"] == "report.txt"
    assert store.read(task, first["id"])[1] == b"report"
    assert store.read(task, second["id"])[1] == b"other"
    assert store.list(uuid.uuid4().hex) == []
    assert not list((store.root / task).glob(".pending-*"))
    assert all(path.stat().st_mode & 0o077 == 0 for path in (store.root / task).iterdir())
    with pytest.raises(ValueError): store.read("../outside", first["id"])
    with pytest.raises(ValueError): store.read(task, "../outside")
    assert safe_name("\u202e\x00...") == "download.bin"
    store.delete(task, first["id"])
    with pytest.raises(ValueError): store.read(task, first["id"])
    assert len(store.list(task)) == 1


def test_size_count_integrity_and_symlinks_are_checked(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    task = uuid.uuid4().hex
    with store.staging(task) as stage:
        with stage.open("wb") as stream: stream.truncate(MAX_BYTES + 1)
        with pytest.raises(ValueError): store.commit(task, stage, "too-large")
    first = save(store, task)
    payload = store.directory(task) / (first["id"] + ".data")
    payload.write_bytes(b"REPORT")  # Same size, different bytes.
    with pytest.raises(ValueError, match="تغير"): store.read(task, first["id"])
    payload.unlink()
    secret = tmp_path / "unrelated.txt"
    secret.write_text("PRIVATE")
    payload.symlink_to(secret)
    with pytest.raises(ValueError): store.read(task, first["id"])
    store.delete(task, first["id"])
    assert secret.read_text() == "PRIVATE"
    for _ in range(MAX_FILES): save(store, task)
    with pytest.raises(ValueError): save(store, task)
    assert len(store.list(task)) == MAX_FILES


def test_download_actions_require_an_observed_target_empty_value_and_approval():
    action = Action.parse(json.dumps(Action("download", "4").to_dict()))
    assert needs_approval(action)
    for changes in [{"target": "../file"}, {"value": "/tmp/chosen-by-model"}]:
        with pytest.raises(ValueError): Action.parse(json.dumps({**action.to_dict(), **changes}))


def test_uuid_collision_does_not_delete_an_existing_output(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "files")
    task = uuid.uuid4().hex
    fixed = uuid.uuid4()
    monkeypatch.setattr("parallax.assistant.artifacts.uuid.uuid4", lambda: fixed)
    first = save(store, task, b"original")
    with pytest.raises(FileExistsError): save(store, task, b"replacement")
    assert store.read(task, first["id"])[1] == b"original"


def test_followup_reference_pins_bytes_without_copying_or_deleting_the_source(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    parent, child, later = (uuid.uuid4().hex for _ in range(3))
    original = save(store, parent, b"selected bytes")
    inherited = store.inherit(child, original)
    assert inherited["id"] != original["id"] and inherited["task_id"] == child
    assert inherited["source_kind"] == "inherited"
    assert not (store.directory(child) / (inherited["id"] + ".data")).exists()
    assert store.read(child, inherited["id"])[1] == b"selected bytes"
    descendant = store.inherit(later, inherited)
    store.delete(child, inherited["id"])
    assert store.read(parent, original["id"])[1] == b"selected bytes"
    assert store.read(later, descendant["id"])[1] == b"selected bytes"
    store.delete(parent, original["id"])
    assert store.list(later)[0]["available"] is False
    with pytest.raises(ValueError): store.read(later, descendant["id"])


@pytest.mark.parametrize("mutation", ["bytes", "manifest", "name", "symlink"])
def test_inherited_reference_rejects_a_changed_source_even_with_updated_source_hash(tmp_path, mutation):
    store = ArtifactStore(tmp_path / "files")
    parent, child = uuid.uuid4().hex, uuid.uuid4().hex
    original = save(store, parent, b"old")
    inherited = store.inherit(child, original)
    data = store.directory(parent) / (original["id"] + ".data")
    manifest = store.directory(parent) / (original["id"] + ".json")
    if mutation in {"bytes", "manifest"}: data.write_bytes(b"new")
    if mutation == "manifest":
        import hashlib
        row = json.loads(manifest.read_text())
        row["sha256"] = hashlib.sha256(b"new").hexdigest()
        manifest.write_text(json.dumps(row))
    if mutation == "name":
        row = json.loads(manifest.read_text())
        row["name"] = "changed.txt"
        manifest.write_text(json.dumps(row))
    if mutation == "symlink":
        data.unlink()
        data.symlink_to(tmp_path / "unrelated.txt")
    with pytest.raises(ValueError): store.read(child, inherited["id"])
    assert store.list(child)[0]["id"] == inherited["id"]


def test_references_count_toward_capacity_and_collision_preserves_existing_file(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path / "files")
    parent, child = uuid.uuid4().hex, uuid.uuid4().hex
    original = save(store, parent)
    existing = save(store, child, b"keep me")
    with monkeypatch.context() as patch:
        patch.setattr("parallax.assistant.artifacts.uuid.uuid4", lambda: uuid.UUID(existing["id"]))
        with pytest.raises(FileExistsError): store.inherit(child, original)
    assert store.read(child, existing["id"])[1] == b"keep me"
    for _ in range(MAX_FILES - 1): store.inherit(child, original)
    with pytest.raises(ValueError): store.inherit(child, original)
    with pytest.raises(ValueError): save(store, child)
    assert len(store.list(child)) == MAX_FILES


def test_reference_cycle_is_unavailable_and_never_followed_recursively(tmp_path):
    store = ArtifactStore(tmp_path / "files")
    parent, child = uuid.uuid4().hex, uuid.uuid4().hex
    original = save(store, parent)
    reference = store.inherit(child, original)
    path = store.directory(parent) / (original["id"] + ".json")
    row = json.loads(path.read_text())
    row.update(source_kind="inherited", source={"task_id": child, "id": reference["id"]})
    path.write_text(json.dumps(row))
    assert store.list(child)[0]["available"] is False
    with pytest.raises(ValueError): store.read(child, reference["id"])
    with pytest.raises(ValueError): store.inherit(uuid.uuid4().hex, reference)
