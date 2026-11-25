"""Tests for the opt-in Apple Reminders sync.

Everything here uses the in-memory fake backend.  The real JXA backend is
never invoked: no test runs ``osascript`` or reads the operator's lists.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from orchpad import cli, sync_reminders
from orchpad.pad import Pad
from orchpad.sync_reminders import (
    MAPS_FILENAME,
    STATE_FILENAME,
    Reminder,
    SyncError,
    decode_body,
    encode_body,
    run_sync,
)


class FakeBackend:
    """In-memory Reminders backend used by every test."""

    def __init__(self, lists=None):
        self.lists = {
            name: [replace(reminder, list_name=name) for reminder in reminders]
            for name, reminders in (lists or {}).items()
        }
        self.fetches = []
        self.creates = 0
        self.updates = 0
        self.deletes = 0
        self._next = 1
        self._clock = 0

    def _stamp(self):
        self._clock += 1
        return f"2026-01-01T00:00:{self._clock:02d}+00:00"

    def fetch(self, list_name):
        if list_name not in self.lists:
            raise SyncError(f"no such Reminders list {list_name!r}")
        self.fetches.append(list_name)
        return [replace(reminder) for reminder in self.lists[list_name]]

    def create(self, list_name, reminder):
        if list_name not in self.lists:
            raise SyncError(f"no such Reminders list {list_name!r}")
        created = replace(
            reminder,
            id=f"fake-{self._next}",
            list_name=list_name,
            modified_at=self._stamp(),
        )
        self._next += 1
        self.creates += 1
        self.lists[list_name].append(created)
        return replace(created)

    def update(self, reminder):
        for index, current in enumerate(self.lists.get(reminder.list_name, [])):
            if current.id == reminder.id:
                updated = replace(reminder, modified_at=self._stamp())
                self.lists[reminder.list_name][index] = updated
                self.updates += 1
                return replace(updated)
        raise SyncError(f"no reminder {reminder.id!r}")

    def delete(self, reminder):
        reminders = self.lists.get(reminder.list_name, [])
        for index, current in enumerate(reminders):
            if current.id == reminder.id:
                del reminders[index]
                self.deletes += 1
                return
        raise SyncError(f"no reminder {reminder.id!r}")

    # -- helpers ---------------------------------------------------------

    def by_title(self, list_name):
        return {reminder.title: reminder for reminder in self.lists[list_name]}

    def titles(self, list_name):
        return sorted(reminder.title for reminder in self.lists[list_name])

    def touch(self, list_name, reminder_id, *, modified_at=None, **changes):
        for index, current in enumerate(self.lists[list_name]):
            if current.id == reminder_id:
                updated = replace(
                    current,
                    modified_at=(
                        modified_at
                        if modified_at is not None
                        else "2099-01-01T00:00:00+00:00"
                    ),
                    **changes,
                )
                self.lists[list_name][index] = updated
                return updated
        raise AssertionError(f"no reminder {reminder_id!r}")

    def remove(self, list_name, reminder_id):
        self.lists[list_name] = [
            reminder
            for reminder in self.lists[list_name]
            if reminder.id != reminder_id
        ]


@pytest.fixture()
def pad(tmp_path):
    return Pad(tmp_path / "pad")


def sync(pad, backend, maps=("Work=Orchpad",), **kwargs):
    return run_sync(pad, list(maps), backend=backend, **kwargs)


def state_pairs(pad):
    data = json.loads((pad.path / STATE_FILENAME).read_text(encoding="utf-8"))
    return data["pairs"]


# ---------------------------------------------------------------------------
# First sync, both directions


def test_first_sync_creates_both_ways_and_is_idempotent(pad):
    pad.add_task(
        "local task",
        ["Work"],
        priority=2,
        due="2026-12-01",
        notes="hello",
        url="https://example.invalid/x",
    )
    backend = FakeBackend(
        {
            "Orchpad": [Reminder("remote-1", "Orchpad", "remote task")],
            "Other": [Reminder("other-1", "Other", "keep me private")],
        }
    )

    plan = sync(pad, backend)
    assert plan["apply"] is False
    assert {action["action"] for action in plan["actions"]} == {
        "create-reminder",
        "create-item",
    }
    # Dry run changed nothing anywhere.
    assert backend.titles("Orchpad") == ["remote task"]
    assert [item.description for item in pad.list()] == ["local task"]
    assert not (pad.path / STATE_FILENAME).exists()

    applied = sync(pad, backend, apply=True)
    assert applied["apply"] is True
    assert backend.titles("Orchpad") == ["local task", "remote task"]
    assert sorted(item.description for item in pad.list()) == [
        "local task",
        "remote task",
    ]
    assert backend.titles("Other") == ["keep me private"]
    assert backend.deletes == 0
    assert backend.fetches == ["Orchpad", "Orchpad"]

    local = backend.by_title("Orchpad")["local task"]
    assert local.priority == 5
    assert local.due == "2026-12-01"
    assert local.body == "hello\n\nURL: https://example.invalid/x"

    imported = next(item for item in pad.list() if item.description == "remote task")
    assert imported.boards == ["Work"]

    assert len(state_pairs(pad)) == 2
    assert sync(pad, backend)["actions"] == []


def test_completed_reminder_imports_as_done(pad):
    backend = FakeBackend(
        {
            "Orchpad": [
                Reminder(
                    "remote-1",
                    "Orchpad",
                    "already done",
                    completed=True,
                    completed_at="2026-11-01T00:00:00+00:00",
                )
            ]
        }
    )
    sync(pad, backend, apply=True)
    item = pad.list()[0]
    assert item.status == "done"
    assert item.completed_at == "2026-11-01T00:00:00+00:00"


def test_notes_are_skipped_and_reported(pad):
    pad.add_task("task", ["Work"])
    pad.add_note("memo", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    plan = sync(pad, backend)
    assert any("notes stay orchpad-only" in line for line in plan["skipped"])
    sync(pad, backend, apply=True)
    assert backend.titles("Orchpad") == ["task"]


def test_repeat_and_subtasks_are_reported_as_orchpad_only(pad):
    parent = pad.add_task("parent", ["Work"], repeat="weekly")
    pad.add_task("child", ["Work"], parent=parent.id)
    backend = FakeBackend({"Orchpad": []})
    plan = sync(pad, backend)
    assert any("repeat rules stay orchpad-only" in line for line in plan["warnings"])
    assert any("subtasks stay orchpad-only" in line for line in plan["warnings"])


# ---------------------------------------------------------------------------
# Updates, completion and conflicts


def test_update_from_pad(pad):
    item = pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)

    pad.edit_fields(item.id, notes="new notes", due="2026-12-02")
    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["update-reminder"]
    assert backend.by_title("Orchpad")["task"].body == ""

    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["task"]
    assert reminder.body == "new notes"
    assert reminder.due == "2026-12-02"


def test_update_from_reminder(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["task"]

    backend.touch(
        "Orchpad",
        reminder.id,
        title="renamed",
        body="remote notes\n\nURL: https://example.invalid/r",
        due="2026-12-05",
        priority=1,
        flagged=True,
    )
    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["update-item"]
    assert "Reminders changed since the last sync" in plan["actions"][0]["reason"]

    sync(pad, backend, apply=True)
    item = pad.list()[0]
    assert item.description == "renamed"
    assert item.notes == "remote notes"
    assert item.url == "https://example.invalid/r"
    assert item.due == "2026-12-05"
    assert item.priority == 3
    assert item.starred is True


def test_complete_from_pad(pad):
    item = pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)

    pad.check(item.id)
    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["complete-reminder"]

    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["task"]
    assert reminder.completed is True
    assert reminder.completed_at


def test_complete_from_reminder(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["task"]

    backend.touch(
        "Orchpad",
        reminder.id,
        completed=True,
        completed_at="2026-11-20T10:00:00+00:00",
    )
    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["complete-item"]

    sync(pad, backend, apply=True)
    item = pad.list()[0]
    assert item.status == "done"
    assert item.completed_at == "2026-11-20T10:00:00+00:00"


def test_conflict_most_recent_side_wins(pad):
    item = pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["task"]

    # Both sides changed; the pad edit is newer than the reminder's clock.
    pad.edit_fields(item.id, notes="pad edit")
    backend.touch(
        "Orchpad",
        reminder.id,
        title="old reminder edit",
        modified_at="2000-01-01T00:00:00+00:00",
    )
    plan = sync(pad, backend)
    assert plan["actions"][0]["action"] == "update-reminder"
    assert "orchpad side is newer" in plan["actions"][0]["reason"]
    sync(pad, backend, apply=True)
    assert backend.by_title("Orchpad")["task"].title == "task"

    # Now the reminder is newer and wins the conflict.
    pad.edit_fields(item.id, notes="pad edit 2")
    backend.touch(
        "Orchpad",
        reminder.id,
        title="new reminder edit",
        modified_at="2099-01-01T00:00:00+00:00",
    )
    plan = sync(pad, backend)
    assert plan["actions"][0]["action"] == "update-item"
    assert "Reminders side is newer" in plan["actions"][0]["reason"]
    sync(pad, backend, apply=True)
    assert pad.list()[0].description == "new reminder edit"


# ---------------------------------------------------------------------------
# Deletions and first-sync safety


def test_pad_deletion_removes_previously_synced_reminder(pad):
    item = pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)

    pad.delete(item.id)
    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["delete-reminder"]
    assert backend.titles("Orchpad") == ["task"]
    assert backend.deletes == 0

    sync(pad, backend, apply=True)
    assert backend.titles("Orchpad") == []
    assert backend.deletes == 1
    assert state_pairs(pad) == []


def test_reminder_deletion_archives_previously_synced_item(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    backend.remove("Orchpad", backend.by_title("Orchpad")["task"].id)

    plan = sync(pad, backend)
    assert [action["action"] for action in plan["actions"]] == ["delete-item"]
    sync(pad, backend, apply=True)
    assert pad.list() == []
    assert "1" in pad.store.load_archive()["items"]


def test_first_sync_never_deletes_anything(pad):
    pad.add_task("local only", ["Work"])
    backend = FakeBackend(
        {"Orchpad": [Reminder("remote-1", "Orchpad", "remote only")]}
    )
    plan = sync(pad, backend)
    assert plan["counts"]["total"] == 2
    assert backend.deletes == 0
    sync(pad, backend, apply=True)
    assert backend.deletes == 0
    assert sorted(item.description for item in pad.list()) == [
        "local only",
        "remote only",
    ]


# ---------------------------------------------------------------------------
# Mapping guards and directions


def test_unmapped_list_is_never_fetched_or_touched(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend(
        {
            "Orchpad": [],
            "Unmapped": [Reminder("p1", "Unmapped", "private")],
        }
    )
    sync(pad, backend, apply=True)
    assert backend.fetches == ["Orchpad"]
    assert backend.titles("Unmapped") == ["private"]


def test_exclude_list_refuses_before_any_backend_call(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend({"Private": []})
    with pytest.raises(SyncError, match="deny list"):
        sync(pad, backend, maps=["Work=Private"], exclude_lists=["Private"])
    assert backend.fetches == []
    assert backend.creates == 0
    assert not (pad.path / STATE_FILENAME).exists()


def test_environment_deny_list_refuses_case_insensitively(pad, monkeypatch):
    backend = FakeBackend({"Denied": []})
    with pytest.raises(SyncError, match="deny list"):
        run_sync(
            pad,
            ["Work=Denied"],
            backend=backend,
            env={"ORCHPAD_SYNC_DENY": "denied, something-else"},
        )
    assert backend.fetches == []


def test_missing_list_is_refused_and_never_created(pad):
    backend = FakeBackend({"Other": []})
    with pytest.raises(SyncError, match="no such Reminders list"):
        run_sync(pad, ["Work=Missing"], backend=backend)
    assert set(backend.lists) == {"Other"}
    assert backend.fetches == []


def test_to_reminders_ignores_unpaired_reminders(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend(
        {"Orchpad": [Reminder("remote-1", "Orchpad", "remote")]}
    )
    plan = sync(pad, backend, direction="to-reminders")
    assert [action["action"] for action in plan["actions"]] == ["create-reminder"]
    sync(pad, backend, direction="to-reminders", apply=True)
    assert [item.description for item in pad.list()] == ["task"]
    assert backend.titles("Orchpad") == ["remote", "task"]


def test_from_reminders_ignores_unpaired_items(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend(
        {"Orchpad": [Reminder("remote-1", "Orchpad", "remote")]}
    )
    plan = sync(pad, backend, direction="from-reminders")
    assert [action["action"] for action in plan["actions"]] == ["create-item"]
    sync(pad, backend, direction="from-reminders", apply=True)
    assert backend.titles("Orchpad") == ["remote"]


# ---------------------------------------------------------------------------
# Field mapping helpers


def test_url_and_body_round_trip():
    assert encode_body(None, "https://example.invalid/a") == (
        "URL: https://example.invalid/a"
    )
    assert decode_body("URL: https://example.invalid/a") == (
        None,
        "https://example.invalid/a",
    )
    assert encode_body("note", "https://example.invalid/b") == (
        "note\n\nURL: https://example.invalid/b"
    )
    assert decode_body("note\n\nURL: https://example.invalid/b") == (
        "note",
        "https://example.invalid/b",
    )
    assert decode_body("just notes") == ("just notes", None)
    assert decode_body("") == (None, None)


def test_priority_and_flag_mapping_from_reminder(pad):
    pad.add_task("high", ["Work"], priority=3)
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    reminder = backend.by_title("Orchpad")["high"]
    assert reminder.priority == 1  # Reminders high

    backend.touch("Orchpad", reminder.id, priority=5, flagged=True)
    sync(pad, backend, apply=True)
    item = pad.list()[0]
    assert item.priority == 2  # medium
    assert item.starred is True


# ---------------------------------------------------------------------------
# Saved mappings and the CLI


def test_saved_mappings_are_reused(pad):
    pad.add_task("task", ["Work"])
    backend = FakeBackend({"Orchpad": []})
    sync(pad, backend, maps=["Work=Orchpad"], save=True)
    assert (pad.path / MAPS_FILENAME).exists()
    assert not (pad.path / STATE_FILENAME).exists()

    plan = run_sync(pad, backend=backend)
    assert plan["mappings"] == [{"board": "Work", "list": "Orchpad"}]
    assert plan["counts"]["total"] == 1


def test_cli_json_plan_uses_fake_backend(tmp_path, monkeypatch, capsys):
    backend = FakeBackend(
        {"Orchpad": [Reminder("remote-1", "Orchpad", "remote task")]}
    )
    monkeypatch.setattr(sync_reminders.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sync_reminders, "_make_backend", lambda: backend)
    pad_dir = tmp_path / "pad"

    code = cli.main(
        [
            "sync",
            "reminders",
            "--map",
            "Work=Orchpad",
            "--pad-dir",
            str(pad_dir),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is False
    assert payload["counts"] == {"to_reminders": 0, "to_orchpad": 1, "total": 1}
    assert payload["actions"][0]["action"] == "create-item"
    assert not (pad_dir / STATE_FILENAME).exists()


def test_cli_refuses_non_macos(tmp_path, monkeypatch, capsys):
    def explode():  # pragma: no cover - must not be called
        raise AssertionError("backend must not be built off macOS")

    monkeypatch.setattr(sync_reminders.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sync_reminders, "_make_backend", explode)
    pad_dir = tmp_path / "pad"

    code = cli.main(
        [
            "sync",
            "reminders",
            "--map",
            "Work=Orchpad",
            "--pad-dir",
            str(pad_dir),
        ]
    )
    assert code == 1
    assert "macOS" in capsys.readouterr().err


def test_cli_requires_a_mapping(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync_reminders.platform, "system", lambda: "Darwin")
    code = cli.main(
        ["sync", "reminders", "--pad-dir", str(tmp_path / "pad")]
    )
    assert code == 1
    assert "no list mappings" in capsys.readouterr().err


def test_corrupt_state_is_refused(pad):
    pad.path.mkdir(parents=True, exist_ok=True)
    (pad.path / STATE_FILENAME).write_text("{not json", encoding="utf-8")
    backend = FakeBackend({"Orchpad": []})
    with pytest.raises(SyncError, match="corrupt sync state"):
        sync(pad, backend)


def test_jxa_backend_uses_osascript_javascript(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            [
                {
                    "id": "x-apple-reminder://1",
                    "list_name": "Orchpad",
                    "title": "remote",
                    "body": "",
                    "priority": 0,
                    "flagged": False,
                    "completed": False,
                }
            ]
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(sync_reminders.subprocess, "run", fake_run)
    backend = sync_reminders.JxaRemindersBackend()
    reminders = backend.fetch("Orchpad")

    assert calls[0][:3] == ["osascript", "-l", "JavaScript"]
    assert calls[0][3] == "-e"
    assert json.loads(calls[0][-1]) == {"op": "fetch", "list": "Orchpad"}
    assert reminders[0].title == "remote"
    assert reminders[0].id == "x-apple-reminder://1"


# ---------------------------------------------------------------------------
# Backend type mapping and per-item resilience


class StrictBackend(FakeBackend):
    """Fake backend that only accepts values JXA can coerce losslessly."""

    def _validate(self, reminder):
        assert type(reminder.priority) is int
        assert reminder.priority in (0, 1, 5, 9)
        assert type(reminder.flagged) is bool
        assert type(reminder.completed) is bool
        assert reminder.due is None or isinstance(reminder.due, str)
        assert reminder.remind is None or isinstance(reminder.remind, str)
        assert reminder.completed_at is None or isinstance(
            reminder.completed_at, str
        )

    def create(self, list_name, reminder):
        self._validate(reminder)
        return super().create(list_name, reminder)

    def update(self, reminder):
        self._validate(reminder)
        return super().update(reminder)


def test_reminder_payload_types_match_apples_dictionary(pad):
    expected = {1: 0, 2: 5, 3: 1}
    for priority in (1, 2, 3):
        item = pad.add_task(
            f"p{priority}",
            ["Work"],
            priority=priority,
            due="2026-12-04",
            remind_at="2026-12-04T09:30:00+01:00",
        )
        reminder = sync_reminders.reminder_for_item(item, "Orchpad")
        assert reminder.priority == expected[priority]
        assert type(reminder.priority) is int
        assert type(reminder.flagged) is bool
        assert type(reminder.completed) is bool

    item = pad.add_task("done", ["Work"], priority=3, due="2026-12-05")
    item = pad.star(item.id)[0]
    done = pad.check(item.id)[0]
    reminder = sync_reminders.reminder_for_item(done, "Orchpad")
    assert reminder.priority == 1
    assert reminder.flagged is True
    assert reminder.completed is True
    payload = json.loads(json.dumps(reminder.to_dict()))
    assert payload["priority"] == 1
    assert type(payload["priority"]) is int
    assert payload["completed"] is True
    assert payload["completed_at"] == done.completed_at
    assert payload["due"] == "2026-12-05"


def test_strict_backend_accepts_completed_and_pending_payloads(pad):
    done = pad.add_task(
        "done", ["Work"], priority=2, due="2026-12-06", notes="n"
    )
    pad.check(done.id)
    pad.add_task("pending", ["Work"], priority=3, due="2026-12-07")
    backend = StrictBackend({"Orchpad": []})

    payload = sync(pad, backend, apply=True)
    assert payload["failures"] == []
    stored = backend.by_title("Orchpad")
    assert stored["done"].completed is True
    assert stored["done"].priority == 5
    assert stored["pending"].completed is False
    assert stored["pending"].priority == 1
    assert sync(pad, backend)["actions"] == []


class FailingBackend(FakeBackend):
    """Fake backend that fails on selected titles, like a bad JXA write."""

    def __init__(self, lists=None, failing_titles=()):
        super().__init__(lists)
        self.failing_titles = set(failing_titles)
        self.failed_calls = []

    def _maybe_fail(self, operation, reminder):
        if reminder.title in self.failing_titles:
            self.failed_calls.append((operation, reminder.title))
            raise SyncError(
                "Apple Reminders error: execution error: Error: "
                "Error: Can't convert types. (-1700)"
            )

    def create(self, list_name, reminder):
        self._maybe_fail("create", reminder)
        return super().create(list_name, reminder)

    def update(self, reminder):
        self._maybe_fail("update", reminder)
        return super().update(reminder)


def test_one_item_failure_is_reported_and_others_still_apply(pad):
    for title in ("bad task", "good one", "good two"):
        pad.add_task(title, ["Work"], due="2026-12-01")
    backend = FailingBackend(
        {"Orchpad": [Reminder("remote-1", "Orchpad", "remote task")]},
        failing_titles={"bad task"},
    )

    payload = sync(pad, backend, apply=True)
    assert len(payload["failures"]) == 1
    failure = payload["failures"][0]
    assert failure["action"] == "create-reminder"
    assert failure["title"] == "bad task"
    assert "Can't convert types" in failure["error"]

    # Both the other pushes and the pull still happened.
    assert backend.titles("Orchpad") == ["good one", "good two", "remote task"]
    assert sorted(item.description for item in pad.list()) == [
        "bad task",
        "good one",
        "good two",
        "remote task",
    ]
    items = {item.id: item.description for item in pad.list()}
    paired = sorted(items[pair["item_id"]] for pair in state_pairs(pad))
    assert paired == ["good one", "good two", "remote task"]

    # A re-run retries only the failed item, then converges.
    backend.failing_titles.clear()
    creates_before = backend.creates
    retry = sync(pad, backend, apply=True)
    assert retry["failures"] == []
    assert backend.creates == creates_before + 1
    assert backend.failed_calls == [("create", "bad task")]
    assert len(state_pairs(pad)) == 4
    assert sync(pad, backend)["actions"] == []


def test_failed_update_keeps_previous_state_and_retries(pad):
    item = pad.add_task("task", ["Work"])
    backend = FailingBackend({"Orchpad": []})
    sync(pad, backend, apply=True)
    before = state_pairs(pad)

    pad.edit_fields(item.id, notes="updated")
    backend.failing_titles.add("task")
    payload = sync(pad, backend, apply=True)
    assert [failure["action"] for failure in payload["failures"]] == [
        "update-reminder"
    ]
    assert state_pairs(pad) == before
    assert backend.by_title("Orchpad")["task"].body == ""

    backend.failing_titles.clear()
    retry = sync(pad, backend, apply=True)
    assert retry["failures"] == []
    assert backend.by_title("Orchpad")["task"].body == "updated"
    assert sync(pad, backend)["actions"] == []


def test_cli_exits_one_when_an_item_fails(tmp_path, monkeypatch, capsys):
    backend = FailingBackend({"Orchpad": []}, failing_titles={"bad task"})
    monkeypatch.setattr(sync_reminders.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sync_reminders, "_make_backend", lambda: backend)
    pad_dir = tmp_path / "pad"
    local = Pad(pad_dir)
    local.add_task("bad task", ["Work"])
    local.add_task("good task", ["Work"])

    code = cli.main(
        [
            "sync",
            "reminders",
            "--map",
            "Work=Orchpad",
            "--pad-dir",
            str(pad_dir),
            "--apply",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "FAILED create-reminder" in out
    assert "Can't convert types" in out
    assert "1 of 2 actions applied; 1 failed" in out
    assert backend.titles("Orchpad") == ["good task"]
    assert len(state_pairs(local)) == 1
