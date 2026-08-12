"""Small dependency-free WebSocket broadcaster for Better Jam state."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
import urllib.error

from .sources import SongSource
from .spotify import SpotifyClient, rate_limit_message, track_info
from .sessions import DeviceSessions


class _Client:
    def __init__(self, connection: socket.socket, device_id: str) -> None:
        self.connection = connection
        self.device_id = device_id
        self.lock = threading.Lock()

    def send(self, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(payload) < 126:
            header = bytes((0x81, len(payload)))
        elif len(payload) < 65_536:
            header = bytes((0x81, 126)) + struct.pack("!H", len(payload))
        else:
            header = bytes((0x81, 127)) + struct.pack("!Q", len(payload))
        with self.lock:
            self.connection.sendall(header + payload)


class WebSocketHub:
    def __init__(
        self,
        spotify: SpotifyClient,
        source: SongSource,
        sessions: DeviceSessions,
        host: str = "0.0.0.0",
        port: int = 8788,
        poll_seconds: float = 2,
    ) -> None:
        self.spotify = spotify
        self.source = source
        self.sessions = sessions
        self.host = host
        self.port = port
        self.poll_seconds = poll_seconds
        self.stop_event = threading.Event()
        self.clients: set[_Client] = set()
        self.clients_lock = threading.Lock()
        self.latest_state: dict | None = None
        self.latest_serialized = ""
        hub = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                client = hub._handshake(self.request)
                if client is None:
                    return
                with hub.clients_lock:
                    hub.clients.add(client)
                print(f"WebSocket connected: {client.device_id}")
                try:
                    client.send({"type": "connected", "device_id": client.device_id})
                    if hub.latest_state:
                        client.send(hub.latest_state)
                    hub._read_until_closed(client)
                except OSError:
                    pass
                finally:
                    with hub.clients_lock:
                        hub.clients.discard(client)
                    print(f"WebSocket disconnected: {client.device_id}")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server((host, port), Handler)

    def start(self) -> None:
        threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
            name="websocket-server",
        ).start()
        threading.Thread(
            target=self._watch_state,
            daemon=True,
            name="websocket-state-watcher",
        ).start()

    def close(self) -> None:
        self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()

    def _watch_state(self) -> None:
        while not self.stop_event.is_set():
            try:
                response = self.spotify.playback_state()
                current = (
                    track_info(response["item"])
                    if response.get("item") else None
                )
                self.publish_state(current=current)
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    print(rate_limit_message(error))
                else:
                    print(f"WebSocket state error: HTTP {error.code}")
            except (OSError, RuntimeError, ValueError) as error:
                print(f"WebSocket state error: {error}")
            self.stop_event.wait(self.poll_seconds)

    def publish_state(self, current: dict | None = None) -> None:
        """Broadcast database changes immediately without waiting on Spotify."""
        if current is None and self.latest_state is not None:
            current = self.latest_state.get("current")
        if current and current.get("id"):
            current = dict(current)
            attribution = self.source.playing_attribution(current["id"])
            if attribution:
                current.update(attribution)
        state = {
            "type": "state",
            "current": current,
            "queue": self.source.queued_tracks(),
            "history": self.source.history(),
        }
        serialized = json.dumps(state, sort_keys=True, ensure_ascii=False)
        if serialized != self.latest_serialized:
            self.latest_state = state
            self.latest_serialized = serialized
            self._broadcast(state)

    def _broadcast(self, value: dict) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            try:
                client.send(value)
            except OSError:
                with self.clients_lock:
                    self.clients.discard(client)

    def _handshake(self, connection: socket.socket) -> _Client | None:
        request = b""
        while b"\r\n\r\n" not in request and len(request) < 16_384:
            chunk = connection.recv(4096)
            if not chunk:
                return None
            request += chunk
        lines = request.decode("latin-1").split("\r\n")
        request_line = lines[0].split()
        if len(request_line) < 2:
            return None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            return None
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        device_id, session_token, is_new = self.sessions.resolve(headers.get("cookie"))
        cookie_header = (
            "Set-Cookie: "
            + self.sessions.set_cookie_header(
                session_token,
                secure=headers.get("x-forwarded-proto", "")
                .split(",", 1)[0].strip().lower() == "https",
            )
            + "\r\n"
            if is_new else ""
        )
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n{cookie_header}\r\n"
            ).encode()
        )
        return _Client(connection, device_id)

    def _read_until_closed(self, client: _Client) -> None:
        while not self.stop_event.is_set():
            header = _receive_exact(client.connection, 2)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", _receive_exact(client.connection, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _receive_exact(client.connection, 8))[0]
            mask = _receive_exact(client.connection, 4) if masked else b""
            payload = _receive_exact(client.connection, length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                with client.lock:
                    client.connection.sendall(bytes((0x8A, len(payload))) + payload)
            if opcode == 0x1:
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if message.get("type") == "refresh":
                    self.publish_state()


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise ConnectionError("WebSocket closed")
        data += chunk
    return data
