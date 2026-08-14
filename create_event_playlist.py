from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

from better_jam.auth import get_access_token
from better_jam.config import client_id


ROOT = Path(__file__).resolve().parent
START_UTC = "2026-08-12 23:00:00"
END_UTC = "2026-08-13 00:30:00"
PLAYLIST_NAME = "Better Jam — Aug 12, 7–8:30 PM"


def spotify_request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"https://api.spotify.com/v1{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        return json.loads(payload) if payload else {}


def main() -> None:
    with sqlite3.connect(ROOT / "queue.db") as connection:
        rows = connection.execute(
            """
            SELECT spotify_uri, title, artist, played_at
            FROM played_tracks
            WHERE played_at >= ? AND played_at < ? AND spotify_uri IS NOT NULL
            ORDER BY played_at, id
            """,
            (START_UTC, END_UTC),
        ).fetchall()

    if not rows:
        raise SystemExit("No played tracks found in the selected time window.")

    print(f"Found {len(rows)} played tracks:")
    for _uri, title, artist, played_at in rows:
        print(f"  {played_at} UTC  {title} — {artist}")

    token = get_access_token(client_id())
    playlist = spotify_request(
        token,
        "POST",
        "/me/playlists",
        {
            "name": PLAYLIST_NAME,
            "public": False,
            "description": "Tracks played on Better Jam, Aug 12, 2026 from 7:00–8:30 PM EDT.",
        },
    )
    uris = [row[0] for row in rows]
    for offset in range(0, len(uris), 100):
        spotify_request(
            token,
            "POST",
            f"/playlists/{playlist['id']}/items",
            {"uris": uris[offset : offset + 100]},
        )
    print(f"PLAYLIST_URL={(playlist.get('external_urls') or {}).get('spotify')}")


if __name__ == "__main__":
    main()
