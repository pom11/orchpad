"""Command-line interface for orchpad.

The flag set mirrors taskbook's CLI.  Reads can be rendered for humans or
emitted as JSON with ``--json``; writes accept the same flags and also
support ``--json`` for machine-friendly confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, TextIO

from . import __version__
from .model import DEFAULT_BOARD, Item
from .pad import Pad
from .store import Store, StoreError

_RESET = "\x1b[0m"
_BOLD_CYAN = "\x1b[1;36m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_BLUE = "\x1b[34m"
_DIM = "\x1b[2m"

_UNICODE_SYMBOLS = {
    "pending": "☐",
    "done": "✔",
    "in_progress": "…",
    "note": "#",
    "star": "★",
    "sep": "·",
}
_ASCII_SYMBOLS = {
    "pending": "[ ]",
    "done": "[x]",
    "in_progress": "[~]",
    "note": "#",
    "star": "*",
    "sep": "|",
}

_PRIORITY_TOKEN = re.compile(r"^p:([123])$")

_SHOW_CLEAR = "\x1b[2J\x1b[H"
_REDRAW_SECONDS = 30.0
_STATUS_RANK = {"in_progress": 0, "pending": 1, "done": 2, None: 3}


class UserError(Exception):
    """A problem with the command line or requested ids."""


class _Parser(argparse.ArgumentParser):
    """Argument parser that reports usage errors with exit code 1.

    argparse normally exits with 2, which this CLI reserves for store
    errors.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _use_unicode(stream: TextIO) -> bool:
    """Whether the stream's encoding can represent the Unicode symbols."""

    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "☐".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _use_color(stream: TextIO) -> bool:
    """ANSI colour is for interactive UTF-8-capable terminals only."""

    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _paint(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _format_item(
    item: Item,
    symbols: dict[str, str],
    color: bool,
    *,
    show_boards: bool = False,
) -> str:
    if item.kind == "note":
        symbol = symbols["note"]
        code = _DIM
    else:
        symbol = symbols[item.status or "pending"]
        code = {
            "done": _GREEN,
            "in_progress": _YELLOW,
            "pending": _RED if item.priority == 3 else _BLUE if item.priority == 2 else "",
        }.get(item.status or "pending", "")

    description = item.description.replace("\r", " ").replace("\n", " ")
    line = f"{item.id:>3}. {symbol} {description}"
    if item.kind == "task" and item.priority > 1:
        line += f" p{item.priority}"
    if item.starred:
        line += f" {symbols['star']}"
    if show_boards:
        line += f"  [{symbols['sep'].join(item.boards)}]"
    return _paint(line, code, color)


def _footer(stats: dict[str, int], symbols: dict[str, str]) -> str:
    sep = f" {symbols['sep']} "
    return (
        f"{stats['percent_done']}% of all tasks complete{sep}"
        f"{stats['done']} done{sep}"
        f"{stats['in_progress']} in progress{sep}"
        f"{stats['pending']} pending{sep}"
        f"{stats['notes']} notes"
    )


def _stats_payload(pad: Pad) -> dict[str, int]:
    stats = pad.stats()
    stats["tasks"] = stats["done"] + stats["in_progress"] + stats["pending"]
    stats["total"] = stats["tasks"] + stats["notes"]
    return stats


def _item_dicts(items: Iterable[Item]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]


def _write_json(out: TextIO, payload: Any) -> None:
    json.dump(payload, out, ensure_ascii=False, indent=2)
    out.write("\n")


def _render_boards(
    boards: dict[str, list[Item]],
    stats: dict[str, int],
    symbols: dict[str, str],
    color: bool,
    out: TextIO,
) -> None:
    if not boards:
        out.write("No items.\n\n")
    for index, (name, items) in enumerate(boards.items()):
        if index:
            out.write("\n")
        out.write(_paint(name, _BOLD_CYAN, color) + "\n")
        for item in items:
            out.write("  " + _format_item(item, symbols, color) + "\n")
    if boards:
        out.write("\n")
    out.write(_paint(_footer(stats, symbols), _DIM, color) + "\n")


def _render_flat(
    title: str,
    items: Sequence[Item],
    stats: dict[str, int],
    symbols: dict[str, str],
    color: bool,
    out: TextIO,
    *,
    empty: str,
) -> None:
    out.write(_paint(title, _BOLD_CYAN, color) + "\n")
    if items:
        for item in items:
            out.write("  " + _format_item(item, symbols, color, show_boards=True) + "\n")
    else:
        out.write(f"  {empty}\n")
    out.write("\n" + _paint(_footer(stats, symbols), _DIM, color) + "\n")


def _render_list(
    items: Sequence[Item],
    stats: dict[str, int],
    symbols: dict[str, str],
    color: bool,
    out: TextIO,
) -> None:
    if items:
        for item in items:
            out.write(_format_item(item, symbols, color, show_boards=True) + "\n")
    else:
        out.write("No items.\n")
    out.write("\n" + _paint(_footer(stats, symbols), _DIM, color) + "\n")


def _render_timeline(
    timeline: dict[str, list[Item]],
    stats: dict[str, int],
    symbols: dict[str, str],
    color: bool,
    out: TextIO,
) -> None:
    if not timeline:
        out.write("No items.\n")
    for index, (day, items) in enumerate(timeline.items()):
        if index:
            out.write("\n")
        out.write(_paint(day, _BOLD_CYAN, color) + "\n")
        for item in items:
            out.write("  " + _format_item(item, symbols, color, show_boards=True) + "\n")
    if timeline:
        out.write("\n")
    out.write(_paint(_footer(stats, symbols), _DIM, color) + "\n")


# -- show and watch ---------------------------------------------------------


@dataclass
class _PadView:
    """Everything read from one pad for the show view."""

    path: Path
    name: str
    boards: dict[str, list[Item]]
    stats: dict[str, int]


def _now() -> datetime:
    """Current time used to render item ages (patched in tests)."""

    return datetime.now(timezone.utc)


def _terminal_width() -> int:
    columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    return columns if columns > 0 else 100


def _format_age(created_at: str, now: datetime) -> str:
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:  # pragma: no cover - store validation prevents this
        return "?"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    seconds = int((now - created).total_seconds())
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _items_stats(items: Iterable[Item]) -> dict[str, int]:
    items = list(items)
    done = sum(1 for item in items if item.status == "done")
    in_progress = sum(1 for item in items if item.status == "in_progress")
    pending = sum(1 for item in items if item.status == "pending")
    notes = sum(1 for item in items if item.kind == "note")
    tasks = done + in_progress + pending
    return {
        "done": done,
        "in_progress": in_progress,
        "pending": pending,
        "notes": notes,
        "percent_done": round(done * 100 / tasks) if tasks else 0,
        "tasks": tasks,
        "total": tasks + notes,
    }


def _combine_stats(views: Sequence[_PadView]) -> dict[str, int]:
    combined = _items_stats(())
    for view in views:
        for key in ("done", "in_progress", "pending", "notes"):
            combined[key] += view.stats[key]
    tasks = combined["done"] + combined["in_progress"] + combined["pending"]
    combined["tasks"] = tasks
    combined["total"] = tasks + combined["notes"]
    combined["percent_done"] = (
        round(combined["done"] * 100 / tasks) if tasks else 0
    )
    return combined


def _discover_pads(root: Path) -> list[Path]:
    """Find every ``*/orchpad/`` pad directory below ``root``."""

    if not root.is_dir():
        return []
    try:
        candidates = list(root.rglob("orchpad"))
    except OSError:  # pragma: no cover - unreadable subtrees
        candidates = []
    pads = [candidate for candidate in candidates if candidate.is_dir()]
    return sorted(pads, key=lambda path: str(path))


def _resolve_pad_paths(
    pad_args: Sequence[str], all_under: Sequence[str]
) -> list[Path]:
    paths = [Path(value).expanduser() for value in pad_args]
    for value in all_under:
        paths.extend(_discover_pads(Path(value).expanduser()))
    if not pad_args and not all_under:
        paths = [Pad().path]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:  # pragma: no cover - broken symlinks
            key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _pad_name(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - broken symlinks
        resolved = path
    return resolved.parent.name or resolved.name or str(path)


def _show_sort_key(item: Item) -> tuple[int, int, int]:
    return (_STATUS_RANK[item.status], -item.priority, item.id)


def _read_pad(path: Path, include_archived: bool) -> _PadView:
    path = Path(path).expanduser()
    name = _pad_name(path)
    if not path.is_dir():
        # A show must never create a pad directory just to display it.
        return _PadView(path, name, {}, _items_stats(()))
    if not (
        (path / "storage.json").is_file()
        or (path / "archive.json").is_file()
    ):
        # An empty pad directory: nothing to lock and nothing to write.
        return _PadView(path, name, {}, _items_stats(()))

    store = Store(path)
    active_document, archive_document = store.snapshot()
    active = [
        Item.from_dict(raw) for raw in active_document["items"].values()
    ]
    items = list(active)
    if include_archived:
        items.extend(
            Item.from_dict(raw)
            for raw in archive_document["items"].values()
        )

    boards: dict[str, list[Item]] = {}
    for item in items:
        for board in item.boards:
            boards.setdefault(board, []).append(item)
    ordered = sorted(
        boards,
        key=lambda board: (board != DEFAULT_BOARD, board.casefold(), board),
    )
    for board in ordered:
        boards[board].sort(key=_show_sort_key)
    return _PadView(
        path,
        name,
        {board: boards[board] for board in ordered},
        _items_stats(active),
    )


def _item_color_code(item: Item) -> str:
    if item.kind == "note":
        return _DIM
    return {
        "done": _GREEN,
        "in_progress": _YELLOW,
        "pending": (
            _RED
            if item.priority == 3
            else _BLUE if item.priority == 2 else ""
        ),
    }.get(item.status or "pending", "")


def _format_show_item(
    item: Item,
    symbols: dict[str, str],
    color: bool,
    width: int,
    now: datetime,
) -> str:
    symbol = (
        symbols["note"]
        if item.kind == "note"
        else symbols[item.status or "pending"]
    )
    description = item.description.replace("\r", " ").replace("\n", " ")
    extras: list[str] = []
    if item.kind == "task" and item.priority > 1:
        extras.append(f"p{item.priority}")
    if item.starred:
        extras.append(symbols["star"])
    extras.append(_format_age(item.created_at, now))
    kanban = item.links.get("kanban")
    if kanban:
        extras.append(f"kanban:{kanban}")
    if item.archived_at:
        extras.append("[archived]")
    prefix = f"  {item.id:>3}. {symbol} "
    suffix = " " + " ".join(extras)
    available = width - len(prefix) - len(suffix)
    if available <= 0:
        description = ""
    elif len(description) > available:
        ellipsis = "…" if symbols is _UNICODE_SYMBOLS else "..."
        if available <= len(ellipsis):
            description = ellipsis[:available]
        else:
            description = (
                description[: available - len(ellipsis)].rstrip() + ellipsis
            )
    return _paint(
        prefix + description + suffix, _item_color_code(item), color
    )


def _render_show(
    views: Sequence[_PadView],
    symbols: dict[str, str],
    color: bool,
    width: int,
    out: TextIO,
    *,
    now: datetime | None = None,
) -> None:
    if not views:
        out.write("No pads found.\n")
        return
    moment = now or _now()
    multiple = len(views) > 1
    for index, view in enumerate(views):
        if multiple:
            if index:
                out.write("\n")
            out.write(_paint(f"== {view.name} ==", _BOLD_CYAN, color) + "\n")
        if not view.boards:
            out.write("Empty pad.\n")
            continue
        for board_index, (board, items) in enumerate(view.boards.items()):
            if board_index:
                out.write("\n")
            out.write(_paint(board, _BOLD_CYAN, color) + "\n")
            for item in items:
                out.write(
                    _format_show_item(item, symbols, color, width, moment)
                    + "\n"
                )
    out.write(
        "\n" + _paint(_footer(_combine_stats(views), symbols), _DIM, color) + "\n"
    )


def _show_payload(views: Sequence[_PadView]) -> dict[str, Any]:
    return {
        "pads": [
            {
                "name": view.name,
                "path": str(view.path),
                "boards": [
                    {"name": board, "items": _item_dicts(items)}
                    for board, items in view.boards.items()
                ],
                "stats": dict(view.stats),
            }
            for view in views
        ],
        "stats": _combine_stats(views),
    }


def _width_arg(value: str) -> int:
    try:
        width = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("width must be an integer") from exc
    if width < 20:
        raise argparse.ArgumentTypeError("width must be at least 20")
    return width


def _interval_arg(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if interval <= 0:
        raise argparse.ArgumentTypeError("interval must be positive")
    return interval


def _build_show_parser(prog: str) -> _Parser:
    parser = _Parser(
        prog=prog,
        description=(
            "Show every board and item in one or more pads, ready for watch."
        ),
        epilog=(
            "examples:\n"
            "  orchpad show\n"
            "  orchpad show --all-under DIR\n"
            "  watch -c orchpad show --color always\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "pads",
        nargs="*",
        metavar="PAD_DIR",
        help="pad directories (default: $ORCHPAD_HOME or ~/.orchpad)",
    )
    parser.add_argument(
        "--all-under",
        action="append",
        default=[],
        metavar="DIR",
        help="find every */orchpad/ pad below DIR",
    )
    parser.add_argument(
        "-a", "--archived", action="store_true", help="include archived items"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="ANSI colour: auto (TTY only), always or never",
    )
    parser.add_argument(
        "--width",
        type=_width_arg,
        default=None,
        metavar="N",
        help="line width (default: terminal width, or 100)",
    )
    return parser


def _build_watch_parser() -> _Parser:
    parser = _build_show_parser("orchpad watch")
    parser.description = (
        "Redraw 'orchpad show' until Ctrl+C, only when the pad files change "
        "or every 30 seconds."
    )
    parser.add_argument(
        "-n",
        "--interval",
        type=_interval_arg,
        default=2.0,
        metavar="SECONDS",
        help="poll interval in seconds (default: 2)",
    )
    return parser


def _show_options(
    args: argparse.Namespace, out: TextIO
) -> tuple[dict[str, str], bool, int]:
    symbols = _UNICODE_SYMBOLS if _use_unicode(out) else _ASCII_SYMBOLS
    if args.color == "always":
        color = True
    elif args.color == "never":
        color = False
    else:
        color = _use_color(out)
    width = args.width if args.width is not None else _terminal_width()
    return symbols, color, width


def _run_show(args: argparse.Namespace, out: TextIO) -> int:
    symbols, color, width = _show_options(args, out)
    paths = _resolve_pad_paths(args.pads, args.all_under)
    views = [_read_pad(path, args.archived) for path in paths]
    if args.json:
        _write_json(out, _show_payload(views))
    else:
        _render_show(views, symbols, color, width, out)
    return 0


def _store_mtimes(paths: Sequence[Path]) -> tuple[int, ...]:
    stamps: list[int] = []
    for path in paths:
        for name in ("storage.json", "archive.json"):
            try:
                stamps.append((path / name).stat().st_mtime_ns)
            except OSError:
                stamps.append(0)
    return tuple(stamps)


def _watch_loop(
    draw: Callable[[], None],
    mtimes: Callable[[], tuple[int, ...]],
    *,
    interval: float,
    out: TextIO,
    iterations: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    redraw_seconds: float = _REDRAW_SECONDS,
) -> int:
    """Poll and redraw until interrupted, a test cap, or Ctrl+C."""

    last_mtimes: tuple[int, ...] | None = None
    last_draw: float | None = None
    polls = 0
    try:
        while iterations is None or polls < iterations:
            current_time = clock()
            current_mtimes = mtimes()
            if (
                last_draw is None
                or current_mtimes != last_mtimes
                or current_time - last_draw >= redraw_seconds
            ):
                out.write(_SHOW_CLEAR)
                draw()
                out.flush()
                last_draw = current_time
                last_mtimes = current_mtimes
            polls += 1
            if iterations is not None and polls >= iterations:
                break
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    return 0


def _run_watch(args: argparse.Namespace, out: TextIO) -> int:
    symbols, color, width = _show_options(args, out)
    paths = _resolve_pad_paths(args.pads, args.all_under)

    def draw() -> None:
        views = [_read_pad(path, args.archived) for path in paths]
        if args.json:
            _write_json(out, _show_payload(views))
        else:
            _render_show(views, symbols, color, width, out)

    return _watch_loop(
        draw,
        lambda: _store_mtimes(paths),
        interval=args.interval,
        out=out,
    )


def _run_show_command(argv: Sequence[str], out: TextIO) -> int:
    parser = (
        _build_watch_parser()
        if argv[0] == "watch"
        else _build_show_parser("orchpad show")
    )
    args = parser.parse_args(argv[1:])
    if argv[0] == "watch":
        return _run_watch(args, out)
    return _run_show(args, out)


def _parse_input(
    values: Sequence[str], *, task: bool
) -> tuple[str, list[str] | None, int]:
    """Split ``@board`` and ``p:N`` tokens out of a new item's text."""

    boards: list[str] = []
    words: list[str] = []
    priority = 1
    for token in " ".join(values).split():
        if token.startswith("@") and len(token) > 1:
            board = token[1:].strip(",;:")
            if board:
                boards.append(board)
                continue
        if task:
            match = _PRIORITY_TOKEN.match(token)
            if match:
                priority = int(match.group(1))
                continue
        words.append(token)
    description = " ".join(words)
    if not description:
        raise UserError("description must not be empty")
    return description, boards or None, priority


def _parse_ids(values: Sequence[str]) -> list[int]:
    ids: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part.startswith("@"):
                part = part[1:]
            try:
                item_id = int(part)
            except ValueError as exc:
                raise UserError(f"invalid id {value!r}") from exc
            if item_id < 1:
                raise UserError(f"invalid id {value!r}")
            if item_id not in ids:
                ids.append(item_id)
    if not ids:
        raise UserError("at least one id is required")
    return ids


def _parse_boards(values: Sequence[str]) -> list[str]:
    boards: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part.startswith("@"):
                part = part[1:]
            if part:
                boards.append(part)
    if not boards:
        raise UserError("at least one board is required")
    return boards


def _parse_filters(values: Sequence[str]) -> list[str]:
    filters: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part.startswith("@"):
                part = part[1:]
            if part:
                filters.append(part)
    return filters


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="orchpad",
        description=(
            "A persistent task and notes pad for agent orchestrators. "
            "With no arguments, show the board view."
        ),
        epilog=(
            "examples:\n"
            '  orchpad -t "write release notes @work p:3"\n'
            '  orchpad -n "remember to tag the release @work"\n'
            "  orchpad -c 1        # toggle done\n"
            "  orchpad -b 1        # toggle in progress\n"
            "  orchpad -p 1 3      # set priority\n"
            "  orchpad -f release  # find\n"
            "  orchpad --stats --json\n"
            "  orchpad show        # everything, watch-friendly\n"
            "  orchpad watch       # redraw show until Ctrl+C\n"
            "\n"
            "exit codes: 0 ok, 1 user error, 2 store error\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("-a", "--archive", action="store_true", help="show archived items")
    parser.add_argument("-b", "--begin", nargs="+", metavar="ID", help="toggle in progress")
    parser.add_argument("-c", "--check", nargs="+", metavar="ID", help="toggle done")
    parser.add_argument("--clear", action="store_true", help="archive every done task")
    parser.add_argument("-d", "--delete", nargs="+", metavar="ID", help="archive items")
    parser.add_argument("-e", "--edit", nargs="+", metavar="ID TEXT", help="replace an item's description")
    parser.add_argument("-f", "--find", nargs="+", metavar="TEXT", help="find items by description")
    parser.add_argument("-l", "--list", nargs="*", metavar="FILTER", help="list active items by status, kind, star, archive or board")
    parser.add_argument("-m", "--move", nargs="+", metavar="ID BOARD", help="replace an item's boards")
    parser.add_argument("-n", "--note", nargs="+", metavar="DESCRIPTION", help="add a note")
    parser.add_argument("-p", "--priority", nargs=2, metavar=("ID", "N"), help="set priority 1, 2 or 3")
    parser.add_argument("-r", "--restore", nargs="+", metavar="ID", help="restore archived items")
    parser.add_argument("-s", "--star", nargs="+", metavar="ID", help="toggle the star")
    parser.add_argument("--pad-dir", metavar="DIR", help="use this pad directory (default: $ORCHPAD_HOME or ~/.orchpad)")
    parser.add_argument("--where", action="store_true", help="print the pad directory")
    parser.add_argument("-t", "--task", nargs="+", metavar="DESCRIPTION", help="add a task")
    parser.add_argument("-i", "--timeline", action="store_true", help="group active items by creation date")
    parser.add_argument("--stats", action="store_true", help="show task counts")
    parser.add_argument("--json", action="store_true", help="emit JSON for machine use")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _dispatch(args: argparse.Namespace, out: TextIO) -> int:
    pad = Pad(args.pad_dir) if args.pad_dir else Pad()
    symbols = _UNICODE_SYMBOLS if _use_unicode(out) else _ASCII_SYMBOLS
    color = _use_color(out)

    actions = {
        "archive": args.archive,
        "begin": args.begin is not None,
        "check": args.check is not None,
        "clear": args.clear,
        "delete": args.delete is not None,
        "edit": args.edit is not None,
        "find": args.find is not None,
        "list": args.list is not None,
        "move": args.move is not None,
        "note": args.note is not None,
        "where": args.where,
        "priority": args.priority is not None,
        "restore": args.restore is not None,
        "star": args.star is not None,
        "stats": args.stats,
        "task": args.task is not None,
        "timeline": args.timeline,
    }
    chosen = [name for name, enabled in actions.items() if enabled]
    if len(chosen) > 1:
        raise UserError(
            "choose one command at a time: " + ", ".join(sorted(chosen))
        )
    action = chosen[0] if chosen else "boards"

    if action == "boards":
        stats = _stats_payload(pad)
        if args.json:
            _write_json(
                out,
                {
                    "boards": [
                        {"name": name, "items": _item_dicts(items)}
                        for name, items in pad.boards().items()
                    ],
                    "stats": stats,
                },
            )
        else:
            _render_boards(pad.boards(), stats, symbols, color, out)
        return 0

    if action == "where":
        if args.json:
            _write_json(out, {"path": str(pad.path)})
        else:
            out.write(f"{pad.path}\n")
        return 0

    if action == "stats":
        stats = _stats_payload(pad)
        if args.json:
            _write_json(out, stats)
        else:
            out.write(
                f"Total: {stats['total']} items "
                f"({stats['tasks']} tasks, {stats['notes']} notes)\n"
            )
            out.write(_paint(_footer(stats, symbols), _DIM, color) + "\n")
        return 0

    if action == "timeline":
        timeline = pad.timeline()
        stats = _stats_payload(pad)
        if args.json:
            _write_json(
                out,
                {
                    "timeline": [
                        {"date": day, "items": _item_dicts(items)}
                        for day, items in timeline.items()
                    ],
                    "stats": stats,
                },
            )
        else:
            _render_timeline(timeline, stats, symbols, color, out)
        return 0

    if action == "archive":
        items = pad.list("archived")
        stats = _stats_payload(pad)
        if args.json:
            _write_json(out, {"items": _item_dicts(items), "stats": stats})
        else:
            _render_flat("Archive", items, stats, symbols, color, out, empty="No archived items.")
        return 0

    if action == "list":
        filters = _parse_filters(args.list or [])
        items = pad.list(*filters)
        stats = _stats_payload(pad)
        if args.json:
            _write_json(out, {"items": _item_dicts(items), "stats": stats})
        else:
            _render_list(items, stats, symbols, color, out)
        return 0

    if action == "find":
        query = " ".join(args.find or []).strip()
        if not query:
            raise UserError("search text must not be empty")
        items = pad.find(query)
        stats = _stats_payload(pad)
        if args.json:
            _write_json(
                out,
                {"query": query, "items": _item_dicts(items), "stats": stats},
            )
        else:
            _render_flat(
                f'Search results for "{query}"',
                items,
                stats,
                symbols,
                color,
                out,
                empty="No matches.",
            )
        return 0

    if action == "task":
        description, boards, priority = _parse_input(args.task, task=True)
        item = pad.add_task(description, boards=boards, priority=priority)
        if args.json:
            _write_json(out, {"item": item.to_dict()})
        else:
            out.write(f"Added task {item.id}: {item.description}\n")
        return 0

    if action == "note":
        description, boards, _ = _parse_input(args.note, task=False)
        item = pad.add_note(description, boards=boards)
        if args.json:
            _write_json(out, {"item": item.to_dict()})
        else:
            out.write(f"Added note {item.id}: {item.description}\n")
        return 0

    if action == "check":
        items = pad.check(*_parse_ids(args.check))
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Updated {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "begin":
        items = pad.begin(*_parse_ids(args.begin))
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Updated {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "star":
        items = pad.star(*_parse_ids(args.star))
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Updated {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "edit":
        ids = _parse_ids([args.edit[0]])
        description = " ".join(args.edit[1:]).strip()
        if not description:
            raise UserError("description must not be empty")
        items = [pad.edit(item_id, description) for item_id in ids]
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Edited {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "move":
        ids = _parse_ids([args.move[0]])
        boards = _parse_boards(args.move[1:])
        items = [pad.move(item_id, boards) for item_id in ids]
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Moved {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "priority":
        ids = _parse_ids([args.priority[0]])
        try:
            priority = int(args.priority[1])
        except ValueError as exc:
            raise UserError("priority must be 1, 2 or 3") from exc
        if priority not in (1, 2, 3):
            raise UserError("priority must be 1, 2 or 3")
        items = [pad.set_priority(item_id, priority) for item_id in ids]
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(
                f"Set priority {priority} on {len(items)} "
                f"item{'s' if len(items) != 1 else ''}.\n"
            )
        return 0

    if action == "delete":
        items = pad.delete(*_parse_ids(args.delete))
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Archived {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "restore":
        items = pad.restore(*_parse_ids(args.restore))
        if args.json:
            _write_json(out, {"items": _item_dicts(items)})
        else:
            out.write(f"Restored {len(items)} item{'s' if len(items) != 1 else ''}.\n")
        return 0

    if action == "clear":
        items = pad.clear()
        if args.json:
            _write_json(
                out,
                {"cleared": len(items), "items": _item_dicts(items)},
            )
        else:
            if items:
                out.write(
                    f"Cleared {len(items)} item{'s' if len(items) != 1 else ''}.\n"
                )
            else:
                out.write("Nothing to clear.\n")
        return 0

    raise UserError(f"unknown command {action!r}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] in ("show", "watch"):
            return _run_show_command(arguments, sys.stdout)
        parser = _build_parser()
        args = parser.parse_args(arguments)
        return _dispatch(args, sys.stdout)
    except SystemExit as exc:  # --help / --version / usage errors
        code = exc.code
        return code if isinstance(code, int) else 1
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        print(f"error: no item with id {exc.args[0]}", file=sys.stderr)
        return 1
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
