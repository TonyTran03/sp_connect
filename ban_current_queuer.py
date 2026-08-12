"""Temporarily prevent whoever queued the current song from queueing again."""

from __future__ import annotations

import argparse
from datetime import datetime

from better_jam.config import PROJECT_ROOT
from better_jam.sources import SQLiteSongSource


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ban the device that queued Better Jam's current song."
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=5,
        help="Ban duration in minutes (default: 5).",
    )
    args = parser.parse_args()
    if args.minutes <= 0:
        parser.error("--minutes must be greater than zero")

    source = SQLiteSongSource(PROJECT_ROOT / "queue.db")
    result = source.ban_current_queuer(round(args.minutes * 60))
    if result is None:
        print("No locally queued song is currently marked as playing; nobody was banned.")
        return 1

    name = result["display_name"] or "Guest"
    until = datetime.fromtimestamp(result["banned_until"]).strftime("%I:%M:%S %p")
    print(
        f"Banned {name} from queueing for {args.minutes:g} minute(s) "
        f"(until {until}). Current song: {result['track']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
