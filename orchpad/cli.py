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
import sys
from typing import Any, Iterable, Sequence, TextIO

from . import __version__
from .model import Item
from .pad import Pad
from .store import StoreError

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

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help / --version / usage errors
        code = exc.code
        return code if isinstance(code, int) else 1
    try:
        return _dispatch(args, sys.stdout)
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
