from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TaskStatus = Literal["queued", "processing", "complete", "failed"]


class ClipProcessRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    url: str = Field(..., min_length=1)
    start_time: str | float | int
    end_time: str | float | int
    subtitle_language: str = Field("en", min_length=2, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def normalize_camel_case_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        aliases = {
            "startTime": "start_time",
            "endTime": "end_time",
            "subtitleLanguage": "subtitle_language",
        }
        for source, target in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        return normalized

    @field_validator("subtitle_language")
    @classmethod
    def validate_subtitle_language(cls, value: str) -> str:
        allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not value or any(char not in allowed_chars for char in value):
            raise ValueError("Subtitle language must be a valid language code.")
        return value


class Sentence(BaseModel):
    id: int
    start: float
    end: float
    text: str


class ClipResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    audio_url: str = Field(..., alias="audioUrl")
    subtitles_url: str = Field(..., alias="subtitlesUrl")
    sentences: list[Sentence]
    expires_at: str = Field(..., alias="expiresAt")


class TaskCreateResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(..., alias="taskId")
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(..., alias="taskId")
    status: TaskStatus
    progress: int
    message: str
    error: str | None = None
    result: ClipResult | None = None


class ApiError(BaseModel):
    detail: str | dict[str, Any]
