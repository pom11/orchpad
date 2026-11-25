from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchpad import cli

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _item(
    item_id: int,
    description: str,
    *,
    kind: str = "task",
    status: str | None = "pending",
    priority: int = 1,
    boards: list[str] | None = None,
    starred: bool = False,
    created: str = "2026-11-25T11:00:00+00:00",
    links: dict[str, str] | None = None,
    archived_at: str | None = None,
) -> dict:
    if kind == "note":
        status = None
        priority = 1
    return {
        "id": item_id,
        "kind": kind,
        "description": description,
        "boards": list(boards or ["My Board"]),
        "priority": priority,
        "starred": starred,
        "status": status,
        "created_at": created,
        "updated_at": created,
        "links": dict(links or {}),
        "archived_at": archived_at,
    }


def _document(items: list[dict]) -> dict:
    return {
        "version": 1,
        "next_id": max((item["id"] for item in items), default=0) + 1,
        "items": {str(item["id"]): item for item in items},
    }


def _write_pad(
    path: Path,
    active: list[dict] | None = None,
    archived: list[dict] | None = None,
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "storage.json").write_text(
        json.dumps(_document(active or [])), encoding="utf-8"
    )
    if archived is not None:
        (path / "archive.json").write_text(
            json.dumps(_document(archived)), encoding="utf-8"
        )
    return path


def _fixed_pad(tmp_path: Path) -> Path:
    pad = tmp_path / "alice" / "orchpad"
    return _write_pad(
        pad,
        [
            _item(
                1,
                "fix the flaky test",
                status="in_progress",
                created="2026-11-25T10:00:00+00:00",
            ),
            _item(
                2,
                "write release notes",
                created="2026-11-24T12:00:00+00:00",
            ),
            _item(
                3,
                "archive old logs",
                status="done",
                created="2026-11-25T11:55:00+00:00",
            ),
            _item(
                4,
                "remember the milk",
                kind="note",
                created="2026-11-20T12:00:00+00:00",
            ),
            _item(
                5,
                "ship the release",
                priority=2,
                starred=True,
                boards=["work"],
                created="2026-11-25T09:30:00+00:00",
                links={"kanban": "board/t_9"},
            ),
        ],
    )


EXPECTED_FIXED = """\
My Board
    1. … fix the flaky test 2h
    2. ☐ write release notes 1d
    3. ✔ archive old logs 5m
    4. # remember the milk 5d

work
    5. ☐ ship the release p2 ★ 2h kanban:board/t_9

25% of all tasks complete · 1 done · 1 in progress · 2 pending · 1 notes
"""


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: NOW)


def _show(capsys, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    if env:
        for key, value in env.items():
            os.environ[key] = value
    try:
        code = cli.main(["show", *args])
    finally:
        if env:
            for key in env:
                os.environ.pop(key, None)
    return code, capsys.readouterr().out


def _run(*args: str, env: dict[str, str] | None = None):
    child = os.environ.copy()
    child.pop("NO_COLOR", None)
    child["PYTHONIOENCODING"] = "utf-8"
    child["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + child["PYTHONPATH"] if child.get("PYTHONPATH") else ""
    )
    child.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "orchpad", *args],
        cwd=ROOT,
        env=child,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_show_snapshot_honours_no_color(tmp_path, capsys):
    pad = _fixed_pad(tmp_path)
    code, out = _show(capsys, str(pad), env={"NO_COLOR": "1"})
    assert code == 0
    assert out == EXPECTED_FIXED
    assert "\x1b[" not in out


def test_show_color_always_keeps_ansi_without_tty(tmp_path, capsys):
    pad = _fixed_pad(tmp_path)
    code, out = _show(
        capsys,
        str(pad),
        "--color",
        "always",
        env={"NO_COLOR": "1"},
    )
    assert code == 0
    assert "\x1b[" in out
    assert ANSI.sub("", out) == EXPECTED_FIXED


def test_show_empty_pad_exits_zero_without_creating_it(tmp_path, capsys):
    pad = tmp_path / "nobody" / "orchpad"
    code, out = _show(capsys, str(pad), "--color", "never")
    assert code == 0
    assert "Empty pad." in out
    assert "0% of all tasks complete" in out
    assert not pad.exists()


