from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.models import ClipProcessRequest, ClipResult, Sentence, TaskCreateResponse, TaskStatusResponse
from app.processing import cleanup_expired_outputs, cleanup_expired_source_cache, process_clip_task, validate_clip_job
from app.tasks import ClipTask, TaskManager


OUTPUT_ROOT = Path(os.getenv("CLIP_OUTPUT_DIR", "/app/data/output"))
SOURCE_CACHE_ROOT = Path(os.getenv("CLIP_CACHE_DIR", "/app/data/cache" if Path("/app").exists() else "data/cache"))
OUTPUT_TTL_SECONDS = int(os.getenv("OUTPUT_TTL_SECONDS", "86400"))
SOURCE_CACHE_TTL_SECONDS = int(os.getenv("SOURCE_CACHE_TTL_SECONDS", "604800"))
TASK_MAX_WORKERS = int(os.getenv("TASK_MAX_WORKERS", "2"))
TASK_ID_RE = re.compile(r"^[a-f0-9-]{36}$")
MEDIA_FILENAMES = {
    "clip.mp3": "audio/mpeg",
    "subtitles.json": "application/json",
}

task_manager = TaskManager(
    output_root=OUTPUT_ROOT,
    ttl_seconds=OUTPUT_TTL_SECONDS,
    max_workers=TASK_MAX_WORKERS,
    process_function=process_clip_task,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    SOURCE_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    cleanup_expired_outputs(OUTPUT_ROOT, OUTPUT_TTL_SECONDS)
    cleanup_expired_source_cache(SOURCE_CACHE_ROOT, SOURCE_CACHE_TTL_SECONDS)
    yield
    task_manager.shutdown()


app = FastAPI(
    title="YouTube Clip Language API",
    version="1.0.0",
    lifespan=lifespan,
)

cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "https://audio.fogjoe.com").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/process", response_model=TaskCreateResponse, status_code=202)
async def process_clip(payload: ClipProcessRequest) -> TaskCreateResponse:
    try:
        job = validate_clip_job(
            url=payload.url,
            start_value=payload.start_time,
            end_value=payload.end_time,
            language=payload.subtitle_language,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task = task_manager.create_task(job)
    return TaskCreateResponse(taskId=task.task_id, status=task.status, message=task.message)


@app.get("/api/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str) -> TaskStatusResponse:
    task = _get_existing_task(task_id)
    return _task_to_response(task)


@app.get("/api/media/{task_id}/{filename}")
async def get_media(task_id: str, filename: str) -> FileResponse:
    task = _get_existing_task(task_id)
    if task.status != "complete" or not task.result:
        raise HTTPException(status_code=404, detail="Task output is not available.")

    media_type = MEDIA_FILENAMES.get(filename)
    if not media_type:
        raise HTTPException(status_code=404, detail="Media file is not available.")

    output_path = (OUTPUT_ROOT / task_id / filename).resolve()
    output_root = OUTPUT_ROOT.resolve()
    if output_root not in output_path.parents or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Media file is not available.")

    return FileResponse(
        output_path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _get_existing_task(task_id: str) -> ClipTask:
    if not TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="Task not found.")
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


def _task_to_response(task: ClipTask) -> TaskStatusResponse:
    result: ClipResult | None = None
    if task.result:
        result = ClipResult(
            audioUrl=f"/api/media/{task.task_id}/clip.mp3",
            subtitlesUrl=f"/api/media/{task.task_id}/subtitles.json",
            sentences=[Sentence(**sentence) for sentence in task.result.sentences],
            expiresAt=task.result.expires_at.isoformat(),
        )

    return TaskStatusResponse(
        taskId=task.task_id,
        status=task.status,
        progress=task.progress,
        message=task.message,
        error=task.error,
        result=result,
    )
