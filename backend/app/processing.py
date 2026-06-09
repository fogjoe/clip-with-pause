from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from app.subtitles import extract_sentences, load_json3


logger = logging.getLogger(__name__)

YOUTUBE_CLIENTS = ["tv", "mweb", "android", "ios", "web"]
ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class ProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClipJob:
    url: str
    start_seconds: float
    end_seconds: float
    subtitle_language: str


@dataclass(frozen=True)
class ClipProcessingResult:
    audio_path: Path
    subtitles_path: Path
    sentences: list[dict[str, Any]]
    expires_at: datetime


def parse_timestamp(value: str | float | int) -> float:
    if isinstance(value, int | float):
        seconds = float(value)
    else:
        raw_value = str(value).strip()
        if not raw_value:
            raise ValueError("Timestamp cannot be empty.")

        if re.fullmatch(r"\d+(\.\d+)?", raw_value):
            seconds = float(raw_value)
        else:
            parts = raw_value.split(":")
            if len(parts) > 3 or any(part == "" for part in parts):
                raise ValueError("Timestamp must be seconds, MM:SS, or HH:MM:SS.")
            parsed_parts = [float(part) for part in parts]
            seconds = 0.0
            for part in parsed_parts:
                seconds = seconds * 60 + part

    if seconds < 0:
        raise ValueError("Timestamp cannot be negative.")
    return seconds


def validate_clip_job(url: str, start_value: str | float | int, end_value: str | float | int, language: str) -> ClipJob:
    parsed_url = urlparse(url)
    host = parsed_url.netloc.lower()
    if parsed_url.scheme not in {"http", "https"} or host not in ALLOWED_YOUTUBE_HOSTS:
        raise ValueError("Only YouTube URLs are supported.")

    start_seconds = parse_timestamp(start_value)
    end_seconds = parse_timestamp(end_value)
    if end_seconds <= start_seconds:
        raise ValueError("End time must be greater than start time.")

    max_clip_seconds = float(os.getenv("MAX_CLIP_SECONDS", "600"))
    if end_seconds - start_seconds > max_clip_seconds:
        raise ValueError(f"Clip duration cannot exceed {int(max_clip_seconds)} seconds.")

    return ClipJob(
        url=url,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        subtitle_language=language,
    )


def process_clip_task(task_id: str, job: ClipJob, output_root: Path, ttl_seconds: int) -> ClipProcessingResult:
    output_dir = output_root / task_id
    temp_dir = Path(tempfile.mkdtemp(prefix=f"clip-{task_id}-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        audio_source = _download_audio(job.url, temp_dir)
        audio_output = output_dir / "clip.mp3"
        _cut_audio_to_mp3(audio_source, audio_output, job.start_seconds, job.end_seconds)

        subtitle_file = _download_json3_subtitles(job.url, job.subtitle_language, temp_dir)
        subtitle_data = load_json3(subtitle_file)
        sentences = extract_sentences(subtitle_data, job.start_seconds, job.end_seconds)
        if not sentences:
            raise ProcessingError("No subtitle sentences were found inside the requested timeframe.")

        subtitles_output = output_dir / "subtitles.json"
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = {
            "sourceUrl": job.url,
            "clipStart": job.start_seconds,
            "clipEnd": job.end_seconds,
            "subtitleLanguage": job.subtitle_language,
            "sentences": sentences,
            "expiresAt": expires_at.isoformat(),
        }
        with subtitles_output.open("w", encoding="utf-8") as json_file:
            json.dump(payload, json_file, ensure_ascii=False, indent=2)

        return ClipProcessingResult(
            audio_path=audio_output,
            subtitles_path=subtitles_output,
            sentences=sentences,
            expires_at=expires_at,
        )
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def cleanup_expired_outputs(output_root: Path, ttl_seconds: int) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    for child in output_root.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > ttl_seconds:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logger.warning("Could not inspect output directory for cleanup: %s", child)


def _base_ydl_options(temp_dir: Path) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_args": {
            "youtube": {
                "player_client": YOUTUBE_CLIENTS,
            }
        },
        "paths": {
            "home": str(temp_dir),
        },
    }

    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file:
        options["cookiefile"] = cookies_file

    return options


