# Better Jam

Better Jam is a continuously running Spotify queue worker. Other applications
write song requests to a database; the worker finds an exact Spotify match,
adds it to the real Spotify queue, detects its repeated hook, plays a 45-second
excerpt, and advances to the next track. Tracks added manually in Spotify are
also processed when they become the current track.

## Run

Set `SPOTIFY_CLIENT_ID` in `.env`, register
`http://127.0.0.1:8888/callback` as the Spotify app redirect URI, start playback
on a Spotify device, then run `main.py` from an IDE or as a background process.
No command-line arguments are required.

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

Rows exist only while waiting to be submitted:

```text
pending -> accepted by Spotify -> row deleted
        \-> failed (kept for inspection when no exact match exists)
```

## Package structure

```text
main.py
better_jam/
  auth.py             Spotify PKCE authorization
  config.py           environment and file locations
  hook_detector.py    repetition-first hook detection
  playback.py         basic excerpt playback helper
  sources.py          database interface and SQLite adapter
  spotify.py          Spotify Web API client
  worker.py           continuous queue orchestration
```

Spotify's queue is the sole source of truth for playback order. To use Postgres,
Supabase, or another external database, implement the
`SongSource` protocol in `sources.py` and pass that implementation to
`QueueWorker` in `main.py`. Spotify Audio Analysis is restricted for many newer
developer applications. On a 403, the worker remains running and uses a neutral
middle excerpt because structural hook detection is not possible without the
analysis data.
