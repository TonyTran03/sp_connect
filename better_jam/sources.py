from __future__ import annotations

import sqlite3
import time
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

    def delete_pending_track(self, spotify_track_id: str) -> None: ...

    def record_queued(self, track: dict) -> int: ...

    def mark_queued(self, request: SongRequest) -> None: ...

    def accepted_queue_count(self) -> int: ...

    def queued_tracks(self) -> list[dict]: ...

    def display_name(self, device_id: str) -> str | None: ...

    def set_display_name(self, device_id: str, display_name: str) -> None: ...

    def playing_attribution(self, spotify_track_id: str) -> dict | None: ...

    def ban_current_queuer(self, seconds: int = 300) -> dict | None: ...

    def ban_remaining_seconds(self, device_id: str) -> int: ...

    def latest_announcement(self) -> dict | None: ...

    def delete_owned_queued(self, request_id: int, device_id: str) -> bool: ...

    def finish_head_if_playing(self, spotify_track_id: str) -> str | None: ...

    def history(self, limit: int | None = None) -> list[dict]: ...

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
            connection.execute("DELETE FROM song_requests WHERE status = 'finished'")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(song_requests)")
            }
            for name, definition in {
                "spotify_uri": "TEXT",
                "album": "TEXT",
                "artwork_url": "TEXT",
                "duration_ms": "INTEGER",
                "requested_by_device": "TEXT",
                "requested_by_name": "TEXT",
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
                    requested_by_device TEXT,
                    requested_by_name TEXT,
                    played_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            played_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(played_tracks)")
            }
            if "requested_by_device" not in played_columns:
                connection.execute(
                    "ALTER TABLE played_tracks ADD COLUMN requested_by_device TEXT"
                )
            if "requested_by_name" not in played_columns:
                connection.execute(
                    "ALTER TABLE played_tracks ADD COLUMN requested_by_name TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS device_profiles (
                    device_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS device_bans (
                    device_id TEXT PRIMARY KEY,
                    banned_until INTEGER NOT NULL,
                    reason TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                  AND requested_by_device = ?
                ORDER BY id
                LIMIT 1
                """,
                (track["id"], track.get("requested_by_device")),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO song_requests (
                    title, artist, spotify_track_id, spotify_uri, album,
                    artwork_url, duration_ms, requested_by_device,
                    requested_by_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track["name"], artist, track["id"], track["uri"],
                    track.get("album"), track.get("image_url"), track.get("duration_ms"),
                    track.get("requested_by_device"),
                    track.get("requested_by_name"),
                ),
            )
            return int(cursor.lastrowid)

    def delete_pending_track(self, spotify_track_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM song_requests
                WHERE status = 'pending' AND spotify_track_id = ?
                """,
                (spotify_track_id,),
            )

    def record_queued(self, track: dict) -> int:
        artists = track.get("artists") or []
        artist = ", ".join(artists) if isinstance(artists[0] if artists else None, str) else ""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO song_requests (
                    title, artist, status, spotify_track_id, spotify_uri,
                    album, artwork_url, duration_ms, requested_by_device
                    , requested_by_name
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track["name"], artist, track["id"], track["uri"],
                    track.get("album"), track.get("image_url"), track.get("duration_ms"),
                    track.get("requested_by_device"),
                    track.get("requested_by_name"),
                ),
            )
            return int(cursor.lastrowid)

    def mark_queued(self, request: SongRequest) -> None:
        self._update(request, "queued", spotify_track_id=request.spotify_track_id)

    def accepted_queue_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM song_requests
                WHERE status IN ('queued', 'cancelled')
                """
            ).fetchone()
        return int(row["count"])

    def queued_tracks(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT requests.id, requests.title, requests.artist,
                       requests.spotify_track_id, requests.spotify_uri,
                       requests.album, requests.artwork_url, requests.duration_ms,
                       requests.requested_by_device, requests.status,
                       COALESCE(requests.requested_by_name,
                                profiles.display_name) AS display_name
                FROM song_requests AS requests
                LEFT JOIN device_profiles AS profiles
                  ON profiles.device_id = requests.requested_by_device
                WHERE requests.status IN ('pending', 'queued')
                ORDER BY requests.id
                """
            ).fetchall()
        return [
            {
                "queue_request_id": row["id"],
                "name": row["title"], "artists": [row["artist"]],
                "id": row["spotify_track_id"], "uri": row["spotify_uri"],
                "album": row["album"], "image_url": row["artwork_url"],
                "duration_ms": row["duration_ms"],
                "requested_by_device": row["requested_by_device"],
                "requested_by_name": row["display_name"],
                "queue_status": "pending" if row["status"] == "pending" else None,
            }
            for row in rows
        ]

    def display_name(self, device_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT display_name FROM device_profiles WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return str(row["display_name"]) if row is not None else None

    def set_display_name(self, device_id: str, display_name: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_profiles (device_id, display_name)
                VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (device_id, display_name),
            )
            connection.execute(
                """
                UPDATE song_requests SET requested_by_name = ?
                WHERE requested_by_device = ?
                  AND status IN ('pending', 'queued', 'playing')
                """,
                (display_name, device_id),
            )
            connection.execute(
                """
                UPDATE played_tracks SET requested_by_name = ?
                WHERE requested_by_device = ?
                """,
                (display_name, device_id),
            )

    def playing_attribution(self, spotify_track_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT requests.requested_by_device,
                       COALESCE(requests.requested_by_name,
                                profiles.display_name) AS display_name
                FROM song_requests AS requests
                LEFT JOIN device_profiles AS profiles
                  ON profiles.device_id = requests.requested_by_device
                WHERE requests.spotify_track_id = ?
                  AND requests.status IN ('playing', 'queued')
                ORDER BY CASE requests.status WHEN 'playing' THEN 0 ELSE 1 END,
                         requests.id
                LIMIT 1
                """,
                (spotify_track_id,),
            ).fetchone()
        if row is None or not row["requested_by_device"]:
            return None
        return {
            "requested_by_device": row["requested_by_device"],
            "requested_by_name": row["display_name"],
        }

    def ban_current_queuer(self, seconds: int = 300) -> dict | None:
        seconds = max(1, int(seconds))
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT requests.title, requests.requested_by_device,
                       COALESCE(requests.requested_by_name,
                                profiles.display_name) AS display_name
                FROM song_requests AS requests
                LEFT JOIN device_profiles AS profiles
                  ON profiles.device_id = requests.requested_by_device
                WHERE requests.status = 'playing'
                  AND requests.requested_by_device IS NOT NULL
                ORDER BY requests.id DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            banned_until = int(time.time()) + seconds
            connection.execute(
                """
                INSERT INTO device_bans (device_id, banned_until, reason)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    banned_until = excluded.banned_until,
                    reason = excluded.reason,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    row["requested_by_device"],
                    banned_until,
                    f"Queued current song: {row['title']}",
                ),
            )
            display_name = row["display_name"] or "Guest"
            connection.execute(
                "INSERT INTO announcements (message, expires_at) VALUES (?, ?)",
                (
                    f"{display_name} was banned from queueing for {seconds // 60 or 1} minute(s)",
                    int(time.time()) + 8,
                ),
            )
        return {
            "device_id": row["requested_by_device"],
            "display_name": row["display_name"],
            "track": row["title"],
            "banned_until": banned_until,
        }

    def ban_remaining_seconds(self, device_id: str) -> int:
        now = int(time.time())
        with self._connection() as connection:
            row = connection.execute(
                "SELECT banned_until FROM device_bans WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                return 0
            remaining = int(row["banned_until"]) - now
            if remaining <= 0:
                connection.execute(
                    "DELETE FROM device_bans WHERE device_id = ?", (device_id,)
                )
                return 0
        return remaining

    def latest_announcement(self) -> dict | None:
        now = int(time.time())
        with self._connection() as connection:
            connection.execute("DELETE FROM announcements WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT id, message, expires_at FROM announcements
                WHERE expires_at > ? ORDER BY id DESC LIMIT 1
                """,
                (now,),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_owned_queued(self, request_id: int, device_id: str) -> bool:
        with self._connection() as connection:
            request = connection.execute(
                """
                SELECT status FROM song_requests
                WHERE id = ? AND status IN ('pending', 'queued')
                  AND requested_by_device = ?
                """,
                (request_id, device_id),
            ).fetchone()
            if request is None:
                return False
            if request["status"] == "pending":
                connection.execute("DELETE FROM song_requests WHERE id = ?", (request_id,))
            else:
                # Spotify cannot remove an accepted queue item. Keep a hidden
                # marker so the worker can skip it when its turn arrives.
                connection.execute(
                    "UPDATE song_requests SET status = 'cancelled' WHERE id = ?",
                    (request_id,),
                )
            return True

    def finish_head_if_playing(self, spotify_track_id: str) -> str | None:
        """Consume the matching head and return queued or cancelled."""
        with self._connection() as connection:
            head = connection.execute(
                """
                SELECT id, spotify_track_id, status FROM song_requests
                WHERE status IN ('queued', 'cancelled') ORDER BY id LIMIT 1
                """
            ).fetchone()
            if head is not None and head["spotify_track_id"] == spotify_track_id:
                status = str(head["status"])
                if status == "queued":
                    # Keep attribution until record_played has written history.
                    connection.execute(
                        "UPDATE song_requests SET status = 'playing' WHERE id = ?",
                        (head["id"],),
                    )
                else:
                    connection.execute(
                        "DELETE FROM song_requests WHERE id = ?", (head["id"],)
                    )
                return status
        return None

    def record_played(self, track: dict) -> None:
        artists = ", ".join(item.get("name", "") for item in track.get("artists", []))
        album = track.get("album") or {}
        images = album.get("images") or []
        with self._connection() as connection:
            attribution = connection.execute(
                """
                SELECT id, requested_by_device, requested_by_name
                FROM song_requests
                WHERE status = 'playing' AND spotify_track_id = ?
                ORDER BY id LIMIT 1
                """,
                (track.get("id"),),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO played_tracks (
                    spotify_track_id, spotify_uri, title, artist, album,
                    artwork_url, duration_ms, requested_by_device,
                    requested_by_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track.get("id"), track.get("uri"), track.get("name"), artists,
                    album.get("name"), images[0].get("url") if images else None,
                    track.get("duration_ms"),
                    attribution["requested_by_device"] if attribution else None,
                    attribution["requested_by_name"] if attribution else None,
                ),
            )
            if attribution is not None:
                connection.execute(
                    "DELETE FROM song_requests WHERE id = ?", (attribution["id"],)
                )

    def history(self, limit: int | None = None) -> list[dict]:
        with self._connection() as connection:
            if limit is None:
                rows = connection.execute(
                    """
                    SELECT played.*,
                           COALESCE(played.requested_by_name,
                                    profiles.display_name) AS display_name
                    FROM played_tracks AS played
                    LEFT JOIN device_profiles AS profiles
                      ON profiles.device_id = played.requested_by_device
                    ORDER BY played.id ASC
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM (
                        SELECT played.*,
                               COALESCE(played.requested_by_name,
                                        profiles.display_name) AS display_name
                        FROM played_tracks AS played
                        LEFT JOIN device_profiles AS profiles
                          ON profiles.device_id = played.requested_by_device
                        ORDER BY played.id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (limit,),
                ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["requested_by_name"] = item.pop("display_name")
            history.append(item)
        return history

    def pending_tracks(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT requests.title, requests.artist, requests.spotify_track_id,
                       requests.spotify_uri, requests.album, requests.artwork_url,
                       requests.duration_ms, requests.requested_by_device,
                       COALESCE(requests.requested_by_name,
                                profiles.display_name) AS display_name
                FROM song_requests AS requests
                LEFT JOIN device_profiles AS profiles
                  ON profiles.device_id = requests.requested_by_device
                WHERE requests.status = 'pending'
                ORDER BY requests.id
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
                "requested_by_device": row["requested_by_device"],
                "requested_by_name": row["display_name"],
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