def _download_audio(url: str, temp_dir: Path) -> Path:
    options = _base_ydl_options(temp_dir)
    options.update(
        {
            "format": "bestaudio/best",
            "outtmpl": str(temp_dir / "audio.%(ext)s"),
        }
    )

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise ProcessingError("yt-dlp did not return metadata for the audio download.")

        prepared_path = Path(downloader.prepare_filename(info))
        if prepared_path.exists():
            return prepared_path

    candidates = [
        path
        for path in temp_dir.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".json3"))
    ]
    if not candidates:
        raise ProcessingError("yt-dlp did not produce an audio file.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _download_json3_subtitles(url: str, language: str, temp_dir: Path) -> Path:
    subtitle_dir = temp_dir / "subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)

    options = _base_ydl_options(temp_dir)
    options.update(
        {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitlesformat": "json3",
            "subtitleslangs": [language],
            "outtmpl": str(subtitle_dir / "%(id)s.%(ext)s"),
        }
    )

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)

    subtitle_files = sorted(subtitle_dir.glob("*.json3"))
    if subtitle_files:
        exact_matches = [path for path in subtitle_files if f".{language}." in path.name]
        return exact_matches[0] if exact_matches else subtitle_files[0]

    available_languages: list[str] = []
    if isinstance(info, dict):
        subtitle_languages = info.get("subtitles", {})
        automatic_languages = info.get("automatic_captions", {})
        if isinstance(subtitle_languages, dict):
            available_languages.extend(subtitle_languages.keys())
        if isinstance(automatic_languages, dict):
            available_languages.extend(automatic_languages.keys())

    unique_languages = ", ".join(sorted(set(available_languages))[:30])
    if unique_languages:
        raise ProcessingError(
            f"No json3 subtitles were found for language '{language}'. Available languages: {unique_languages}."
        )
    raise ProcessingError(f"No json3 subtitles were found for language '{language}'.")


def _cut_audio_to_mp3(source_path: Path, output_path: Path, start_seconds: float, end_seconds: float) -> None:
    ffmpeg_binary = _resolve_ffmpeg_binary()
    duration = end_seconds - start_seconds
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-ar",
        "44100",
        "-b:a",
        "128k",
        str(output_path),
    ]

    timeout_seconds = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "1800"))
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        error_tail = result.stderr[-1200:] if result.stderr else "No FFmpeg error output was provided."
        raise ProcessingError(f"FFmpeg failed while cutting audio: {error_tail}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise ProcessingError("FFmpeg did not produce a valid MP3 file.")


def _resolve_ffmpeg_binary() -> str:
    configured_binary = os.getenv("FFMPEG_BINARY")
    if configured_binary:
        configured_path = shutil.which(configured_binary) or configured_binary
        if Path(configured_path).exists():
            return configured_path

    try:
        import static_ffmpeg

        add_paths = getattr(static_ffmpeg, "add_paths", None)
        if callable(add_paths):
            add_paths()

        path_binary = shutil.which("ffmpeg")
        if path_binary:
            return path_binary

        run_module = getattr(static_ffmpeg, "run", None)
        resolver = getattr(run_module, "get_or_fetch_platform_executables_else_raise", None) if run_module else None
        if callable(resolver):
            ffmpeg_binary, _ffprobe_binary = resolver()
            return str(ffmpeg_binary)
    except Exception as exc:
        logger.warning("static-ffmpeg resolution failed: %s", exc)

    path_binary = shutil.which("ffmpeg")
    if path_binary:
        return path_binary

    raise ProcessingError("FFmpeg binary could not be resolved from static-ffmpeg, FFMPEG_BINARY, or PATH.")
