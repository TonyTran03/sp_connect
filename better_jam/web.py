from __future__ import annotations

import json
import mimetypes
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .spotify import SpotifyClient, rate_limit_message, track_info
from .sources import SongSource
from .sessions import DeviceSessions


STATIC_ROOT = Path(__file__).resolve().parent / "static"


def create_server(
    client: SpotifyClient,
    source: SongSource,
    sessions: DeviceSessions,
    on_queue_change: Callable[[], None] | None = None,
    current_track: Callable[[], dict | None] | None = None,
    websocket_public_url: str = "",
    host: str = "0.0.0.0",
    port: int = 8000,
) -> ThreadingHTTPServer:
    search_cache: dict[str, tuple[float, list[dict]]] = {}
    search_cache_lock = threading.Lock()
    search_inflight: dict[str, threading.Event] = {}
    # Track metadata changes rarely, so popular party searches can safely avoid
    # another Spotify request for the duration of an event.
    search_cache_seconds = 3_600

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
                device_id = self._device_id()
                queue = source.queued_tracks()
                for item in queue:
                    item["can_remove"] = item.get("requested_by_device") == device_id
                self._json(
                    {
                        "current": current_track() if current_track else None,
                        # Next up contains only requests accepted through the
                        # local/Wi-Fi Better Jam service. Spotify context and
                        # autoplay items are intentionally excluded.
                        "queue": queue,
                        "device_id": device_id,
                        "display_name": source.display_name(device_id) or "",
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

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/api/profile":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                display_name = " ".join(str(body.get("display_name", "")).split())
                if not 1 <= len(display_name) <= 24:
                    raise ValueError("Name must be between 1 and 24 characters")
                if not display_name.isprintable():
                    raise ValueError("Name contains unsupported characters")
                device_id = self._device_id()
                source.set_display_name(device_id, display_name)
                if on_queue_change:
                    on_queue_change()
                self._json({"ok": True, "display_name": display_name})
            except (ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, status=400)

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
                ban_remaining = source.ban_remaining_seconds(device_id)
                if ban_remaining > 0:
                    minutes, seconds = divmod(ban_remaining, 60)
                    wait = (
                        f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
                    )
                    self._json(
                        {"error": f"Queue cooldown active. Try again in {wait}."},
                        status=403,
                    )
                    return
                track["requested_by_device"] = device_id
                submitted_name = " ".join(
                    str(track.get("requested_by_name", "")).split()
                )
                if 1 <= len(submitted_name) <= 24 and submitted_name.isprintable():
                    track["requested_by_name"] = submitted_name
                    source.set_display_name(device_id, submitted_name)
                else:
                    track["requested_by_name"] = source.display_name(device_id)
                # Accept instantly into Better Jam. The worker submits at most
                # one request per poll and keeps only a small Spotify buffer.
                request_id = source.enqueue(track)
                if on_queue_change:
                    on_queue_change()
                print(f"Requested by device {device_id}: {track['name']}")
                self._json(
                    {
                        "ok": True,
                        "queued": True,
                        "request_id": request_id,
                        "message": f"{track['name']} added to Next up",
                        "device_id": device_id,
                    },
                    status=201,
                )
            except (ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, status=400)

        def _search(self, query_string: str) -> None:
            query = urllib.parse.parse_qs(query_string).get("q", [""])[0].strip()
            if len(query) < 2:
                self._json({"tracks": []})
                return
            cache_key = " ".join(query.casefold().split())
            with search_cache_lock:
                cached = search_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < search_cache_seconds:
                self._json({"tracks": cached[1]})
                return
            with search_cache_lock:
                search_finished = search_inflight.get(cache_key)
                owns_search = search_finished is None
                if owns_search:
                    search_finished = threading.Event()
                    search_inflight[cache_key] = search_finished
            if not owns_search:
                # If several guests search for the same song at once, only the
                # first request reaches Spotify. The others reuse its result.
                search_finished.wait(20)
                with search_cache_lock:
                    cached = search_cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < search_cache_seconds:
                    self._json({"tracks": cached[1]})
                else:
                    self._json({"error": "Search temporarily unavailable"}, status=503)
                return
            try:
                tracks = [track_info(item) for item in client.search(query, limit=10)]
                with search_cache_lock:
                    if len(search_cache) >= 2_000:
                        oldest = min(search_cache, key=lambda key: search_cache[key][0])
                        search_cache.pop(oldest, None)
                    search_cache[cache_key] = (time.monotonic(), tracks)
                self._json({"tracks": tracks})
            except urllib.error.HTTPError as error:
                detail = getattr(error, "spotify_error_detail", None)
                if detail is None:
                    detail = error.read().decode(errors="replace")
                self._json(
                    {
                        "error": rate_limit_message(error)
                        if error.code == 429
                        else f"Spotify search failed ({error.code})",
                        "detail": detail,
                    },
                    status=error.code,
                )
            except OSError as error:
                self._json({"error": f"Spotify search failed: {error}"}, status=502)
            finally:
                with search_cache_lock:
                    finished = search_inflight.pop(cache_key, None)
                    if finished:
                        finished.set()

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
                    "GET /api/config ",
                    "GET /api/search?",
                    "POST /api/queue ",
                    "DELETE /api/queue/",
                    "PUT /api/profile ",
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
                forwarded_proto = self.headers.get("X-Forwarded-Proto", "")
                is_https = forwarded_proto.split(",", 1)[0].strip().lower() == "https"
                self.send_header(
                    "Set-Cookie", sessions.set_cookie_header(token, secure=is_https)
                )

    return ThreadingHTTPServer((host, port), Handler)
