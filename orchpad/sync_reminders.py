"""Opt-in, one-way-or-both-ways sync with Apple Reminders on macOS.

The integration is deliberately conservative:

* only lists named in ``--map BOARD=LIST`` (or in ``<pad>/sync.json``) are
  ever read or written; every other list is invisible to this module,
* reminder lists are never created or deleted, only reminders inside a
  mapped list,
* every run is a dry run unless ``--apply`` is given,
* deletions only propagate for pairs recorded by an earlier sync, so the
  first sync can never delete anything.

The actual AppleScript bridge lives in :class:`JxaRemindersBackend` behind
the tiny :class:`RemindersBackend` protocol, which lets the tests use an
in-memory fake and never touch ``osascript``.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .model import Item, utcnow
from .pad import Pad

STATE_FILENAME = "sync-reminders.json"
MAPS_FILENAME = "sync.json"
STATE_VERSION = 1
DIRECTIONS = ("both", "to-reminders", "from-reminders")
DENY_ENV = "ORCHPAD_SYNC_DENY"

#: orchpad priority (1 normal, 2 medium, 3 high) to Reminders priority
#: (0 none, 5 medium, 1 high).
PRIORITY_TO_REMINDERS = {1: 0, 2: 5, 3: 1}
#: Reminders priority back to orchpad's three-step scale.  "low" (9) and
#: anything unexpected folds into normal.
PRIORITY_FROM_REMINDERS = {0: 1, 1: 3, 5: 2, 9: 1}

URL_SEPARATOR = "\n\nURL: "

REMINDER_ACTIONS = frozenset(
    {"create-reminder", "update-reminder", "complete-reminder", "delete-reminder"}
)
ITEM_ACTIONS = frozenset(
    {"create-item", "update-item", "complete-item", "delete-item"}
)
_ITEM_WRITE_FIELDS = ("description", "notes", "url", "due", "remind_at")


class SyncError(Exception):
    """A refused or failed Reminders sync."""


# ---------------------------------------------------------------------------
# Backend interface


@dataclass
class Reminder:
    """One reminder in a mapped list, in the backend's own representation."""

    id: str
    list_name: str
    title: str
    body: str = ""
    due: str | None = None
    remind: str | None = None
    priority: int = 0
    flagged: bool = False
    completed: bool = False
    completed_at: str | None = None
    modified_at: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Reminder":
        return cls(
            id=str(data.get("id", "")),
            list_name=str(data.get("list_name", "")),
            title=str(data.get("title", data.get("name", ""))),
            body=str(data.get("body") or ""),
            due=data.get("due"),
            remind=data.get("remind"),
            priority=int(data.get("priority") or 0),
            flagged=bool(data.get("flagged")),
            completed=bool(data.get("completed")),
            completed_at=data.get("completed_at"),
            modified_at=data.get("modified_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "list_name": self.list_name,
            "title": self.title,
            "body": self.body,
            "due": self.due,
            "remind": self.remind,
            "priority": self.priority,
            "flagged": self.flagged,
            "completed": self.completed,
            "completed_at": self.completed_at,
            "modified_at": self.modified_at,
        }


class RemindersBackend(Protocol):
    """The narrow surface the sync engine needs from Reminders.

    Implementations may only ever look at the list passed to :meth:`fetch`.
    """

    def fetch(self, list_name: str) -> list[Reminder]:
        """Return the reminders currently in ``list_name``."""

    def create(self, list_name: str, reminder: Reminder) -> Reminder:
        """Create ``reminder`` in ``list_name`` and return the stored copy."""

    def update(self, reminder: Reminder) -> Reminder:
        """Write ``reminder``'s fields to the existing reminder."""

    def delete(self, reminder: Reminder) -> None:
        """Delete an existing reminder."""


# ---------------------------------------------------------------------------
# Field mapping

_JXA_SCRIPT = r"""
function run(argv) {
  var request = JSON.parse(argv[0]);
  var app = Application('Reminders');

  function pad(value) { return (value < 10 ? '0' : '') + value; }

  // Local time with an explicit offset, so a midnight date stays a date
  // when it comes back to Python instead of drifting across time zones.
  function localIso(value) {
    if (value === null || value === undefined) return null;
    var d = value instanceof Date ? value : new Date(value);
    if (isNaN(d.getTime())) return null;
    // Some Reminders versions report a cleared date as 1 Jan 1904.
    if (d.getFullYear() <= 1904) return null;
    var offset = -d.getTimezoneOffset();
    var sign = offset >= 0 ? '+' : '-';
    var abs = Math.abs(offset);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) +
      sign + pad(Math.floor(abs / 60)) + ':' + pad(abs % 60);
  }

  // Build Date objects explicitly instead of handing ISO strings to
  // Date.parse: an invalid or mis-parsed date then surfaces as a clear error
  // here rather than as Apple's opaque "Can't convert types" (-1700).
  function toDate(value, label) {
    if (value === null || value === undefined || value === '') return null;
    if (value instanceof Date) {
      if (isNaN(value.getTime())) {
        throw new Error(label + ': invalid date');
      }
      return value;
    }
    var text = String(value).trim();
    var match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?)?(Z|[+-]\d{2}:\d{2})?$/.exec(text);
    if (!match) {
      throw new Error(label + ": unsupported date '" + text + "'");
    }
    var year = Number(match[1]);
    var month = Number(match[2]);
    var day = Number(match[3]);
    var hour = Number(match[4] || '0');
    var minute = Number(match[5] || '0');
    var second = Number(match[6] || '0');
    var fraction = match[7] || '';
    while (fraction.length < 3) fraction += '0';
    var milli = Number(fraction || '0');
    var offset = match[8];
    var stamp;
    if (offset) {
      var utc = Date.UTC(year, month - 1, day, hour, minute, second, milli);
      if (offset !== 'Z') {
        var sign = offset.charAt(0) === '-' ? -1 : 1;
        var bits = offset.substring(1).split(':');
        utc -= sign * (Number(bits[0]) * 60 + Number(bits[1])) * 60000;
      }
      stamp = new Date(utc);
    } else {
      stamp = new Date(year, month - 1, day, hour, minute, second, milli);
      // Reject dates that JavaScript silently rolled over (Feb 30, etc.).
      if (
        stamp.getFullYear() !== year ||
        stamp.getMonth() !== month - 1 ||
        stamp.getDate() !== day ||
        stamp.getHours() !== hour ||
        stamp.getMinutes() !== minute ||
        stamp.getSeconds() !== second
      ) {
        throw new Error(label + ": invalid date '" + text + "'");
      }
    }
    if (isNaN(stamp.getTime())) {
      throw new Error(label + ": invalid date '" + text + "'");
    }
    return stamp;
  }

  function sameMoment(left, right) {
    if (!left || !right) return !left && !right;
    var a = left instanceof Date ? left : new Date(left);
    var b = right instanceof Date ? right : new Date(right);
    return a.getTime() === b.getTime();
  }

  // Read every field through the list's bulk property arrays.  Per-element
  // accessors are pathologically slow on current macOS (tens of seconds per
  // call), while these bulk reads take about a second each.
  function serializeAll(list) {
    var ids = list.reminders.id();
    var names = list.reminders.name();
    var bodies = list.reminders.body();
    var dues = list.reminders.dueDate();
    var reminds = list.reminders.remindMeDate();
    var priorities = list.reminders.priority();
    var flags = list.reminders.flagged();
    var completions = list.reminders.completed();
    var completionDates = list.reminders.completionDate();
    var modifieds = list.reminders.modificationDate();
    var result = [];
    for (var index = 0; index < ids.length; index++) {
      result.push({
        id: String(ids[index]),
        list_name: request.list,
        title: names[index] == null ? '' : String(names[index]),
        body: bodies[index] == null ? '' : String(bodies[index]),
        due: localIso(dues[index]),
        remind: localIso(reminds[index]),
        priority: Number(priorities[index] || 0),
        flagged: flags[index] === true,
        completed: completions[index] === true,
        completed_at: localIso(completionDates[index]),
        modified_at: localIso(modifieds[index])
      });
    }
    return result;
  }

  function findById(items, id) {
    for (var index = 0; index < items.length; index++) {
      if (items[index].id === id) return index;
    }
    return -1;
  }

  function containsId(ids, id) {
    for (var index = 0; index < ids.length; index++) {
      if (String(ids[index]) === id) return true;
    }
    return false;
  }

  function listByName(name) {
    // Address a single list by name first, so unmapped lists are never
    // enumerated in the normal path.
    try {
      var list = app.lists.byName(name);
      if (list && list.name() === name) return list;
    } catch (error) {
      // fall through to the name scan / clear error below
    }
    var lists = app.lists();
    for (var index = 0; index < lists.length; index++) {
      if (lists[index].name() === name) return lists[index];
    }
    throw new Error("no Reminders list named '" + name + "'");
  }

  function writeFields(reminder, fields, current) {
    // Every property is written with the type Reminders' dictionary expects:
    // strings for name/body, an integer priority (0 none, 1 high, 5 medium,
    // 9 low), a boolean flag, and real Date objects for dates.  Only fields
    // that differ are touched; each per-element write is expensive.
    var title = fields.title == null ? '' : String(fields.title);
    var body = fields.body == null ? '' : String(fields.body);
    var priority = Number(fields.priority);
    if (
      !isFinite(priority) ||
      (priority !== 0 && priority !== 1 && priority !== 5 && priority !== 9)
    ) {
      throw new Error('unsupported Reminders priority ' + fields.priority);
    }
    var flagged = fields.flagged === true;
    var completed = fields.completed === true;
    var due = toDate(fields.due, 'due');
    var remind = toDate(fields.remind, 'remind');

    if (!current || current.title !== title) reminder.name = title;
    if (!current || current.body !== body) reminder.body = body;
    if (!current || Number(current.priority) !== priority) {
      reminder.priority = priority;
    }
    if (!current || current.flagged !== flagged) reminder.flagged = flagged;

    // Assigning null raises -1700, so only real Date objects are written.
    // A truly cleared date therefore keeps its value for this run; the pair
    // stores the actual value, so the next run converges instead of
    // retrying the impossible write forever.
    if (due && (!current || !sameMoment(current.due, due))) {
      reminder.dueDate = due;
    }
    if (remind && (!current || !sameMoment(current.remind, remind))) {
      reminder.remindMeDate = remind;
    }

    // completionDate is read-only in the Reminders dictionary; `completed`
    // is the writable switch and Reminders stamps the completion date itself.
    if (!current || current.completed !== completed) {
      try {
        reminder.completed = completed;
      } catch (error) {
        throw new Error('completed: ' + (error && error.message ? error.message : error));
      }
    }
  }

  if (request.op === 'fetch') {
    return JSON.stringify(serializeAll(listByName(request.list)));
  }
  if (request.op === 'create') {
    var list = listByName(request.list);
    var before = list.reminders.id();
    var created = app.Reminder({name: request.reminder.title});
    list.reminders.push(created);
    try {
      writeFields(created, request.reminder, {
        title: request.reminder.title == null ? '' : String(request.reminder.title),
        body: '',
        priority: 0,
        flagged: false,
        completed: false,
        due: null,
        remind: null
      });
    } catch (error) {
      // Never leave a half-written reminder behind; the next run retries.
      try { app.delete(created); } catch (ignored) {}
      throw error;
    }
    var after = list.reminders.id();
    var createdId = null;
    for (var cursor = 0; cursor < after.length; cursor++) {
      if (!containsId(before, String(after[cursor]))) {
        createdId = String(after[cursor]);
      }
    }
    if (createdId === null) createdId = String(created.id());
    var stored = serializeAll(list);
    var at = findById(stored, createdId);
    if (at === -1) {
      throw new Error("created reminder '" + createdId + "' is not in the list");
    }
    return JSON.stringify(stored[at]);
  }
  if (request.op === 'update' || request.op === 'delete') {
    var target = listByName(request.list);
    var current = serializeAll(target);
    var found = findById(current, request.reminder.id);
    if (found === -1) {
      throw new Error("no reminder with id '" + request.reminder.id + "' in list '" + request.list + "'");
    }
    var existing = target.reminders()[found];
    if (request.op === 'delete') {
      app.delete(existing);
      return JSON.stringify({deleted: request.reminder.id});
    }
    writeFields(existing, request.reminder, current[found]);
    var refreshed = serializeAll(target);
    var updatedAt = findById(refreshed, request.reminder.id);
    if (updatedAt === -1) {
      throw new Error("reminder '" + request.reminder.id + "' disappeared after update");
    }
    return JSON.stringify(refreshed[updatedAt]);
  }
  throw new Error("unknown op '" + request.op + "'");
}
"""


class JxaRemindersBackend:
    """Apple Reminders backend built on ``osascript -l JavaScript`` (JXA)."""

    def _call(self, request: Mapping[str, Any]) -> Any:
        command = [
            "osascript",
            "-l",
            "JavaScript",
            "-e",
            _JXA_SCRIPT,
            json.dumps(request, ensure_ascii=False),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
        except OSError as exc:  # pragma: no cover - osascript missing
            raise SyncError(f"could not run osascript: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise SyncError(f"Apple Reminders error: {detail or 'osascript failed'}")
        try:
            return json.loads(completed.stdout or "null")
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise SyncError(f"unexpected osascript output: {completed.stdout!r}") from exc

    def fetch(self, list_name: str) -> list[Reminder]:
        payload = self._call({"op": "fetch", "list": list_name})
        if not isinstance(payload, list):
            raise SyncError("unexpected Reminders response for fetch")
        return [Reminder.from_dict(raw) for raw in payload]

    def create(self, list_name: str, reminder: Reminder) -> Reminder:
        payload = self._call(
            {"op": "create", "list": list_name, "reminder": reminder.to_dict()}
        )
        return Reminder.from_dict(payload)

    def update(self, reminder: Reminder) -> Reminder:
        payload = self._call(
            {"op": "update", "list": reminder.list_name, "reminder": reminder.to_dict()}
        )
        return Reminder.from_dict(payload)

    def delete(self, reminder: Reminder) -> None:
        self._call(
            {"op": "delete", "list": reminder.list_name, "reminder": reminder.to_dict()}
        )


def _make_backend() -> RemindersBackend:
    """Build the real backend; tests replace this function."""

    return JxaRemindersBackend()


def encode_body(notes: str | None, url: str | None) -> str:
    """Fold an orchpad URL into the Reminders body (which has no URL field)."""

    text = notes or ""
    if not url:
        return text
    if text:
        return f"{text}{URL_SEPARATOR}{url}"
    return f"URL: {url}"


def decode_body(body: str | None) -> tuple[str | None, str | None]:
    """Split a Reminders body back into ``(notes, url)``."""

    text = body or ""
    if text.startswith("URL: "):
        value = text[len("URL: "):].strip()
        return None, value or None
    index = text.rfind(URL_SEPARATOR)
    if index != -1:
        notes = text[:index]
        value = text[index + len(URL_SEPARATOR):].strip()
        return notes or None, value or None
    return text or None, None


def _local_timezone() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_local_timezone())
    return moment


def canonical_when(value: str | None) -> str | None:
    """Normalise a due/reminder/completion value for fingerprint comparison.

    A local midnight becomes a date (``YYYY-MM-DD``); everything else is
    converted to UTC.  This keeps a date-only orchpad due date and the
    equivalent midnight reminder from looking different forever.
    """

    if not value:
        return None
    moment = _parse_datetime(value)
    if (
        moment.hour == 0
        and moment.minute == 0
        and moment.second == 0
        and moment.microsecond == 0
    ):
        return moment.date().isoformat()
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _due_for_item(value: str | None) -> str | None:
    canonical = canonical_when(value)
    if canonical is None or len(canonical) == 10:
        return canonical
    return canonical


def _remind_for_item(value: str | None) -> str | None:
    canonical = canonical_when(value)
    if canonical is None:
        return None
    if len(canonical) == 10:
        return f"{canonical}T00:00:00+00:00"
    return canonical


def _timestamp_for_item(value: str | None) -> str | None:
    """A completion timestamp; dates become midnight UTC."""

    canonical = canonical_when(value)
    if canonical is None:
        return None
    if len(canonical) == 10:
        return f"{canonical}T00:00:00Z"
    return canonical


def item_fingerprint(item: Item) -> dict[str, Any]:
    completed = item.status == "done"
    return {
        "title": item.description,
        "body": encode_body(item.notes, item.url),
        "due": canonical_when(item.due),
        "remind": canonical_when(item.remind_at),
        "priority": item.priority,
        "flagged": bool(item.starred),
        "completed": completed,
        "completed_at": canonical_when(item.completed_at) if completed else None,
    }


def reminder_fingerprint(reminder: Reminder) -> dict[str, Any]:
    completed = bool(reminder.completed)
    return {
        "title": reminder.title,
        "body": reminder.body or "",
        "due": canonical_when(reminder.due),
        "remind": canonical_when(reminder.remind),
        "priority": PRIORITY_FROM_REMINDERS.get(reminder.priority, 1),
        "flagged": bool(reminder.flagged),
        "completed": completed,
        "completed_at": canonical_when(reminder.completed_at) if completed else None,
    }


def reminder_for_item(item: Item, list_name: str, reminder_id: str = "") -> Reminder:
    """Build the Reminders view of an orchpad task."""

    return Reminder(
        id=reminder_id,
        list_name=list_name,
        title=item.description,
        body=encode_body(item.notes, item.url),
        due=item.due,
        remind=item.remind_at,
        priority=PRIORITY_TO_REMINDERS[item.priority],
        flagged=item.starred,
        completed=item.status == "done",
        completed_at=item.completed_at if item.status == "done" else None,
    )


def item_changes_from_reminder(reminder: Reminder) -> dict[str, Any]:
    """The orchpad field values a reminder maps to."""

    notes, url = decode_body(reminder.body)
    return {
        "description": reminder.title,
        "notes": notes,
        "url": url,
        "due": _due_for_item(reminder.due),
        "remind_at": _remind_for_item(reminder.remind),
        "priority": PRIORITY_FROM_REMINDERS.get(reminder.priority, 1),
        "starred": bool(reminder.flagged),
        "completed": bool(reminder.completed),
        "completed_at": _timestamp_for_item(reminder.completed_at),
    }


# ---------------------------------------------------------------------------
# Mapping and state files


@dataclass
class Pair:
    """One orchpad item <-> one Reminders reminder, with last-sync hashes."""

    board: str
    list_name: str
    item_id: int
    reminder_id: str
    item_fingerprint: dict[str, Any]
    reminder_fingerprint: dict[str, Any]
    synced_at: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "list": self.list_name,
            "item_id": self.item_id,
            "reminder_id": self.reminder_id,
            "item_fingerprint": dict(self.item_fingerprint),
            "reminder_fingerprint": dict(self.reminder_fingerprint),
            "synced_at": self.synced_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Pair":
        return cls(
            board=str(data["board"]),
            list_name=str(data["list"]),
            item_id=int(data["item_id"]),
            reminder_id=str(data["reminder_id"]),
            item_fingerprint=dict(data.get("item_fingerprint") or {}),
            reminder_fingerprint=dict(data.get("reminder_fingerprint") or {}),
            synced_at=str(data.get("synced_at") or utcnow()),
        )


class SyncState:
    """In-memory view of ``<pad>/sync-reminders.json``."""

    def __init__(self, pairs: Sequence[Pair] = ()) -> None:
        self.pairs: dict[tuple[str, int], Pair] = {}
        for pair in pairs:
            self.pairs[(pair.board, pair.item_id)] = pair

    def for_board(self, board: str) -> list[Pair]:
        return [pair for pair in self.pairs.values() if pair.board == board]

    def put(self, pair: Pair) -> None:
        self.pairs[(pair.board, pair.item_id)] = pair

    def drop_item(self, board: str, item_id: int) -> None:
        self.pairs.pop((board, item_id), None)

    def drop_reminder(self, reminder_id: str) -> None:
        for key, pair in list(self.pairs.items()):
            if pair.reminder_id == reminder_id:
                del self.pairs[key]

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "pairs": [pair.to_dict() for pair in self.pairs.values()],
        }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_state(path: Path) -> SyncState:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return SyncState()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SyncError(f"{path}: corrupt sync state: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise SyncError(f"{path}: unsupported sync state version")
    raw_pairs = data.get("pairs")
    if not isinstance(raw_pairs, list):
        raise SyncError(f"{path}: 'pairs' must be a list")
    try:
        return SyncState([Pair.from_dict(raw) for raw in raw_pairs])
    except (KeyError, TypeError, ValueError) as exc:
        raise SyncError(f"{path}: invalid sync state: {exc}") from exc


def save_state(path: Path, state: SyncState) -> None:
    _write_json(path, state.to_payload())


def parse_mapping(value: str) -> tuple[str, str]:
    """Parse a ``BOARD=LIST`` argument."""

    if not isinstance(value, str) or "=" not in value:
        raise SyncError("--map must look like BOARD=LIST")
    board, list_name = value.split("=", 1)
    board = board.strip()
    list_name = list_name.strip()
    if not board or not list_name:
        raise SyncError("--map must look like BOARD=LIST")
    return board, list_name


def _validate_mappings(mappings: Sequence[tuple[str, str]]) -> None:
    boards: set[str] = set()
    lists: set[str] = set()
    for board, list_name in mappings:
        if board in boards:
            raise SyncError(f"board {board!r} is mapped more than once")
        if list_name.casefold() in lists:
            raise SyncError(
                f"Reminders list {list_name!r} is mapped more than once; "
                "each list can feed only one board"
            )
        boards.add(board)
        lists.add(list_name.casefold())


def load_saved_maps(path: Path) -> list[tuple[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SyncError(f"{path}: corrupt mapping file: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise SyncError(f"{path}: unsupported mapping file version")
    maps = data.get("maps")
    if not isinstance(maps, dict):
        raise SyncError(f"{path}: 'maps' must be an object")
    result: list[tuple[str, str]] = []
    for board, list_name in maps.items():
        if not isinstance(board, str) or not isinstance(list_name, str):
            raise SyncError(f"{path}: map entries must be strings")
        result.append((board, list_name))
    return result


def save_maps(path: Path, mappings: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge ``mappings`` into ``path`` and return the merged list.

    A board keeps one list, and a list feeds one board: saving a new mapping
    replaces any earlier mapping for either side.
    """

    merged: list[tuple[str, str]] = list(load_saved_maps(path))
    for board, list_name in mappings:
        merged = [
            (old_board, old_list)
            for old_board, old_list in merged
            if old_board != board
            and old_list.casefold() != list_name.casefold()
        ]
        merged.append((board, list_name))
    _write_json(
        path,
        {"version": STATE_VERSION, "maps": {board: name for board, name in merged}},
    )
    return merged


def denied_list_names(
    exclude_lists: Sequence[str], env: Mapping[str, str] | None = None
) -> set[str]:
    environment = os.environ if env is None else env
    names = {
        part.strip()
        for part in environment.get(DENY_ENV, "").split(",")
        if part.strip()
    }
    names.update(name.strip() for name in exclude_lists if name and name.strip())
    return {name.casefold() for name in names}


# ---------------------------------------------------------------------------
# Planning


@dataclass
class Change:
    """One planned write to exactly one side."""

    action: str
    board: str
    list_name: str
    title: str
    reason: str = ""
    item_id: int | None = None
    reminder_id: str | None = None
    desired: Reminder | None = None
    item_changes: dict[str, Any] | None = None

    @property
    def side(self) -> str:
        return "reminders" if self.action in REMINDER_ACTIONS else "orchpad"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "side": self.side,
            "board": self.board,
            "list": self.list_name,
            "item_id": self.item_id,
            "reminder_id": self.reminder_id,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass
class Plan:
    direction: str
    apply: bool
    mappings: list[tuple[str, str]]
    changes: list[Change] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        to_reminders = sum(1 for change in self.changes if change.side == "reminders")
        return {
            "to_reminders": to_reminders,
            "to_orchpad": len(self.changes) - to_reminders,
            "total": len(self.changes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "apply": self.apply,
            "mappings": [
                {"board": board, "list": list_name}
                for board, list_name in self.mappings
            ],
            "actions": [change.to_dict() for change in self.changes],
            "skipped": list(self.skipped),
            "warnings": list(self.warnings),
            "failures": [dict(failure) for failure in self.failures],
            "counts": self.counts(),
        }


def _newer_side(item: Item, reminder: Reminder) -> str | None:
    """Which side wins a conflict, or ``None`` for an exact tie."""

    item_time = _parse_datetime(item.updated_at)
    if not reminder.modified_at:
        return "orchpad"
    reminder_time = _parse_datetime(reminder.modified_at)
    if item_time > reminder_time:
        return "orchpad"
    if reminder_time > item_time:
        return "reminders"
    return None


def _reminder_action(
    item: Item, pair: Pair, reminder: Reminder, reason: str
) -> Change:
    action = "update-reminder"
    if item.status == "done" and not reminder.completed:
        action = "complete-reminder"
    return Change(
        action=action,
        board=pair.board,
        list_name=pair.list_name,
        title=item.description,
        reason=reason,
        item_id=item.id,
        reminder_id=pair.reminder_id,
        desired=reminder_for_item(item, pair.list_name, pair.reminder_id),
    )


def _item_action(
    item: Item, pair: Pair, reminder: Reminder, reason: str
) -> Change:
    action = "update-item"
    if reminder.completed and item.status != "done":
        action = "complete-item"
    return Change(
        action=action,
        board=pair.board,
        list_name=pair.list_name,
        title=reminder.title,
        reason=reason,
        item_id=item.id,
        reminder_id=reminder.id,
        item_changes=item_changes_from_reminder(reminder),
    )


def _create_item_action(board: str, list_name: str, reminder: Reminder) -> Change:
    return Change(
        action="create-item",
        board=board,
        list_name=list_name,
        title=reminder.title,
        reason="new reminder in a mapped list",
        reminder_id=reminder.id,
        item_changes=item_changes_from_reminder(reminder),
    )


def _skipped_notes(items: Sequence[Item]) -> list[str]:
    return [
        f"#{item.id} {item.description!r}: notes stay orchpad-only"
        for item in items
        if item.kind == "note"
    ]


def build_plan(
    pad: Pad,
    state: SyncState,
    mappings: Sequence[tuple[str, str]],
    backend: RemindersBackend,
    direction: str,
) -> Plan:
    """Fetch mapped lists and compute the change set without writing."""

    if direction not in DIRECTIONS:
        raise SyncError(
            f"direction must be one of {', '.join(DIRECTIONS)}"
        )
    plan = Plan(direction=direction, apply=False, mappings=list(mappings))
    items = {item.id: item for item in pad.list()}
    push = direction in ("both", "to-reminders")
    pull = direction in ("both", "from-reminders")

    for board, list_name in mappings:
        reminders = backend.fetch(list_name)
        by_reminder = {reminder.id: reminder for reminder in reminders}
        board_items = [
            item for item in items.values() if board in item.boards
        ]
        pairs = [pair for pair in state.for_board(board) if pair.list_name == list_name]
        paired_items = {pair.item_id for pair in pairs}
        paired_reminders = {pair.reminder_id for pair in pairs}

        for pair in pairs:
            item = items.get(pair.item_id)
            reminder = by_reminder.get(pair.reminder_id)

            if item is None and reminder is None:
                state.drop_item(board, pair.item_id)
                continue

            if item is None:
                if direction == "from-reminders":
                    # Pull-only sync never deletes reminders.
                    continue
                plan.changes.append(
                    Change(
                        action="delete-reminder",
                        board=board,
                        list_name=list_name,
                        title=reminder.title if reminder else pair.reminder_id,
                        reason=(
                            "previously synced item was deleted from the pad"
                        ),
                        reminder_id=pair.reminder_id,
                        desired=reminder,
                    )
                )
                continue

            if reminder is None:
                # A missing reminder is a deletion in Reminders.  Push-only
                # sync re-creates it (the pad is the source of truth);
                # both/from-reminders propagate the deletion to the pad.
                if direction == "to-reminders":
                    plan.changes.append(
                        Change(
                            action="create-reminder",
                            board=board,
                            list_name=list_name,
                            title=item.description,
                            reason=(
                                "previously synced reminder is gone; re-creating it"
                            ),
                            item_id=item.id,
                            desired=reminder_for_item(item, list_name),
                        )
                    )
                else:
                    plan.changes.append(
                        Change(
                            action="delete-item",
                            board=board,
                            list_name=list_name,
                            title=item.description,
                            reason=(
                                "previously synced reminder was deleted in Reminders"
                            ),
                            item_id=item.id,
                            reminder_id=pair.reminder_id,
                        )
                    )
                continue

            current_item = item_fingerprint(item)
            current_reminder = reminder_fingerprint(reminder)
            item_changed = current_item != pair.item_fingerprint
            reminder_changed = current_reminder != pair.reminder_fingerprint
            if not item_changed and not reminder_changed:
                continue

            if item_changed and not reminder_changed:
                winner = "orchpad"
            elif reminder_changed and not item_changed:
                winner = "reminders"
            else:
                winner = _newer_side(item, reminder)
                if winner is None:
                    plan.skipped.append(
                        f"#{item.id} {item.description!r}: both sides changed "
                        "at the same time; left untouched"
                    )
                    continue

            if winner == "orchpad":
                if push:
                    reason = (
                        "orchpad changed since the last sync"
                        if not reminder_changed
                        else "conflict: orchpad side is newer"
                    )
                    plan.changes.append(
                        _reminder_action(item, pair, reminder, reason)
                    )
            else:
                if pull:
                    reason = (
                        "Reminders changed since the last sync"
                        if not item_changed
                        else "conflict: Reminders side is newer"
                    )
                    plan.changes.append(
                        _item_action(item, pair, reminder, reason)
                    )

        if push:
            for item in board_items:
                if item.id in paired_items:
                    continue
                if item.kind != "task":
                    plan.skipped.append(_skipped_notes([item])[0])
                    continue
                plan.changes.append(
                    Change(
                        action="create-reminder",
                        board=board,
                        list_name=list_name,
                        title=item.description,
                        reason="new task on a mapped board",
                        item_id=item.id,
                        desired=reminder_for_item(item, list_name),
                    )
                )

        if pull:
            for reminder in reminders:
                if reminder.id in paired_reminders:
                    continue
                if not reminder.title.strip():
                    plan.skipped.append(
                        f"Reminders {reminder.id}: empty title; not imported"
                    )
                    continue
                plan.changes.append(
                    _create_item_action(board, list_name, reminder)
                )

        if push:
            for item in board_items:
                if item.kind != "task":
                    continue
                if item.repeat:
                    plan.warnings.append(
                        f"#{item.id}: repeat rules stay orchpad-only; JXA "
                        "does not expose recurrence"
                    )
                children = [
                    child
                    for child in items.values()
                    if child.parent == item.id
                ]
                if children or item.parent is not None:
                    plan.warnings.append(
                        f"#{item.id}: subtasks stay orchpad-only; JXA does "
                        "not expose them"
                    )

    # Keep warnings stable and duplicate-free.
    plan.warnings = list(dict.fromkeys(plan.warnings))
    plan.skipped = list(dict.fromkeys(plan.skipped))
    return plan


# ---------------------------------------------------------------------------
# Applying


def _read_item(pad: Pad, item_id: int) -> Item | None:
    document = pad.store.load_active()
    raw = document["items"].get(str(item_id))
    return Item.from_dict(raw) if raw is not None else None


def _create_item(pad: Pad, board: str, reminder: Reminder) -> Item:
    changes = item_changes_from_reminder(reminder)
    stamp = utcnow()
    with pad.store.transaction() as transaction:
        item_id = transaction.active["next_id"]
        item = Item(
            id=item_id,
            kind="task",
            description=changes["description"],
            boards=[board],
            priority=changes["priority"],
            starred=changes["starred"],
            status="done" if changes["completed"] else "pending",
            created_at=stamp,
            updated_at=stamp,
            due=changes["due"],
            remind_at=changes["remind_at"],
            notes=changes["notes"],
            url=changes["url"],
            completed_at=(
                changes["completed_at"] or stamp
                if changes["completed"]
                else None
            ),
        )
        transaction.active["next_id"] = item_id + 1
        transaction.active["items"][str(item_id)] = item.to_dict()
        transaction.mark_active()
    return item


def _set_completion(
    pad: Pad, item_id: int, completed: bool, completed_at: str | None
) -> Item:
    stamp = utcnow()
    with pad.store.transaction() as transaction:
        raw = transaction.active["items"].get(str(item_id))
        if raw is None:
            raise SyncError(f"item {item_id} disappeared while syncing")
        item = Item.from_dict(raw)
        item.status = "done" if completed else "pending"
        item.completed_at = (completed_at or stamp) if completed else None
        item.updated_at = stamp
        transaction.active["items"][str(item_id)] = item.to_dict()
        transaction.mark_active()
    return item


def _write_item_from_reminder(
    pad: Pad, item: Item, changes: Mapping[str, Any]
) -> Item:
    fields = {
        name: changes[name]
        for name in _ITEM_WRITE_FIELDS
        if item.to_dict().get(name) != changes[name]
    }
    if fields:
        item = pad.edit_fields(item.id, **fields)
    if item.priority != changes["priority"]:
        item = pad.set_priority(item.id, changes["priority"])
    if item.starred != changes["starred"]:
        item = pad.star(item.id)[0]
    completed = bool(changes["completed"])
    if completed != (item.status == "done"):
        item = _set_completion(
            pad, item.id, completed, changes.get("completed_at")
        )
    return item


def _pair_for(
    board: str,
    list_name: str,
    item: Item,
    reminder: Reminder,
) -> Pair:
    return Pair(
        board=board,
        list_name=list_name,
        item_id=item.id,
        reminder_id=reminder.id,
        item_fingerprint=item_fingerprint(item),
        reminder_fingerprint=reminder_fingerprint(reminder),
        synced_at=utcnow(),
    )


def _apply_change(
    change: Change,
    backend: RemindersBackend,
    pad: Pad,
    state: SyncState,
) -> None:
    action = change.action

    if action == "create-reminder":
        assert change.desired is not None
        created = backend.create(change.list_name, change.desired)
        item = _read_item(pad, change.item_id)  # type: ignore[arg-type]
        if item is None:
            raise SyncError(
                f"item {change.item_id} disappeared while syncing"
            )
        state.put(_pair_for(change.board, change.list_name, item, created))
        return

    if action in ("update-reminder", "complete-reminder"):
        assert change.desired is not None
        updated = backend.update(change.desired)
        item = _read_item(pad, change.item_id)  # type: ignore[arg-type]
        if item is None:
            raise SyncError(
                f"item {change.item_id} disappeared while syncing"
            )
        state.put(_pair_for(change.board, change.list_name, item, updated))
        return

    if action == "delete-reminder":
        if change.desired is not None:
            backend.delete(change.desired)
        state.drop_reminder(change.reminder_id or "")
        return

    if action == "create-item":
        assert change.item_changes is not None
        reminder = Reminder(
            id=change.reminder_id or "",
            list_name=change.list_name,
            title=change.title,
            body=encode_body(
                change.item_changes["notes"], change.item_changes["url"]
            ),
            due=change.item_changes["due"],
            remind=change.item_changes["remind_at"],
            priority=PRIORITY_TO_REMINDERS[
                change.item_changes["priority"]
            ],
            flagged=change.item_changes["starred"],
            completed=change.item_changes["completed"],
            completed_at=change.item_changes["completed_at"],
        )
        item = _create_item(pad, change.board, reminder)
        state.put(_pair_for(change.board, change.list_name, item, reminder))
        return

    if action in ("update-item", "complete-item"):
        item = _read_item(pad, change.item_id)  # type: ignore[arg-type]
        if item is None:
            raise SyncError(
                f"item {change.item_id} disappeared while syncing"
            )
        assert change.item_changes is not None
        item = _write_item_from_reminder(pad, item, change.item_changes)
        # The reminder object itself is not carried on item-side changes;
        # synthesise it from the stored fingerprint-shaped fields.
        reminder = Reminder(
            id=change.reminder_id or "",
            list_name=change.list_name,
            title=change.title,
            body=encode_body(
                change.item_changes["notes"], change.item_changes["url"]
            ),
            due=change.item_changes["due"],
            remind=change.item_changes["remind_at"],
            priority=PRIORITY_TO_REMINDERS[
                change.item_changes["priority"]
            ],
            flagged=change.item_changes["starred"],
            completed=change.item_changes["completed"],
            completed_at=change.item_changes["completed_at"],
        )
        state.put(_pair_for(change.board, change.list_name, item, reminder))
        return

    if action == "delete-item":
        pad.delete(change.item_id)  # type: ignore[arg-type]
        state.drop_item(change.board, change.item_id)  # type: ignore[arg-type]
        return

    raise SyncError(f"unknown sync action {action!r}")  # pragma: no cover


_APPLY_ORDER = {
    "create-reminder": 0,
    "create-item": 0,
    "update-reminder": 1,
    "update-item": 1,
    "complete-reminder": 1,
    "complete-item": 1,
    "delete-reminder": 2,
    "delete-item": 2,
}


def apply_plan(
    plan: Plan,
    backend: RemindersBackend,
    pad: Pad,
    state: SyncState,
    state_path: Path,
) -> list[dict[str, Any]]:
    """Execute changes independently, persisting only successful pairs.

    A failure on one item (for example an Apple Reminders type error) is
    recorded and the remaining items are still attempted.  The pair state is
    rolled back for that item, so a re-run retries exactly the failed work
    and then converges.  Returns the list of failures.
    """

    failures: list[dict[str, Any]] = []
    ordered = sorted(
        plan.changes, key=lambda change: _APPLY_ORDER.get(change.action, 9)
    )
    try:
        for change in ordered:
            snapshot = dict(state.pairs)
            try:
                _apply_change(change, backend, pad, state)
            except Exception as exc:  # one bad item must not abort the sync
                state.pairs = snapshot
                failures.append(
                    {
                        **change.to_dict(),
                        "error": str(exc) or exc.__class__.__name__,
                    }
                )
    finally:
        # Only successful changes touched the state; failures were rolled
        # back above.  The write itself is atomic.
        save_state(state_path, state)

    plan.apply = True
    plan.failures = failures
    return failures


# ---------------------------------------------------------------------------
# Entry point


def run_sync(
    pad: Pad,
    maps: Sequence[str] | None = None,
    *,
    apply: bool = False,
    direction: str = "both",
    exclude_lists: Sequence[str] = (),
    save: bool = False,
    backend: RemindersBackend | None = None,
    state_path: Path | None = None,
    maps_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Plan (and optionally apply) one Reminders sync.

    ``maps`` holds raw ``BOARD=LIST`` strings.  With no maps, the mappings
    saved in ``<pad>/sync.json`` are used.  Nothing is written to Reminders
    unless ``apply=True``; the state file is written only after an apply.
    """

    if direction not in DIRECTIONS:
        raise SyncError(f"direction must be one of {', '.join(DIRECTIONS)}")

    # Refuse other platforms before anything else, even when the user has
    # not supplied a mapping yet.
    if backend is None and platform.system() != "Darwin":
        raise SyncError(
            "Apple Reminders sync requires macOS (osascript is only "
            "available there)"
        )

    state_path = state_path or pad.path / STATE_FILENAME
    maps_path = maps_path or pad.path / MAPS_FILENAME

    provided = [parse_mapping(value) for value in (maps or [])]
    if save and not provided:
        raise SyncError("--save needs at least one --map")
    mappings = provided if provided else load_saved_maps(maps_path)
    if not mappings:
        raise SyncError(
            "no list mappings: pass --map BOARD=LIST or save mappings with --save"
        )
    _validate_mappings(mappings)

    denied = denied_list_names(exclude_lists, env)
    for _board, list_name in mappings:
        if list_name.casefold() in denied:
            raise SyncError(
                f"refusing to sync Reminders list {list_name!r}: it is on the "
                f"deny list (--exclude-list or {DENY_ENV})"
            )

    if backend is None:
        backend = _make_backend()

    if save:
        save_maps(maps_path, provided)

    state = load_state(state_path)
    plan = build_plan(pad, state, mappings, backend, direction)
    if apply:
        apply_plan(plan, backend, pad, state, state_path)
    return plan.to_dict()
