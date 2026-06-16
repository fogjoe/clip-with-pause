from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
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


@dataclass(frozen=True)
class AudioDownloadResult:
    source_path: Path
    info: dict[str, Any]


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    source: str
    url: str


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
        audio_download = _download_audio(job.url, temp_dir)
        audio_output = output_dir / "clip.mp3"
        _cut_audio_to_mp3(audio_download.source_path, audio_output, job.start_seconds, job.end_seconds)

        subtitle_file = _download_json3_subtitles(audio_download.info, job.subtitle_language, temp_dir)
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
        "noprogress": True,
        "noplaylist": True,
        "cachedir": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": float(os.getenv("YTDLP_SOCKET_TIMEOUT_SECONDS", "30")),
        "sleep_interval_requests": float(os.getenv("YTDLP_SLEEP_INTERVAL_REQUESTS", "1")),
        "extractor_args": {
            "youtube": {
                "player_client": YOUTUBE_CLIENTS,
            }
        },
        "paths": {
            "home": str(temp_dir),
        },
    }

    js_runtimes = _configured_js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes

    cookies_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookies_file:
        options["cookiefile"] = cookies_file

    proxy = os.getenv("YTDLP_PROXY")
    if proxy:
        options["proxy"] = proxy

    return options


def _configured_js_runtimes() -> dict[str, dict[str, str]]:
    raw_value = os.getenv("YTDLP_JS_RUNTIMES", "node")
    runtimes: dict[str, dict[str, str]] = {}

    for item in raw_value.split(","):
        runtime = item.strip()
        if not runtime:
            continue

        name, separator, configured_path = runtime.partition(":")
        name = name.strip().lower()
        if not name:
            continue

        config: dict[str, str] = {}
        if separator and configured_path.strip():
            config["path"] = configured_path.strip()
        runtimes[name] = config

    return runtimes


def _download_audio(url: str, temp_dir: Path) -> AudioDownloadResult:
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
            return AudioDownloadResult(source_path=prepared_path, info=info)

    candidates = [
        path
        for path in temp_dir.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".json3"))
    ]
    if not candidates:
        raise ProcessingError("yt-dlp did not produce an audio file.")
    return AudioDownloadResult(source_path=max(candidates, key=lambda path: path.stat().st_mtime), info=info)


def _download_json3_subtitles(info: dict[str, Any], language: str, temp_dir: Path) -> Path:
    subtitle_dir = temp_dir / "subtitles"
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    subtitle_track = _select_json3_subtitle_track(info, language)
    subtitle_path = subtitle_dir / f"{info.get('id', 'subtitle')}.{subtitle_track.language}.json3"
    options = _base_ydl_options(temp_dir)

    subtitle_retries = int(os.getenv("YTDLP_SUBTITLE_RETRIES", "4"))
    retry_base_seconds = float(os.getenv("YTDLP_SUBTITLE_RETRY_BASE_SECONDS", "3"))
    last_error: Exception | None = None

    for attempt in range(subtitle_retries + 1):
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                response = downloader.urlopen(subtitle_track.url)
                subtitle_bytes = response.read()
            if not subtitle_bytes:
                raise ProcessingError("The selected subtitle track was empty.")
            subtitle_path.write_bytes(subtitle_bytes)
            return subtitle_path
        except Exception as exc:
            last_error = exc
            if not _is_retryable_subtitle_error(exc) or attempt >= subtitle_retries:
                break
            delay_seconds = retry_base_seconds * (2**attempt)
            logger.warning(
                "Retrying subtitle download after transient error, attempt %s of %s: %s",
                attempt + 1,
                subtitle_retries,
                exc,
            )
            time.sleep(delay_seconds)

    if last_error and _is_rate_limit_error(last_error):
        raise ProcessingError(
            "YouTube returned HTTP 429 while downloading subtitles. Wait a while before retrying, "
            "or configure YTDLP_COOKIES_FILE with browser cookies and optionally YTDLP_PROXY for a different egress IP."
        ) from last_error

    if last_error and _is_transient_network_error(last_error):
        raise ProcessingError(
            "YouTube subtitle download failed after retrying because the network or TLS connection closed unexpectedly. "
            "Retry the clip, and if it keeps happening check your Mac proxy/VPN/DNS settings or configure YTDLP_PROXY "
            "with a stable proxy."
        ) from last_error

    raise ProcessingError(f"Could not download the selected subtitle track: {last_error}") from last_error


def _select_json3_subtitle_track(info: dict[str, Any], language: str) -> SubtitleTrack:
    subtitle_sources = [
        ("manual", info.get("subtitles", {})),
        ("automatic", info.get("automatic_captions", {})),
    ]

    for source_name, tracks_by_language in subtitle_sources:
        if not isinstance(tracks_by_language, dict):
            continue
        matching_language = _matching_subtitle_language(tracks_by_language, language)
        if not matching_language:
            continue
        tracks = tracks_by_language.get(matching_language, [])
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            if isinstance(track, dict) and track.get("ext") == "json3" and isinstance(track.get("url"), str):
                return SubtitleTrack(language=matching_language, source=source_name, url=track["url"])

    available_languages: list[str] = []
    for _source_name, tracks_by_language in subtitle_sources:
        if isinstance(tracks_by_language, dict):
            available_languages.extend(tracks_by_language.keys())

    unique_languages = ", ".join(sorted(set(available_languages))[:30])
    if unique_languages:
        raise ProcessingError(
            f"No json3 subtitles were found for language '{language}'. Available languages: {unique_languages}."
        )
    raise ProcessingError(f"No json3 subtitles were found for language '{language}'.")


def _matching_subtitle_language(tracks_by_language: dict[str, Any], language: str) -> str | None:
    if language in tracks_by_language:
        return language

    normalized_language = language.lower()
    for available_language in tracks_by_language:
        if available_language.lower() == normalized_language:
            return available_language

    for available_language in tracks_by_language:
        if available_language.lower().split("-")[0] == normalized_language.split("-")[0]:
            return available_language

    return None


def _is_rate_limit_error(error: Exception) -> bool:
    return "429" in str(error) or "too many requests" in str(error).lower()


def _is_retryable_subtitle_error(error: Exception) -> bool:
    return _is_rate_limit_error(error) or _is_transient_network_error(error)


def _is_transient_network_error(error: Exception) -> bool:
    message = str(error).lower()
    if "certificate verify failed" in message:
        return False

    transient_markers = (
        "unexpected_eof",
        "eof occurred in violation of protocol",
        "connection reset",
        "connection aborted",
        "connection closed",
        "remote end closed connection",
        "tls handshake",
        "read timed out",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "network is unreachable",
    )
    return any(marker in message for marker in transient_markers)


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
