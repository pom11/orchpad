from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from orchpad import Pad, StoreError
from orchpad.model import Item
from orchpad.store import SCHEMA_VERSION, Store, _empty_document

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone(timedelta(hours=1)))


@pytest.fixture()
def pad(tmp_path):
    return Pad(tmp_path / "pad", clock=lambda: NOW)


def make_item(**overrides):
    values = {"id": 1, "kind": "task", "description": "write tests"}
    values.update(overrides)
    return Item(**values)


def test_item_reminder_fields_round_trip():
    item = make_item(
        due="2026-12-01",
        remind_at="2026-11-30T09:00:00+01:00",
        repeat="weekly",
        notes="line one\nline two",
        url="https://example.invalid/item",
        tags=["#work", "urgent", "work"],
        parent=4,
        completed_at="2026-11-25T12:00:00+00:00",
    )
    rebuilt = Item.from_dict(item.to_dict())
    assert rebuilt == item
    assert rebuilt.tags == ["work", "urgent"]
    assert rebuilt.repeat == "weekly"


def test_old_items_default_new_fields():
    item = Item.from_dict(
        {"id": 1, "kind": "task", "description": "old"}
    )
    assert item.due is None
    assert item.remind_at is None
    assert item.repeat is None
    assert item.notes is None
    assert item.url is None
    assert item.tags == []
    assert item.parent is None
    assert item.completed_at is None


@pytest.mark.parametrize("value", ["not-a-date", "2026-13-01", 3, " "])
def test_invalid_due_rejected(value):
    with pytest.raises(ValueError):
        make_item(due=value)


def test_invalid_reminder_repeat_tags_and_parent_rejected():
    with pytest.raises(ValueError):
        make_item(remind_at="tomorrow")
    with pytest.raises(ValueError):
        make_item(repeat="fortnightly")
    with pytest.raises(ValueError):
        make_item(tags="work")
    with pytest.raises(ValueError):
        make_item(tags=["two words"])
    with pytest.raises(ValueError):
        make_item(tags=[""])
    with pytest.raises(ValueError):
        make_item(parent=0)
    with pytest.raises(ValueError):
        make_item(parent=True)
    with pytest.raises(ValueError):
        make_item(parent=1)


def test_repeat_none_normalises_to_no_repeat():
    assert make_item(repeat="none").repeat is None


def test_v1_document_is_read_without_rewriting(tmp_path):
    path = tmp_path / "pad"
    path.mkdir()
    document = {
        "version": 1,
        "next_id": 2,
        "items": {
            "1": {
                "id": 1,
                "kind": "task",
                "description": "old task",
                "boards": ["My Board"],
                "priority": 2,
                "starred": False,
                "status": "pending",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "links": {},
                "archived_at": None,
            }
        },
    }
    (path / "storage.json").write_text(json.dumps(document))
    before = (path / "storage.json").read_bytes()

    items = Pad(path).list()
    assert [item.description for item in items] == ["old task"]
    assert items[0].due is None
    assert (path / "storage.json").read_bytes() == before


def test_v1_document_migrates_losslessly_on_first_write(tmp_path):
    path = tmp_path / "pad"
    path.mkdir()
    document = {
        "version": 1,
        "next_id": 2,
        "items": {
            "1": {
                "id": 1,
                "kind": "task",
                "description": "old task",
                "boards": ["work"],
                "priority": 3,
                "starred": True,
                "status": "in_progress",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "links": {"kanban": "board/t_1"},
                "archived_at": None,
            }
        },
    }
    (path / "storage.json").write_text(json.dumps(document))

    Pad(path).add_task("new task")
    raw = json.loads((path / "storage.json").read_text())
    assert raw["version"] == SCHEMA_VERSION
    assert raw["next_id"] == 3
    assert raw["items"]["1"]["description"] == "old task"
    assert raw["items"]["1"]["priority"] == 3
    assert raw["items"]["1"]["starred"] is True
    assert raw["items"]["1"]["links"] == {"kanban": "board/t_1"}
    assert raw["items"]["1"]["due"] is None


def test_unknown_future_schema_is_refused(tmp_path):
    path = tmp_path / "pad"
    path.mkdir()
    document = _empty_document()
    document["version"] = SCHEMA_VERSION + 1
    (path / "storage.json").write_text(json.dumps(document))
    before = (path / "storage.json").read_bytes()
    with pytest.raises(StoreError):
        Pad(path).list()
    assert (path / "storage.json").read_bytes() == before


def test_new_documents_use_schema_version_two(tmp_path):
    path = tmp_path / "pad"
    Pad(path).add_task("one")
    raw = json.loads((path / "storage.json").read_text())
    assert raw["version"] == 2
    assert Store(path).read_active()["version"] == 2


def test_add_with_reminder_fields(pad):
    item = pad.add_task(
        "dentist",
        due="2026-12-01",
        remind_at="2026-11-30T09:00:00+01:00",
        repeat="monthly",
        notes="bring the card",
        url="https://example.invalid/dentist",
        tags=["#health", "health", "family"],
    )
    assert item.due == "2026-12-01"
    assert item.remind_at == "2026-11-30T09:00:00+01:00"
    assert item.repeat == "monthly"
    assert item.notes == "bring the card"
    assert item.url == "https://example.invalid/dentist"
    assert item.tags == ["health", "family"]
    assert pad.list()[0].to_dict()["tags"] == ["health", "family"]


