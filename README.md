# YouTube Clip Audio

Production-oriented local deployment for generating YouTube audio clips, extracting `json3` subtitles, and playing sentence-level audio loops in a language-learning UI.

## Directory Structure

```text
.
├── .dockerignore
├── .env.example
├── README.md
├── backend.Dockerfile
├── frontend.Dockerfile
├── docker-compose.yml
├── backend
│   ├── app
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── processing.py
│   │   ├── subtitles.py
│   │   └── tasks.py
│   └── requirements.txt
└── frontend
    ├── app
    │   ├── globals.css
    │   ├── layout.tsx
    │   └── page.tsx
    ├── components
    │   └── LanguagePlayer.tsx
    ├── lib
    │   └── api.ts
    ├── next-env.d.ts
    ├── next.config.mjs
    ├── package.json
    ├── postcss.config.js
    ├── tailwind.config.ts
    └── tsconfig.json
```

## Services

- `backend`: FastAPI API that uses `yt-dlp`, `static-ffmpeg`, and subprocess-based FFmpeg execution.
- `frontend`: Next.js App Router UI with polling and synchronized sentence playback.
- `cloudflared`: Official Cloudflare Tunnel daemon. SSL and public reverse proxying are handled by Cloudflare.

## API

`POST /api/process`

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "startTime": "00:01:30",
  "endTime": "00:02:10",
  "subtitleLanguage": "en"
}
```

Response:

```json
{
  "taskId": "uuid",
  "status": "queued",
  "message": "Task queued."
}
```

Poll task status:

```text
GET /api/tasks/{taskId}
```

Completed tasks return `audioUrl`, `subtitlesUrl`, sentence timing data, and an expiration timestamp. Temporary download files are deleted after each task. Final MP3 and JSON outputs are stored under a task-specific directory and removed after the configured TTL.

## Cloudflare Tunnel

Create a remotely managed Cloudflare Tunnel and configure the public hostname:

```text
Hostname: audio.fogjoe.com
Service: http://frontend:3000
```

The `cloudflared` container runs in the same Docker Compose network, so it can resolve the `frontend` service name.
The Compose file forces `cloudflared` to use HTTP/2 transport because many local networks block QUIC traffic on UDP port 7844. It also passes `--url http://frontend:3000` so the connector has an explicit local ingress target even if the Zero Trust public hostname config does not provide one.

## Deployment

Copy `.env.example` to `.env` and set `TUNNEL_TOKEN`.

```bash
docker compose up --build -d
```

Local service URLs:

```text
Frontend: http://127.0.0.1:3000
Backend health: http://127.0.0.1:8000/api/health
```

The Docker images use official Python and Node base images that support Linux ARM64 on Apple Silicon.
The backend image installs Node.js 22 and `yt-dlp-ejs` so `yt-dlp` can solve YouTube JavaScript challenges required for some videos.

## Configuration

- `TUNNEL_TOKEN`: Cloudflare Tunnel token.
- `CORS_ORIGINS`: Allowed CORS origins. Defaults to `https://audio.fogjoe.com`.
- `OUTPUT_TTL_SECONDS`: Retention time for generated MP3 and JSON outputs.
- `TASK_MAX_WORKERS`: Number of concurrent backend processing workers.
- `MAX_CLIP_SECONDS`: Maximum accepted clip length.
- `YTDLP_COOKIES_FILE`: Optional cookie file path for `yt-dlp`.
- `YTDLP_JS_RUNTIMES`: JavaScript runtime used by `yt-dlp` challenge solving. Defaults to `node`.
- `YTDLP_PROXY`: Optional proxy URL for `yt-dlp`, for example `socks5://host.docker.internal:7890`.
- `YTDLP_SLEEP_INTERVAL_REQUESTS`: Delay between `yt-dlp` HTTP requests.
- `YTDLP_SUBTITLE_RETRIES`: Number of retry attempts for subtitle downloads.
- `YTDLP_SUBTITLE_RETRY_BASE_SECONDS`: Base delay for subtitle retry backoff.
- `FFMPEG_BINARY`: Optional explicit FFmpeg binary path.

## YouTube Rate Limits

YouTube can return `HTTP Error 429: Too Many Requests`, especially for subtitle downloads. The backend reduces request pressure by reusing the first `yt-dlp` extraction result and retrying direct json3 subtitle downloads with backoff.

If 429 continues, export browser cookies to Netscape format, place the file at:

```text
backend/cookies/youtube-cookies.txt
```

Then set this in `.env`:

```env
YTDLP_COOKIES_FILE=/app/cookies/youtube-cookies.txt
YTDLP_SLEEP_INTERVAL_REQUESTS=2
YTDLP_SUBTITLE_RETRIES=5
YTDLP_SUBTITLE_RETRY_BASE_SECONDS=5
```

Restart the backend:

```bash
docker compose up -d --build backend
```

`backend/cookies/youtube-cookies.txt` is the path on the Mac host. `/app/cookies/youtube-cookies.txt` is the path inside the backend container and is the value that should be used for `YTDLP_COOKIES_FILE`.
