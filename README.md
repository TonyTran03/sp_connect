# Better Jam

Better Jam starts near the middle of each song and plays it for a short time so
everyone gets a turn on aux. People on the same local network can search
Spotify, add a song to the shared queue, and follow what is playing next.

## Run

Set `SPOTIFY_CLIENT_ID` in `.env`, register
`http://127.0.0.1:8888/callback` as the Spotify app redirect URI, start playback
on a Spotify device, then run `main.py` from an IDE or as a background process.
No command-line arguments are required.

The website uses port `8787`; live WebSocket updates use port `8788`. Allow both
ports through the host firewall for phones on the same Wi-Fi. Each browser keeps
a persistent device ID in local storage so later queue ownership/removal can be
tied to the device that submitted a request.

For a public tunnel, copy `.env.example` to `.env` and set the public origin:

```env
PUBLIC_URL=https://
PUBLIC_WEBSOCKET_URL=wss:
```

Route ordinary traffic to `http://localhost:8787` and `/ws` WebSocket traffic
to `http://localhost:8788` through the tunnel. Guests never connect directly to
either local port.

Optional playback settings also belong in `.env`:

```env
EXCERPT_SECONDS=45
FADE_SECONDS=3
WEB_PORT=8787
```

The default setup creates `queue.db`. Add requests from another application:

```sql
INSERT INTO song_requests (title, artist)
VALUES ('Blinding Lights', 'The Weeknd');
```

Guest requests are accepted locally first and broadcast immediately. The queue
feeder submits at most one track per poll until five tracks are accepted ahead
by Spotify:

```text
pending -> queued in Spotify -> playing -> played history
        \-> failed (kept for inspection when Spotify rejects it)
```

## Package structure

```text
main.py
better_jam/
  auth.py             Spotify PKCE authorization
  config.py           environment and file locations
  hook_detector.py    repetition-first hook detection
  playback.py         basic excerpt playback helper
  realtime.py         WebSocket device connections and state broadcasts
  sources.py          database interface and SQLite adapter
  spotify.py          Spotify Web API client
  worker.py           continuous queue orchestration
```

Better Jam's database is the source of truth for guest requests while Spotify
holds only a small playback buffer. To use Postgres, Supabase, or another
external database, implement the
`SongSource` protocol in `sources.py` and pass that implementation to
`QueueWorker` in `main.py`. Excerpts use the middle of each track without calling
Spotify Audio Analysis.
