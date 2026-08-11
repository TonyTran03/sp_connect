from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol


@dataclass(frozen=True)
class SongRequest:
    id: int | str
    title: str
    artist: str
    spotify_track_id: str | None = None
    spotify_uri: str | None = None


class SongSource(Protocol):
    """Implement this protocol to connect any external database."""

    def pending(self, limit: int = 20) -> list[SongRequest]: ...

    def delete(self, request: SongRequest) -> None: ...

    def mark_failed(self, request: SongRequest, reason: str) -> None: ...

    def record_played(self, track: dict) -> None: ...

    def enqueue(self, track: dict) -> int: ...

    def history(self, limit: int = 100) -> list[dict]: ...

    def pending_tracks(self) -> list[dict]: ...


class SQLiteSongSource:
    """Local implementation useful now and as a database-adapter example."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS song_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    spotify_track_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                )
                """
            )
            # Migrate rows accepted by Spotify under the previous lifecycle.
            connection.execute("DELETE FROM song_requests WHERE status IN ('queued', 'finished')")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(song_requests)")
            }
            for name, definition in {
                "spotify_uri": "TEXT",
                "album": "TEXT",
                "artwork_url": "TEXT",
                "duration_ms": "INTEGER",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE song_requests ADD COLUMN {name} {definition}")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS played_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    spotify_track_id TEXT NOT NULL,
                    spotify_uri TEXT,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT,
                    artwork_url TEXT,
                    duration_ms INTEGER,
                    played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def pending(self, limit: int = 20) -> list[SongRequest]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, artist, spotify_track_id, spotify_uri
                FROM song_requests
                WHERE status = 'pending'
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SongRequest(
                row["id"], row["title"], row["artist"],
                row["spotify_track_id"], row["spotify_uri"],
            )
            for row in rows
        ]

    def enqueue(self, track: dict) -> int:
        artists = track.get("artists") or []
        artist = ", ".join(artists) if isinstance(artists[0] if artists else None, str) else ""
        with self._connection() as connection:
            existing = connection.execute(
                """
                SELECT id
                FROM song_requests
                WHERE status = 'pending' AND spotify_track_id = ?
                ORDER BY id
                LIMIT 1
                """,
                (track["id"],),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO song_requests (
                    title, artist, spotify_track_id, spotify_uri, album,
                    artwork_url, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track["name"], artist, track["id"], track["uri"],
                    track.get("album"), track.get("image_url"), track.get("duration_ms"),
                ),
            )
            return int(cursor.lastrowid)

    def record_played(self, track: dict) -> None:
        artists = ", ".join(item.get("name", "") for item in track.get("artists", []))
        album = track.get("album") or {}
        images = album.get("images") or []
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO played_tracks (
                    spotify_track_id, spotify_uri, title, artist, album,
                    artwork_url, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track.get("id"), track.get("uri"), track.get("name"), artists,
                    album.get("name"), images[0].get("url") if images else None,
                    track.get("duration_ms"),
                ),
            )

    def history(self, limit: int = 100) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM played_tracks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_tracks(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT title, artist, spotify_track_id, spotify_uri, album,
                       artwork_url, duration_ms
                FROM song_requests
                WHERE status = 'pending'
                ORDER BY id
                """
            ).fetchall()
        return [
            {
                "name": row["title"],
                "artists": [row["artist"]],
                "id": row["spotify_track_id"],
                "uri": row["spotify_uri"],
                "album": row["album"],
                "image_url": row["artwork_url"],
                "duration_ms": row["duration_ms"],
                "queue_status": "pending",
            }
            for row in rows
        ]

    def delete(self, request: SongRequest) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM song_requests WHERE id = ?", (request.id,))

    def mark_failed(self, request: SongRequest, reason: str) -> None:
        self._update(request, "failed", error=reason)

    def _update(
        self,
        request: SongRequest,
        status: str,
        spotify_track_id: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE song_requests
                SET status = ?, spotify_track_id = ?, error = ?
                WHERE id = ?
                """,
                (status, spotify_track_id, error, request.id),
            )
