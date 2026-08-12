from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable


API_ROOT = "https://api.spotify.com/v1"


def rate_limit_message(error: urllib.error.HTTPError) -> str:
    retry_after = getattr(error, "spotify_retry_after", None)
    reason = getattr(error, "spotify_rate_limit_reason", None)
    if reason == "QUOTA_EXCEEDED":
        return "Spotify Development Mode quota exceeded; retry time is not specified"
    if retry_after is not None:
        return f"Spotify rate limited: retry in {retry_after} second(s)"
    return "Spotify rate limited; retry time was not provided"


class SpotifyClient:
    def __init__(
        self,
        access_token: str,
        refresh_access_token: Callable[[], str] | None = None,
    ) -> None:
        self.access_token = access_token
        self._refresh_access_token = refresh_access_token
        self._request_lock = threading.Lock()
        self._rate_limited_until = 0.0
        self._playback_lock = threading.Lock()
        self._playback_cache: dict = {}
        self._playback_cached_at = 0.0

    def _request(self, method: str, path: str, query: dict | None = None) -> dict | None:
        url = f"{API_ROOT}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        with self._request_lock:
            delay = self._rate_limited_until - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            for attempt in range(2):
                request = urllib.request.Request(
                    url,
                    method=method,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                try:
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
                except urllib.error.HTTPError as error:
                    if (
                        error.code == 401
                        and attempt == 0
                        and self._refresh_access_token is not None
                    ):
                        error.close()
                        self.access_token = self._refresh_access_token()
                        self._playback_cached_at = 0.0
                        print("Spotify access token refreshed; retrying request.")
                        continue
                    if error.code == 429:
                        try:
                            retry_after = max(1, int(error.headers.get("Retry-After", "5")))
                        except (TypeError, ValueError):
                            retry_after = 5
                        detail = error.read().decode(errors="replace")
                        reason = None
                        try:
                            reason = (json.loads(detail).get("error") or {}).get("reason")
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        error.spotify_retry_after = retry_after
                        error.spotify_rate_limit_reason = reason
                        error.spotify_error_detail = detail
                        self._rate_limited_until = time.monotonic() + retry_after
                    raise

    def queue(self) -> dict:
        return self._request("GET", "/me/player/queue") or {}

    def playback_state(self, max_age_seconds: float = 4.5) -> dict:
        """Return a briefly cached player state shared by worker and WebSocket."""
        with self._playback_lock:
            if (
                self._playback_cache
                and time.monotonic() - self._playback_cached_at < max_age_seconds
            ):
                return self._playback_cache
            state = self._request("GET", "/me/player") or {}
            self._playback_cache = state
            self._playback_cached_at = time.monotonic()
            return state

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
        self._playback_cached_at = 0.0

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
        self._playback_cached_at = 0.0


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
