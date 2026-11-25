# orchpad

A persistent, lightweight task and notes pad for agent orchestrators: boards,
tasks, notes, priorities, an archive with restore, and a timeline. Pure Python,
no dependencies, one JSON store per pad written atomically.

Ships with a Hermes plugin so each orchestrator profile keeps its own pad, and
a read-only overview across pads for a supervising orchestrator.

Status: early development.

## Install

Requires Python 3.10 or newer. There are no runtime dependencies.

```sh
pip install git+https://github.com/pom11/orchpad
```

For development:

```sh
git clone https://github.com/pom11/orchpad
cd orchpad
python -m pip install -e ".[dev]"
python -m pytest
```

## Quick start

```python
from orchpad import Pad

pad = Pad()  # $ORCHPAD_HOME or ~/.orchpad

task = pad.add_task("write the release notes", boards=["work"], priority=3)
note = pad.add_note("remember to bump the version", boards=["work"])

pad.begin(task.id)          # toggle in progress
pad.star(task.id)           # toggle starred
pad.check(task.id)          # toggle done
pad.link(task.id, "kanban", "board/t_123")

pad.list("work")            # active items on a board
pad.list("done", "task")    # filters combine
pad.find("release")         # case-insensitive substring search
pad.boards()                # {board: [items]}
pad.timeline()              # {date: [items]}
pad.stats()                 # counts plus percent done

pad.delete(task.id)         # move to the archive
pad.restore(task.id)        # bring it back, same id
pad.clear()                 # archive every done task
```

Every id is unique per pad and is never reused, including after deletion and
restore. Unknown ids raise `KeyError`. `delete` and `clear` write to
`archive.json`; `restore` moves items back to `storage.json`.

## Python API

| Method | Purpose |
| --- | --- |
| `add_task(desc, boards=None, priority=1)` | Add a pending task |
| `add_note(desc, boards=None)` | Add a note |
| `check(*ids)` / `begin(*ids)` | Toggle done / in progress |
| `star(*ids)` | Toggle starred |
| `set_priority(id, p)` | Set priority 1, 2 or 3 (tasks only) |
| `edit(id, desc)` | Replace a description |
| `move(id, boards)` | Replace an item's boards |
| `delete(*ids)` / `restore(*ids)` | Archive / unarchive items |
| `clear()` | Archive all done tasks |
| `find(text)` | Case-insensitive description search |
| `list(*attrs)` | Filter by status, kind, star, archive or board |
| `boards()` / `timeline()` / `stats()` | Read-only views |
| `link(id, key, value)` | Attach a free-form link |

Pads are directories containing `storage.json`, `archive.json` and a `.lock`
file. Reads and writes take an `fcntl` lock, and writes are staged in the same
directory, flushed, `fsync`ed, atomically replaced, then the directory is
`fsync`ed. Corrupt files raise `orchpad.StoreError` and are never overwritten.

Design inspired by [taskbook](https://github.com/klaudiosinani/taskbook) (MIT).
