"""``orchpad-hermes``: install the bundled plugin into a Hermes profile.

This is a separate console script so the main ``orchpad`` CLI stays free of
any Hermes-specific flags or imports.  Only this package knows where Hermes
keeps its profiles; the installer copies the plugin files and never imports
Hermes.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .install import install_hermes_plugin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchpad-hermes",
        description="Install the bundled orchpad plugin into a Hermes profile.",
    )
    parser.add_argument(
        "--hermes-home",
        required=True,
        metavar="DIR",
        help="Hermes home (or the root holding profiles/).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="NAME",
        help="Install for this named profile instead of the default one.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the install summary as JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = install_hermes_plugin(args.hermes_home, args.profile)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"Installed the orchpad Hermes plugin for profile "
            f"{result['profile']} into {result['path']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
