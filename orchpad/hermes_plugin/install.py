"""Install the bundled Hermes plugin into a Hermes profile home."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PLUGIN_NAME = "orchpad"
PLUGIN_FILES = ("plugin.yaml", "__init__.py", "tools.py")


def plugin_source() -> Path:
    """Directory of the plugin bundled inside this package."""

    return Path(__file__).resolve().parent


def resolve_profile_home(
    hermes_home: str | os.PathLike[str], profile: str | None = None
) -> Path:
    """Resolve a Hermes root and optional profile name to its profile home.

    Hermes keeps the default profile at the root and named profiles at
    ``<root>/profiles/<name>``.
    """

    root = Path(hermes_home).expanduser()
    if profile and profile != "default":
        return root / "profiles" / profile
    return root


def install_hermes_plugin(
    hermes_home: str | os.PathLike[str], profile: str | None = None
) -> dict[str, Any]:
    """Copy the plugin into ``<profile home>/plugins/orchpad`` idempotently.

    Returns a summary with the target directory and the files that changed.
    """

    home = resolve_profile_home(hermes_home, profile)
    target = home / "plugins" / PLUGIN_NAME
    target.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    source = plugin_source()
    for name in PLUGIN_FILES:
        data = (source / name).read_bytes()
        destination = target / name
        if destination.exists() and destination.read_bytes() == data:
            continue
        temporary = destination.with_name(f".{name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, destination)
        written.append(name)

    return {
        "plugin": PLUGIN_NAME,
        "profile": profile or "default",
        "path": str(target),
        "written": written,
    }
