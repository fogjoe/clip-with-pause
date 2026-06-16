"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { LanguagePlayer } from "@/components/LanguagePlayer";
import { assetUrl, createClipTask, getClipTask, type TaskResponse } from "@/lib/api";

const SUBTITLE_LANGUAGE_OPTIONS = [
  { label: "English", value: "en" },
  { label: "Chinese", value: "zh" },
  { label: "Spanish", value: "es" },
];

export default function Home() {
  const [url, setUrl] = useState("");
  const [startTime, setStartTime] = useState("");
  const [endTime, setEndTime] = useState("");
  const [subtitleLanguage, setSubtitleLanguage] = useState("en");
  const [task, setTask] = useState<TaskResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isWorking = task?.status === "queued" || task?.status === "processing";
  const playerData = task?.status === "complete" ? task.result : null;
  const taskId = task?.taskId;
  const taskStatus = task?.status;

  useEffect(() => {
    if (!taskId || taskStatus === "complete" || taskStatus === "failed") {
      return;
    }

    let cancelled = false;
    const pollingTaskId = taskId;
    async function poll() {
      try {
        const nextTask = await getClipTask(pollingTaskId);
        if (!cancelled) {
          setTask(nextTask);
        }
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError instanceof Error ? pollError.message : "Task polling failed.");
        }
      }
    }

    void poll();
    const intervalId = window.setInterval(poll, 1600);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [taskId, taskStatus]);

  const progressValue = useMemo(() => {
    if (!task) {
      return 0;
    }
    return Math.min(100, Math.max(0, task.progress ?? 0));
  }, [task]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setTask(null);
    setIsSubmitting(true);
    try {
      const createdTask = await createClipTask({
        url,
        startTime,
        endTime,
        subtitleLanguage,
      });
      setTask(createdTask);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Clip request failed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  if (playerData) {
    return (
      <main className="min-h-screen bg-neutral-950">
        <LanguagePlayer
          audioUrl={assetUrl(playerData.audioUrl)}
          subtitlesUrl={assetUrl(playerData.subtitlesUrl)}
          sentences={playerData.sentences}
          title="YouTube Clip Audio"
          clipLabel={`${startTime} - ${endTime}`}
          onBack={() => setTask(null)}
        />
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-neutral-100">
      <div className="mx-auto flex w-full max-w-5xl flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
        <header className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold text-neutral-950">YouTube Clip Audio</h1>
          <p className="text-sm text-neutral-600">audio.fogjoe.com</p>
        </header>

        <section className="rounded-md border border-neutral-200 bg-white p-4 shadow-sm sm:p-5">
          <form className="grid gap-4" onSubmit={handleSubmit}>
            <label className="block">
              <span className="mb-2 block text-sm font-medium text-neutral-700">YouTube URL</span>
              <input
                type="url"
                required
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://www.youtube.com/watch?v=..."
                className="h-11 w-full rounded-md border border-neutral-300 px-3 text-sm text-neutral-950 outline-none focus:border-teal-700 focus:ring-2 focus:ring-teal-100"
              />
            </label>

            <div className="grid gap-4 sm:grid-cols-[1fr_1fr_10rem]">
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-neutral-700">Start</span>
                <input
                  type="text"
                  required
                  value={startTime}
                  onChange={(event) => setStartTime(event.target.value)}
                  placeholder="00:01:30"
                  className="h-11 w-full rounded-md border border-neutral-300 px-3 text-sm text-neutral-950 outline-none focus:border-teal-700 focus:ring-2 focus:ring-teal-100"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-sm font-medium text-neutral-700">End</span>
                <input
                  type="text"
                  required
                  value={endTime}
                  onChange={(event) => setEndTime(event.target.value)}
                  placeholder="00:02:10"
                  className="h-11 w-full rounded-md border border-neutral-300 px-3 text-sm text-neutral-950 outline-none focus:border-teal-700 focus:ring-2 focus:ring-teal-100"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-sm font-medium text-neutral-700">Subtitle</span>
                <select
                  required
                  value={subtitleLanguage}
                  onChange={(event) => setSubtitleLanguage(event.target.value)}
                  className="h-11 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-950 outline-none focus:border-teal-700 focus:ring-2 focus:ring-teal-100"
                >
                  {SUBTITLE_LANGUAGE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <button
                type="submit"
                disabled={isSubmitting || isWorking}
                className="h-11 rounded-md bg-teal-700 px-5 text-sm font-semibold text-white hover:bg-teal-800 disabled:cursor-not-allowed disabled:bg-neutral-400"
              >
                {isSubmitting || isWorking ? "Processing" : "Create clip"}
              </button>

              {task && (
                <div className="min-w-0 flex-1 sm:max-w-md">
                  <div className="mb-1 flex items-center justify-between gap-3 text-sm">
                    <span className="truncate font-medium text-neutral-700">{task.message}</span>
                    <span className="font-medium tabular-nums text-neutral-500">{progressValue}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-neutral-200">
                    <div
                      className="h-full bg-amber-500 transition-all"
                      style={{ width: `${progressValue}%` }}
                    />
                  </div>
                </div>
              )}
            </div>
          </form>
        </section>

        {error && (
          <section className="rounded-md border border-red-200 bg-red-50 p-4 text-sm font-medium text-red-800">
            {error}
          </section>
        )}

        {task?.status === "failed" && task.error && (
          <section className="rounded-md border border-red-200 bg-red-50 p-4 text-sm font-medium text-red-800">
            {task.error}
          </section>
        )}

      </div>
    </main>
  );
}
