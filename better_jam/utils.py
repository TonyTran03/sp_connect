"""Easy-to-edit behavior settings for Better Jam.

Keep future party rules here too, such as banned Spotify track IDs or artists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JamSettings:
    excerpt_seconds: float = 55
    poll_seconds: float = 2
    websocket_port: int = 8788

    fade_out_seconds: float = 2
    fade_in_seconds: float = 2
    volume_interval_seconds: float = 0.20

    # None means return to the volume Spotify reported before the fade began.
    # Set this to a number from 0 to 100 to always fade back to a fixed volume.
    fade_in_target_volume: int | None = None
    restore_original_volume: bool = True

    # Reserved party controls. Add Spotify track IDs (not full URLs) or artist
    # names here when you are ready to enforce bans.
    banned_track_ids: frozenset[str] = frozenset()
    banned_artists: frozenset[str] = frozenset()

    def target_volume(self, starting_volume: int) -> int:
        target = (
            starting_volume
            if self.fade_in_target_volume is None
            else self.fade_in_target_volume
        )
        return max(0, min(100, int(target)))


# This is the one object to edit when changing how the jam behaves.
SETTINGS = JamSettings()
