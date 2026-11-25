from __future__ import annotations

import json

import pytest

from orchpad import Pad, StoreError
from orchpad.model import DEFAULT_BOARD, MAX_DESCRIPTION


@pytest.fixture()
def pad(tmp_path):
    return Pad(tmp_path / "pad")


def test_add_task_defaults(pad):
    item = pad.add_task("write the core")
    assert isinstance(item.id, int)
    assert item.kind == "task"
    assert item.description == "write the core"
    assert item.boards == [DEFAULT_BOARD]
    assert item.priority == 1
    assert item.status == "pending"
    assert item.starred is False
    assert item.archived_at is None
    assert pad.list("task") == [item]


def test_add_task_with_boards_and_priority(pad):
    item = pad.add_task("ship it", boards=["work", "home", "work"], priority=3)
    assert item.boards == ["work", "home"]
    assert item.priority == 3


def test_add_note(pad):
    note = pad.add_note("remember this", boards=["ideas"])
    assert note.kind == "note"
    assert note.status is None
    assert note.priority == 1
    assert note.boards == ["ideas"]


def test_add_validation(pad):
    with pytest.raises(ValueError):
        pad.add_task("")
    with pytest.raises(ValueError):
        pad.add_task("   ")
    with pytest.raises(ValueError):
        pad.add_task("x" * (MAX_DESCRIPTION + 1))
    with pytest.raises(ValueError):
        pad.add_task("bad priority", priority=4)
    with pytest.raises(ValueError):
        pad.add_task("bad boards", boards=[])
    assert pad.list() == []


def test_check_toggles_done(pad):
    item = pad.add_task("toggle me")
    assert pad.check(item.id)[0].status == "done"
    assert pad.check(item.id)[0].status == "pending"


def test_begin_toggles_in_progress(pad):
    item = pad.add_task("start me")
    assert pad.begin(item.id)[0].status == "in_progress"
    assert pad.begin(item.id)[0].status == "pending"


def test_begin_from_done(pad):
    item = pad.add_task("resume me")
    pad.check(item.id)
    assert pad.begin(item.id)[0].status == "in_progress"


def test_notes_cannot_be_checked_or_prioritized(pad):
    note = pad.add_note("just a note")
    with pytest.raises(ValueError):
        pad.check(note.id)
    with pytest.raises(ValueError):
        pad.begin(note.id)
    with pytest.raises(ValueError):
        pad.set_priority(note.id, 2)


def test_star_toggles(pad):
    item = pad.add_task("star me")
    assert pad.star(item.id)[0].starred is True
    assert pad.star(item.id)[0].starred is False
    assert pad.star(item.id)[0].starred is True
    assert [entry.id for entry in pad.list("starred")] == [item.id]


def test_set_priority(pad):
    item = pad.add_task("important")
    assert pad.set_priority(item.id, 3).priority == 3
    assert pad.list()[0].priority == 3
    with pytest.raises(ValueError):
        pad.set_priority(item.id, 0)


def test_edit(pad):
    item = pad.add_task("first")
    edited = pad.edit(item.id, "second")
    assert edited.description == "second"
    assert pad.list()[0].description == "second"
    with pytest.raises(ValueError):
        pad.edit(item.id, "  ")


def test_move(pad):
    item = pad.add_task("move me")
    moved = pad.move(item.id, ["work", "later", "work"])
    assert moved.boards == ["work", "later"]
    assert pad.move(item.id, [DEFAULT_BOARD]).boards == [DEFAULT_BOARD]


def test_delete_and_restore_round_trip(pad):
    item = pad.add_task("keep me", boards=["work"], priority=2)
    pad.star(item.id)
    pad.link(item.id, "kanban", "board/t_1")
    deleted = pad.delete(item.id)
    assert deleted[0].archived_at is not None
    assert pad.list() == []
    assert [entry.id for entry in pad.list("archived")] == [item.id]
    assert pad.list("archived")[0].links == {"kanban": "board/t_1"}

    restored = pad.restore(item.id)
    assert restored[0].archived_at is None
    assert restored[0].starred is True
    assert restored[0].priority == 2
    assert restored[0].boards == ["work"]
    assert pad.list("archived") == []
    assert pad.list()[0].to_dict() == restored[0].to_dict()


