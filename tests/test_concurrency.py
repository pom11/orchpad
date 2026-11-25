from __future__ import annotations

import multiprocessing as mp

from orchpad import Pad

WRITERS = 2
TASKS_PER_WRITER = 20


def _add_many(path: str, prefix: str, count: int) -> None:
    pad = Pad(path)
    for index in range(count):
        pad.add_task(f"{prefix}-{index}")


def test_concurrent_writers_both_land(tmp_path):
    path = tmp_path / "pad"
    Pad(path).add_task("seed")

    context = mp.get_context("spawn")
    processes = [
        context.Process(
            target=_add_many,
            args=(str(path), f"writer{writer}", TASKS_PER_WRITER),
        )
        for writer in range(WRITERS)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
    assert [process.exitcode for process in processes] == [0, 0]

    items = Pad(path).list("task")
    expected = 1 + WRITERS * TASKS_PER_WRITER
    assert len(items) == expected
    assert sorted(item.id for item in items) == list(range(1, expected + 1))
    descriptions = {item.description for item in items}
    assert "seed" in descriptions
    for writer in range(WRITERS):
        for index in range(TASKS_PER_WRITER):
            assert f"writer{writer}-{index}" in descriptions
