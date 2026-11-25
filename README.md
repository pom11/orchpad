# orchpad

A persistent, lightweight task and notes pad for agent orchestrators: boards,
tasks, notes, priorities, an archive with restore, and a timeline. Pure Python,
no dependencies, one JSON store per pad written atomically.

Ships with a Hermes plugin so each orchestrator profile keeps its own pad, and
a read-only overview across pads for a supervising orchestrator.

Status: early development.

Design inspired by [taskbook](https://github.com/klaudiosinani/taskbook) (MIT).
