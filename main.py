"""Start the continuous Spotify queue worker."""

import threading
import socket

from better_jam.auth import get_access_token
from better_jam.config import PROJECT_ROOT, client_id, float_setting
from better_jam.sources import SQLiteSongSource
from better_jam.spotify import SpotifyClient
from better_jam.worker import QueueWorker
from better_jam.web import create_server
 

def main() -> None:
    spotify = SpotifyClient(get_access_token(client_id()))
    songs = SQLiteSongSource(PROJECT_ROOT / "queue.db")
    worker = QueueWorker(
        spotify,
        songs,
        excerpt_seconds=float_setting("EXCERPT_SECONDS", 45),
        fade_seconds=float_setting("FADE_SECONDS", 3),
    )
    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=worker.run_forever,
        args=(stop_event,),
        daemon=True,
        name="spotify-queue-worker",
    )
    worker_thread.start()

    web_port = int(float_setting("WEB_PORT", 8787))
    server = create_server(spotify, songs, port=web_port)
    print(f"Website available on this computer at http://127.0.0.1:{web_port}")
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
        server.server_close()


if __name__ == "__main__":
    main()
