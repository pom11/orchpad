"""High-level, persistent pad API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

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

LIST_FILTERS = frozenset(STATUSES) | frozenset(KINDS) | {"starred", "archived"}


def _default_path() -> Path:
    configured = os.environ.get("ORCHPAD_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path(os.path.expanduser("~")) / ".orchpad"


def _sort_items(items: Iterable[Item]) -> list[Item]:
    return sorted(items, key=lambda item: (-item.priority, item.id))


class Pad:
    """A task and notes pad backed by a directory of JSON documents.

    ``path`` defaults to ``$ORCHPAD_HOME`` or ``~/.orchpad``.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else _default_path()
        self.store = Store(self.path)

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
    ) -> Item:
        description = validate_description(description)
        board_list = normalize_boards(boards)
        with self.store.transaction() as transaction:
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
    ) -> Item:
        """Add a pending task and return it."""

        return self._add("task", desc, boards, priority, "pending")

    def add_note(
        self, desc: str, boards: Sequence[str] | None = None
    ) -> Item:
        """Add a note and return it."""

        return self._add("note", desc, boards, 1, None)

    # -- task state -------------------------------------------------------

    def check(self, *ids: int) -> list[Item]:
        """Toggle ``done`` for each task id."""

        def change(item: Item) -> None:
            if item.kind != "task":
                raise ValueError(f"item {item.id} is a note, not a task")
            item.status = "pending" if item.status == "done" else "done"

        return self._update(ids, change)

    def begin(self, *ids: int) -> list[Item]:
        """Toggle ``in_progress`` for each task id."""

        def change(item: Item) -> None:
            if item.kind != "task":
                raise ValueError(f"item {item.id} is a note, not a task")
            item.status = (
                "pending" if item.status == "in_progress" else "in_progress"
            )

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
        instead of active ones; any other unknown value is treated as a
        board name.
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
        for attr in filters:
            if attr in STATUSES:
                items = [item for item in items if item.status == attr]
            elif attr in KINDS:
                items = [item for item in items if item.kind == attr]
            elif attr == "starred":
                items = [item for item in items if item.starred]
            elif attr == "archived":
                continue
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
