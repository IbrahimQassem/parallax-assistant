"""History is a bounded view of saved results, never a task resume operation."""
import json
import os
import uuid

import pytest

from parallax.assistant.report_store import MAX_REPORT_BYTES, PAGE_SIZE, ReportStore


def save(root, *, status="verified", **changes):
    identifier = uuid.uuid4().hex
    row = {"id": identifier, "status": status, "result": "Saved answer",
           "completion": {"status": "verified", "done": ["Read"], "remaining": [], "reason": "Recorded evidence",
                          "evidence": [{"source_id": "S1", "claim": "Read the offer", "observed_after_action": False}],
                          "effect_checks": [{"id": "check", "status": "unverified", "source_id": None}]},
           "sources": [{"id": "S1", "title": "Offer", "url": "https://example.com/", "observed_at": 1234}],
           "started_at": 1200, "finished_at": 1234, **changes}
    path = root / (identifier + ".json")
    path.write_text(json.dumps(row))
    return path


def test_history_paginates_equal_timestamps_without_repeating_rows_or_reading_all_bodies(tmp_path, monkeypatch):
    paths = [save(tmp_path, result=f"Answer {i}") for i in range(24)]
    for path in paths: os.utime(path, ns=(1000000000000, 1000000000000))
    (tmp_path / "unrelated.json").write_text("PRIVATE")
    broken = paths[3]
    broken.write_text("{broken")
    os.utime(broken, ns=(1000000000000, 1000000000000))
    store = ReportStore(tmp_path)
    reads = []
    original = store.load
    def counted(identifier):
        reads.append(identifier)
        return original(identifier)
    monkeypatch.setattr(store, "load", counted)
    first = store.page()
    assert len(first["items"]) == len(reads) == PAGE_SIZE
    assert first["next_cursor"]
    newer = save(tmp_path, result="Arrived while browsing")
    items, cursor = first["items"], first["next_cursor"]
    while cursor:
        page = store.page(cursor)
        items.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(items) == len({item["id"] for item in items}) == 24
    assert [item["id"] for item in items] == sorted(path.stem for path in paths)
    assert next(item for item in items if item["id"] == broken.stem)["available"] is False
    assert store.page()["items"][0]["id"] == newer.stem


def test_saved_status_is_historical_and_unvalidated_legacy_results_are_not_promoted(tmp_path):
    store = ReportStore(tmp_path)
    old = save(tmp_path, completion=None, report={"summary": "Legacy report"}, task="PRIVATE-REQUEST", page_text="PRIVATE-PAGE")
    view = store.load(old.stem)
    assert view["status"] == view["completion"]["status"] == "unverified"
    assert view["report"]["findings"] == []
    assert "PRIVATE" not in json.dumps(view)
    failed = save(tmp_path, status="failed")
    view = store.load(failed.stem)
    assert view["status"] == "failed" and view["completion"]["status"] == "unverified"
    assert store.page()["items"][0]["status"] == "failed"


@pytest.mark.parametrize("mutation", ["json", "array", "id", "active", "source", "timestamp", "report", "oversized", "symlink", "fifo", "deep"])
def test_unreadable_saved_report_cannot_be_opened_or_expose_arbitrary_files(tmp_path, mutation):
    path = save(tmp_path)
    row = json.loads(path.read_text())
    if mutation == "json": path.write_text("{broken")
    elif mutation == "array": path.write_text("[]")
    elif mutation == "oversized": path.write_bytes(b" " * (MAX_REPORT_BYTES + 1))
    elif mutation == "deep": path.write_text("[" * 10000 + "]" * 10000)
    elif mutation in {"symlink", "fifo"}:
        path.unlink()
        if mutation == "fifo": os.mkfifo(path)
        else:
            outside = tmp_path / "private.txt"
            outside.write_text("PRIVATE-CONTENT")
            path.symlink_to(outside)
    else:
        if mutation == "id": row["id"] = uuid.uuid4().hex
        if mutation == "active": row["status"] = "running"
        if mutation == "source": row["sources"][0]["url"] = "file:///private/secret"
        if mutation == "timestamp": row["finished_at"] = float("nan")
        if mutation == "report": row["report"] = {"summary": "Broken", "findings": "not a list"}
        path.write_text(json.dumps(row))
    store = ReportStore(tmp_path)
    with pytest.raises((ValueError, OSError)): store.load(path.stem)
    page = store.page()
    assert all(not item["available"] for item in page["items"])
    assert "PRIVATE" not in json.dumps(page)


@pytest.mark.parametrize("identifier", ["../private", "A" * 32, "", "0" * 32 + "/../private"])
def test_history_rejects_nonopaque_ids(tmp_path, identifier):
    with pytest.raises(ValueError): ReportStore(tmp_path).load(identifier)


@pytest.mark.parametrize("cursor", ["../private", "123", "-1:wrong", "9" * 21 + ":" + "a" * 32])
def test_history_rejects_malformed_cursor(tmp_path, cursor):
    with pytest.raises(ValueError): ReportStore(tmp_path).page(cursor)
