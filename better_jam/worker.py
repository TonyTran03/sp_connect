from __future__ import annotations

import re
import threading
import unicodedata
import urllib.error

from .hook_detector import detect_hook
from .playback import fade_to_next
from .sources import SongSource
from .spotify import SpotifyClient


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def exact_track_match(title: str, artist: str, candidates: list[dict]) -> dict | None:
    expected_title = _normalized(title)
    expected_artist = _normalized(artist)
    for candidate in candidates:
        candidate_artists = {_normalized(item.get("name", "")) for item in candidate.get("artists", [])}
        if _normalized(candidate.get("name", "")) == expected_title and expected_artist in candidate_artists:
            return candidate
    return None


class QueueWorker:
    def __init__(
        self,
        client: SpotifyClient,
        source: SongSource,
        excerpt_seconds: float = 45,
        poll_seconds: float = 2,
        fade_out_seconds: float = 3,
        fade_in_seconds: float = 3,
        volume_interval_seconds: float = 0.20,
        fade_in_target_volume: int | None = None,
        restore_original_volume: bool = True,
    ) -> None:
        if not 45 <= excerpt_seconds <= 60:
            raise ValueError("excerpt_seconds must be between 45 and 60")
        self.client = client
        self.source = source
        self.excerpt_seconds = excerpt_seconds
        self.poll_seconds = poll_seconds
        self.fade_out_seconds = max(0.0, fade_out_seconds)
        self.fade_in_seconds = max(0.0, fade_in_seconds)
        self.volume_interval_seconds = max(0.05, volume_interval_seconds)
        self.fade_in_target_volume = fade_in_target_volume
        self.restore_original_volume = restore_original_volume
        self._current_track_id: str | None = None
        self._current_track_processed = False
        self._prepared_track: tuple[str, dict] | None = None

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_event = stop_event or threading.Event()
        print("Queue worker started. Monitoring Spotify and the request database...")
        while not stop_event.is_set():
            try:
                self._enqueue_pending()
                self._process_current_track(stop_event)
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                print(f"Spotify API error ({error.code}): {detail}")
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Worker error: {error}")
            stop_event.wait(self.poll_seconds)

    def _enqueue_pending(self) -> None:
        for request in self.source.pending():
            try:
                match = None
                if request.spotify_uri and request.spotify_track_id:
                    match = {
                        "uri": request.spotify_uri,
                        "id": request.spotify_track_id,
                        "name": request.title,
                        "artists": [{"name": request.artist}],
                    }
                else:
                    results = self.client.search_tracks(request.title, request.artist)
                    match = exact_track_match(request.title, request.artist, results)
                if not match:
                    self.source.mark_failed(request, "No exact Spotify title/artist match")
                    print(f"No exact match: {request.title} — {request.artist}")
                    continue
                self.client.add_to_queue(match["uri"])
                self.source.mark_queued(request)
                print(f"Queued: {match['name']} — {match['artists'][0]['name']}")
            except Exception as error:
                # Leave transient API failures pending so the next poll retries.
                print(f"Could not queue {request.title}: {error}")

    def _process_current_track(self, stop_event: threading.Event) -> None:
        playback = self.client.playback_state()
        current = playback.get("item")
        current_id = current.get("id") if current else None
        if not current_id:
            self._current_track_id = None
            self._current_track_processed = False
            return

        if current_id != self._current_track_id:
            self._current_track_id = current_id
            self._current_track_processed = False
            self.source.finish_head_if_playing(current_id)
        if self._current_track_processed:
            return

        # Mark it before playback control so API propagation delays cannot make
        # the same track start a second excerpt on the following poll.
        self._current_track_processed = True
        already_prepared = bool(
            self._prepared_track and self._prepared_track[0] == current_id
        )
        if already_prepared:
            hook = self._prepared_track[1]
            self._prepared_track = None
        else:
            hook = self._find_hook(current)
        print(
            f"Playing {current.get('name')} from {hook['start']:.1f}s "
            f"to {hook['end']:.1f}s ({hook['method']}, {hook['confidence']} confidence)"
        )
        device = playback.get("device") or {}
        device_id = device.get("id")
        if not device_id:
            self._current_track_processed = False
            raise RuntimeError("Spotify did not report an active playback device")
        print(f"Controlling Spotify device: {device.get('name', device_id)}")
        repeat_state = playback.get("repeat_state", "unknown")
        print(f"Spotify repeat state: {repeat_state}")
        if repeat_state != "off":
            self.client.set_repeat("off", device_id=device_id)
            print("Spotify repeat disabled for queue progression.")
        if not already_prepared:
            self.client.seek(hook["start"], device_id=device_id)
        excerpt_duration = hook["end"] - hook["start"]
        can_fade = bool(device.get("supports_volume")) and device.get("volume_percent") is not None
        fade_duration = min(self.fade_out_seconds, excerpt_duration) if can_fade else 0
        play_duration = excerpt_duration - fade_duration

        print(f"Playing for {play_duration:.1f}s before transition...")
        if stop_event.wait(play_duration):
            return

        self.source.record_played(current)

        fade_to_next(
            self.client,
            playback,
            fade_out_seconds=fade_duration if can_fade else self.fade_out_seconds,
            fade_in_seconds=self.fade_in_seconds,
            volume_interval_seconds=self.volume_interval_seconds,
            fade_in_target_volume=self.fade_in_target_volume,
            restore_original_volume=self.restore_original_volume,
            stop_event=stop_event,
            prepare_next=self._prepare_next_track,
        )

    def _find_hook(self, track: dict) -> dict:
        track_id = track.get("id")
        try:
            return detect_hook(
                self.client.audio_analysis(track_id),
                excerpt_seconds=self.excerpt_seconds,
            )
        except urllib.error.HTTPError as error:
            if error.code != 403:
                raise
            duration = float(track.get("duration_ms", 0)) / 1000
            print(
                "Spotify denied Audio Analysis (403). "
                "Using a middle excerpt because structural hook detection is unavailable."
            )
            return _middle_excerpt(duration, self.excerpt_seconds)

    def _prepare_next_track(self, playback: dict) -> None:
        track = playback.get("item") or {}
        track_id = track.get("id")
        device_id = (playback.get("device") or {}).get("id")
        if not track_id or not device_id:
            raise RuntimeError("Spotify did not report the next track or active device")
        hook = self._find_hook(track)
        self.client.seek(hook["start"], device_id=device_id)
        self._prepared_track = (track_id, hook)
        print(f"Next track positioned at {hook['start']:.1f}s; beginning fade-in.")


def _middle_excerpt(duration: float, excerpt_seconds: float) -> dict:
    if duration <= 0:
        raise ValueError("Spotify did not provide a usable track duration")
    length = min(excerpt_seconds, duration)
    start = max(0.0, (duration - length) / 2)
    return {
        "start": start,
        "end": start + length,
        "method": "middle_fallback",
        "confidence": "unavailable",
        "reason": "Spotify Audio Analysis returned 403",
    }
