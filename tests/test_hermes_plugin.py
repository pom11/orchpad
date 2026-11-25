from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchpad import Pad
from orchpad.hermes_plugin import register
from orchpad.hermes_plugin import tools as plugin_tools
from orchpad.hermes_plugin.cli import main as hermes_main
from orchpad.hermes_plugin.install import (
    install_hermes_plugin,
    plugin_source,
    resolve_profile_home,
)


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def tools(tmp_path):
    return plugin_tools.OrchpadTools(tmp_path / "pad")


@pytest.fixture()
def handlers(tools):
    return plugin_tools.build_handlers(tools)


class StubRunner:
    def __init__(self, create=None, status=None, fail=False):
        self.calls = []
        self.create = create or {"id": "t_100", "status": "ready"}
        self.status = status
        self.fail = fail

    def __call__(self, argv, stdin=""):
        self.calls.append((list(argv), stdin))
        if self.fail:
            raise plugin_tools.KanbanError("kanban unavailable")
        if "create" in argv:
            return json.dumps(self.create)
        return json.dumps({"id": "t_100", "status": self.status})


def test_pad_path_follows_profile_home(hermes_home):
    assert plugin_tools.default_profile_home() == hermes_home
    assert plugin_tools.default_pad_path() == hermes_home / "orchpad"
    plugin_tools.OrchpadTools().add(description="from env")
    assert Pad(hermes_home / "orchpad").list()[0].description == "from env"