def test_show_empty_default_pad(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ORCHPAD_HOME", str(tmp_path / "home-pad"))
    code, out = _show(capsys, "--color", "never")
    assert code == 0
    assert "Empty pad." in out
    assert not (tmp_path / "home-pad").exists()


def test_show_never_writes_pad_files(tmp_path, capsys):
    pad = _fixed_pad(tmp_path)
    before = (pad / "storage.json").read_bytes()
    unicode_before = (pad / "storage.json").stat().st_mtime_ns
    code, _ = _show(capsys, str(pad), str(pad))
    assert code == 0
    assert (pad / "storage.json").read_bytes() == before
    assert (pad / "storage.json").stat().st_mtime_ns == unicode_before
    assert not (pad / "archive.json").exists()


def test_show_archived_flag_includes_archived_items(tmp_path, capsys):
    pad = _write_pad(
        tmp_path / "alice" / "orchpad",
        [_item(1, "active task")],
        [
            _item(
                9,
                "old archived task",
                archived_at="2026-11-22T00:00:00+00:00",
                created="2026-11-22T00:00:00+00:00",
            )
        ],
    )
    code, plain = _show(capsys, str(pad), "--color", "never")
    assert code == 0
    assert "old archived task" not in plain
    assert "1 pending" in plain

    code, archived = _show(
        capsys, str(pad), "--archived", "--color", "never"
    )
    assert code == 0
    assert "old archived task" in archived
    assert "[archived]" in archived
    # Archived items are displayed but not counted in the footer.
    assert "1 pending" in archived
    assert "2 pending" not in archived


def test_show_multiple_pads_uses_parent_names_and_combined_footer(tmp_path, capsys):
    first = _write_pad(
        tmp_path / "alice" / "orchpad", [_item(1, "alice task")]
    )
    second = _write_pad(
        tmp_path / "bob" / "orchpad",
        [_item(1, "bob pending"), _item(2, "bob done", status="done")],
    )
    code, out = _show(
        capsys, str(first), str(second), "--color", "never"
    )
    assert code == 0
    assert "== alice ==" in out
    assert "== bob ==" in out
    assert out.index("== alice ==") < out.index("== bob ==")
    assert "33% of all tasks complete · 1 done · 0 in progress · 2 pending" in out


def test_all_under_discovers_pads_and_ignores_non_pads(tmp_path, capsys):
    profiles = tmp_path / "profiles"
    _write_pad(profiles / "alice" / "orchpad", [_item(1, "alice task")])
    _write_pad(
        profiles / "bob" / "orchpad",
        [_item(1, "bob pending"), _item(2, "bob done", status="done")],
    )
    (profiles / "zoe" / "notes").mkdir(parents=True)
    (profiles / "dana" / "orchpad").mkdir(parents=True)
    _write_pad(
        profiles / "team" / "carol" / "orchpad", [_item(1, "nested task")]
    )

    code, out = _show(
        capsys, "--all-under", str(profiles), "--color", "never"
    )
    assert code == 0
    for name in ("alice", "bob", "carol"):
        assert f"== {name} ==" in out
    assert "zoe" not in out
    for text in ("alice task", "bob pending", "bob done", "nested task"):
        assert text in out
    # An empty pad directory is still a pad: one section, "Empty pad.".
    assert "== dana ==" in out
    assert "Empty pad." in out
    assert "25% of all tasks complete · 1 done · 0 in progress · 3 pending" in out


def test_all_under_with_no_pads_reports_none(tmp_path, capsys):
    empty = tmp_path / "profiles"
    empty.mkdir()
    code, out = _show(capsys, "--all-under", str(empty))
    assert code == 0
    assert "No pads found." in out


def test_show_json_shape(tmp_path, capsys):
    pad = _write_pad(
        tmp_path / "alice" / "orchpad",
        [
            _item(1, "first", status="in_progress"),
            _item(2, "second", status="done"),
            _item(3, "a note", kind="note"),
        ],
    )
    code, out = _show(capsys, str(pad), "--json")
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {"pads", "stats"}
    assert payload["stats"]["done"] == 1
    assert payload["stats"]["in_progress"] == 1
    assert payload["stats"]["notes"] == 1
    assert payload["stats"]["percent_done"] == 50
    assert payload["stats"]["tasks"] == 2
    assert payload["stats"]["total"] == 3

    (view,) = payload["pads"]
    assert view["name"] == "alice"
    assert view["path"] == str(pad)
    assert view["stats"]["total"] == 3
    (board,) = view["boards"]
    assert board["name"] == "My Board"
    assert [item["description"] for item in board["items"]] == [
        "first",
        "second",
        "a note",
    ]
    assert board["items"][0]["status"] == "in_progress"
    assert board["items"][1]["archived_at"] is None


def test_show_json_includes_archived_items(tmp_path, capsys):
    pad = _write_pad(
        tmp_path / "alice" / "orchpad",
        [_item(1, "active")],
        [_item(7, "archived", archived_at="2026-11-24T00:00:00+00:00")],
    )
    code, out = _show(capsys, str(pad), "--archived", "--json")
    assert code == 0
    payload = json.loads(out)
    boards = payload["pads"][0]["boards"]
    items = [item for board in boards for item in board["items"]]
    assert [item["id"] for item in items] == [1, 7]
    assert items[1]["archived_at"] is not None
    assert payload["stats"]["total"] == 1


def test_show_width_truncates_descriptions_with_ellipsis(tmp_path, capsys):
    long_text = "release " + "x" * 200
    pad = _write_pad(
        tmp_path / "alice" / "orchpad", [_item(1, long_text)]
    )
    code, out = _show(capsys, str(pad), "--width", "50", "--color", "never")
    assert code == 0
    item_lines = [line for line in out.splitlines() if line.strip().startswith("1.")]
    assert len(item_lines) == 1
    line = item_lines[0]
    assert len(line) <= 50
    assert "…" in line
    assert long_text not in out
    assert line.startswith("    1. ☐ release x")


def test_show_width_validation(tmp_path, capsys):
    pad = _write_pad(tmp_path / "alice" / "orchpad", [_item(1, "task")])
    for value in ("10", "nope"):
        code = cli.main(["show", str(pad), "--width", value])
        assert code == 1
        assert "width" in capsys.readouterr().err


def test_terminal_width_falls_back_to_100(monkeypatch):
    def no_terminal(fallback=(80, 24)):
        return os.terminal_size(fallback)

    monkeypatch.setattr(cli.shutil, "get_terminal_size", no_terminal)
    assert cli._terminal_width() == 100


def test_show_help_lists_watch_flags(capsys):
    code = cli.main(["show", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    for flag in ("--all-under", "--archived", "--json", "--color", "--width"):
        assert flag in out


def test_watch_help_lists_interval(capsys):
    code = cli.main(["watch", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "--interval" in out and "-n" in out
    assert "--all-under" in out


class _FakeClock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += self.step


def test_watch_redraws_only_on_mtime_change_or_every_30s():
    draws = 0

    def draw() -> None:
        nonlocal draws
        draws += 1

    clock = _FakeClock(step=2)
    out = io.StringIO()
    code = cli._watch_loop(
        draw,
        lambda: (1, 2),
        interval=2,
        out=out,
        iterations=5,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0
    assert draws == 1
    assert out.getvalue().count(cli._SHOW_CLEAR) == 1

    draws = 0
    clock = _FakeClock(step=15)
    code = cli._watch_loop(
        draw,
        lambda: (1, 2),
        interval=2,
        out=io.StringIO(),
        iterations=3,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == 0
    assert draws == 2


def test_watch_redraws_when_store_mtime_changes():
    state = {"mtime": 1}
    draws = 0

    def draw() -> None:
        nonlocal draws
        draws += 1

    clock = _FakeClock(step=1)

    def sleep(seconds: float) -> None:
        state["mtime"] = 2
        clock.sleep(seconds)

    code = cli._watch_loop(
        draw,
        lambda: (state["mtime"],),
        interval=1,
        out=io.StringIO(),
        iterations=2,
        clock=clock,
        sleep=sleep,
    )
    assert code == 0
    assert draws == 2


def test_watch_exits_zero_on_keyboard_interrupt():
    draws = 0

    def draw() -> None:
        nonlocal draws
        draws += 1

    def sleep(seconds: float) -> None:
        raise KeyboardInterrupt

    code = cli._watch_loop(
        draw,
        lambda: (1,),
        interval=2,
        out=io.StringIO(),
        clock=_FakeClock(step=0),
        sleep=sleep,
    )
    assert code == 0
    assert draws == 1


def test_watch_uses_show_rendering(tmp_path, capsys):
    pad = _write_pad(
        tmp_path / "alice" / "orchpad", [_item(1, "watched task")]
    )
    calls = {"count": 0}

    def fake_loop(draw, mtimes, *, interval, out, **kwargs):
        draw()
        calls["count"] += 1
        calls["mtime_count"] = len(mtimes())
        calls["interval"] = interval
        return 0

    original = cli._watch_loop
    cli._watch_loop = fake_loop
    try:
        code = cli.main(
            ["watch", str(pad), "-n", "0.5", "--color", "never"]
        )
    finally:
        cli._watch_loop = original
    assert code == 0
    out = capsys.readouterr().out
    assert "watched task" in out
    assert calls == {"count": 1, "mtime_count": 2, "interval": 0.5}


def test_show_subprocess_smoke(tmp_path):
    pad = _write_pad(
        tmp_path / "alice" / "orchpad", [_item(1, "smoke task")]
    )
    result = _run(
        "show", str(pad), "--color", "never", "--width", "60"
    )
    assert result.returncode == 0, result.stderr
    assert "smoke task" in result.stdout
    assert "of all tasks complete" in result.stdout
    assert "\x1b[" not in result.stdout
