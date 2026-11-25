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

## Command line

Installing the package puts two equivalent console scripts on `PATH`,
`orchpad` and `op`. `python -m orchpad` works without installing the scripts.
Pads live in `$ORCHPAD_HOME` or `~/.orchpad`; `--pad-dir` prints the location
in use.

```sh
op                              # board view
op -t "write release notes @work p:3"
op -n "remember to tag the release @work"
op -c 1                         # toggle done
op -b 1                         # toggle in progress
op -s 1                         # toggle the star
op -p 1 3                       # set priority 1, 2 or 3
op -e 1 "new description"
op -m 1 work home               # replace the item's boards
op -f release                   # case-insensitive search
op -l done                      # filter by status, kind, starred, archived or board
op -i                           # timeline grouped by creation date
op --stats
op -d 1                         # archive
op -a                           # show the archive
op -r 1                         # restore from the archive
op --clear                      # archive every done task
```

New tasks and notes accept `@board` tokens anywhere in the description to
select boards, and tasks accept `p:2` or `p:3` to set priority; those tokens
are removed from the stored description. Commands take comma-separated ids
(`op -c 1,2`) and a leading `@` is optional (`op -c @1`).

Every read view supports `--json` for machine use: the board view, `--archive`,
`--list`, `--find`, `--timeline`, `--stats` and `--pad-dir`. Writes also accept
`--json` and return the affected items. Read views end with a footer such as:

```
50% of all tasks complete · 1 done · 0 in progress · 1 pending · 2 notes
```

ANSI colour is used only on an interactive terminal; piped output is plain and
`NO_COLOR` disables colour. Terminals whose encoding cannot represent the
symbols get an ASCII fallback (`[ ]`, `[x]`, `[~]`, `*`).

Exit codes: `0` success, `1` user error (unknown id, empty description, bad
flag), `2` store error (unreadable or corrupt pad files).

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
