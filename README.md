# orchpad

Tasks, boards and notes for the command line, in Python. orchpad is a port of
[taskbook](https://github.com/klaudiosinani/taskbook) (MIT) with the same flat
flag syntax (`-t`, `-n`, `-c`, `-b`, `@board`, `p:3`, ...) and a number of
features taskbook does not have.

Pure Python 3.10+, no runtime dependencies, one JSON store per pad.

## Features

From taskbook:

- tasks and notes organised into boards, with priorities and stars
- board view, timeline view, search, filtered lists
- archive on delete, with restore (same id)

Added in orchpad:

- **crash-safe storage**: every write is atomic (temp file, fsync, rename) and
  a move between the active list and the archive can never lose an item
- **safe for concurrent writers**: a file lock serialises processes that share a pad
- **`--json` on every view**, for scripts and agents
- **`orchpad show` / `orchpad watch`**: one screen with everything, for one pad
  or many (`--all-under DIR`), built to run under `watch` or on its own loop
- **multiple pads**: `--pad-dir DIR` or `$ORCHPAD_HOME`
- **links**: attach references (a ticket, a PR, a kanban card) to any item
- **a Python API** (`from orchpad import Pad`)
- **optional integrations**: a [Hermes](#integrations) agent plugin that gives
  each agent profile its own pad and can promote an item to a kanban card

Status: early development (0.1.x).

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
Pads live in `$ORCHPAD_HOME` or `~/.orchpad`; `--pad-dir DIR` picks another pad and `--where` prints the location
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
op show                         # one screen with everything
op show --all-under DIR         # every pad below DIR, one section each
op watch                        # redraw show until Ctrl+C
```

New tasks and notes accept `@board` tokens anywhere in the description to
select boards, and tasks accept `p:2` or `p:3` to set priority; those tokens
are removed from the stored description. Commands take comma-separated ids
(`op -c 1,2`) and a leading `@` is optional (`op -c @1`).

Every read view supports `--json` for machine use: the board view, `--archive`,
`--list`, `--find`, `--timeline`, `--stats` and `--where`. Writes also accept
`--json` and return the affected items. Read views end with a footer such as:

```
50% of all tasks complete · 1 done · 0 in progress · 1 pending · 2 notes
```

ANSI colour is used only on an interactive terminal; piped output is plain and
`NO_COLOR` disables colour. Terminals whose encoding cannot represent the
symbols get an ASCII fallback (`[ ]`, `[x]`, `[~]`, `*`).

Exit codes: `0` success, `1` user error (unknown id, empty description, bad
flag), `2` store error (unreadable or corrupt pad files).

## Integrations

### Hermes plugin

Every Hermes profile keeps its own pad at `<profile home>/orchpad/`. The
package ships the plugin and a small installer as a separate console script,
so the main `orchpad` CLI and the core library never import Hermes:

```sh
orchpad-hermes --hermes-home ~/.hermes --profile coder
```

`--profile` is optional and defaults to the profile at the Hermes home itself;
`--json` prints the install summary for machine use. Re-running the installer
is idempotent and only rewrites files whose contents changed.

The plugin exposes `orchpad_add`, `orchpad_update`, `orchpad_list` and
`orchpad_promote`. The pad is for the orchestrator's own follow-ups, ideas,
observations and waiting-on items; real work for a worker is promoted to a
kanban card, which is linked back onto the pad item.

## Watching a pad

`orchpad show [PAD_DIR ...]` prints one screen with every board and item in a
pad, then a footer with the combined stats. Each line carries the status
symbol, description, priority, star, age and any `kanban` link. It is
read-only (a shared lock, never an item write) and deterministic: boards are
sorted by name with the default board first, items by status (in progress,
pending, done, notes), priority descending, then id. Descriptions are
truncated with an ellipsis to fit `--width` (default: the terminal width, or
100 when there is no terminal).

`--all-under DIR` finds every `*/orchpad/` pad below `DIR` and prints one
section per pad, headed by the pad directory's parent name, with a combined
footer. `--archived` adds archived items (marked `[archived]`), `--json` emits
the same data for machines, and `--color auto|always|never` controls ANSI
colour. A pad with no items prints `Empty pad.` and exits `0`.

For a live view, use the system `watch` command with forced colour:

```sh
watch -c orchpad show --color always
```

Stock macOS has no `watch`; use the built-in loop instead. It clears the
screen, redraws whenever the pad files change (or every 30 seconds), and exits
`0` on Ctrl+C:

```sh
orchpad watch
orchpad watch --all-under DIR -n 1
```

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

## Credits

orchpad started as a Python port of [taskbook](https://github.com/klaudiosinani/taskbook)
by Klaudio Sinani (MIT). See LICENSE.
