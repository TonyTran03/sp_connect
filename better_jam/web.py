from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .spotify import SpotifyClient, track_info
from .sources import SongSource


STATIC_ROOT = Path(__file__).resolve().parent / "static"


def create_server(
    client: SpotifyClient,
    source: SongSource,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/search":
                self._search(parsed.query)
                return
            if parsed.path == "/api/queue":
                response = client.queue()
                self._json(
                    {
                        "current": track_info(response["currently_playing"])
                        if response.get("currently_playing") else None,
                        # Next up contains only requests accepted through the
                        # local/Wi-Fi Better Jam service. Spotify context and
                        # autoplay items are intentionally excluded.
                        "queue": source.queued_tracks(),
                    }
                )
                return
            if parsed.path == "/api/history":
                self._json({"tracks": source.history()})
                return
            self._static(parsed.path)

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
                try:
                    # A success response now means Spotify itself accepted the
                    # track, rather than merely saving a database request.
                    client.add_to_queue(track["uri"])
                    source.delete_pending_track(track["id"])
                    source.record_queued(track)
                    self._json({"ok": True, "queued": True}, status=201)
                except (urllib.error.HTTPError, OSError) as spotify_error:
                    # Preserve one request for the background worker to retry.
                    request_id = source.enqueue(track)
                    self._json(
                        {
                            "ok": True,
                            "queued": False,
                            "request_id": request_id,
                            "message": "Saved as pending; Spotify will retry it",
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
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            print(f"Web: {format % args}")

    return ThreadingHTTPServer((host, port), Handler)
