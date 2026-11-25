"""Agent tools for the per-profile orchpad Hermes plugin.

The pad is deliberately personal: it holds the orchestrator's own
follow-ups, ideas, observations and waiting-on items.  Real work that a
worker should pick up belongs on kanban, so :func:`OrchpadTools.promote`
creates a card and links it back to the pad item.

Every tool takes a small JSON object and returns a JSON string, which is
the shape Hermes expects from a tool handler.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from orchpad import Pad, dates
from orchpad.model import KINDS, PRIORITIES

CACHE_TTL_SECONDS = 300

FIELD_ACTIONS = ("due", "remind", "repeat", "notes", "url", "tags", "parent")

_ADDED_FIELD_SCHEMA: dict[str, Any] = {
    "due": {
        "type": "string",
        "description": (
            "ISO date/datetime, today, tomorrow, mon..sun, +3d, +2w or +1h."
        ),
    },
    "remind_at": {
        "type": "string",
        "description": "Reminder datetime or relative time such as +1h.",
    },
    "repeat": {
        "type": "string",
        "description": (
            "none, daily, weekdays, weekly, monthly, yearly, "
            "'every N days' or 'every N weeks'."
        ),
    },
    "notes": {"type": "string", "description": "Multi-line notes body."},
    "url": {"type": "string", "description": "Reference URL."},
    "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tags without the leading '#'.",
    },
    "parent": {
        "type": "integer",
        "description": "Parent task id for a subtask.",
    },
}

_RULE = (
    "The pad is for the orchestrator's own follow-ups, ideas, observations "
    "and waiting-on items. Real work for a worker goes to kanban via "
    "orchpad_promote."
)

ADD_DESCRIPTION = (
    "Add a task or note to this profile's private orchpad. " + _RULE
)
UPDATE_DESCRIPTION = (
    "Update one orchpad item: check/begin/star toggle it, or set priority, "
    "edit its text, move boards, delete to the archive, or restore. " + _RULE
)
LIST_DESCRIPTION = (
    "List orchpad items and stats, optionally filtered by status, kind, "
    "starred, board or archived. Items linked to a finished kanban card are "
    "checked automatically (best effort). " + _RULE
)
PROMOTE_DESCRIPTION = (
    "Promote an orchpad item to a kanban card for a worker. Creates the card "
    "with the item text plus a back-reference, then records the card in "
    "links['kanban'] and marks the item in progress."
)

ADD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS), "default": "task"},
        "description": {"type": "string", "description": "Item text."},
        "boards": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Board names; defaults to the pad's default board.",
        },
        "priority": {
            "type": "integer",
            "enum": list(PRIORITIES),
            "description": "Task priority: 1 normal, 2 medium, 3 high.",
        },
        **_ADDED_FIELD_SCHEMA,
    },
    "required": ["description"],
    "additionalProperties": False,
}

UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "Orchpad item id."},
        "action": {
            "type": "string",
            "enum": [
                "check",
                "begin",
                "star",
                "priority",
                "edit",
                "move",
                "delete",
                "restore",
                *FIELD_ACTIONS,
            ],
        },
        "value": {
            "description": (
                "Action value: priority (1-3), edit text, a board-name list "
                "for move, or the new value for due/remind/repeat/notes/url/"
                "tags/parent. Not used by check/begin/star/delete/restore."
            )
        },
        **_ADDED_FIELD_SCHEMA,
        "force": {
            "type": "boolean",
            "default": False,
            "description": "Check a parent task even when subtasks are open.",
        },
    },
    "required": ["id", "action"],
    "additionalProperties": False,
}

LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Status, kind, 'starred', 'archived', 'today', 'overdue', "
                "'upcoming', 'scheduled', '#tag' or board-name filters."
            ),
        },
        "board": {"type": "string", "description": "Board-name shorthand."},
        "include_archived": {
            "type": "boolean",
            "default": False,
            "description": "Append archived items to the result.",
        },
    },
    "additionalProperties": False,
}

PROMOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "Orchpad item id."},
        "board": {"type": "string", "description": "Kanban board slug."},
        "assignee": {
            "type": "string",
            "description": "Hermes profile to assign the card to.",
        },
        "title": {
            "type": "string",
            "description": "Card title; defaults to the item's first line.",
        },
    },
    "required": ["id", "board"],
    "additionalProperties": False,
}

TOOL_SPECS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("orchpad_add", ADD_SCHEMA, ADD_DESCRIPTION),
    ("orchpad_update", UPDATE_SCHEMA, UPDATE_DESCRIPTION),
    ("orchpad_list", LIST_SCHEMA, LIST_DESCRIPTION),
    ("orchpad_promote", PROMOTE_SCHEMA, PROMOTE_DESCRIPTION),
)

Runner = Callable[[Sequence[str], str], str]


class KanbanError(RuntimeError):
    """A ``hermes kanban`` call failed or returned something unreadable."""


def default_profile_home(env: Mapping[str, str] | None = None) -> Path:
    """Return the active Hermes profile home.

    Hermes exports ``HERMES_HOME`` for the active profile; when it is unset
    the process is on the default profile at ``~/.hermes``.
    """

    values = os.environ if env is None else env
    configured = values.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes"


def default_pad_path(env: Mapping[str, str] | None = None) -> Path:
    """Per-profile pad directory: ``<profile home>/orchpad``."""

    return default_profile_home(env) / "orchpad"


def run_hermes(argv: Sequence[str], stdin: str = "") -> str:
    """Run a ``hermes`` command and return its stdout.

    Raises :class:`KanbanError` on a non-zero exit or a missing executable.
    """

    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise KanbanError("hermes executable not found on PATH") from exc
    except OSError as exc:
        raise KanbanError(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise KanbanError("hermes command timed out") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise KanbanError(
            f"hermes exited {completed.returncode}: {detail or 'no output'}"
        )
    return completed.stdout


def _card_id(payload: Any) -> str:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, Mapping):
        for key in ("id", "task_id", "card_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        task = payload.get("task")
        if isinstance(task, Mapping):
            return _card_id(task)
    raise KanbanError("hermes kanban output has no card id")


def _card_status(payload: Any) -> str | None:
    if isinstance(payload, Mapping):
        value = payload.get("status")
        if isinstance(value, str) and value.strip():
            return value.strip()
        task = payload.get("task")
        if isinstance(task, Mapping):
            return _card_status(task)
    return None


class OrchpadTools:
    """Implementation of the four orchpad agent tools.

    ``pad_path`` and ``runner`` are injectable so tests (and other callers)
    never need the real ``~/.hermes`` or a real ``hermes`` executable.
    """

    def __init__(
        self,
        pad_path: str | os.PathLike[str] | None = None,
        *,
        runner: Runner | None = None,
        cache_ttl: float = CACHE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.pad_path = (
            Path(pad_path).expanduser() if pad_path is not None else default_pad_path()
        )
        self._runner = runner or run_hermes
        self._cache_ttl = cache_ttl
        self._clock = clock or time.monotonic
        self._status_cache: dict[str, tuple[float, str | None]] = {}

    @property
    def pad(self) -> Pad:
        return Pad(self.pad_path)

    # -- tools ------------------------------------------------------------

    def add(
        self,
        kind: str = "task",
        description: str = "",
        boards: Sequence[str] | None = None,
        priority: int | None = None,
        *,
        due: str | None = None,
        remind_at: str | None = None,
        repeat: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        tags: Sequence[str] | None = None,
        parent: int | None = None,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        fields = self._prepare_fields(
            {
                "due": due,
                "remind_at": remind_at,
                "repeat": repeat,
                "notes": notes,
                "url": url,
                "tags": tags,
                "parent": parent,
            }
        )
        if kind == "note":
            if priority not in (None, 1):
                raise ValueError("notes cannot have a priority")
            item = self.pad.add_note(description, boards=boards, **fields)
        else:
            value = 1 if priority is None else priority
            item = self.pad.add_task(
                description, boards=boards, priority=value, **fields
            )
        return item.to_dict()

    @staticmethod
    def _prepare_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
        prepared: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None:
                if key in (
                    "due",
                    "remind_at",
                    "repeat",
                    "notes",
                    "url",
                    "tags",
                    "parent",
                ):
                    prepared[key] = None
                continue
            if key == "due":
                prepared[key] = dates.parse_due(value)
            elif key == "remind_at":
                prepared[key] = dates.parse_remind_at(value)
            elif key == "repeat":
                prepared[key] = dates.parse_repeat_token(value)
            elif key == "tags":
                prepared[key] = list(value)
            else:
                prepared[key] = value
        return prepared

    def update(
        self,
        item_id: int,
        action: str,
        value: Any = None,
        *,
        fields: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        pad = self.pad
        if action == "check":
            item = pad.check(item_id, force=force)[0]
        elif action == "begin":
            item = pad.begin(item_id)[0]
        elif action == "star":
            item = pad.star(item_id)[0]
        elif action == "priority":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("priority value must be 1, 2 or 3")
            item = pad.set_priority(item_id, value)
        elif action == "edit":
            if not isinstance(value, str):
                raise ValueError("edit value must be a string")
            item = pad.edit(item_id, value)
        elif action == "move":
            if not isinstance(value, (list, tuple)):
                raise ValueError("move value must be a list of board names")
            item = pad.move(item_id, value)
        elif action == "delete":
            item = pad.delete(item_id)[0]
        elif action == "restore":
            item = pad.restore(item_id)[0]
        elif action in FIELD_ACTIONS:
            key = "remind_at" if action == "remind" else action
            prepared = self._prepare_fields({key: value})
            item = pad.edit_fields(item_id, **prepared)
        else:
            raise ValueError(f"unknown action {action!r}")
        if fields:
            item = pad.edit_fields(item_id, **self._prepare_fields(fields))
        return item.to_dict()

    def list(
        self,
        filters: Iterable[str] | None = None,
        board: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        if isinstance(filters, str):
            filters = [filters]
        attrs = list(filters or [])
        if board:
            attrs.append(board)
        active = self.pad.list(*attrs)
        self._sync_linked(active)
        # Re-read: auto-checking writes through a fresh Pad, so the items
        # loaded above are stale.
        items = self.pad.list(*attrs)
        archived: list[Any] = []
        if include_archived:
            archived_attrs = list(filters or [])
            if board:
                archived_attrs.append(board)
            archived_attrs.append("archived")
            archived = self.pad.list(*archived_attrs)
        seen: set[int] = set()
        combined = []
        for item in [*items, *archived]:
            if item.id not in seen:
                seen.add(item.id)
                combined.append(item)
        return {
            "items": [item.to_dict() for item in combined],
            "stats": self.pad.stats(),
        }

    def promote(
        self,
        item_id: int,
        board: str,
        assignee: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(board, str) or not board.strip():
            raise ValueError("board must be a non-empty string")
        board = board.strip()
        pad = self.pad
        item = self._find_active(pad, item_id)
        if item.kind != "task":
            raise ValueError(
                f"item {item_id} is a note; only tasks can be promoted to kanban"
            )

        card_title = (title or self._title_from(item.description)).strip()
        if not card_title:
            raise ValueError("title must be a non-empty string")
        body = (
            f"{item.description}\n\n"
            f"---\n"
            f"Promoted from orchpad item #{item.id} "
            f"(boards: {', '.join(item.boards)})."
        )
        argv = [
            "hermes",
            "kanban",
            "--board",
            board,
            "create",
            card_title,
            "--body-file",
            "-",
            "--json",
        ]
        if assignee:
            argv.extend(["--assignee", assignee])
        output = self._runner(argv, body)
        try:
            payload = json.loads(output) if output.strip() else None
        except json.JSONDecodeError as exc:
            raise KanbanError(f"hermes kanban returned invalid JSON: {exc}") from exc
        card = _card_id(payload)

        item = pad.link(item.id, "kanban", f"{board}/{card}")
        if item.status != "in_progress":
            item = pad.begin(item.id)[0]
        return {"item": item.to_dict(), "kanban": f"{board}/{card}"}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _title_from(description: str) -> str:
        for line in description.splitlines():
            if line.strip():
                return line.strip()[:120]
        return description.strip()[:120]

    @staticmethod
    def _find_active(pad: Pad, item_id: int) -> Any:
        for item in pad.list():
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def _sync_linked(self, items: Iterable[Any]) -> None:
        """Check off tasks whose linked kanban card is done or archived."""

        for item in items:
            if item.kind != "task" or item.status == "done":
                continue
            link = item.links.get("kanban")
            if not link or "/" not in link:
                continue
            board, _, card = link.partition("/")
            if not board or not card:
                continue
            status = self._kanban_status(board, card)
            if status in ("done", "archived"):
                try:
                    self.pad.check(item.id)
                except (KeyError, ValueError):
                    # The item moved between the read and the write; the
                    # next list will see the fresh state.
                    continue

    def _kanban_status(self, board: str, card: str) -> str | None:
        key = f"{board}/{card}"
        now = self._clock()
        cached = self._status_cache.get(key)
        if cached is not None and now - cached[0] < self._cache_ttl:
            return cached[1]
        try:
            output = self._runner(
                ["hermes", "kanban", "--board", board, "show", card, "--json"],
                "",
            )
            payload = json.loads(output) if output.strip() else None
            status = _card_status(payload)
        except (KanbanError, OSError, ValueError, json.JSONDecodeError):
            # Best effort: a failed read leaves the item alone.
            return None
        self._status_cache[key] = (now, status)
        return status


def build_handlers(tools: OrchpadTools) -> dict[str, Callable[..., str]]:
    """Wrap the tool methods as Hermes handlers returning JSON strings."""

    def _json_result(call: Callable[[], dict[str, Any]]) -> str:
        try:
            return json.dumps({"ok": True, **call()}, ensure_ascii=False)
        except (KeyError, TypeError, ValueError, KanbanError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def add(args: Mapping[str, Any], **_: Any) -> str:
        return _json_result(
            lambda: {
                "item": tools.add(
                    kind=args.get("kind", "task"),
                    description=args.get("description", ""),
                    boards=args.get("boards"),
                    priority=args.get("priority"),
                    due=args.get("due"),
                    remind_at=args.get("remind_at"),
                    repeat=args.get("repeat"),
                    notes=args.get("notes"),
                    url=args.get("url"),
                    tags=args.get("tags"),
                    parent=args.get("parent"),
                )
            }
        )

    def update(args: Mapping[str, Any], **_: Any) -> str:
        fields = {
            key: args[key]
            for key in (
                "due",
                "remind_at",
                "repeat",
                "notes",
                "url",
                "tags",
                "parent",
            )
            if key in args
        }
        return _json_result(
            lambda: {
                "item": tools.update(
                    args["id"],
                    args["action"],
                    args.get("value"),
                    fields=fields,
                    force=bool(args.get("force", False)),
                )
            }
        )

    def list_items(args: Mapping[str, Any], **_: Any) -> str:
        return _json_result(
            lambda: tools.list(
                args.get("filters"),
                args.get("board"),
                bool(args.get("include_archived", False)),
            )
        )

    def promote(args: Mapping[str, Any], **_: Any) -> str:
        return _json_result(
            lambda: tools.promote(
                args["id"],
                args["board"],
                args.get("assignee"),
                args.get("title"),
            )
        )

    return {
        "orchpad_add": add,
        "orchpad_update": update,
        "orchpad_list": list_items,
        "orchpad_promote": promote,
    }
