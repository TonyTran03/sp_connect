
import time
import urllib.parse
import urllib.request

def next_track(access_token: str) -> None:
    request = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/next",
        method = "POST",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=20):
        pass



def seek_track(access_token: str, position_seconds: float) -> float:
    """Seek and return the monotonic time when Spotify accepted the request."""
    if position_seconds < 0:
        raise ValueError("position_seconds cannot be negative")

    position_ms = int(position_seconds * 1000)
    url = (
        "https://api.spotify.com/v1/me/player/seek?"
        + urllib.parse.urlencode({"position_ms": position_ms})
    )

    request = urllib.request.Request(
        url,
        method="PUT",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    with urllib.request.urlopen(request, timeout=20):
        pass

    return time.monotonic()


def play_start_then_skip(access_token: str, start: float, end: float) -> None:
    """Play the current track from start until end, then skip to the next track."""
    if end <= start:
        raise ValueError("end must be greater than start")

    seek_accepted_at = seek_track(access_token, start)
    deadline = seek_accepted_at + (end - start)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 0.25))

    next_track(access_token)
