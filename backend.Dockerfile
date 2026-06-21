FROM node:22-bookworm-slim AS node-runtime

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/backend/requirements.txt

RUN python -c "import static_ffmpeg; static_ffmpeg.add_paths()"

COPY backend/app /app/app

RUN mkdir -p /app/data/output /app/data/cache

ENV CLIP_OUTPUT_DIR=/app/data/output
ENV CLIP_CACHE_DIR=/app/data/cache

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
