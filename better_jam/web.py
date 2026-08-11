from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .spotify import SpotifyClient, track_info
from .sources import SongSource
from .sessions import DeviceSessions


STATIC_ROOT = Path(__file__).resolve().parent / "static"


def create_server(
    client: SpotifyClient,
    source: SongSource,
    sessions: DeviceSessions,
    on_queue_change: Callable[[], None] | None = None,
    websocket_public_url: str = "",
    host: str = "0.0.0.0",
    port: int = 8000,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/search":
                self._search(parsed.query)
                return
            if parsed.path == "/api/config":
                self._json({"websocket_url": websocket_public_url})
                return
            if parsed.path == "/api/queue":
                response = client.queue()
                device_id = self._device_id()
                queue = source.queued_tracks()
                for item in queue:
                    item["can_remove"] = item.get("requested_by_device") == device_id
                self._json(
                    {
                        "current": track_info(response["currently_playing"])
                        if response.get("currently_playing") else None,
                        # Next up contains only requests accepted through the
                        # local/Wi-Fi Better Jam service. Spotify context and
                        # autoplay items are intentionally excluded.
                        "queue": queue,
                        "device_id": device_id,
                    }
                )
                return
            if parsed.path == "/api/history":
                self._json({"tracks": source.history()})
                return
            if parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                return
            self._static(parsed.path)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            prefix = "/api/queue/"
            if not parsed.path.startswith(prefix):
                self.send_error(404)
                return
            try:
                request_id = int(parsed.path[len(prefix):])
            except ValueError:
                self._json({"error": "Invalid queue request ID"}, status=400)
                return
            device_id = self._device_id()
            if not source.delete_owned_queued(request_id, device_id):
                self._json(
                    {"error": "You can only remove your own queued song"},
                    status=403,
                )
                return
            if on_queue_change:
                on_queue_change()
            print(f"Removed queue request {request_id} by device {device_id}")
            self._json({"ok": True, "request_id": request_id})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/queue":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 100_000:
                    raise ValueError("Invalid request size")
                track = json.loads(self.rfile.read(length))
                required = ("id", "uri", "name", "artists")
                if not all(track.get(field) for field in required):
                    raise ValueError("Missing track metadata")
                device_id = self._device_id()
                track["requested_by_device"] = device_id
                try:
                    # A success response now means Spotify itself accepted the
                    # track, rather than merely saving a database request.
                    client.add_to_queue(track["uri"])
                    source.delete_pending_track(track["id"])
                    source.record_queued(track)
                    if on_queue_change:
                        on_queue_change()
                    print(f"Queued by device {device_id}: {track['name']}")
                    self._json(
                        {"ok": True, "queued": True, "device_id": device_id},
                        status=201,
                    )
                except (urllib.error.HTTPError, OSError) as spotify_error:
                    # Preserve one request for the background worker to retry.
                    request_id = source.enqueue(track)
                    if on_queue_change:
                        on_queue_change()
                    self._json(
                        {
                            "ok": True,
                            "queued": False,
                            "request_id": request_id,
                            "message": "Saved as pending; Spotify will retry it",
                            "device_id": device_id,
                        },
                        status=202,
                    )
            except (ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, status=400)

        def _search(self, query_string: str) -> None:
            query = urllib.parse.parse_qs(query_string).get("q", [""])[0].strip()
            if len(query) < 2:
                self._json({"tracks": []})
                return
            try:
                tracks = [track_info(item) for item in client.search(query, limit=10)]
                self._json({"tracks": tracks})
            except urllib.error.HTTPError as error:
                detail = error.read().decode(errors="replace")
                self._json(
                    {"error": f"Spotify search failed ({error.code})", "detail": detail},
                    status=error.code,
                )
            except OSError as error:
                self._json({"error": f"Spotify search failed: {error}"}, status=502)

        def _static(self, path: str) -> None:
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
                self.send_error(404)
                return
            body = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: dict, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self._send_session_cookie()
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            request_line = str(args[0]) if args else ""
            status = str(args[1]) if len(args) > 1 else ""
            is_static_success = (
                request_line.startswith("GET ")
                and any(
                    request_line.split(" ", 2)[1].endswith(extension)
                    for extension in (".css", ".js")
                )
                and status.startswith(("2", "3"))
            )
            if is_static_success:
                return
            if request_line.startswith(("GET / HTTP/", "GET /favicon.ico HTTP/")):
                return
            if request_line.startswith(
                (
                    "GET /api/queue ",
                    "GET /api/history ",
                    "GET /api/search?",
                    "POST /api/queue ",
                    "DELETE /api/queue/",
                )
            ):
                return
            print(f"Web: {format % args}")

        def _device_id(self) -> str:
            device_id, token, is_new = sessions.resolve(self.headers.get("Cookie"))
            if is_new:
                self._new_session_token = token
            return device_id

        def _send_session_cookie(self) -> None:
            token = getattr(self, "_new_session_token", None)
            if token:
                self.send_header("Set-Cookie", sessions.set_cookie_header(token))

    return ThreadingHTTPServer((host, port), Handler)
