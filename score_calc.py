# calculating the score of a song given the sound and loudness


def find_best_window(analysis, window_seconds=45):
    segments = analysis["segments"]
    best = None

    for segment in segments:
        start = segment["start"]
        end = start + window_seconds

        window = [
            s for s in segments
            if start <= s["start"] < end
        ]

        if not window:
            continue

        average_loudness = sum(s["loudness_max"] for s in window) / len(window)
        onset_density = len(window) / window_seconds
        confidence = sum(s["confidence"] for s in window) / len(window)

        score = (
            average_loudness * 0.5
            + onset_density * 10
            + confidence * 5
        )

        if best is None or score > best["score"]:
            best = {
                "start": start,
                "end": end,
                "score": score,
            }

    return best