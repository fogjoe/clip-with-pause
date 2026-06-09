export type Sentence = {
  id: number;
  start: number;
  end: number;
  text: string;
};

export type ClipResult = {
  audioUrl: string;
  subtitlesUrl: string;
  sentences: Sentence[];
  expiresAt: string;
};

export type TaskStatus = "queued" | "processing" | "complete" | "failed";

export type TaskResponse = {
  taskId: string;
  status: TaskStatus;
  progress?: number;
  message: string;
  error?: string | null;
  result?: ClipResult | null;
};

export type ClipProcessPayload = {
  url: string;
  startTime: string;
  endTime: string;
  subtitleLanguage: string;
};

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/$/, "");

export async function createClipTask(payload: ClipProcessPayload): Promise<TaskResponse> {
  return requestJson<TaskResponse>("/api/process", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

export async function getClipTask(taskId: string): Promise<TaskResponse> {
  return requestJson<TaskResponse>(`/api/tasks/${taskId}`, {
    method: "GET",
  });
}

export function assetUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  return `${API_BASE_URL}${path}`;
}

async function requestJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      }
    } catch {
      detail = response.statusText;
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

