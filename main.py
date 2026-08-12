"""Start the continuous Spotify queue worker."""

import threading
import socket

from better_jam.auth import get_access_token
from better_jam.config import PROJECT_ROOT, client_id, float_setting, string_setting
from better_jam.sources import SQLiteSongSource
from better_jam.spotify import SpotifyClient
from better_jam.worker import QueueWorker
from better_jam.web import create_server
from better_jam.utils import SETTINGS
from better_jam.realtime import WebSocketHub
from better_jam.sessions import DeviceSessions
 

def main() -> None:
    spotify = SpotifyClient(get_access_token(client_id()))
    songs = SQLiteSongSource(PROJECT_ROOT / "queue.db")
    public_url = string_setting("PUBLIC_URL")
    websocket_public_url = string_setting("PUBLIC_WEBSOCKET_URL")
    if not websocket_public_url and public_url:
        websocket_public_url = (
            public_url.replace("https://", "wss://", 1)
            .replace("http://", "ws://", 1)
            .rstrip("/") + "/ws"
        )
    sessions = DeviceSessions(
        PROJECT_ROOT / ".device_secret",
        secure_cookie=public_url.startswith("https://"),
    )
    worker = QueueWorker(
        spotify,
        songs,
        excerpt_seconds=SETTINGS.excerpt_seconds,
        poll_seconds=SETTINGS.poll_seconds,
        fade_out_seconds=SETTINGS.fade_out_seconds,
        fade_in_seconds=SETTINGS.fade_in_seconds,
        volume_interval_seconds=SETTINGS.volume_interval_seconds,
        fade_in_target_volume=SETTINGS.fade_in_target_volume,
        restore_original_volume=SETTINGS.restore_original_volume,
        queue_ahead=SETTINGS.spotify_queue_ahead,
    )
    stop_event = threading.Event()
    realtime = WebSocketHub(
        spotify,
        songs,
        sessions,
        port=SETTINGS.websocket_port,
        poll_seconds=SETTINGS.poll_seconds,
    )
    realtime.start()
    worker_thread = threading.Thread(
        target=worker.run_forever,
        args=(stop_event,),
        daemon=True,
        name="spotify-queue-worker",
    )
    worker_thread.start()

    web_port = int(float_setting("WEB_PORT", 8787))
    server = create_server(
        spotify,
        songs,
        sessions,
        on_queue_change=realtime.publish_state,
        current_track=lambda: (
            realtime.latest_state.get("current") if realtime.latest_state else None
        ),
        websocket_public_url=websocket_public_url,
        port=web_port,
    )
    print(f"Website available on this computer at http://127.0.0.1:{web_port}")
    print(f"Live updates available on WebSocket port {SETTINGS.websocket_port}")
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
        print(f"Other devices on this Wi-Fi can try http://{local_ip}:{web_port}")
    except OSError:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping Better Jam...")
    finally:
        stop_event.set()
        realtime.close()
        server.server_close()


if __name__ == "__main__":
    main()
