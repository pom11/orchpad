"""High-level, persistent pad API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import dates
from .model import (
    DEFAULT_BOARD,
    KINDS,
    PRIORITIES,
    STATUSES,
    Item,
    normalize_boards,
    utcnow,
    validate_description,
)
from .store import Store, StoreError, Transaction

LIST_FILTERS = frozenset(STATUSES) | frozenset(KINDS) | {
    "starred",
    "archived",
    "today",
    "overdue",
    "upcoming",
    "scheduled",
}

ITEM_FIELDS = frozenset(
    {
        "description",
        "due",
        "remind_at",
        "repeat",
        "notes",
        "url",
        "tags",
        "parent",
    }
)


def _default_path() -> Path:
    configured = os.environ.get("ORCHPAD_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(os.path.expanduser("~")) / ".orchpad"


def _sort_items(items: Iterable[Item]) -> list[Item]:
    return sorted(items, key=lambda item: (-item.priority, item.id))


def order_subtasks(items: Iterable[Item]) -> list[tuple[Item, int]]:
    """Order items with each subtask directly under its parent.

    The depth starts at zero for top-level items.  An item whose parent is
    not in the given group is treated as top-level there.
    """

    ordered = list(items)
    by_id = {item.id: item for item in ordered}
    children: dict[int, list[Item]] = {}
    roots: list[Item] = []
    for item in ordered:
        if (
            item.parent is not None
            and item.parent in by_id
            and item.parent != item.id
        ):
            children.setdefault(item.parent, []).append(item)
        else:
            roots.append(item)

    result: list[tuple[Item, int]] = []

    def walk(item: Item, depth: int, seen: set[int]) -> None:
        if item.id in seen:
            return
        seen.add(item.id)
        result.append((item, depth))
        for child in children.get(item.id, []):
            walk(child, depth + 1, seen)

    seen: set[int] = set()
    for root in roots:
        walk(root, 0, seen)
    for item in ordered:  # cycle safety: never drop an item from a view
        if item.id not in seen:
            walk(item, 0, seen)
    return result


class Pad:
    """A task and notes pad backed by a directory of JSON documents.

    ``path`` defaults to ``$ORCHPAD_HOME`` or ``~/.orchpad``.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else _default_path()
        self.store = Store(self.path)
        self._clock = clock or dates.now

    def _now(self) -> Any:
        return dates.coerce_now(self._clock())

    # -- internal helpers -------------------------------------------------

    def _load_items(self, document: dict[str, Any], path: Path) -> dict[int, Item]:
        items: dict[int, Item] = {}
        for key, raw in document["items"].items():
            try:
                item = Item.from_dict(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise StoreError(
                    f"{path}: invalid item {key!r}: {exc}"
                ) from exc
            items[item.id] = item
        return items

    def _load_active(self, transaction: Transaction) -> dict[int, Item]:
        return self._load_items(transaction.active, self.store.active_path)

    def _load_archive(self, transaction: Transaction) -> dict[int, Item]:
        return self._load_items(transaction.archive, self.store.archive_path)

    def _active_items(self) -> list[Item]:
        active = self.store.load_active()
        return list(self._load_items(active, self.store.active_path).values())

    def _archived_items(self) -> list[Item]:
        archive = self.store.load_archive()
        return list(self._load_items(archive, self.store.archive_path).values())

    @staticmethod
    def _resolve(items: dict[int, Item], ids: Sequence[int]) -> list[Item]:
        wanted = list(dict.fromkeys(ids))
        missing = [item_id for item_id in wanted if item_id not in items]
        if missing:
            raise KeyError(missing[0])
        return [items[item_id] for item_id in wanted]

    def _update(
        self, ids: Sequence[int], change: Callable[[Item], None]
    ) -> list[Item]:
        with self.store.transaction() as transaction:
            items = self._load_active(transaction)
            affected = self._resolve(items, ids)
            stamp = utcnow()
            for item in affected:
                change(item)
                item.updated_at = stamp
                transaction.active["items"][str(item.id)] = item.to_dict()
            transaction.mark_active()
        return affected

    @staticmethod
    def _archive_all(
        transaction: Transaction,
        items: Sequence[Item],
        stamp: str,
    ) -> None:
        for item in items:
            item.archived_at = stamp
            item.updated_at = stamp
            transaction.archive["items"][str(item.id)] = item.to_dict()
            transaction.archive["next_id"] = max(
                transaction.archive["next_id"], item.id + 1
            )
            del transaction.active["items"][str(item.id)]
        transaction.mark_active()
        transaction.mark_archive()

    # -- creation ---------------------------------------------------------

    def _add(
        self,
        kind: str,
        description: str,
        boards: Sequence[str] | None,
        priority: int,
        status: str | None,
        *,
        due: str | None = None,
        remind_at: str | None = None,
        repeat: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        tags: Sequence[str] | None = None,
        parent: int | None = None,
    ) -> Item:
        description = validate_description(description)
        board_list = normalize_boards(boards)
        with self.store.transaction() as transaction:
            items = self._load_active(transaction)
            if parent is not None:
                if parent not in items:
                    raise KeyError(parent)
                if items[parent].kind != "task":
                    raise ValueError(f"item {parent} is a note, not a task")
            item_id = transaction.active["next_id"]
            stamp = utcnow()
            item = Item(
                id=item_id,
                kind=kind,
                description=description,
                boards=board_list,
                priority=priority,
                status=status,
                created_at=stamp,
                updated_at=stamp,
                due=due,
                remind_at=remind_at,
                repeat=repeat,
                notes=notes,
                url=url,
                tags=list(tags or []),
                parent=parent,
            )
            transaction.active["next_id"] = item_id + 1
            transaction.active["items"][str(item_id)] = item.to_dict()
            transaction.mark_active()
        return item

    def add_task(
        self,
        desc: str,
        boards: Sequence[str] | None = None,
        priority: int = 1,
        *,
        due: str | None = None,
        remind_at: str | None = None,
        repeat: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        tags: Sequence[str] | None = None,
        parent: int | None = None,
    ) -> Item:
        """Add a pending task and return it."""

        return self._add(
            "task",
            desc,
            boards,
            priority,
            "pending",
            due=due,
            remind_at=remind_at,
            repeat=repeat,
            notes=notes,
            url=url,
            tags=tags,
            parent=parent,
        )

    def add_note(
        self,
        desc: str,
        boards: Sequence[str] | None = None,
        *,
        due: str | None = None,
        remind_at: str | None = None,
        repeat: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        tags: Sequence[str] | None = None,
        parent: int | None = None,
    ) -> Item:
        """Add a note and return it."""

        return self._add(
            "note",
            desc,
            boards,
            1,
            None,
            due=due,
            remind_at=remind_at,
            repeat=repeat,
            notes=notes,
            url=url,
            tags=tags,
            parent=parent,
        )

    # -- task state -------------------------------------------------------

    def check(self, *ids: int, force: bool = False) -> list[Item]:
        """Toggle ``done`` for each task id.

        A parent with open subtasks needs ``force=True``.  Checking a
        repeating task keeps the done item and appends a fresh pending
        occurrence with its due and reminder advanced.
        """

        with self.store.transaction() as transaction:
            items = self._load_active(transaction)
            affected = self._resolve(items, ids)
            stamp = utcnow()
            moment = self._now()
            for item in affected:
                if item.kind != "task":
                    raise ValueError(f"item {item.id} is a note, not a task")
                if item.status == "done":
                    continue
                open_subtasks = [
                    child
                    for child in items.values()
                    if child.parent == item.id and child.status != "done"
                ]
                if open_subtasks and not force:
                    raise ValueError(
                        f"item {item.id} has open subtasks "
                        f"({', '.join(str(child.id) for child in open_subtasks)}); "
                        "use --force to check it anyway"
                    )

            results: list[Item] = []
            occurrences: list[Item] = []
            for item in affected:
                if item.status == "done":
                    item.status = "pending"
                    item.completed_at = None
                else:
                    item.status = "done"
                    item.completed_at = stamp
                    if item.repeat:
                        occurrences.append(
                            self._next_occurrence(transaction, item, stamp, moment)
                        )
                item.updated_at = stamp
                transaction.active["items"][str(item.id)] = item.to_dict()
                results.append(item)
            transaction.mark_active()
        return [*results, *occurrences]

    def _next_occurrence(
        self,
        transaction: Transaction,
        item: Item,
        stamp: str,
        moment: Any,
    ) -> Item:
        """Create the next pending copy of a repeating task."""

        item_id = transaction.active["next_id"]
        occurrence = Item(
            id=item_id,
            kind="task",
            description=item.description,
            boards=list(item.boards),
            priority=item.priority,
            starred=False,
            status="pending",
            created_at=stamp,
            updated_at=stamp,
            links=dict(item.links),
            due=dates.advance(item.due, item.repeat, now_=moment),
            remind_at=dates.advance(item.remind_at, item.repeat, now_=moment),
            repeat=item.repeat,
            notes=item.notes,
            url=item.url,
            tags=list(item.tags),
            parent=item.parent,
        )
        transaction.active["next_id"] = item_id + 1
        transaction.active["items"][str(item_id)] = occurrence.to_dict()
        return occurrence

    def begin(self, *ids: int) -> list[Item]:
        """Toggle ``in_progress`` for each task id."""

        def change(item: Item) -> None:
            if item.kind != "task":
                raise ValueError(f"item {item.id} is a note, not a task")
            item.status = (
                "pending" if item.status == "in_progress" else "in_progress"
            )
            if item.status != "done":
                item.completed_at = None

        return self._update(ids, change)

    def star(self, *ids: int) -> list[Item]:
        """Toggle the star on each id."""

        def change(item: Item) -> None:
            item.starred = not item.starred

        return self._update(ids, change)

    # -- editing ----------------------------------------------------------

    def set_priority(self, id: int, priority: int) -> Item:
        """Set a task's priority (1 normal, 2 medium, 3 high)."""

        if isinstance(priority, bool) or priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")

        def change(item: Item) -> None:
            if item.kind != "task":
                raise ValueError(f"item {item.id} is a note, not a task")
            item.priority = priority

        return self._update([id], change)[0]

    def edit(self, id: int, desc: str) -> Item:
        """Replace an item's description."""

        description = validate_description(desc)

        def change(item: Item) -> None:
            item.description = description

        return self._update([id], change)[0]

    def move(self, id: int, boards: Sequence[str]) -> Item:
        """Replace an item's board list."""

        board_list = normalize_boards(boards)

        def change(item: Item) -> None:
            item.boards = list(board_list)

        return self._update([id], change)[0]

    def link(self, id: int, key: str, value: str) -> Item:
        """Attach a free-form link (for example ``kanban``) to an item."""

        if not isinstance(key, str) or not key.strip():
            raise ValueError("link key must be a non-empty string")
        if not isinstance(value, str):
            raise ValueError("link value must be a string")

        def change(item: Item) -> None:
            item.links[key] = value

        return self._update([id], change)[0]

    def edit_fields(self, id: int, **fields: Any) -> Item:
        """Set reminders-style fields on an item; ``None`` clears a field."""

        unknown = set(fields) - ITEM_FIELDS
        if unknown:
            raise ValueError(
                "unknown fields: " + ", ".join(sorted(unknown))
            )

        with self.store.transaction() as transaction:
            items = self._load_active(transaction)
            item = self._resolve(items, [id])[0]
            if "description" in fields:
                fields["description"] = validate_description(fields["description"])
            parent = fields.get("parent")
            if parent is not None:
                if parent not in items:
                    raise KeyError(parent)
                if parent == id:
                    raise ValueError("an item cannot be its own parent")
                if items[parent].kind != "task":
                    raise ValueError(f"item {parent} is a note, not a task")
            rebuilt = item.replace(**fields)
            for key, value in rebuilt.to_dict().items():
                setattr(item, key, value)
            item.updated_at = utcnow()
            transaction.active["items"][str(item.id)] = item.to_dict()
            transaction.mark_active()
        return item

    def set_due(self, id: int, due: str | None) -> Item:
        return self.edit_fields(id, due=due)

    def set_remind_at(self, id: int, remind_at: str | None) -> Item:
        return self.edit_fields(id, remind_at=remind_at)

    def set_repeat(self, id: int, repeat: str | None) -> Item:
        return self.edit_fields(id, repeat=repeat)

    def set_notes(self, id: int, notes: str | None) -> Item:
        return self.edit_fields(id, notes=notes)

    def set_url(self, id: int, url: str | None) -> Item:
        return self.edit_fields(id, url=url)

    def set_tags(self, id: int, tags: Sequence[str]) -> Item:
        return self.edit_fields(id, tags=list(tags))

    def set_parent(self, id: int, parent: int | None) -> Item:
        return self.edit_fields(id, parent=parent)

    # -- archive ----------------------------------------------------------

    def delete(self, *ids: int) -> list[Item]:
        """Move items to the archive."""

        with self.store.transaction() as transaction:
            items = self._resolve(self._load_active(transaction), ids)
            self._archive_all(transaction, items, utcnow())
        return items

    def restore(self, *ids: int) -> list[Item]:
        """Move archived items back onto the pad."""

        with self.store.transaction() as transaction:
            items = self._resolve(self._load_archive(transaction), ids)
            stamp = utcnow()
            for item in items:
                item.archived_at = None
                item.updated_at = stamp
                transaction.active["items"][str(item.id)] = item.to_dict()
                transaction.active["next_id"] = max(
                    transaction.active["next_id"], item.id + 1
                )
                del transaction.archive["items"][str(item.id)]
            transaction.mark_active()
            transaction.mark_archive()
        return items

    def clear(self) -> list[Item]:
        """Archive every done task and return the archived items."""

        with self.store.transaction() as transaction:
            items = sorted(
                (
                    item
                    for item in self._load_active(transaction).values()
                    if item.kind == "task" and item.status == "done"
                ),
                key=lambda item: item.id,
            )
            if items:
                self._archive_all(transaction, items, utcnow())
        return items

    # -- reading ----------------------------------------------------------

    def find(self, text: str) -> list[Item]:
        """Find active items whose description contains ``text``."""

        if not isinstance(text, str):
            raise ValueError("search text must be a string")
        needle = text.casefold()
        return _sort_items(
            item
            for item in self._active_items()
            if needle in item.description.casefold()
        )

    def list(self, *attrs: str) -> list[Item]:
        """List items filtered by status, kind, star, archive or board.

        All given filters must match.  ``archived`` selects archived items
        instead of active ones; ``today``, ``overdue``, ``upcoming`` and
        ``scheduled`` filter by due date; ``#tag`` filters by tag; any other
        unknown value is treated as a board name.
        """

        filters: list[str] = []
        for attr in attrs:
            if not isinstance(attr, str) or not attr.strip():
                raise ValueError("list filters must be non-empty strings")
            filters.append(attr.strip())

        items = (
            self._archived_items()
            if "archived" in filters
            else self._active_items()
        )
        moment = self._now()
        for attr in filters:
            if attr in STATUSES:
                items = [item for item in items if item.status == attr]
            elif attr in KINDS:
                items = [item for item in items if item.kind == attr]
            elif attr == "starred":
                items = [item for item in items if item.starred]
            elif attr == "archived":
                continue
            elif attr == "scheduled":
                items = [item for item in items if item.due is not None]
            elif attr == "today":
                items = [
                    item
                    for item in items
                    if dates.due_date(item.due, now_=moment) == moment.date()
                ]
            elif attr == "overdue":
                items = [
                    item
                    for item in items
                    if dates.due_state(item.due, now_=moment) == "overdue"
                ]
            elif attr == "upcoming":
                items = [
                    item
                    for item in items
                    if dates.due_state(item.due, now_=moment) == "upcoming"
                ]
            elif attr.startswith("#") and len(attr) > 1:
                tag = attr[1:]
                items = [item for item in items if tag in item.tags]
            else:
                items = [item for item in items if attr in item.boards]
        return _sort_items(items)

    def boards(self) -> dict[str, list[Item]]:
        """Group active items by board, default board first."""

        groups: dict[str, list[Item]] = {}
        for item in _sort_items(self._active_items()):
            for board in item.boards:
                groups.setdefault(board, []).append(item)
        order = sorted(
            groups, key=lambda name: (name != DEFAULT_BOARD, name.casefold())
        )
        return {name: groups[name] for name in order}

    def timeline(self) -> dict[str, list[Item]]:
        """Group active items by creation date, newest date first."""

        groups: dict[str, list[Item]] = {}
        for item in self._active_items():
            groups.setdefault(item.created_at[:10], []).append(item)
        return {
            day: sorted(groups[day], key=lambda item: item.id)
            for day in sorted(groups, reverse=True)
        }

    def stats(self) -> dict[str, int]:
        """Return counts of active items and the integer percent done."""

        items = self._active_items()
        done = sum(1 for item in items if item.status == "done")
        in_progress = sum(
            1 for item in items if item.status == "in_progress"
        )
        pending = sum(1 for item in items if item.status == "pending")
        notes = sum(1 for item in items if item.kind == "note")
        tasks = done + in_progress + pending
        percent_done = round(done * 100 / tasks) if tasks else 0
        return {
            "done": done,
            "in_progress": in_progress,
            "pending": pending,
            "notes": notes,
            "percent_done": percent_done,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Pad({str(self.path)!r})"
