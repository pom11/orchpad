"""Data model for orchpad items."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping

from .dates import normalize_repeat

MAX_DESCRIPTION = 4000
MAX_NOTES = 20000

KINDS = ("task", "note")
STATUSES = ("pending", "in_progress", "done")
PRIORITIES = (1, 2, 3)
DEFAULT_BOARD = "My Board"


def utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_description(description: Any) -> str:
    """Validate and return an item description.

    Descriptions must be non-empty strings of at most
    :data:`MAX_DESCRIPTION` characters.
    """

    if not isinstance(description, str):
        raise ValueError("description must be a string")
    if not description.strip():
        raise ValueError("description must not be empty")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(
            f"description must be at most {MAX_DESCRIPTION} characters"
        )
    return description


def normalize_boards(boards: Any) -> list[str]:
    """Validate a board list, returning a de-duplicated copy.

    ``None`` selects the default board.  Board names must be non-empty
    strings.
    """

    if boards is None:
        return [DEFAULT_BOARD]
    if isinstance(boards, str) or not isinstance(boards, (list, tuple)):
        raise ValueError("boards must be a list of board names")
    result: list[str] = []
    for board in boards:
        if not isinstance(board, str) or not board.strip():
            raise ValueError("board names must be non-empty strings")
        if board not in result:
            result.append(board)
    if not result:
        raise ValueError("at least one board is required")
    return result


def _validate_timestamp(value: Any, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc


def validate_due(value: Any) -> str | None:
    """Validate an optional ISO date or offset datetime."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("due must be an ISO date or datetime")
    text = value.strip()
    try:
        if len(text) == 10:
            date.fromisoformat(text)
        else:
            datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("due must be an ISO date or datetime") from exc
    return text


def validate_remind_at(value: Any) -> str | None:
    """Validate an optional reminder datetime."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("remind_at must be an ISO-8601 datetime")
    try:
        datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("remind_at must be an ISO-8601 datetime") from exc
    return value.strip()


def validate_repeat(value: Any) -> str | None:
    return normalize_repeat(value)


def validate_notes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("notes must be a string")
    if not value.strip():
        return None
    if len(value) > MAX_NOTES:
        raise ValueError(f"notes must be at most {MAX_NOTES} characters")
    return value


def validate_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    text = value.strip()
    return text or None


def normalize_tags(tags: Any) -> list[str]:
    """Validate a tag list, dropping ``#`` prefixes and duplicates."""

    if tags is None:
        return []
    if isinstance(tags, str) or not isinstance(tags, (list, tuple)):
        raise ValueError("tags must be a list of strings")
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError("tags must be strings")
        value = tag.strip()
        if value.startswith("#"):
            value = value[1:]
        if not value:
            raise ValueError("tags must be non-empty strings")
        if any(character.isspace() for character in value):
            raise ValueError("tags must not contain whitespace")
        if value not in result:
            result.append(value)
    return result


def validate_parent(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("parent must be a task id")
    if value < 1:
        raise ValueError("parent must be a positive task id")
    return value


@dataclass
class Item:
    """One task or note on a pad."""

    id: int
    kind: str
    description: str
    boards: list[str] = field(default_factory=lambda: [DEFAULT_BOARD])
    priority: int = 1
    starred: bool = False
    status: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    links: dict[str, str] = field(default_factory=dict)
    archived_at: str | None = None
    due: str | None = None
    remind_at: str | None = None
    repeat: str | None = None
    notes: str | None = None
    url: str | None = None
    tags: list[str] = field(default_factory=list)
    parent: int | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int):
            raise ValueError("id must be an integer")
        if self.id < 1:
            raise ValueError("id must be positive")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        self.description = validate_description(self.description)
        self.boards = normalize_boards(self.boards)
        if isinstance(self.priority, bool) or self.priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if self.kind == "note" and self.priority != 1:
            raise ValueError("notes cannot have a priority")
        if not isinstance(self.starred, bool):
            raise ValueError("starred must be a boolean")
        if self.kind == "note":
            if self.status is not None:
                raise ValueError("notes cannot have a status")
        else:
            if self.status is None:
                self.status = "pending"
            elif self.status not in STATUSES:
                raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        if not isinstance(self.links, Mapping):
            raise ValueError("links must be a mapping")
        links: dict[str, str] = {}
        for key, value in self.links.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("link keys must be non-empty strings")
            if not isinstance(value, str):
                raise ValueError("link values must be strings")
            links[key] = value
        self.links = links
        _validate_timestamp(self.archived_at, "archived_at", optional=True)
        self.due = validate_due(self.due)
        self.remind_at = validate_remind_at(self.remind_at)
        self.repeat = validate_repeat(self.repeat)
        self.notes = validate_notes(self.notes)
        self.url = validate_url(self.url)
        self.tags = normalize_tags(self.tags)
        self.parent = validate_parent(self.parent)
        if self.parent == self.id:
            raise ValueError("an item cannot be its own parent")
        _validate_timestamp(self.completed_at, "completed_at", optional=True)

    def replace(self, **changes: Any) -> "Item":
        """Return a validated copy with ``changes`` applied."""

        data = self.to_dict()
        data.update(changes)
        return Item.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the item."""

        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "boards": list(self.boards),
            "priority": self.priority,
            "starred": self.starred,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "links": dict(self.links),
            "archived_at": self.archived_at,
            "due": self.due,
            "remind_at": self.remind_at,
            "repeat": self.repeat,
            "notes": self.notes,
            "url": self.url,
            "tags": list(self.tags),
            "parent": self.parent,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Item":
        """Build an :class:`Item` from a stored mapping."""

        if not isinstance(data, Mapping):
            raise ValueError("item must be a mapping")
        return cls(
            id=data["id"],
            kind=data["kind"],
            description=data["description"],
            boards=list(data.get("boards", [DEFAULT_BOARD])),
            priority=data.get("priority", 1),
            starred=data.get("starred", False),
            status=data.get("status"),
            created_at=data.get("created_at", utcnow()),
            updated_at=data.get("updated_at", utcnow()),
            links=dict(data.get("links") or {}),
            archived_at=data.get("archived_at"),
            due=data.get("due"),
            remind_at=data.get("remind_at"),
            repeat=data.get("repeat"),
            notes=data.get("notes"),
            url=data.get("url"),
            tags=data.get("tags"),
            parent=data.get("parent"),
            completed_at=data.get("completed_at"),
        )