def test_default_profile_home_without_env(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert plugin_tools.default_profile_home() == Path.home() / ".hermes"


def test_add_task_and_note(handlers):
    result = json.loads(
        handlers["orchpad_add"](
            {"kind": "task", "description": "follow up", "boards": ["ops"], "priority": 3}
        )
    )
    assert result["ok"] is True
    assert result["item"]["kind"] == "task"
    assert result["item"]["priority"] == 3
    assert result["item"]["boards"] == ["ops"]

    note = json.loads(
        handlers["orchpad_add"]({"kind": "note", "description": "an idea"})
    )
    assert note["ok"] is True
    assert note["item"]["kind"] == "note"
    assert note["item"]["status"] is None


def test_add_rejects_bad_input(handlers):
    assert json.loads(handlers["orchpad_add"]({}))["ok"] is False
    assert (
        json.loads(
            handlers["orchpad_add"](
                {"kind": "note", "description": "x", "priority": 2}
            )
        )["ok"]
        is False
    )
    assert (
        json.loads(handlers["orchpad_add"]({"kind": "bug", "description": "x"}))["ok"]
        is False
    )


def test_update_actions(tools):
    task = tools.add(description="work")["id"]
    assert tools.update(task, "begin")["status"] == "in_progress"
    assert tools.update(task, "star")["starred"] is True
    assert tools.update(task, "priority", 3)["priority"] == 3
    assert tools.update(task, "edit", "better text")["description"] == "better text"
    assert tools.update(task, "move", ["home", "work"])["boards"] == ["home", "work"]
    assert tools.update(task, "check")["status"] == "done"
    assert tools.update(task, "check")["status"] == "pending"
    deleted = tools.update(task, "delete")
    assert deleted["archived_at"] is not None
    assert tools.list()["items"] == []
    restored = tools.update(task, "restore")
    assert restored["archived_at"] is None
    assert [item["id"] for item in tools.list()["items"]] == [task]


def test_update_errors_are_json(handlers):
    for args in (
        {"id": 999, "action": "star"},
        {"id": 1, "action": "explode"},
        {"id": 1, "action": "priority", "value": "high"},
    ):
        payload = json.loads(handlers["orchpad_update"](args))
        assert payload["ok"] is False
        assert payload["error"]


def test_list_filters_stats_and_archive(tools):
    first = tools.add(description="pending task", boards=["work"])["id"]
    second = tools.add(description="done task", boards=["work"])["id"]
    tools.add(description="note", kind="note", boards=["ideas"])
    tools.update(second, "check")
    tools.update(first, "star")

    work = tools.list(filters=["task"], board="work")
    assert {item["id"] for item in work["items"]} == {first, second}
    assert work["stats"] == {
        "done": 1,
        "in_progress": 0,
        "pending": 1,
        "notes": 1,
        "percent_done": 50,
    }

    starred = tools.list(filters=["starred"])
    assert [item["id"] for item in starred["items"]] == [first]

    tools.update(first, "delete")
    assert {item["id"] for item in tools.list(filters=["task"])["items"]} == {second}
    archived = tools.list(filters=["task"], include_archived=True)
    assert {item["id"] for item in archived["items"]} == {first, second}
    assert archived["stats"]["pending"] == 0


def test_promote_creates_card_links_and_marks_in_progress(tools):
    item = tools.add(
        description="draft the release notes\nsecond line", boards=["work"], priority=2
    )
    runner = StubRunner(create={"id": "t_42", "status": "ready"})
    tools._runner = runner

    result = tools.promote(item["id"], "work", assignee="coder", title="Release notes")

    assert result["kanban"] == "work/t_42"
    assert result["item"]["links"] == {"kanban": "work/t_42"}
    assert result["item"]["status"] == "in_progress"
    argv, stdin = runner.calls[0]
    assert argv == [
        "hermes",
        "kanban",
        "--board",
        "work",
        "create",
        "Release notes",
        "--body-file",
        "-",
        "--json",
        "--assignee",
        "coder",
    ]
    assert "draft the release notes" in stdin
    assert "#" in stdin and "orchpad" in stdin.lower()


def test_promote_defaults_title_to_first_line(tools):
    item = tools.add(description="first line\nsecond line")
    runner = StubRunner()
    tools._runner = runner
    tools.promote(item["id"], "default")
    assert runner.calls[0][0][5] == "first line"


def test_promote_rejects_notes_and_failed_calls(tools):
    note = tools.add(description="just a note", kind="note")
    with pytest.raises(ValueError):
        tools.promote(note["id"], "work")

    task = tools.add(description="real work")
    runner = StubRunner(fail=True)
    tools._runner = runner
    with pytest.raises(plugin_tools.KanbanError):
        tools.promote(task["id"], "work")
    stored = next(
        item for item in Pad(tools.pad_path).list() if item.id == task["id"]
    )
    assert stored.links == {}
    assert stored.status == "pending"


def test_promote_bad_json_is_an_error(tools):
    item = tools.add(description="real work")
    tools._runner = lambda argv, stdin: "not json"
    with pytest.raises(plugin_tools.KanbanError):
        tools.promote(item["id"], "work")


def test_list_auto_checks_finished_kanban_cards(tools):
    runner = StubRunner(status="done")
    tools._runner = runner
    item = tools.add(description="linked work")
    tools.promote(item["id"], "work")
    # promote itself does not read the card back
    calls_after_promote = len(runner.calls)

    result = tools.list()
    assert result["items"][0]["status"] == "done"
    assert len(runner.calls) == calls_after_promote + 1
    show_argv, show_stdin = runner.calls[-1]
    assert show_argv == [
        "hermes",
        "kanban",
        "--board",
        "work",
        "show",
        "t_100",
        "--json",
    ]
    assert show_stdin == ""

    # Cached for five minutes: no second read.
    tools.list()
    assert len(runner.calls) == calls_after_promote + 1


def test_list_cache_expires_and_failures_leave_items_alone(tmp_path):
    clock = [0.0]
    tools = plugin_tools.OrchpadTools(tmp_path / "pad", clock=lambda: clock[0])
    runner = StubRunner(status="ready")
    tools._runner = runner
    item = tools.add(description="linked work")
    tools.promote(item["id"], "work")

    assert tools.list()["items"][0]["status"] == "in_progress"
    reads = len(runner.calls)
    clock[0] = plugin_tools.CACHE_TTL_SECONDS + 1
    tools.list()
    assert len(runner.calls) == reads + 1

    failing = plugin_tools.OrchpadTools(
        tmp_path / "other", runner=StubRunner(fail=True)
    )
    failed_item = failing.add(description="cannot read")
    failing.pad.link(failed_item["id"], "kanban", "work/t_9")
    assert failing.list()["items"][0]["status"] == "pending"

    missing = plugin_tools.OrchpadTools(
        tmp_path / "third", runner=lambda argv, stdin: "garbage"
    )
    missing_item = missing.add(description="unreadable")
    missing.pad.link(missing_item["id"], "kanban", "work/t_10")
    assert missing.list()["items"][0]["status"] == "pending"


def test_plugin_manifest_shape():
    manifest = (plugin_source() / "plugin.yaml").read_text()
    assert "name: orchpad" in manifest
    for name in ("orchpad_add", "orchpad_update", "orchpad_list", "orchpad_promote"):
        assert f"- {name}" in manifest


def test_register_exposes_four_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    class Context:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

    ctx = Context()
    register(ctx)
    assert set(ctx.tools) == {
        "orchpad_add",
        "orchpad_update",
        "orchpad_list",
        "orchpad_promote",
    }
    for tool in ctx.tools.values():
        assert tool["toolset"] == "orchpad"
        assert "kanban" in tool["description"].lower()
        assert callable(tool["handler"])

    added = json.loads(
        ctx.tools["orchpad_add"]["handler"]({"description": "via registered tool"})
    )
    assert added["ok"] is True
    assert Pad(tmp_path / "hermes" / "orchpad").list()[0].description == (
        "via registered tool"
    )


def test_resolve_profile_home(tmp_path):
    root = tmp_path / "hermes"
    assert resolve_profile_home(root) == root
    assert resolve_profile_home(root, "default") == root
    assert resolve_profile_home(root, "coder") == root / "profiles" / "coder"


def test_install_plugin_default_and_idempotent(tmp_path):
    root = tmp_path / "hermes"
    first = install_hermes_plugin(root)
    target = root / "plugins" / "orchpad"
    assert Path(first["path"]) == target
    assert first["profile"] == "default"
    for name in ("plugin.yaml", "__init__.py", "tools.py"):
        assert (target / name).read_bytes() == (plugin_source() / name).read_bytes()

    second = install_hermes_plugin(root)
    assert second["written"] == []
    for name in ("plugin.yaml", "__init__.py", "tools.py"):
        assert (target / name).read_bytes() == (plugin_source() / name).read_bytes()

    (target / "tools.py").write_text("stale")
    third = install_hermes_plugin(root)
    assert third["written"] == ["tools.py"]
    assert (target / "tools.py").read_bytes() == (plugin_source() / "tools.py").read_bytes()


def test_install_plugin_named_profile(tmp_path):
    root = tmp_path / "hermes"
    result = install_hermes_plugin(root, "coder")
    assert Path(result["path"]) == root / "profiles" / "coder" / "plugins" / "orchpad"
    assert (Path(result["path"]) / "plugin.yaml").is_file()


def test_cli_install_json(tmp_path, capsys):
    root = tmp_path / "hermes"
    assert hermes_main(["--hermes-home", str(root), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(root / "plugins" / "orchpad")
    assert set(payload["written"]) == {"plugin.yaml", "__init__.py", "tools.py"}


def test_cli_install_plain_and_profile(tmp_path, capsys):
    root = tmp_path / "hermes"
    assert (
        hermes_main(
            [
                "--hermes-home",
                str(root),
                "--profile",
                "coder",
            ]
        )
        == 0
    )
    assert "coder" in capsys.readouterr().out
    assert (root / "profiles" / "coder" / "plugins" / "orchpad").is_dir()