def test_delete_unknown_and_restore_unknown(pad):
    pad.add_task("one")
    with pytest.raises(KeyError) as excinfo:
        pad.delete(99)
    assert excinfo.value.args[0] == 99
    with pytest.raises(KeyError):
        pad.restore(1)


def test_clear_archives_only_done_tasks(pad):
    done = pad.add_task("done")
    pending = pad.add_task("pending")
    active = pad.add_task("active")
    pad.check(done.id)
    pad.begin(active.id)
    note = pad.add_note("note")

    archived = pad.clear()
    assert [item.id for item in archived] == [done.id]
    assert [item.id for item in pad.list()] == [pending.id, active.id, note.id]
    assert [item.id for item in pad.list("archived")] == [done.id]
    assert pad.clear() == []


def test_find_is_case_insensitive(pad):
    upper = pad.add_task("Write THE Docs")
    pad.add_task("unrelated")
    pad.add_note("the note")
    assert [item.id for item in pad.find("the")] == [upper.id, 3]
    assert pad.find("missing") == []
    with pytest.raises(ValueError):
        pad.find(1)


def test_list_filters(pad):
    low = pad.add_task("low")
    high = pad.add_task("high", priority=3)
    note = pad.add_note("note", boards=["work"])
    done = pad.add_task("done", boards=["work"])
    pad.check(done.id)

    assert [item.id for item in pad.list()] == [high.id, low.id, note.id, done.id]
    assert [item.id for item in pad.list("pending")] == [high.id, low.id]
    assert [item.id for item in pad.list("done")] == [done.id]
    assert [item.id for item in pad.list("task")] == [
        high.id,
        low.id,
        done.id,
    ]
    assert [item.id for item in pad.list("note")] == [note.id]
    assert [item.id for item in pad.list("work")] == [note.id, done.id]
    assert [item.id for item in pad.list("work", "task")] == [done.id]
    assert [item.id for item in pad.list("task", "done")] == [done.id]
    assert pad.list("unknown-board") == []


def test_list_validation(pad):
    with pytest.raises(ValueError):
        pad.list("")
    with pytest.raises(ValueError):
        pad.list(1)


def test_boards_groups_items(pad):
    work = pad.add_task("work item", boards=["work"])
    both = pad.add_task("both", boards=["work", "home"])
    note = pad.add_note("idea", boards=["home"])
    pad.add_task("default")

    grouped = pad.boards()
    assert list(grouped) == [DEFAULT_BOARD, "home", "work"]
    assert [item.id for item in grouped[DEFAULT_BOARD]] == [4]
    assert [item.id for item in grouped["home"]] == [both.id, note.id]
    assert [item.id for item in grouped["work"]] == [work.id, both.id]


def test_timeline_groups_by_created_date(pad, monkeypatch):
    stamps = iter(
        [
            "2026-01-01T10:00:00+00:00",
            "2026-01-02T10:00:00+00:00",
            "2026-01-02T11:00:00+00:00",
        ]
    )
    monkeypatch.setattr("orchpad.pad.utcnow", lambda: next(stamps))
    first = pad.add_task("first")
    second = pad.add_task("second")
    third = pad.add_note("third")

    timeline = pad.timeline()
    assert list(timeline) == ["2026-01-02", "2026-01-01"]
    assert [item.id for item in timeline["2026-01-02"]] == [second.id, third.id]
    assert [item.id for item in timeline["2026-01-01"]] == [first.id]


def test_stats_and_percent(pad):
    assert pad.stats() == {
        "done": 0,
        "in_progress": 0,
        "pending": 0,
        "notes": 0,
        "percent_done": 0,
    }
    done = pad.add_task("done")
    pad.add_task("pending")
    started = pad.add_task("started")
    pad.add_task("last")
    pad.add_note("note")
    pad.check(done.id)
    pad.begin(started.id)
    assert pad.stats() == {
        "done": 1,
        "in_progress": 1,
        "pending": 2,
        "notes": 1,
        "percent_done": 25,
    }


