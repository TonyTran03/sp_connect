"""Interactively remove one Better Jam request or clear all of Next up."""

from __future__ import annotations

import sqlite3

from better_jam.config import PROJECT_ROOT
from clear_next_up import clear_next_up, notify_websocket


DATABASE = PROJECT_ROOT / "queue.db"


def active_requests(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return connection.execute(
        """
        SELECT id, title, artist, status, requested_by_device
        FROM song_requests
        WHERE status IN ('pending', 'queued')
        ORDER BY id
        """
    ).fetchall()


def remove_request(connection: sqlite3.Connection, request: sqlite3.Row) -> None:
    if request["status"] == "pending":
        connection.execute("DELETE FROM song_requests WHERE id = ?", (request["id"],))
    else:
        # Spotify cannot dequeue an accepted item. Hide it now and preserve a
        # marker so the worker skips it when its turn arrives.
        connection.execute(
            "UPDATE song_requests SET status = 'cancelled' WHERE id = ?",
            (request["id"],),
        )


def main() -> None:
    with sqlite3.connect(DATABASE) as connection:
        requests = active_requests(connection)

    if not requests:
        print("Next up is already empty.")
        return

    print("\nNEXT UP")
    for number, request in enumerate(requests, start=1):
        owner = request["requested_by_device"] or "unknown device"
        print(
            f"  {number}. {request['title']} - {request['artist']} "
            f"[{request['status']}; {owner}]"
        )

    choice = input("\nEnter a song #, 'all', or 'q': ").strip().lower()
    if choice in {"q", "quit", "exit"}:
        print("No changes made.")
        return

    if choice == "all":
        changed = clear_next_up(DATABASE)
        message = f"Cleared {changed} Next up request(s)."
    else:
        try:
            selected = int(choice)
        except ValueError:
            print("Invalid choice. Enter a displayed number, 'all', or 'q'.")
            return
        if not 1 <= selected <= len(requests):
            print(f"Choose a number from 1 to {len(requests)}.")
            return
        request = requests[selected - 1]
        with sqlite3.connect(DATABASE) as connection:
            connection.row_factory = sqlite3.Row
            current = connection.execute(
                "SELECT * FROM song_requests WHERE id = ?", (request["id"],)
            ).fetchone()
            if current is None or current["status"] not in {"pending", "queued"}:
                print("That request is no longer in Next up.")
                return
            remove_request(connection, current)
        message = f"Removed {request['title']} - {request['artist']}."

    try:
        notify_websocket()
        print(f"{message} Connected devices notified.")
    except (OSError, RuntimeError) as error:
        print(f"{message} WebSocket notification failed: {error}")


if __name__ == "__main__":
    main()
