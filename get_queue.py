"""Print the current user's Spotify queue.

First add http://127.0.0.1:8888/callback as a redirect URI in your Spotify
app settings. The first run opens a browser for authorization; later runs use
the refresh token cached in .spotify_token.json.
"""

from libraries import *

ROOT = Path(__file__).resolve().parent
TOKEN_FILE = ROOT / ".spotify_token.json"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-playback-state user-modify-playback-state"


def load_env() -> None:
    """Load simple KEY=VALUE entries from .env without an extra dependency."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def post_form(url: str, fields: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def save_token(token: dict) -> dict:
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def authorize(client_id: str) -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)

    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (method name required by HTTPServer)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            for key in ("code", "state", "error"):
                if key in query:
                    result[key] = query[key][0]
            body = b"Spotify authorization received. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    print("Opening Spotify authorization in your browser...")
    print(f"If it does not open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)
    server = HTTPServer(("127.0.0.1", 8888), CallbackHandler)
    server.timeout = 180
    server.handle_request()
    server.server_close()

    if result.get("error"):
        raise RuntimeError(f"Spotify authorization failed: {result['error']}")
    if not result.get("code") or result.get("state") != state:
        raise RuntimeError("No valid Spotify authorization callback was received.")

    token = post_form(
        "https://accounts.spotify.com/api/token",
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    return save_token(token)


def get_access_token(client_id: str) -> str:
    token: dict = {}
    if TOKEN_FILE.exists():
        try:
            token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    requested_scopes = set(SCOPE.split())
    granted_scopes = set(token.get("scope", "").split())
    if not requested_scopes.issubset(granted_scopes):
        # A refresh token cannot acquire permissions that were not originally
        # granted, so send the user through authorization again.
        token = {}

    if token.get("access_token") and token.get("expires_at", 0) > time.time() + 30:
        return token["access_token"]

    if token.get("refresh_token"):
        refreshed = post_form(
            "https://accounts.spotify.com/api/token",
            {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
            },
        )
        refreshed.setdefault("refresh_token", token["refresh_token"])
        return save_token(refreshed)["access_token"]

    return authorize(client_id)["access_token"]


def get_queue(access_token: str) -> dict:
    request = urllib.request.Request(
        "https://api.spotify.com/v1/me/player/queue",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def describe_item(item: dict | None) -> str:
    if not item:
        return "Nothing playing"
    artists = ", ".join(artist["name"] for artist in item.get("artists", []))
    name = item.get("name", "Unknown")
    return f"{name}, {artists}" if artists else name


def format_duration(duration_ms: int | None) -> str | None:
    if duration_ms is None:
        return None
    minutes, seconds = divmod(duration_ms // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def track_info(item: dict) -> dict:
    """Extract useful fields from a Spotify track object."""
    album = item.get("album") or {}
    return {
        "name": item.get("name"),
        "artists": [artist.get("name") for artist in item.get("artists", [])],
        "album": album.get("name"),
        "duration_ms": item.get("duration_ms"),
        "duration": format_duration(item.get("duration_ms")),
        "explicit": item.get("explicit", False),
        "id": item.get("id"),
        "uri": item.get("uri"),
        "url": (item.get("external_urls") or {}).get("spotify"),
    }


# def print_track(item: dict, heading: str) -> None:
#     info = track_info(item)
#     print(heading)
#     print(f"  Track:    {info['name']}")
#     print(f"  Artist:   {', '.join(info['artists'])}")
#     print(f"  Album:    {info['album']}")
#     print(f"  Duration: {info['duration']}")
#     print(f"  Explicit: {'yes' if info['explicit'] else 'no'}")
#     print(f"  ID:       {info['id']}")
#     print(f"  URI:      {info['uri']}")
#     print(f"  URL:      {info['url']}")




    # if current:
    #     print_track(current, "Now playing")
    # else:
    #     print("Now playing: nothing")

    # if not items:
    #     print("\nQueue is empty.")
    #     return 0

    # print("\nUp next")
    # for number, item in enumerate(items, 1):
    #     print_track(item, f"\n{number}.")
    # return 0

    
