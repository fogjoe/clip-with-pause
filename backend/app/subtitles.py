from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SENTENCE_END_RE = re.compile(r"[.!?\u3002\uff01\uff1f]+[\"')\]}]*$")
SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SubtitleFragment:
    start: float
    end: float
    text: str


def load_json3(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as subtitle_file:
        data = json.load(subtitle_file)
    if not isinstance(data, dict):
        raise ValueError("Subtitle file did not contain a JSON object.")
    return data


def extract_sentences(
    data: dict[str, Any],
    clip_start: float,
    clip_end: float,
    max_end_extension: float = 0,
) -> list[dict[str, Any]]:
    search_end = clip_end + max(0, max_end_extension)
    fragments = _extract_fragments(data, clip_start, search_end)
    sentences = _group_fragments_into_sentences(fragments, clip_start)
    requested_duration = clip_end - clip_start
    return [sentence for sentence in sentences if sentence["start"] < requested_duration]


def _extract_fragments(data: dict[str, Any], clip_start: float, clip_end: float) -> list[SubtitleFragment]:
    events = data.get("events", [])
    if not isinstance(events, list):
        return []

    raw_fragments: list[SubtitleFragment] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue

        start_ms = event.get("tStartMs")
        if not isinstance(start_ms, int | float):
            continue

        text = _event_text(event)
        if not text:
            continue

        duration_ms = event.get("dDurationMs")
        if isinstance(duration_ms, int | float) and duration_ms > 0:
            end_seconds = (start_ms + duration_ms) / 1000
        else:
            end_seconds = _next_event_start(events, index, start_ms) / 1000

        start_seconds = start_ms / 1000
        if end_seconds <= clip_start or start_seconds >= clip_end:
            continue

        clipped_start = max(start_seconds, clip_start)
        clipped_end = min(end_seconds, clip_end)
        if clipped_end <= clipped_start:
            continue

        raw_fragments.append(
            SubtitleFragment(
                start=clipped_start,
                end=clipped_end,
                text=text,
            )
        )

    return sorted(raw_fragments, key=lambda fragment: fragment.start)


def _event_text(event: dict[str, Any]) -> str:
    segments = event.get("segs", [])
    if not isinstance(segments, list):
        return ""

    text = "".join(
        str(segment.get("utf8", ""))
        for segment in segments
        if isinstance(segment, dict)
    )
    text = text.replace("\n", " ")
    return SPACE_RE.sub(" ", text).strip()


def _next_event_start(events: list[Any], current_index: int, fallback_start_ms: float) -> float:
    for next_event in events[current_index + 1 :]:
        if isinstance(next_event, dict):
            next_start = next_event.get("tStartMs")
            if isinstance(next_start, int | float) and next_start > fallback_start_ms:
                return next_start
    return fallback_start_ms + 1500


def _group_fragments_into_sentences(
    fragments: list[SubtitleFragment],
    clip_start: float,
) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    buffer: list[str] = []
    sentence_start: float | None = None
    sentence_end: float | None = None
    last_end: float | None = None

    def flush() -> None:
        nonlocal buffer, sentence_start, sentence_end, last_end
        if sentence_start is None or sentence_end is None or not buffer:
            return

        text = _join_caption_parts(buffer)
        if text:
            sentences.append(
                {
                    "id": len(sentences),
                    "start": round(max(0, sentence_start - clip_start), 3),
                    "end": round(max(0, sentence_end - clip_start), 3),
                    "text": text,
                }
            )

        buffer = []
        sentence_start = None
        sentence_end = None
        last_end = None

    for fragment in fragments:
        if last_end is not None and fragment.start - last_end > 1.25:
            flush()

        if sentence_start is None:
            sentence_start = fragment.start

        sentence_end = fragment.end if sentence_end is None else max(sentence_end, fragment.end)
        buffer.append(fragment.text)
        last_end = fragment.end

        if SENTENCE_END_RE.search(fragment.text):
            flush()

    flush()
    return [sentence for sentence in sentences if sentence["end"] > sentence["start"]]


def _join_caption_parts(parts: list[str]) -> str:
    text = " ".join(part.strip() for part in parts if part.strip())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([([{])\s+", r"\1", text)
    text = SPACE_RE.sub(" ", text)
    return text.strip()
