from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import REDIRECT_URI, SCOPES, TOKEN_FILE


def _post_form(url: str, fields: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _save_token(token: dict, app_client_id: str) -> dict:
    token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3600))
    # Refresh tokens are issued to one Spotify application. Remembering the
    # client prevents an opaque HTTP 400 after SPOTIFY_CLIENT_ID is changed.
    token["client_id"] = app_client_id
    TOKEN_FILE.write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def _authorize(app_client_id: str) -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": app_client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
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

    return _save_token(
        _post_form(
            "https://accounts.spotify.com/api/token",
            {
                "client_id": app_client_id,
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        ),
        app_client_id,
    )


def get_access_token(app_client_id: str, force_refresh: bool = False) -> str:
    token: dict = {}
    if TOKEN_FILE.exists():
        try:
            token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    if token.get("client_id") and token["client_id"] != app_client_id:
        token = {}
    if not set(SCOPES.split()).issubset(set(token.get("scope", "").split())):
        token = {}
    if (
        not force_refresh
        and token.get("access_token")
        and token.get("expires_at", 0) > time.time() + 30
    ):
        return token["access_token"]
    if token.get("refresh_token"):
        try:
            refreshed = _post_form(
                "https://accounts.spotify.com/api/token",
                {
                    "client_id": app_client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                },
            )
        except urllib.error.HTTPError as error:
            if error.code not in {400, 401}:
                raise
            print("Saved Spotify authorization is no longer valid; signing in again...")
        else:
            refreshed.setdefault("refresh_token", token["refresh_token"])
            refreshed.setdefault("scope", token.get("scope", ""))
            return _save_token(refreshed, app_client_id)["access_token"]
    return _authorize(app_client_id)["access_token"]

