"""CLI for the border wait-time feed, the same shape as scraper.social and scraper.kb.

    python -m scraper.border poll [--prune] [--dry-run]   # record one reading (GitHub Actions)
    python -m scraper.border selfcheck [--json]           # does live CBP still parse?
    python -m scraper.border serve [--port 8088]          # local HTTP pull surface
    python -m scraper.border chat                         # the DM experience in a terminal
    python -m scraper.border demo                         # five scripted scenes
    python -m scraper.border simulate                     # a fake Instagram against the feed

Each command is also its own module (python -m scraper.border.poll, ...). Only poll and
selfcheck are production; serve and the three rehearsal tools are for local use, the
way mcp_server.py is.
"""

from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "poll": "scraper.border.poll",
    "selfcheck": "scraper.border.selfcheck",
    "serve": "scraper.border.api",
    "chat": "scraper.border.devtools.chat",
    "demo": "scraper.border.devtools.demo",
    "simulate": "scraper.border.devtools.fake_instagram",
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help") or args[0] not in COMMANDS:
        print(__doc__)
        return 0 if args and args[0] in ("-h", "--help") else 2
    # Imported on demand, so `poll` on a CI runner never loads the rehearsal tools.
    return importlib.import_module(COMMANDS[args[0]]).main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
