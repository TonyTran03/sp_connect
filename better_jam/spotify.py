from __future__ import annotations

import json
import urllib.parse
import urllib.request


API_ROOT = "https://api.spotify.com/v1"


class SpotifyClient:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    def _request(self, method: str, path: str, query: dict | None = None) -> dict | None:
        url = f"{API_ROOT}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method=method,
            headers={"Authorization": f"Bearer {self.access_token}"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            if not body.strip():
                return None
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                # Player control endpoints can return a successful response
                # with a non-JSON body. There is nothing useful to parse.
                return None
            return json.loads(body)

    def queue(self) -> dict:
        return self._request("GET", "/me/player/queue") or {}

    def playback_state(self) -> dict:
        return self._request("GET", "/me/player") or {}

    def audio_analysis(self, track_id: str) -> dict:
        return self._request("GET", f"/audio-analysis/{track_id}") or {}

    def search_tracks(self, title: str, artist: str, limit: int = 10) -> list[dict]:
        query = f'track:"{title}" artist:"{artist}"'
        response = self._request(
            "GET", "/search", {"q": query, "type": "track", "limit": limit}
        ) or {}
        return response.get("tracks", {}).get("items", [])

    def search(self, query: str, limit: int = 10) -> list[dict]:
        response = self._request(
            "GET", "/search", {"q": query, "type": "track", "limit": limit}
        ) or {}
        return response.get("tracks", {}).get("items", [])

    def add_to_queue(self, uri: str) -> None:
        self._request("POST", "/me/player/queue", {"uri": uri})

    def next_track(self, device_id: str | None = None) -> None:
        query = {"device_id": device_id} if device_id else None
        self._request("POST", "/me/player/next", query)

    def set_repeat(self, state: str, device_id: str | None = None) -> None:
        if state not in {"off", "track", "context"}:
            raise ValueError("repeat state must be off, track, or context")
        query = {"state": state}
        if device_id:
            query["device_id"] = device_id
        self._request("PUT", "/me/player/repeat", query)

    def set_volume(self, volume_percent: int, device_id: str | None = None) -> None:
        if not 0 <= volume_percent <= 100:
            raise ValueError("volume_percent must be between 0 and 100")
        query: dict[str, int | str] = {"volume_percent": volume_percent}
        if device_id:
            query["device_id"] = device_id
        self._request("PUT", "/me/player/volume", query)

    def seek(self, position_seconds: float, device_id: str | None = None) -> None:
        if position_seconds < 0:
            raise ValueError("position_seconds cannot be negative")
        query: dict[str, int | str] = {"position_ms": int(position_seconds * 1000)}
        if device_id:
            query["device_id"] = device_id
        self._request("PUT", "/me/player/seek", query)


def track_info(item: dict) -> dict:
    album = item.get("album") or {}
    duration_ms = item.get("duration_ms")
    duration = None
    if duration_ms is not None:
        minutes, seconds = divmod(duration_ms // 1000, 60)
        duration = f"{minutes}:{seconds:02d}"
    return {
        "name": item.get("name"),
        "artists": [artist.get("name") for artist in item.get("artists", [])],
        "album": album.get("name"),
        "duration_ms": duration_ms,
        "duration": duration,
        "id": item.get("id"),
        "uri": item.get("uri"),
        "url": (item.get("external_urls") or {}).get("spotify"),
        "image_url": ((album.get("images") or [{}])[0]).get("url"),
    }
