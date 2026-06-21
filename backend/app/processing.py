from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp

from app.subtitles import extract_sentences, load_json3


logger = logging.getLogger(__name__)

YOUTUBE_CLIENTS = ["tv", "mweb", "android", "ios", "web"]
SOURCE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
COMPLETE_SENTENCE_MAX_EXTENSION_SECONDS = 20.0
CLIP_TAIL_PADDING_SECONDS = 0.35
ALLOWED_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

_SOURCE_CACHE_LOCKS: dict[str, threading.Lock] = {}
_SOURCE_CACHE_LOCKS_GUARD = threading.Lock()


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
    cache_dir: Path


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
        audio_download = _get_cached_audio(job.url, temp_dir)
        subtitle_file = _get_cached_json3_subtitles(
            audio_download.info,
            job.subtitle_language,
            temp_dir,
            audio_download.cache_dir,
        )
        subtitle_data = load_json3(subtitle_file)
        sentences = extract_sentences(
            subtitle_data,
            job.start_seconds,
            job.end_seconds,
            max_end_extension=_complete_sentence_max_extension_seconds(),
        )
        if not sentences:
            raise ProcessingError("No subtitle sentences were found inside the requested timeframe.")

        effective_end_seconds = _effective_audio_end_seconds(job, sentences, audio_download.info)
        audio_output = output_dir / "clip.mp3"
        _cut_audio_to_mp3(audio_download.source_path, audio_output, job.start_seconds, effective_end_seconds)

        subtitles_output = output_dir / "subtitles.json"
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = {
            "sourceUrl": job.url,
            "clipStart": job.start_seconds,
            "clipEnd": effective_end_seconds,
            "requestedClipEnd": job.end_seconds,
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


def cleanup_expired_source_cache(cache_root: Path | None = None, ttl_seconds: int | None = None) -> None:
    if cache_root is None:
        cache_root = _source_cache_root()
    if ttl_seconds is None:
        ttl_seconds = _source_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return

    cache_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    for child in cache_root.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > ttl_seconds:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            logger.warning("Could not inspect source cache directory for cleanup: %s", child)


def _complete_sentence_max_extension_seconds() -> float:
    return _float_env("COMPLETE_SENTENCE_MAX_EXTENSION_SECONDS", COMPLETE_SENTENCE_MAX_EXTENSION_SECONDS)


def _clip_tail_padding_seconds() -> float:
    return _float_env("CLIP_TAIL_PADDING_SECONDS", CLIP_TAIL_PADDING_SECONDS)


def _effective_audio_end_seconds(job: ClipJob, sentences: list[dict[str, Any]], info: dict[str, Any]) -> float:
    end_seconds = job.end_seconds
    if sentences:
        try:
            end_seconds = max(end_seconds, job.start_seconds + float(sentences[-1]["end"]))
        except (KeyError, TypeError, ValueError):
            logger.warning("Could not read final sentence end time for clip extension.")

    end_seconds += _clip_tail_padding_seconds()
    source_duration = _source_duration_seconds(info)
    if source_duration is not None:
        end_seconds = min(end_seconds, source_duration)

    return max(job.start_seconds + 0.001, end_seconds)


def _source_duration_seconds(info: dict[str, Any]) -> float | None:
    duration = info.get("duration")
    if isinstance(duration, int | float) and duration > 0:
        return float(duration)
    return None


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        return max(0, float(raw_value))
    except ValueError:
        logger.warning("Ignoring invalid %s value: %s", name, raw_value)
        return default


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


def _get_cached_audio(url: str, temp_dir: Path) -> AudioDownloadResult:
    cache_key = _source_cache_key(url)
    cache_dir = _source_cache_root() / cache_key

    with _source_cache_lock(cache_key):
        cached_audio = _load_cached_audio(cache_dir)
        if cached_audio:
            logger.info("Using cached source audio for %s", cache_key)
            return cached_audio

        logger.info("Downloading source audio for %s", cache_key)
        return _download_audio_to_cache(url, temp_dir, cache_dir)


def _download_audio_to_cache(url: str, temp_dir: Path, cache_dir: Path) -> AudioDownloadResult:
    download_dir = temp_dir / "audio-download"
    download_dir.mkdir(parents=True, exist_ok=True)
    options = _base_ydl_options(download_dir)
    options.update(
        {
            "format": "bestaudio/best",
            "outtmpl": str(download_dir / "audio.%(ext)s"),
        }
    )

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        if not isinstance(info, dict):
            raise ProcessingError("yt-dlp did not return metadata for the audio download.")

        prepared_path = Path(downloader.prepare_filename(info))
        if not prepared_path.exists():
            prepared_path = _newest_downloaded_audio(download_dir)

    if not prepared_path:
        raise ProcessingError("yt-dlp did not produce an audio file.")

    cache_dir.mkdir(parents=True, exist_ok=True)
    for old_source in cache_dir.glob("source.*"):
        old_source.unlink(missing_ok=True)

    cached_source_path = cache_dir / f"source{prepared_path.suffix}"
    shutil.move(str(prepared_path), cached_source_path)

    info_path = cache_dir / "info.json"
    with info_path.open("w", encoding="utf-8") as info_file:
        json.dump(downloader.sanitize_info(info), info_file, ensure_ascii=False)

    _touch_cache_entry(cache_dir)
    return AudioDownloadResult(source_path=cached_source_path, info=info, cache_dir=cache_dir)


def _newest_downloaded_audio(download_dir: Path) -> Path | None:
    candidates = [
        path
        for path in download_dir.iterdir()
        if path.is_file() and not path.name.endswith((".part", ".json3"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _load_cached_audio(cache_dir: Path) -> AudioDownloadResult | None:
    info_path = cache_dir / "info.json"
    if not info_path.is_file():
        return None

    source_candidates = [
        path
        for path in cache_dir.glob("source.*")
        if path.is_file() and path.stat().st_size > 0
    ]
    if not source_candidates:
        return None

    try:
        with info_path.open("r", encoding="utf-8") as info_file:
            info = json.load(info_file)
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable source cache metadata: %s", info_path)
        return None

    source_path = max(source_candidates, key=lambda path: path.stat().st_mtime)
    _touch_cache_entry(cache_dir)
    return AudioDownloadResult(source_path=source_path, info=info, cache_dir=cache_dir)


def _touch_cache_entry(cache_dir: Path) -> None:
    now = time.time()
    try:
        os.utime(cache_dir, (now, now))
        for child in cache_dir.iterdir():
            os.utime(child, (now, now))
    except OSError:
        logger.warning("Could not update source cache timestamp: %s", cache_dir)


def _source_cache_key(url: str) -> str:
    parsed_url = urlparse(url)
    host = parsed_url.netloc.lower()
    video_id = ""

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        video_id = parse_qs(parsed_url.query).get("v", [""])[0]
        path_parts = [part for part in parsed_url.path.split("/") if part]
        if not video_id and len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            video_id = path_parts[1]
    elif host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed_url.path.strip("/").split("/")[0]

    if video_id and re.fullmatch(r"[\w-]{6,128}", video_id):
        return video_id

    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", url).strip("-")
    return safe_key[:120] or "unknown-source"


def _source_cache_root() -> Path:
    configured_root = os.getenv("CLIP_CACHE_DIR")
    if configured_root:
        return Path(configured_root)
    if Path("/app").exists():
        return Path("/app/data/cache")
    return Path.cwd() / "data" / "cache"


def _source_cache_ttl_seconds() -> int:
    return int(os.getenv("SOURCE_CACHE_TTL_SECONDS", str(SOURCE_CACHE_TTL_SECONDS)))


def _source_cache_lock(cache_key: str) -> threading.Lock:
    with _SOURCE_CACHE_LOCKS_GUARD:
        lock = _SOURCE_CACHE_LOCKS.get(cache_key)
        if not lock:
            lock = threading.Lock()
            _SOURCE_CACHE_LOCKS[cache_key] = lock
        return lock


def _get_cached_json3_subtitles(info: dict[str, Any], language: str, temp_dir: Path, cache_dir: Path) -> Path:
    language_cache_key = _safe_cache_component(language.lower())
    subtitle_cache_path = cache_dir / f"subtitles.{language_cache_key}.json3"
    if subtitle_cache_path.is_file() and subtitle_cache_path.stat().st_size > 0:
        _touch_cache_entry(cache_dir)
        return subtitle_cache_path

    subtitle_file = _download_json3_subtitles(info, language, temp_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temp_cache_path = cache_dir / f"{subtitle_cache_path.name}.tmp-{threading.get_ident()}"
    shutil.copy2(subtitle_file, temp_cache_path)
    temp_cache_path.replace(subtitle_cache_path)
    _touch_cache_entry(cache_dir)
    return subtitle_cache_path


def _safe_cache_component(value: str) -> str:
    safe_value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe_value[:80] or "default"


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