def test_check_sets_and_clears_completed_at(pad):
    item = pad.add_task("finish")
    done = pad.check(item.id)[0]
    assert done.status == "done"
    assert done.completed_at is not None
    pending = pad.check(item.id)[0]
    assert pending.status == "pending"
    assert pending.completed_at is None


def test_repeating_task_creates_next_occurrence(pad):
    item = pad.add_task(
        "standup",
        due="2026-11-26",
        remind_at="2026-11-26T08:00:00+01:00",
        repeat="daily",
        tags=["work"],
        notes="yesterday/today",
    )
    done, occurrence = pad.check(item.id)

    assert done.status == "done"
    assert done.completed_at is not None
    assert done.due == "2026-11-26"

    assert occurrence.id != item.id
    assert occurrence.status == "pending"
    assert occurrence.description == "standup"
    assert occurrence.due == "2026-11-27"
    assert occurrence.remind_at == "2026-11-27T08:00:00+01:00"
    assert occurrence.repeat == "daily"
    assert occurrence.tags == ["work"]
    assert occurrence.notes == "yesterday/today"
    assert occurrence.completed_at is None
    assert [entry.id for entry in pad.list()] == [done.id, occurrence.id]


def test_repeating_task_without_due_still_creates_occurrence(pad):
    item = pad.add_task("weekly review", repeat="weekly")
    done, occurrence = pad.check(item.id)
    assert done.status == "done"
    assert occurrence.due is None
    assert occurrence.status == "pending"


def test_subtasks_and_parent_force(pad):
    parent = pad.add_task("parent")
    child = pad.add_task("child", parent=parent.id)
    grandchild = pad.add_task("grandchild", parent=child.id)
    assert child.parent == parent.id
    assert grandchild.parent == child.id

    with pytest.raises(ValueError, match="--force"):
        pad.check(parent.id)
    assert pad.list("done") == []

    results = pad.check(parent.id, force=True)
    assert results[0].status == "done"

    # Close the leaves first; then no open subtasks remain.
    pad.check(grandchild.id)
    pad.check(child.id)
    assert {item.id for item in pad.list("done")} == {
        parent.id,
        child.id,
        grandchild.id,
    }


def test_subtask_requires_an_existing_task(pad):
    note = pad.add_note("just a note")
    with pytest.raises(KeyError):
        pad.add_task("orphan", parent=99)
    with pytest.raises(ValueError):
        pad.add_task("under a note", parent=note.id)


def test_order_subtasks_indents_children_under_parents(pad):
    from orchpad.pad import order_subtasks

    parent = pad.add_task("parent", priority=3)
    child = pad.add_task("child", parent=parent.id)
    sibling = pad.add_task("sibling")
    ordered = order_subtasks(pad.list())
    assert [(item.id, depth) for item, depth in ordered] == [
        (parent.id, 0),
        (child.id, 1),
        (sibling.id, 0),
    ]


def test_edit_fields_and_setters(pad):
    item = pad.add_task("item")
    assert pad.set_due(item.id, "2026-12-01").due == "2026-12-01"
    assert pad.set_remind_at(
        item.id, "2026-12-01T09:00:00+01:00"
    ).remind_at == "2026-12-01T09:00:00+01:00"
    assert pad.set_repeat(item.id, "week").repeat == "weekly"
    assert pad.set_notes(item.id, "body").notes == "body"
    assert pad.set_url(item.id, "https://example.invalid").url == (
        "https://example.invalid"
    )
    assert pad.set_tags(item.id, ["#a", "b"]).tags == ["a", "b"]
    assert pad.set_due(item.id, None).due is None
    assert pad.set_notes(item.id, "").notes is None

    with pytest.raises(ValueError):
        pad.edit_fields(item.id, mystery="x")
    with pytest.raises(KeyError):
        pad.set_parent(item.id, 42)


def test_due_filters(pad):
    today = pad.add_task("today", due="2026-11-25")
    overdue = pad.add_task("overdue", due="2026-11-24")
    upcoming = pad.add_task("upcoming", due="2026-11-27")
    far = pad.add_task("far", due="2026-12-30")
    unscheduled = pad.add_task("unscheduled")

    assert [item.id for item in pad.list("today")] == [today.id]
    assert [item.id for item in pad.list("overdue")] == [overdue.id]
    assert [item.id for item in pad.list("upcoming")] == [upcoming.id]
    assert [item.id for item in pad.list("scheduled")] == [
        today.id,
        overdue.id,
        upcoming.id,
        far.id,
    ]
    assert unscheduled.id not in {item.id for item in pad.list("scheduled")}


def test_tag_filter(pad):
    tagged = pad.add_task("tagged", tags=["work", "home"])
    pad.add_task("untagged")
    assert [item.id for item in pad.list("#work")] == [tagged.id]
    assert [item.id for item in pad.list("#work", "task")] == [tagged.id]
    assert pad.list("#missing") == []
