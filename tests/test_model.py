from __future__ import annotations

import pytest

from orchpad.model import (
    DEFAULT_BOARD,
    MAX_DESCRIPTION,
    Item,
    normalize_boards,
    validate_description,
)


def make_item(**overrides):
    values = {"id": 1, "kind": "task", "description": "write tests"}
    values.update(overrides)
    return Item(**values)


def test_task_defaults():
    item = make_item()
    assert item.boards == [DEFAULT_BOARD]
    assert item.priority == 1
    assert item.starred is False
    assert item.status == "pending"
    assert item.links == {}
    assert item.archived_at is None
    assert item.created_at == item.updated_at
    assert item.created_at.endswith("+00:00")


def test_note_defaults():
    item = make_item(kind="note")
    assert item.status is None
    assert item.priority == 1


def test_round_trip():
    item = make_item(
        id=7,
        boards=["work", "home", "work"],
        priority=3,
        starred=True,
        status="in_progress",
        links={"kanban": "board/t_1"},
        archived_at="2026-01-02T03:04:05+00:00",
    )
    rebuilt = Item.from_dict(item.to_dict())
    assert rebuilt == item
    assert rebuilt.boards == ["work", "home"]


def test_timestamps_are_utc_iso():
    item = make_item()
    assert item.created_at[4] == "-" and item.created_at[10] == "T"
    assert "+00:00" in item.created_at


@pytest.mark.parametrize("description", ["", "   ", "\n\t", 42, None])
def test_invalid_description(description):
    with pytest.raises(ValueError):
        make_item(description=description)


def test_description_length_boundary():
    item = make_item(description="x" * MAX_DESCRIPTION)
    assert len(item.description) == MAX_DESCRIPTION
    with pytest.raises(ValueError):
        make_item(description="x" * (MAX_DESCRIPTION + 1))


def test_invalid_kind():
    with pytest.raises(ValueError):
        make_item(kind="event")


@pytest.mark.parametrize("priority", [0, 4, "2", True, None])
def test_invalid_priority(priority):
    with pytest.raises(ValueError):
        make_item(priority=priority)


def test_notes_reject_status_and_priority():
    with pytest.raises(ValueError):
        make_item(kind="note", status="done")
    with pytest.raises(ValueError):
        make_item(kind="note", priority=2)


def test_tasks_reject_unknown_status():
    with pytest.raises(ValueError):
        make_item(status="blocked")


@pytest.mark.parametrize("item_id", [0, -1, "1", True, None])
def test_invalid_id(item_id):
    with pytest.raises(ValueError):
        make_item(id=item_id)


def test_normalize_boards():
    assert normalize_boards(None) == [DEFAULT_BOARD]
    assert normalize_boards(["a", "b", "a"]) == ["a", "b"]
    with pytest.raises(ValueError):
        normalize_boards([])
    with pytest.raises(ValueError):
        normalize_boards("board")
    with pytest.raises(ValueError):
        normalize_boards([""])


def test_validate_description():
    assert validate_description(" ok ") == " ok "
    with pytest.raises(ValueError):
        validate_description("")


def test_invalid_links():
    with pytest.raises(ValueError):
        make_item(links={"": "x"})
    with pytest.raises(ValueError):
        make_item(links={"kanban": 1})
    with pytest.raises(ValueError):
        make_item(links="kanban")


def test_invalid_timestamps():
    with pytest.raises(ValueError):
        make_item(created_at="yesterday")
    with pytest.raises(ValueError):
        make_item(archived_at="not-a-date")