def test_link(pad):
    item = pad.add_task("linked")
    linked = pad.link(item.id, "kanban", "board/t_9")
    assert linked.links == {"kanban": "board/t_9"}
    pad.link(item.id, "url", "https://example.invalid/1")
    assert pad.list()[0].links == {
        "kanban": "board/t_9",
        "url": "https://example.invalid/1",
    }
    with pytest.raises(ValueError):
        pad.link(item.id, "", "x")
    with pytest.raises(ValueError):
        pad.link(item.id, "key", 3)


@pytest.mark.parametrize(
    "operation",
    [
        lambda pad, item_id: pad.check(item_id),
        lambda pad, item_id: pad.begin(item_id),
        lambda pad, item_id: pad.star(item_id),
        lambda pad, item_id: pad.set_priority(item_id, 2),
        lambda pad, item_id: pad.edit(item_id, "new"),
        lambda pad, item_id: pad.move(item_id, ["work"]),
        lambda pad, item_id: pad.delete(item_id),
        lambda pad, item_id: pad.restore(item_id),
        lambda pad, item_id: pad.link(item_id, "k", "v"),
    ],
)
def test_unknown_ids_raise_key_error(pad, operation):
    with pytest.raises(KeyError) as excinfo:
        operation(pad, 4242)
    assert excinfo.value.args[0] == 4242


def test_multi_id_operations_are_atomic(pad):
    kept = pad.add_task("kept")
    with pytest.raises(KeyError):
        pad.check(kept.id, 999)
    assert pad.list()[0].status == "pending"


def test_multi_id_operations(pad):
    first = pad.add_task("first")
    second = pad.add_task("second")
    assert [item.id for item in pad.check(first.id, second.id)] == [
        first.id,
        second.id,
    ]
    assert [item.status for item in pad.list("done")] == ["done", "done"]
    assert [item.id for item in pad.delete(first.id, second.id)] == [
        first.id,
        second.id,
    ]
    assert pad.list() == []
    assert [item.id for item in pad.restore(first.id, second.id)] == [
        first.id,
        second.id,
    ]


def test_ids_are_never_reused(pad):
    first = pad.add_task("one")
    second = pad.add_task("two")
    pad.delete(second.id)
    third = pad.add_task("three")
    assert first.id == 1
    assert second.id == 2
    assert third.id == 3
    pad.restore(second.id)
    fourth = pad.add_task("four")
    assert fourth.id == 4
    assert sorted(item.id for item in pad.list()) == [1, 2, 3, 4]


def test_archive_schema_and_next_id(pad):
    item = pad.add_task("archived")
    pad.delete(item.id)
    raw = json.loads((pad.path / "archive.json").read_text())
    assert raw["version"] == 2
    assert raw["next_id"] == 2
    assert raw["items"]["1"]["archived_at"] is not None
    active = json.loads((pad.path / "storage.json").read_text())
    assert active["next_id"] == 2
    assert active["items"] == {}


def test_env_home_sets_default_path(tmp_path, monkeypatch):
    home = tmp_path / "custom-home"
    monkeypatch.setenv("ORCHPAD_HOME", str(home))
    pad = Pad()
    assert pad.path == home
    pad.add_task("via env")
    assert Pad().list()[0].description == "via env"
    assert (home / "storage.json").exists()


def test_home_falls_back_to_dot_orchpad(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCHPAD_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    pad = Pad()
    assert pad.path == tmp_path / ".orchpad"
    pad.add_task("via home")
    assert (tmp_path / ".orchpad" / "storage.json").exists()


def test_corrupt_store_surfaces(pad):
    pad.add_task("one")
    (pad.path / "storage.json").write_text("{oops")
    with pytest.raises(StoreError):
        pad.list()


def test_notes_are_not_archived_by_clear_and_do_not_count(pad):
    note = pad.add_note("note")
    assert pad.clear() == []
    assert pad.list("note") == [note]
    assert pad.stats()["notes"] == 1
