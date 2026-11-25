"""Hermes plugin entry point: register the orchpad agent tools.

Hermes loads this directory from ``<profile home>/plugins/orchpad`` and
calls :func:`register`.  Install it with
``orchpad install-hermes-plugin --hermes-home DIR [--profile NAME]``.
"""

from __future__ import annotations

from typing import Any

try:  # loaded as a package by Hermes or by tests
    from . import tools as _tools
except ImportError:  # pragma: no cover - direct import without package parent
    import tools as _tools  # type: ignore[no-redef]

TOOLSET = "orchpad"
EMOJI = "\U0001f5d2"


def register(ctx: Any) -> None:
    """Register the four orchpad tools with a Hermes plugin context."""

    handlers = _tools.build_handlers(_tools.OrchpadTools())
    for name, schema, description in _tools.TOOL_SPECS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handlers[name],
            emoji=EMOJI,
            description=description,
        )
