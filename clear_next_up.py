"""Clear Better Jam's Next up list and notify connected browsers."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct

from better_jam.config import PROJECT_ROOT


WEBSOCKET_HOST = "127.0.0.1"
WEBSOCKET_PORT = 8788


def clear_next_up(database_path=PROJECT_ROOT / "queue.db") -> int:
    import sqlite3

    with sqlite3.connect(database_path) as connection:
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM song_requests WHERE status = 'pending'"
        ).fetchone()[0]
        queued_count = connection.execute(
            "SELECT COUNT(*) FROM song_requests WHERE status = 'queued'"
        ).fetchone()[0]
        # Pending requests have not reached Spotify and can be removed. Queued
        # requests must remain as hidden tombstones because Spotify offers no
        # dequeue endpoint; the worker will skip them when their turn arrives.
        connection.execute(
            "DELETE FROM song_requests WHERE status = 'pending'"
        )
        connection.execute(
            "UPDATE song_requests SET status = 'cancelled' WHERE status = 'queued'"
        )
        return int(pending_count + queued_count)


def notify_websocket() -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    with socket.create_connection((WEBSOCKET_HOST, WEBSOCKET_PORT), timeout=5) as connection:
        connection.sendall(
            (
                "GET /ws HTTP/1.1\r\n"
                f"Host: {WEBSOCKET_HOST}:{WEBSOCKET_PORT}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += connection.recv(4096)
        if b"101 Switching Protocols" not in response:
            raise RuntimeError("Better Jam WebSocket rejected the connection")

        payload = json.dumps({"type": "refresh"}).encode()
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if len(payload) < 126:
            header = bytes((0x81, 0x80 | len(payload)))
        else:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(payload))
        connection.sendall(header + mask + masked)


def main() -> None:
    removed = clear_next_up()
    try:
        notify_websocket()
        print(f"Cleared {removed} Next up request(s); connected devices notified.")
    except (OSError, RuntimeError) as error:
        print(f"Cleared {removed} Next up request(s). WebSocket notification failed: {error}")


if __name__ == "__main__":
    main()
