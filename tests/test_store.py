from __future__ import annotations

import json
import os

import pytest

from orchpad import Pad, Store, StoreError
from orchpad.store import SCHEMA_VERSION, Transaction, _empty_document


def test_missing_documents_use_empty_schema(tmp_path):
    store = Store(tmp_path / "pad")
    active = store.read_active()
    archive = store.read_archive()
    assert active == {"version": SCHEMA_VERSION, "next_id": 1, "items": {}}
    assert archive == active
    assert not (tmp_path / "pad").exists()


def test_transaction_writes_only_marked_documents(tmp_path):
    store = Store(tmp_path / "pad")
    with store.transaction() as transaction:
        transaction.active["next_id"] = 5
        transaction.mark_active()
    assert not store.archive_path.exists()
    with store.transaction() as transaction:
        transaction.archive["next_id"] = 9
        transaction.mark_archive()
    assert store.read_active()["next_id"] == 5
    assert store.read_archive()["next_id"] == 9


def test_transaction_discards_unmarked_changes(tmp_path):
    store = Store(tmp_path / "pad")
    with store.transaction() as transaction:
        transaction.active["next_id"] = 77
    assert store.read_active()["next_id"] == 1


def test_pad_persists_across_instances(tmp_path):
    path = tmp_path / "pad"
    first = Pad(path).add_task("persisted", boards=["work"], priority=2)
    second = Pad(path).list("task")[0]
    assert second.to_dict() == first.to_dict()


def test_schema_shape_on_disk(tmp_path):
    path = tmp_path / "pad"
    Pad(path).add_task("one")
    raw = json.loads((path / "storage.json").read_text())
    assert raw["version"] == SCHEMA_VERSION
    assert raw["next_id"] == 2
    assert list(raw["items"]) == ["1"]
    assert raw["items"]["1"]["description"] == "one"


@pytest.mark.parametrize("payload", ["{not json", "[]", "null", '{"version": 2}'])
def test_corrupt_active_document_raises_and_is_untouched(tmp_path, payload):
    path = tmp_path / "pad"
    path.mkdir()
    (path / "storage.json").write_text(payload)
    before = (path / "storage.json").read_bytes()
    with pytest.raises(StoreError):
        Pad(path).list()
    assert (path / "storage.json").read_bytes() == before


def test_corrupt_archive_document_raises_and_is_untouched(tmp_path):
    path = tmp_path / "pad"
    Pad(path).add_task("one")
    (path / "archive.json").write_text("{broken")
    before = (path / "archive.json").read_bytes()
    with pytest.raises(StoreError):
        Pad(path).delete(1)
    assert (path / "archive.json").read_bytes() == before
    assert [item.id for item in Pad(path).list()] == [1]


def test_semantically_invalid_item_raises(tmp_path):
    path = tmp_path / "pad"
    path.mkdir()
    document = _empty_document()
    document["items"]["1"] = {"id": 1, "kind": "task"}
    (path / "storage.json").write_text(json.dumps(document))
    before = (path / "storage.json").read_bytes()
    with pytest.raises(StoreError):
        Pad(path).list()
    assert (path / "storage.json").read_bytes() == before


def test_mismatched_item_key_raises(tmp_path):
    path = tmp_path / "pad"
    Pad(path).add_task("one")
    document = json.loads((path / "storage.json").read_text())
    document["items"]["2"] = document["items"].pop("1")
    (path / "storage.json").write_text(json.dumps(document))
    with pytest.raises(StoreError):
        Pad(path).list()


def test_failed_replace_keeps_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "pad"
    Pad(path).add_task("one")
    before = (path / "storage.json").read_bytes()

    def explode(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        Pad(path).add_task("two")

    assert (path / "storage.json").read_bytes() == before
    assert not list(path.glob(".*.tmp"))
    monkeypatch.undo()
    new = Pad(path).add_task("two")
    assert new.id == 2
    assert [item.description for item in Pad(path).list()] == ["one", "two"]


def test_transaction_touch_helper(tmp_path):
    transaction = Transaction(Store(tmp_path / "pad"))
    transaction.touch("active")
    transaction.touch("archive")
    assert transaction.dirty == {"active", "archive"}
    with pytest.raises(ValueError):
        transaction.touch("other")


def test_lock_creates_pad_directory(tmp_path):
    store = Store(tmp_path / "pad")
    with store.lock():
        pass
    assert (tmp_path / "pad" / ".lock").exists()


def _crash_on_second_write(monkeypatch):
    from orchpad import store as store_mod
    real = store_mod.Store._write
    calls = {"n": 0}

    def flaky(self, path, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash between the two writes")
        return real(self, path, data)

    monkeypatch.setattr(store_mod.Store, "_write", flaky)


def _present(tmp_path, item_id):
    from orchpad.pad import Pad
    active, archive = Pad(tmp_path).store.snapshot()
    return str(item_id) in active["items"] or str(item_id) in archive["items"]


def test_crash_mid_delete_keeps_the_item_somewhere(tmp_path, monkeypatch):
    from orchpad.pad import Pad
    pad = Pad(tmp_path)
    item = pad.add_task("keep me")
    _crash_on_second_write(monkeypatch)
    try:
        pad.delete(item.id)
    except OSError:
        pass
    monkeypatch.undo()
    assert _present(tmp_path, item.id)


def test_crash_mid_restore_keeps_the_item_somewhere(tmp_path, monkeypatch):
    from orchpad.pad import Pad
    pad = Pad(tmp_path)
    item = pad.add_task("keep me")
    pad.delete(item.id)
    _crash_on_second_write(monkeypatch)
    try:
        pad.restore(item.id)
    except OSError:
        pass
    monkeypatch.undo()
    assert _present(tmp_path, item.id)
