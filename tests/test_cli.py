from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchpad import __version__, cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path):
    return {"ORCHPAD_HOME": str(tmp_path / "pad")}


def run(env, *args, extra=None):
    child = os.environ.copy()
    child.pop("NO_COLOR", None)
    child["PYTHONIOENCODING"] = "utf-8"
    child["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + child["PYTHONPATH"] if child.get("PYTHONPATH") else ""
    )
    child.update(env)
    if extra:
        child.update(extra)
    return subprocess.run(
        [sys.executable, "-m", "orchpad", *args],
        cwd=ROOT,
        env=child,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def add_task(env, *args):
    result = run(env, "-t", *args)
    assert result.returncode == 0, result.stderr
    return result


def add_task_json(env, *args):
    result = run(env, "-t", *args, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["item"]


def test_no_args_shows_board_and_footer(env):
    add_task(env, "write docs")
    result = run(env)
    assert result.returncode == 0
    assert "My Board" in result.stdout
    assert "☐ write docs" in result.stdout
    assert (
        "0% of all tasks complete · 0 done · 0 in progress · 1 pending · 0 notes"
        in result.stdout
    )
    assert "\x1b[" not in result.stdout


def test_board_view_orders_default_board_first(env):
    add_task(env, "work item @work")
    add_task(env, "default item")
    result = run(env)
    assert result.returncode == 0
    assert result.stdout.index("My Board") < result.stdout.index("\nwork\n")
    assert "work item" in result.stdout and "default item" in result.stdout


def test_task_tokens_and_boards_json(env):
    item = add_task_json(env, "ship the release @work @home p:3")
    assert item["kind"] == "task"
    assert item["description"] == "ship the release"
    assert item["boards"] == ["work", "home"]
    assert item["priority"] == 3
    assert item["status"] == "pending"


def test_task_tokens_in_separate_arguments(env):
    item = add_task_json(env, "buy", "milk", "@work", "p:2")
    assert item["description"] == "buy milk"
    assert item["boards"] == ["work"]
    assert item["priority"] == 2


def test_board_tokens_strip_punctuation(env):
    item = add_task_json(env, "do x @work, @home")
    assert item["description"] == "do x"
    assert item["boards"] == ["work", "home"]


def test_note_flag_and_boards_json(env):
    result = run(env, "-n", "remember the milk @ideas", "--json")
    assert result.returncode == 0, result.stderr
    note = json.loads(result.stdout)["item"]
    assert note["kind"] == "note"
    assert note["description"] == "remember the milk"
    assert note["boards"] == ["ideas"]
    assert note["status"] is None
    assert note["priority"] == 1


def test_check_toggles_and_symbol(env):
    add_task(env, "toggle me")
    checked = run(env, "-c", "1", "--json")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["items"][0]["status"] == "done"
    board = run(env)
    assert "✔ toggle me" in board.stdout
    unchecked = run(env, "-c", "1", "--json")
    assert json.loads(unchecked.stdout)["items"][0]["status"] == "pending"


def test_begin_toggles_and_symbol(env):
    add_task(env, "start me")
    begun = run(env, "-b", "1", "--json")
    assert begun.returncode == 0, begun.stderr
    assert json.loads(begun.stdout)["items"][0]["status"] == "in_progress"
    assert "… start me" in run(env).stdout
    assert json.loads(run(env, "-b", "1", "--json").stdout)["items"][0][
        "status"
    ] == "pending"


def test_star_toggles_and_symbol(env):
    add_task(env, "star me")
    starred = run(env, "-s", "1", "--json")
    assert starred.returncode == 0, starred.stderr
    assert json.loads(starred.stdout)["items"][0]["starred"] is True
    assert "★" in run(env).stdout
    assert json.loads(run(env, "-s", "1", "--json").stdout)["items"][0][
        "starred"
    ] is False


def test_multiple_ids_comma_separated(env):
    add_task(env, "one")
    add_task(env, "two")
    result = run(env, "-c", "1,2", "--json")
    assert result.returncode == 0, result.stderr
    assert [item["status"] for item in json.loads(result.stdout)["items"]] == [
        "done",
        "done",
    ]


def test_priority_flag(env):
    add_task(env, "priority test")
    result = run(env, "-p", "1", "3", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"][0]["priority"] == 3
    assert run(env, "-p", "1", "4").returncode == 1


def test_priority_flag_rejects_notes(env):
    run(env, "-n", "just a note")
    result = run(env, "-p", "1", "2")
    assert result.returncode == 1
    assert "note" in result.stderr


def test_edit_flag(env):
    add_task(env, "old words")
    result = run(env, "-e", "1", "new", "words", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"][0]["description"] == "new words"


def test_edit_empty_description_exits_1(env):
    add_task(env, "old words")
    result = run(env, "-e", "1", "")
    assert result.returncode == 1
    assert "description" in result.stderr


def test_move_flag(env):
    add_task(env, "move me")
    result = run(env, "-m", "1", "work", "home", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["items"][0]["boards"] == ["work", "home"]
    comma = run(env, "-m", "1", "work, home", "--json")
    assert json.loads(comma.stdout)["items"][0]["boards"] == ["work", "home"]
    assert run(env, "-m", "1").returncode == 1


def test_delete_archive_restore(env):
    add_task(env, "old task")
    deleted = run(env, "-d", "1", "--json")
    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout)["items"][0]["archived_at"] is not None

    board = run(env)
    assert "old task" not in board.stdout
    assert "0% of all tasks complete · 0 done · 0 in progress · 0 pending · 0 notes" in board.stdout

    archive = run(env, "-a")
    assert archive.returncode == 0
    assert "Archive" in archive.stdout
    assert "old task" in archive.stdout

    archived = run(env, "-a", "--json")
    assert [item["id"] for item in json.loads(archived.stdout)["items"]] == [1]

    restored = run(env, "-r", "1", "--json")
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["items"][0]["archived_at"] is None
    assert "old task" in run(env).stdout


def test_archive_empty_message(env):
    result = run(env, "-a")
    assert result.returncode == 0
    assert "No archived items." in result.stdout


def test_clear_flag(env):
    add_task(env, "done one")
    add_task(env, "still open")
    run(env, "-c", "1")
    result = run(env, "--clear", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["cleared"] == 1
    assert [item["id"] for item in payload["items"]] == [1]
    assert "done one" not in run(env).stdout
    assert "done one" in run(env, "-a").stdout
    assert json.loads(run(env, "--clear", "--json").stdout)["cleared"] == 0


def test_find_flag(env):
    add_task(env, "release notes")
    add_task(env, "write code")
    result = run(env, "-f", "RELEASE", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["query"] == "RELEASE"
    assert [item["description"] for item in payload["items"]] == ["release notes"]

    human = run(env, "-f", "release")
    assert 'Search results for "release"' in human.stdout
    assert "release notes" in human.stdout

    empty = run(env, "-f", "nothing-matches")
    assert empty.returncode == 0
    assert "No matches." in empty.stdout


def test_list_flag_filters(env):
    add_task(env, "pending task")
    add_task(env, "done task")
    run(env, "-c", "2")
    run(env, "-n", "a note")
    add_task(env, "work task @work")

    everything = run(env, "-l", "--json")
    assert everything.returncode == 0, everything.stderr
    assert len(json.loads(everything.stdout)["items"]) == 4

    done = json.loads(run(env, "-l", "done", "--json").stdout)
    assert [item["description"] for item in done["items"]] == ["done task"]

    notes = json.loads(run(env, "-l", "note", "--json").stdout)
    assert [item["description"] for item in notes["items"]] == ["a note"]

    board = json.loads(run(env, "-l", "@work", "--json").stdout)
    assert [item["description"] for item in board["items"]] == ["work task"]

    human = run(env, "-l", "pending")
    assert "pending task" in human.stdout
    assert "done task" not in human.stdout


def test_timeline_flag(env):
    add_task(env, "timeline task")
    result = run(env, "-i", "--json")
    assert result.returncode == 0, result.stderr
    timeline = json.loads(result.stdout)["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["date"] == timeline[0]["items"][0]["created_at"][:10]
    assert timeline[0]["items"][0]["description"] == "timeline task"

    human = run(env, "-i")
    assert timeline[0]["date"] in human.stdout
    assert "timeline task" in human.stdout


def test_stats_flag(env):
    add_task(env, "first")
    add_task(env, "second")
    run(env, "-c", "1")
    result = run(env, "--stats", "--json")
    assert result.returncode == 0, result.stderr
    stats = json.loads(result.stdout)
    assert stats["done"] == 1
    assert stats["pending"] == 1
    assert stats["in_progress"] == 0
    assert stats["notes"] == 0
    assert stats["percent_done"] == 50
    assert stats["tasks"] == 2
    assert stats["total"] == 2

    human = run(env, "--stats")
    assert "Total: 2 items (2 tasks, 0 notes)" in human.stdout
    assert "50% of all tasks complete · 1 done · 0 in progress · 1 pending · 0 notes" in human.stdout


def test_where_flag(env):
    result = run(env, "--where")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == env["ORCHPAD_HOME"]
    machine = run(env, "--where", "--json")
    assert json.loads(machine.stdout)["path"] == env["ORCHPAD_HOME"]
    assert not Path(env["ORCHPAD_HOME"]).exists()


def test_pad_dir_selects_another_pad(env, tmp_path):
    other = tmp_path / "other-pad"
    added = run(env, "--pad-dir", str(other), "-t", "elsewhere")
    assert added.returncode == 0, added.stderr
    assert (other / "storage.json").is_file()
    shown = run(env, "--pad-dir", str(other), "--json")
    assert "elsewhere" in shown.stdout
    default = run(env, "--json")
    assert "elsewhere" not in default.stdout


def test_version_flag(env):
    for flag in ("--version", "-v"):
        result = run(env, flag)
        assert result.returncode == 0
        assert __version__ in result.stdout


def test_help_flag(env):
    for flag in ("--help", "-h"):
        result = run(env, flag)
        assert result.returncode == 0
        for text in (
            "--archive",
            "--begin",
            "--check",
            "--json",
            "--pad-dir",
            "--where",
            "--stats",
            "--timeline",
            "exit codes",
        ):
            assert text in result.stdout


def test_every_read_view_json_parses(env):
    add_task(env, "json task @work")
    for args in (
        ("--json",),
        ("-a", "--json"),
        ("-l", "--json"),
        ("-l", "work", "--json"),
        ("-f", "json", "--json"),
        ("-i", "--json"),
        ("--stats", "--json"),
        ("--where", "--json"),
    ):
        result = run(env, *args)
        assert result.returncode == 0, (args, result.stderr)
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        assert "boards" in payload or "items" in payload or "timeline" in payload or "path" in payload or "percent_done" in payload


def test_json_write_payloads(env):
    task = json.loads(run(env, "-t", "one", "--json").stdout)["item"]
    note = json.loads(run(env, "-n", "two", "--json").stdout)["item"]
    assert task["id"] == 1 and note["id"] == 2
    checked = json.loads(run(env, "-c", "1", "--json").stdout)["items"]
    assert checked[0]["status"] == "done"
    starred = json.loads(run(env, "-s", "2", "--json").stdout)["items"]
    assert starred[0]["starred"] is True
    deleted = json.loads(run(env, "-d", "2", "--json").stdout)["items"]
    assert deleted[0]["archived_at"] is not None
    restored = json.loads(run(env, "-r", "2", "--json").stdout)["items"]
    assert restored[0]["archived_at"] is None


def test_unknown_id_exits_1(env):
    result = run(env, "-c", "42")
    assert result.returncode == 1
    assert "no item with id 42" in result.stderr


def test_invalid_id_exits_1(env):
    result = run(env, "-c", "abc")
    assert result.returncode == 1
    assert "invalid id" in result.stderr


def test_empty_description_exits_1(env):
    assert run(env, "-t", "").returncode == 1
    assert run(env, "-t", "   ").returncode == 1
    assert run(env, "-n", "").returncode == 1


def test_unknown_flag_exits_1_not_2(env):
    result = run(env, "--bogus")
    assert result.returncode == 1
    assert "unrecognized arguments" in result.stderr


def test_conflicting_commands_exit_1(env):
    add_task(env, "one")
    result = run(env, "-c", "1", "--archive")
    assert result.returncode == 1
    assert "choose one command" in result.stderr


def test_store_error_exits_2(env):
    home = Path(env["ORCHPAD_HOME"])
    home.mkdir(parents=True)
    (home / "storage.json").write_text("{oops", encoding="utf-8")
    read = run(env)
    assert read.returncode == 2
    assert "corrupt" in read.stderr
    write = run(env, "-t", "nope")
    assert write.returncode == 2


def test_no_color_env_and_piped_output(env):
    add_task(env, "plain task")
    piped = run(env)
    assert "\x1b[" not in piped.stdout
    no_color = run(env, extra={"NO_COLOR": "1"})
    assert "\x1b[" not in no_color.stdout


class _FakeStream:
    def __init__(self, *, tty: bool, encoding: str = "utf-8") -> None:
        self._tty = tty
        self.encoding = encoding

    def isatty(self) -> bool:
        return self._tty


def test_use_color_requires_tty_and_honours_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    assert cli._use_color(_FakeStream(tty=True)) is True
    assert cli._use_color(_FakeStream(tty=False)) is False
    monkeypatch.setenv("NO_COLOR", "")
    assert cli._use_color(_FakeStream(tty=True)) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert cli._use_color(_FakeStream(tty=True)) is False


def test_use_unicode_follows_stream_encoding(monkeypatch):
    assert cli._use_unicode(_FakeStream(tty=False, encoding="utf-8")) is True
    assert cli._use_unicode(_FakeStream(tty=False, encoding="ascii")) is False
    assert cli._use_unicode(_FakeStream(tty=False, encoding="not-a-codec")) is False


def test_ascii_symbol_fallback(env):
    add_task(env, "pending task")
    add_task(env, "done task")
    add_task(env, "active task")
    run(env, "-c", "2", extra={"PYTHONIOENCODING": "ascii"})
    run(env, "-b", "3", extra={"PYTHONIOENCODING": "ascii"})
    run(env, "-s", "1", extra={"PYTHONIOENCODING": "ascii"})

    result = run(env, extra={"PYTHONIOENCODING": "ascii"})
    assert result.returncode == 0
    assert "[ ] pending task *" in result.stdout
    assert "[x] done task" in result.stdout
    assert "[~] active task" in result.stdout
    assert "☐" not in result.stdout and "✔" not in result.stdout
    assert "·" not in result.stdout
    assert "% of all tasks complete | " in result.stdout


def test_console_script_entry_points_declared():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'orchpad = "orchpad.cli:main"' in pyproject
    assert 'op = "orchpad.cli:main"' in pyproject


def test_task_reminder_tokens_and_json_fields(env):
    item = add_task_json(
        env,
        "pay rent",
        "due:today",
        "remind:+1h",
        "every:month",
        "#home",
        "#money",
    )
    assert item["description"] == "pay rent"
    assert item["due"] is not None
    assert item["remind_at"] is not None
    assert item["repeat"] == "monthly"
    assert item["tags"] == ["home", "money"]
    for key in (
        "due",
        "remind_at",
        "repeat",
        "notes",
        "url",
        "tags",
        "parent",
        "completed_at",
    ):
        assert key in item


def test_quoted_due_token(env):
    item = add_task_json(env, "call", 'due:"fri 17:00"')
    assert item["description"] == "call"
    assert "T17:00:00" in item["due"]


def test_due_notes_url_tags_flags(env):
    add_task(env, "field test")
    result = run(env, "--due", "1", "2026-12-01", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["item"]["due"] == "2026-12-01"

    cleared = run(env, "--due", "1", "", "--json")
    assert json.loads(cleared.stdout)["item"]["due"] is None

    notes = run(env, "--notes", "1", "first line", "second line", "--json")
    assert json.loads(notes.stdout)["item"]["notes"] == "first line second line"

    url = run(env, "--url", "1", "https://example.invalid/x", "--json")
    assert json.loads(url.stdout)["item"]["url"] == (
        "https://example.invalid/x"
    )
    assert json.loads(run(env, "--url", "1", "", "--json").stdout)["item"][
        "url"
    ] is None

    tags = run(env, "--tags", "1", "work", "urgent", "--json")
    assert json.loads(tags.stdout)["item"]["tags"] == ["work", "urgent"]
    assert json.loads(run(env, "--tags", "1", "--json").stdout)["item"][
        "tags"
    ] == []


def test_subtask_flag_and_force_check(env):
    add_task(env, "parent")
    subtask = run(env, "--sub", "1", "child", "due:today", "--json")
    assert subtask.returncode == 0, subtask.stderr
    child = json.loads(subtask.stdout)["item"]
    assert child["parent"] == 1
    assert child["due"] is not None

    refused = run(env, "-c", "1")
    assert refused.returncode == 1
    assert "--force" in refused.stderr
    assert json.loads(run(env, "-l", "done", "--json").stdout)["items"] == []

    forced = run(env, "-c", "1", "--force", "--json")
    assert forced.returncode == 0, forced.stderr
    assert json.loads(forced.stdout)["items"][0]["status"] == "done"


def test_subtask_inherits_parent_boards_and_renders_nested(env):
    parent = add_task_json(env, "parent alpha", "@hscc")
    subtask = run(
        env, "--sub", str(parent["id"]), "child beta", "--json"
    )
    assert subtask.returncode == 0, subtask.stderr
    child = json.loads(subtask.stdout)["item"]
    assert child["parent"] == parent["id"]
    assert child["boards"] == ["hscc"]

    override = run(
        env, "--sub", str(parent["id"]), "override gamma", "@other", "--json"
    )
    assert override.returncode == 0, override.stderr
    assert json.loads(override.stdout)["item"]["boards"] == ["other"]

    listed = json.loads(run(env, "-l", "--json").stdout)["items"]
    by_id = {item["id"]: item for item in listed}
    assert by_id[child["id"]]["parent"] == parent["id"]
    assert by_id[child["id"]]["boards"] == ["hscc"]

    board_view = run(env)
    assert board_view.returncode == 0, board_view.stderr
    board_lines = board_view.stdout.splitlines()
    parent_line = next(line for line in board_lines if "parent alpha" in line)
    child_line = next(line for line in board_lines if "child beta" in line)
    assert board_lines.index(parent_line) < board_lines.index(child_line)
    assert parent_line.startswith("    1.")
    assert child_line.startswith("      2.")
    assert "My Board" not in board_view.stdout

    list_view = run(env, "-l")
    assert list_view.returncode == 0, list_view.stderr
    list_lines = list_view.stdout.splitlines()
    parent_line = next(line for line in list_lines if "parent alpha" in line)
    child_line = next(line for line in list_lines if "child beta" in line)
    assert list_lines.index(parent_line) < list_lines.index(child_line)
    assert parent_line.startswith("  1.")
    assert child_line.startswith("    2.")
    assert "[hscc]" in parent_line and "[hscc]" in child_line


def test_date_views_skip_done_tasks_unless_done_requested(env):
    done = add_task_json(env, "finished tomorrow", "due:tomorrow")
    add_task_json(env, "open tomorrow", "due:tomorrow")
    checked = run(env, "-c", str(done["id"]))
    assert checked.returncode == 0, checked.stderr

    def descriptions(*filters):
        payload = json.loads(run(env, "-l", *filters, "--json").stdout)
        return {item["description"] for item in payload["items"]}

    assert descriptions("upcoming") == {"open tomorrow"}
    assert descriptions("scheduled") == {"open tomorrow"}
    assert descriptions("upcoming", "done") == {"finished tomorrow"}
    assert descriptions("scheduled", "done") == {"finished tomorrow"}


def test_repeating_check_creates_next_occurrence(env):
    add_task(env, "daily standup", "due:today", "every:day")
    result = run(env, "-c", "1", "--json")
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)["items"]
    assert len(items) == 2
    done, occurrence = items
    assert done["status"] == "done"
    assert done["completed_at"] is not None
    assert occurrence["status"] == "pending"
    assert occurrence["repeat"] == "daily"
    assert occurrence["due"] != done["due"]
    listed = json.loads(run(env, "-l", "--json").stdout)["items"]
    assert {item["id"] for item in listed} == {done["id"], occurrence["id"]}


def test_new_list_filters(env):
    add_task(env, "today task", "due:today", "#now")
    add_task(env, "tomorrow task", "due:tomorrow")
    add_task(env, "yesterday task", "due:yesterday")
    add_task(env, "unscheduled")

    def descriptions(*filters):
        payload = json.loads(run(env, "-l", *filters, "--json").stdout)
        return [item["description"] for item in payload["items"]]

    assert descriptions("today") == ["today task"]
    assert descriptions("overdue") == ["yesterday task"]
    assert descriptions("upcoming") == ["tomorrow task"]
    assert sorted(descriptions("scheduled")) == [
        "today task",
        "tomorrow task",
        "yesterday task",
    ]
    assert descriptions("#now") == ["today task"]


def test_cli_help_lists_reminder_flags(env):
    result = run(env, "--help")
    assert result.returncode == 0
    for flag in ("--due", "--notes", "--url", "--sub", "--force"):
        assert flag in result.stdout
