from __future__ import annotations

import threading
from collections.abc import Callable

from .spotify import SpotifyClient


def fade_to_next(
    client: SpotifyClient,
    playback: dict,
    fade_out_seconds: float = 3,
    fade_in_seconds: float = 3,
    volume_interval_seconds: float = 0.20,
    fade_in_target_volume: int | None = None,
    restore_original_volume: bool = True,
    stop_event: threading.Event | None = None,
    prepare_next: Callable[[dict], dict | None] | None = None,
) -> dict:
    """Fade out, skip, prepare/seek the next track, then fade it in."""
    stop_event = stop_event or threading.Event()
    current = playback.get("item") or {}
    current_id = current.get("id")
    device = playback.get("device") or {}
    device_id = device.get("id")
    original_volume = device.get("volume_percent")
    can_fade = (
        (fade_out_seconds > 0 or fade_in_seconds > 0)
        and bool(device.get("supports_volume"))
        and original_volume is not None
        and device_id is not None
    )
    volume_changed = False
    transition_complete = False
    restore_device_id = device_id
    target_volume = (
        int(original_volume)
        if fade_in_target_volume is None
        else max(0, min(100, int(fade_in_target_volume)))
    ) if original_volume is not None else 0

    try:
        print("\nTRANSITION")
        if can_fade:
            volume_changed = True
            if not _fade(
                client, int(original_volume), 0, fade_out_seconds, device_id,
                stop_event, volume_interval_seconds
            ):
                return {}
        client.next_track()

        next_playback = _wait_for_next(client, current_id, stop_event)
        next_device = next_playback.get("device") or {}
        restore_device_id = next_device.get("id") or device_id

        if prepare_next:
            prepared_playback = prepare_next(next_playback)
            if prepared_playback:
                next_playback = prepared_playback
                next_device = next_playback.get("device") or {}
                restore_device_id = next_device.get("id") or restore_device_id

        next_item = next_playback.get("item") or {}
        print(f"  Next     {next_item.get('name', 'next track')}")

        if can_fade:
            if not _fade(
                client,
                0,
                target_volume,
                fade_in_seconds,
                restore_device_id,
                stop_event,
                volume_interval_seconds,
            ):
                return next_playback
        transition_complete = True
        return next_playback
    finally:
        # A successful fade-in already ends at the requested target. Restore
        # and verify only if an interruption/error leaves volume mid-fade.
        if volume_changed and restore_original_volume and not transition_complete:
            _restore_volume(client, int(original_volume), stop_event)


def _wait_for_next(
    client: SpotifyClient,
    previous_track_id: str | None,
    stop_event: threading.Event,
) -> dict:
    for _ in range(10):
        if stop_event.wait(1):
            return {}
        playback = client.playback_state()
        item = playback.get("item") or {}
        if item.get("id") != previous_track_id:
            return playback
    raise RuntimeError("Spotify accepted the skip but still reports the same track")


def _fade(
    client: SpotifyClient,
    start_volume: int,
    end_volume: int,
    duration: float,
    device_id: str,
    stop_event: threading.Event,
    interval_seconds: float,
) -> bool:
    if duration <= 0:
        client.set_volume(end_volume, device_id=device_id)
        return True
    interval_seconds = max(0.05, interval_seconds)
    steps = max(1, round(duration / interval_seconds))
    interval = duration / steps
    for step in range(1, steps + 1):
        progress = step / steps
        if steps == 5:
            # Party transition curve requested for a two-second fade:
            # out: 100, 90, 70, 40, 20, 0
            # in:    0, 20, 40, 70, 90, 100
            curve = (
                (0.10, 0.30, 0.60, 0.80, 1.00)
                if end_volume < start_volume
                else (0.20, 0.40, 0.70, 0.90, 1.00)
            )
            eased = curve[step - 1]
        else:
            eased = progress**3 * (progress * (progress * 6 - 15) + 10)
        volume = round(start_volume + (end_volume - start_volume) * eased)
        if stop_event.wait(interval):
            return False
        client.set_volume(volume, device_id=device_id)
    return True


def _restore_volume(
    client: SpotifyClient,
    original_volume: int,
    stop_event: threading.Event,
    attempts: int = 5,
) -> None:
    """Restore and verify volume on whichever Connect device is now active."""
    for attempt in range(1, attempts + 1):
        # Omitting device_id avoids targeting a stale pre-transition device.
        client.set_volume(original_volume)
        stop_event.wait(1)
        playback = client.playback_state()
        device = playback.get("device") or {}
        reported_volume = device.get("volume_percent")
        if reported_volume is not None and abs(int(reported_volume) - original_volume) <= 2:
            return
    print(
        f"Warning: Spotify did not confirm restoration to {original_volume}% "
        f"after {attempts} attempts."
    )
