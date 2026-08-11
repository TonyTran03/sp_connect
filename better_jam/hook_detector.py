"""Find a likely hook by detecting repeated, beat-aligned musical phrases."""

from __future__ import annotations

import math
from statistics import fmean, median


def _overlap(item: dict, start: float, end: float) -> float:
    item_start = float(item.get("start", 0))
    item_end = item_start + float(item.get("duration", 0))
    return max(0.0, min(item_end, end) - max(item_start, start))


def _mean_vector(segments: list[dict], start: float, end: float, field: str, size: int) -> list[float]:
    relevant = [segment for segment in segments if _overlap(segment, start, end)]
    if not relevant:
        return [0.0] * size
    result = []
    for index in range(size):
        values = [segment.get(field, [0.0] * size)[index] for segment in relevant]
        result.append(fmean(values))
    return result


def _standardize(vectors: list[list[float]]) -> list[list[float]]:
    if not vectors:
        return []
    columns = list(zip(*vectors))
    centers = [fmean(column) for column in columns]
    scales = []
    for column, center in zip(columns, centers):
        variance = fmean((value - center) ** 2 for value in column)
        scales.append(math.sqrt(variance) or 1.0)
    return [
        [(value - center) / scale for value, center, scale in zip(vector, centers, scales)]
        for vector in vectors
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    left_length = math.sqrt(sum(value * value for value in left))
    right_length = math.sqrt(sum(value * value for value in right))
    if not left_length or not right_length:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_length * right_length)


def _phrase_similarity(left: dict, right: dict) -> float:
    similarities = [_cosine(a, b) for a, b in zip(left["bars"], right["bars"])]
    return fmean(similarities)


def _build_phrases(analysis: dict, bars_per_phrase: int) -> list[dict]:
    bars = analysis.get("bars", [])
    segments = analysis.get("segments", [])
    phrases = []
    for index in range(0, len(bars) - bars_per_phrase + 1, bars_per_phrase):
        phrase_bars = bars[index : index + bars_per_phrase]
        descriptors = []
        for bar in phrase_bars:
            start = float(bar["start"])
            end = start + float(bar["duration"])
            descriptors.append(
                _mean_vector(segments, start, end, "pitches", 12)
                + _mean_vector(segments, start, end, "timbre", 6)
            )
        phrases.append(
            {
                "start": float(phrase_bars[0]["start"]),
                "end": float(phrase_bars[-1]["start"]) + float(phrase_bars[-1]["duration"]),
                "bars": descriptors,
            }
        )

    all_bar_vectors = [bar for phrase in phrases for bar in phrase["bars"]]
    standardized = iter(_standardize(all_bar_vectors))
    for phrase in phrases:
        phrase["bars"] = [next(standardized) for _ in phrase["bars"]]
    return phrases


def detect_hook(analysis: dict, excerpt_seconds: float = 45, bars_per_phrase: int = 8) -> dict:
    """Return a repeated hook occurrence, or an explicit low-confidence fallback."""
    phrases = _build_phrases(analysis, bars_per_phrase)
    if not phrases:
        raise ValueError("analysis does not contain enough bars")

    pairs = []
    for left_index, left in enumerate(phrases):
        for right_index in range(left_index + 1, len(phrases)):
            # Adjacent phrases are often continuations, not genuine repetitions.
            if right_index == left_index + 1:
                continue
            pairs.append((left_index, right_index, _phrase_similarity(left, phrases[right_index])))

    if not pairs:
        return _fallback(phrases, analysis, excerpt_seconds, "not enough phrases to detect repetition")

    similarities = [pair[2] for pair in pairs]
    center = median(similarities)
    deviations = [abs(value - center) for value in similarities]
    threshold = center + 2.5 * median(deviations)
    highest = max(similarities)
    lowest = min(similarities)
    if threshold >= highest:
        # If one phrase occurs throughout much of the song, repeated matches
        # can themselves form the median. Separate the upper and lower modes.
        if highest - lowest < 0.1:
            return _fallback(phrases, analysis, excerpt_seconds, "no clear repeated phrase")
        threshold = (highest + lowest) / 2
    repeated_pairs = [pair for pair in pairs if pair[2] >= threshold]
    if not repeated_pairs:
        return _fallback(phrases, analysis, excerpt_seconds, "no clear repeated phrase")

    groups: list[set[int]] = []
    for left, right, _similarity in repeated_pairs:
        touching = [group for group in groups if left in group or right in group]
        if not touching:
            groups.append({left, right})
            continue
        merged = {left, right}
        for group in touching:
            merged.update(group)
            groups.remove(group)
        groups.append(merged)

    # Structural rule: the most frequently recurring phrase is the hook.
    # For ties, prefer the cluster covering the most separated song positions.
    group = max(groups, key=lambda item: (len(item), max(item) - min(item)))
    occurrence_index = max(group)  # Prefer the final, typically fullest reprise.
    phrase = phrases[occurrence_index]
    start, end = _expand(phrase["start"], phrase["end"], _duration(analysis), excerpt_seconds)
    matching_pairs = [pair for pair in repeated_pairs if pair[0] in group and pair[1] in group]
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "method": "repeated_phrase",
        "confidence": "high" if len(group) >= 3 else "medium",
        "occurrences": [round(phrases[index]["start"], 3) for index in sorted(group)],
        "similarity": round(fmean(pair[2] for pair in matching_pairs), 3),
    }


def _duration(analysis: dict) -> float:
    track_duration = analysis.get("track", {}).get("duration")
    if track_duration:
        return float(track_duration)
    bars = analysis.get("bars", [])
    return float(bars[-1]["start"]) + float(bars[-1]["duration"])


def _expand(start: float, end: float, duration: float, target: float) -> tuple[float, float]:
    extra = max(0.0, target - (end - start))
    start = max(0.0, start - extra / 2)
    end = min(duration, start + target)
    start = max(0.0, end - target)
    return start, end


def _fallback(phrases: list[dict], analysis: dict, target: float, reason: str) -> dict:
    # No claim of a best part: return a neutral middle excerpt and disclose why.
    duration = _duration(analysis)
    middle = duration / 2
    start, end = _expand(middle, middle, duration, target)
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "method": "middle_fallback",
        "confidence": "low",
        "reason": reason,
        "occurrences": [],
    }
