"""Atomic JSON storage for orchpad pads.

A pad directory holds two documents with the same schema::

    {"version": 1, "next_id": N, "items": {"1": {...}, ...}}

``storage.json`` contains active items and ``archive.json`` contains
deleted items.  Every read-modify-write runs under an ``fcntl.flock`` on
``<dir>/.lock`` and every write is staged in a temporary file in the same
directory before being atomically moved into place with :func:`os.replace`.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .model import Item

SCHEMA_VERSION = 1


class StoreError(Exception):
    """Raised when a pad file is corrupt or has an unsupported schema."""


def _empty_document() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "next_id": 1, "items": {}}


def _validate_document(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StoreError(f"{path}: expected a JSON object")
    if data.get("version") != SCHEMA_VERSION:
        raise StoreError(
            f"{path}: unsupported schema version {data.get('version')!r}"
        )
    next_id = data.get("next_id")
    if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
        raise StoreError(f"{path}: 'next_id' must be a positive integer")
    items = data.get("items")
    if not isinstance(items, dict):
        raise StoreError(f"{path}: 'items' must be an object")
    for key, raw in items.items():
        if not isinstance(key, str):
            raise StoreError(f"{path}: item keys must be strings")
        try:
            item = Item.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"{path}: invalid item {key!r}: {exc}") from exc
        if str(item.id) != key:
            raise StoreError(
                f"{path}: item key {key!r} does not match id {item.id}"
            )
    return data


class Transaction:
    """Mutable view of a pad, committed when the context manager exits.

    Documents are loaded lazily so an operation that only touches
    ``storage.json`` is not affected by a corrupt ``archive.json``.
    """

    def __init__(self, store: "Store") -> None:
        self._store = store
        self._active: dict[str, Any] | None = None
        self._archive: dict[str, Any] | None = None
        self._dirty: set[str] = set()
        self._loaded_ids: dict[str, set[str]] = {}

    @property
    def active(self) -> dict[str, Any]:
        if self._active is None:
            self._active = self._store.read_active()
            self._loaded_ids["active"] = set(self._active["items"])
        return self._active

    @property
    def archive(self) -> dict[str, Any]:
        if self._archive is None:
            self._archive = self._store.read_archive()
            self._loaded_ids["archive"] = set(self._archive["items"])
        return self._archive

    def gained(self, document: str) -> bool:
        """Whether ``document`` now holds an item id it did not hold on load."""
        data = self._active if document == "active" else self._archive
        if data is None:
            return False
        return bool(set(data["items"]) - self._loaded_ids.get(document, set()))

    def mark_active(self) -> None:
        self._dirty.add("active")

    def mark_archive(self) -> None:
        self._dirty.add("archive")

    @property
    def dirty(self) -> set[str]:
        return set(self._dirty)

    def touch(self, document: str) -> None:
        if document == "active":
            self.mark_active()
        elif document == "archive":
            self.mark_archive()
        else:  # pragma: no cover - internal misuse
            raise ValueError(f"unknown document {document!r}")


class Store:
    """A pad directory with atomic, lock-protected JSON documents."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.active_path = self.path / "storage.json"
        self.archive_path = self.path / "archive.json"
        self.lock_path = self.path / ".lock"

    @contextmanager
    def lock(self, *, exclusive: bool = True) -> Iterator[None]:
        """Hold the cross-process pad lock."""

        self.path.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                try:
                    data = json.load(handle)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise StoreError(f"{path}: corrupt JSON: {exc}") from exc
        except FileNotFoundError:
            return _empty_document()
        return _validate_document(data, path)

    def read_active(self) -> dict[str, Any]:
        """Read ``storage.json`` (the caller must hold the lock)."""

        return self._read(self.active_path)

    def read_archive(self) -> dict[str, Any]:
        """Read ``archive.json`` (the caller must hold the lock)."""

        return self._read(self.archive_path)

    def load_active(self) -> dict[str, Any]:
        """Read ``storage.json`` under a shared lock."""

        with self.lock(exclusive=False):
            return self.read_active()

    def load_archive(self) -> dict[str, Any]:
        """Read ``archive.json`` under a shared lock."""

        with self.lock(exclusive=False):
            return self.read_archive()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Lock the pad and yield a lazily loaded transaction."""

        with self.lock():
            transaction = Transaction(self)
            yield transaction
            # Write the document that GAINED items first: a crash between the
            # two writes then leaves a moved item in both documents
            # (recoverable) instead of in neither (lost). Delete moves
            # active -> archive, restore moves archive -> active.
            order = ["active", "archive"]
            if transaction.gained("archive"):
                order.reverse()
            for name in order:
                if name in transaction.dirty:
                    path = (self.active_path if name == "active"
                            else self.archive_path)
                    self._write(path, getattr(transaction, name))

    def snapshot(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a consistent ``(active, archive)`` snapshot."""

        with self.lock(exclusive=False):
            return self.read_active(), self.read_archive()

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        _validate_document(data, path)
        self.path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=self.path
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            self._fsync_dir()
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _fsync_dir(self) -> None:
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
