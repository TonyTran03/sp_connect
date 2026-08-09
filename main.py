
from libraries import *


from get_queue import get_access_token, get_queue, load_env, track_info

from skip_to_position import *

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["queue", "next", "seek"],
        nargs="?",
        default="queue",
    )
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("position", type=float, nargs="?")
    args = parser.parse_args()

    load_env()
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    if not client_id:
        print("Set SPOTIFY_CLIENT_ID in .env first.", file=sys.stderr)
        return 1

    try:
        access_token = get_access_token(client_id)

        if args.command == "next":
            next_track(access_token)
            print("Skipped to the next track.")
            return 0

        if args.command == "seek":
            if args.position is None:
                parser.error("seek requires a position in seconds")

            seek_track(access_token, args.position)
            print(f"Jumped to {args.position:g} seconds.")
            return 0

        queue = get_queue(access_token)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        print(f"Spotify API error ({error.code}): {detail}", file=sys.stderr)
        return 1

    current = queue.get("currently_playing")
    items = queue.get("queue", [])

    if args.json:
        print(
            json.dumps(
                {
                    "currently_playing": track_info(current) if current else None,
                    "queue": [track_info(item) for item in items],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
