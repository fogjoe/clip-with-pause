"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { Sentence } from "@/lib/api";

const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5] as const;
const SEEK_STEP_SECONDS = 5;

type LanguagePlayerProps = {
  audioUrl: string;
  subtitlesUrl: string;
  sentences: Sentence[];
  title?: string;
  clipLabel?: string;
  onBack?: () => void;
};

export function LanguagePlayer({
  audioUrl,
  subtitlesUrl,
  sentences,
  title = "YouTube Clip Audio",
  clipLabel,
  onBack,
}: LanguagePlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const activeIndexRef = useRef(0);
  const completedLoopsRef = useRef(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [loopTarget, setLoopTarget] = useState(3);
  const [autoAdvance, setAutoAdvance] = useState(true);
  const [showSubtitles, setShowSubtitles] = useState(true);
  const [fillMode, setFillMode] = useState(false);
  const [showSentenceList, setShowSentenceList] = useState(false);

  const activeSentence = sentences[activeIndex] ?? null;
  const progressValue = duration > 0 ? Math.min(duration, currentTime) : 0;

  const activeText = useMemo(() => {
    if (!activeSentence) {
      return "";
    }
    return fillMode ? maskSentence(activeSentence.text) : activeSentence.text;
  }, [activeSentence, fillMode]);

  useEffect(() => {
    activeIndexRef.current = activeIndex;
  }, [activeIndex]);

  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.playbackRate = playbackRate;
    }
  }, [playbackRate]);

  useEffect(() => {
    setActiveIndex(0);
    activeIndexRef.current = 0;
    completedLoopsRef.current = 0;
    setCurrentTime(0);
    setDuration(0);
    setIsPlaying(false);
  }, [audioUrl]);

  function setCurrentSentence(index: number, shouldPlay: boolean) {
    const audio = audioRef.current;
    const nextIndex = Math.min(sentences.length - 1, Math.max(0, index));
    const sentence = sentences[nextIndex];
    if (!audio || !sentence) {
      return;
    }

    completedLoopsRef.current = 0;
    activeIndexRef.current = nextIndex;
    setActiveIndex(nextIndex);
    audio.currentTime = sentence.start;
    audio.playbackRate = playbackRate;
    setCurrentTime(sentence.start);
    if (shouldPlay) {
      void audio.play();
    }
  }

  function togglePlayback() {
    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    if (audio.paused) {
      if (activeSentence && audio.currentTime >= activeSentence.end) {
        audio.currentTime = activeSentence.start;
      }
      void audio.play();
    } else {
      audio.pause();
    }
  }

  function handleTimeUpdate() {
    const audio = audioRef.current;
    if (!audio || sentences.length === 0) {
      return;
    }

    const nextTime = audio.currentTime;
    setCurrentTime(nextTime);
    const currentSentence = sentences[activeIndexRef.current] ?? sentences[0];

    if (nextTime >= currentSentence.end - 0.04 && nextTime <= currentSentence.end + 0.75) {
      if (completedLoopsRef.current + 1 < loopTarget) {
        completedLoopsRef.current += 1;
        audio.currentTime = currentSentence.start;
        setCurrentTime(currentSentence.start);
        if (!audio.paused) {
          void audio.play();
        }
        return;
      }

      completedLoopsRef.current = 0;
      if (autoAdvance && activeIndexRef.current < sentences.length - 1) {
        setCurrentSentence(activeIndexRef.current + 1, !audio.paused);
      } else {
        audio.pause();
        audio.currentTime = currentSentence.end;
        setCurrentTime(currentSentence.end);
      }
      return;
    }

    const detectedIndex = findSentenceIndex(sentences, nextTime);
    if (detectedIndex !== activeIndexRef.current) {
      completedLoopsRef.current = 0;
      activeIndexRef.current = detectedIndex;
      setActiveIndex(detectedIndex);
    }
  }

  function handleSeek(value: number) {
    const audio = audioRef.current;
    if (!audio) {
      return;
    }
    completedLoopsRef.current = 0;
    audio.currentTime = value;
    setCurrentTime(value);
    const detectedIndex = findSentenceIndex(sentences, value);
    activeIndexRef.current = detectedIndex;
    setActiveIndex(detectedIndex);
  }

  function seekBy(deltaSeconds: number) {
    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    const audioDuration = Number.isFinite(audio.duration) ? audio.duration : duration;
    const rawTime = audio.currentTime + deltaSeconds;
    const nextTime = audioDuration > 0 ? Math.min(audioDuration, rawTime) : rawTime;
    handleSeek(Math.max(0, nextTime));
  }

  function adjustPlaybackRate(direction: -1 | 1) {
    const nearestIndex = SPEEDS.reduce((bestIndex, speed, index) => {
      const bestDistance = Math.abs(SPEEDS[bestIndex] - playbackRate);
      const nextDistance = Math.abs(speed - playbackRate);
      return nextDistance < bestDistance ? index : bestIndex;
    }, 0);
    const nextIndex = Math.min(SPEEDS.length - 1, Math.max(0, nearestIndex + direction));
    setPlaybackRate(SPEEDS[nextIndex]);
  }

  useEffect(() => {
    function handleKeyboardShortcut(event: KeyboardEvent) {
      if (shouldIgnoreKeyboardShortcut(event) || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      if (event.shiftKey && isKey(event, "ArrowLeft")) {
        event.preventDefault();
        setCurrentSentence(activeIndexRef.current - 1, isPlaying);
        return;
      }

      if (event.shiftKey && isKey(event, "ArrowRight")) {
        event.preventDefault();
        setCurrentSentence(activeIndexRef.current + 1, isPlaying);
        return;
      }

      if (isSpaceKey(event)) {
        if (!event.repeat) {
          event.preventDefault();
          togglePlayback();
        }
        return;
      }

      switch (event.key) {
        case "ArrowLeft":
          event.preventDefault();
          seekBy(-SEEK_STEP_SECONDS);
          return;
        case "ArrowRight":
          event.preventDefault();
          seekBy(SEEK_STEP_SECONDS);
          return;
        case "ArrowUp":
        case "PageUp":
          event.preventDefault();
          setCurrentSentence(activeIndexRef.current - 1, isPlaying);
          return;
        case "ArrowDown":
        case "PageDown":
          event.preventDefault();
          setCurrentSentence(activeIndexRef.current + 1, isPlaying);
          return;
        case "Home":
          event.preventDefault();
          setCurrentSentence(0, isPlaying);
          return;
        case "End":
          event.preventDefault();
          setCurrentSentence(sentences.length - 1, isPlaying);
          return;
        default:
          break;
      }

      const key = event.key.toLowerCase();
      switch (key) {
        case "k":
          if (!event.repeat) {
            event.preventDefault();
            togglePlayback();
          }
          return;
        case "j":
          event.preventDefault();
          seekBy(-SEEK_STEP_SECONDS);
          return;
        case "l":
          event.preventDefault();
          seekBy(SEEK_STEP_SECONDS);
          return;
        case "r":
          if (!event.repeat) {
            event.preventDefault();
            setCurrentSentence(activeIndexRef.current, isPlaying);
          }
          return;
        case "s":
          if (!event.repeat) {
            event.preventDefault();
            setShowSubtitles((value) => !value);
          }
          return;
        case "f":
          if (!event.repeat) {
            event.preventDefault();
            setFillMode((value) => !value);
          }
          return;
        case "a":
          if (!event.repeat) {
            event.preventDefault();
            setAutoAdvance((value) => !value);
          }
          return;
        case "m":
          if (!event.repeat) {
            event.preventDefault();
            setShowSentenceList((value) => !value);
          }
          return;
        case ",":
          event.preventDefault();
          adjustPlaybackRate(-1);
          return;
        case ".":
          event.preventDefault();
          adjustPlaybackRate(1);
          return;
        default:
          break;
      }
    }

    window.addEventListener("keydown", handleKeyboardShortcut);
    return () => window.removeEventListener("keydown", handleKeyboardShortcut);
  }, [activeIndex, duration, isPlaying, playbackRate, sentences]);

  return (
    <section className="flex min-h-screen flex-col bg-neutral-950 text-white">
      <audio
        ref={audioRef}
        src={audioUrl}
        preload="metadata"
        className="hidden"
        onLoadedMetadata={(event) => {
          const audio = event.currentTarget;
          setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
          if (sentences[0]) {
            setCurrentSentence(0, false);
          }
        }}
        onPlay={() => setIsPlaying(true)}
        onPause={() => setIsPlaying(false)}
        onEnded={() => setIsPlaying(false)}
        onTimeUpdate={handleTimeUpdate}
      />

      <header className="flex h-14 shrink-0 items-center justify-between border-b border-white/10 bg-neutral-900 px-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={onBack}
            className="flex h-10 w-10 items-center justify-center rounded-md text-2xl text-white hover:bg-white/10"
            aria-label="Back"
          >
            &larr;
          </button>
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold sm:text-lg">{title}</h2>
            {clipLabel && <p className="truncate text-xs text-neutral-400">{clipLabel}</p>}
          </div>
        </div>
        <div className="flex h-9 w-9 items-center justify-center rounded-full border border-white/20 text-sm font-semibold text-neutral-200">
          {sentences.length}
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col">
        <div className="relative flex min-h-[240px] shrink-0 items-center justify-center bg-black sm:min-h-[320px] lg:min-h-[380px]">
          <div className="absolute inset-x-0 top-0 h-px bg-white/10" />
          <div className="flex aspect-video w-full max-w-4xl items-center justify-center bg-neutral-900">
            <button
              type="button"
              onClick={togglePlayback}
              className="flex h-20 w-20 items-center justify-center rounded-full bg-white/90 text-3xl font-semibold text-neutral-950 shadow-lg hover:bg-white"
              aria-label={isPlaying ? "Pause" : "Play"}
              aria-keyshortcuts="Space K"
            >
              {isPlaying ? "||" : ">"}
            </button>
          </div>
          <input
            type="range"
            min={0}
            max={duration || 0}
            step={0.01}
            value={progressValue}
            onChange={(event) => handleSeek(Number(event.target.value))}
            className="absolute inset-x-0 bottom-0 h-1 w-full cursor-pointer accent-teal-500"
            aria-label="Audio progress"
          />
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2 border-y border-white/10 bg-neutral-900 px-3 py-2 text-sm text-neutral-300 sm:px-5">
          <label className="flex h-9 items-center gap-2 rounded-md border border-white/10 px-2">
            <span>Repeat</span>
            <input
              type="number"
              min={1}
              max={20}
              value={loopTarget}
              onChange={(event) => {
                const value = Number(event.target.value);
                setLoopTarget(Number.isFinite(value) ? Math.min(20, Math.max(1, value)) : 1);
                completedLoopsRef.current = 0;
              }}
              className="h-7 w-12 rounded border border-white/10 bg-neutral-950 px-2 text-white outline-none focus:border-teal-500"
              aria-label="Repeat count"
            />
          </label>

          <button
            type="button"
            onClick={() => setShowSubtitles((value) => !value)}
            aria-keyshortcuts="S"
            className={`h-9 rounded-md border px-3 ${
              showSubtitles
                ? "border-teal-500 bg-teal-600 text-white"
                : "border-white/10 text-neutral-300 hover:bg-white/10"
            }`}
          >
            Subtitles
          </button>

          <button
            type="button"
            onClick={() => setFillMode((value) => !value)}
            aria-keyshortcuts="F"
            className={`h-9 rounded-md border px-3 ${
              fillMode
                ? "border-teal-500 bg-teal-600 text-white"
                : "border-white/10 text-neutral-300 hover:bg-white/10"
            }`}
          >
            Fill
          </button>

          <select
            value={playbackRate}
            onChange={(event) => setPlaybackRate(Number(event.target.value))}
            className="h-9 rounded-md border border-white/10 bg-neutral-950 px-3 text-white outline-none focus:border-teal-500"
            aria-label="Playback speed"
          >
            {SPEEDS.map((speed) => (
              <option key={speed} value={speed}>
                {speed}x
              </option>
            ))}
          </select>

          <label className="ml-auto flex h-9 items-center gap-2 rounded-md border border-white/10 px-3">
            <input
              type="checkbox"
              checked={autoAdvance}
              onChange={(event) => setAutoAdvance(event.target.checked)}
              className="h-4 w-4 accent-teal-500"
              aria-keyshortcuts="A"
            />
            Auto
          </label>

          <span className="w-20 text-right tabular-nums text-neutral-400">
            {sentences.length > 0 ? activeIndex + 1 : 0} / {sentences.length}
          </span>
        </div>

        <div className="flex min-h-0 flex-1 flex-col bg-neutral-900">
          <div className="flex min-h-[220px] flex-1 items-start justify-center px-5 py-12 text-center sm:items-center sm:py-8">
            {showSubtitles && activeSentence && (
              <div className="mx-auto max-w-5xl">
                <p className="text-2xl font-medium leading-relaxed text-white sm:text-3xl">
                  {activeText}
                </p>
                <p className="mt-6 text-sm font-medium tabular-nums text-neutral-500">
                  {formatTime(activeSentence.start)} - {formatTime(activeSentence.end)}
                </p>
              </div>
            )}
          </div>

          {showSentenceList && (
            <div className="mx-3 mb-3 max-h-[34vh] overflow-y-auto rounded-md border border-white/10 bg-neutral-950 sm:mx-5">
              {sentences.map((sentence, index) => (
                <button
                  key={sentence.id}
                  type="button"
                  onClick={() => setCurrentSentence(index, true)}
                  className={`grid w-full grid-cols-[4.5rem_1fr] gap-3 border-b border-white/5 px-3 py-3 text-left last:border-b-0 ${
                    index === activeIndex ? "bg-teal-600/25" : "hover:bg-white/5"
                  }`}
                >
                  <span className="text-xs font-medium tabular-nums text-neutral-500">
                    {formatTime(sentence.start)}
                  </span>
                  <span className="text-sm leading-6 text-neutral-100">
                    {fillMode ? maskSentence(sentence.text) : sentence.text}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <footer className="grid shrink-0 grid-cols-[auto_auto_1fr_auto_auto] items-center gap-2 border-t border-white/10 bg-neutral-900 px-3 py-3 sm:px-5">
        <button
          type="button"
          onClick={() => setCurrentSentence(activeIndex - 1, isPlaying)}
          disabled={activeIndex <= 0}
          className="flex h-11 w-11 items-center justify-center rounded-md text-2xl text-white hover:bg-white/10 disabled:cursor-not-allowed disabled:text-neutral-600"
          aria-label="Previous sentence"
          aria-keyshortcuts="ArrowUp PageUp Shift+ArrowLeft"
        >
          &larr;
        </button>
        <button
          type="button"
          onClick={() => setShowSentenceList((value) => !value)}
          className="flex h-11 w-11 items-center justify-center rounded-md text-xl text-white hover:bg-white/10"
          aria-label="Sentence list"
          aria-keyshortcuts="M"
        >
          &#9776;
        </button>
        <button
          type="button"
          onClick={() => setCurrentSentence(activeIndex + 1, true)}
          disabled={activeIndex >= sentences.length - 1}
          className="h-11 min-w-0 rounded-md bg-teal-500 px-4 text-sm font-semibold text-white hover:bg-teal-600 disabled:cursor-not-allowed disabled:bg-neutral-700 sm:text-base"
          aria-keyshortcuts="ArrowDown PageDown Shift+ArrowRight"
        >
          Next sentence &rarr;
        </button>
        <button
          type="button"
          onClick={togglePlayback}
          className="flex h-11 w-11 items-center justify-center rounded-md text-xl text-white hover:bg-white/10"
          aria-label={isPlaying ? "Pause" : "Play"}
          aria-keyshortcuts="Space K"
        >
          {isPlaying ? "||" : ">"}
        </button>
        <a
          href={audioUrl}
          className="hidden h-11 items-center rounded-md border border-white/10 px-3 text-sm font-medium text-neutral-200 hover:bg-white/10 sm:flex"
        >
          MP3
        </a>
        <a href={subtitlesUrl} className="sr-only">
          JSON
        </a>
      </footer>
    </section>
  );
}

function shouldIgnoreKeyboardShortcut(event: KeyboardEvent): boolean {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return false;
  }

  if (target.isContentEditable) {
    return true;
  }

  const tagName = target.tagName.toLowerCase();
  if (tagName === "input" || tagName === "select" || tagName === "textarea") {
    return true;
  }

  return (tagName === "button" || tagName === "a") && event.key === "Enter";
}

function isSpaceKey(event: KeyboardEvent): boolean {
  return event.key === " " || event.key === "Spacebar" || event.code === "Space";
}

function isKey(event: KeyboardEvent, key: string): boolean {
  return event.key === key || event.code === key;
}

function findSentenceIndex(sentences: Sentence[], currentTime: number): number {
  const exactIndex = sentences.findIndex(
    (sentence) => currentTime >= sentence.start && currentTime < sentence.end,
  );
  if (exactIndex >= 0) {
    return exactIndex;
  }

  for (let index = sentences.length - 1; index >= 0; index -= 1) {
    if (currentTime >= sentences[index].start) {
      return index;
    }
  }
  return 0;
}

function formatTime(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(safeSeconds / 60);
  const remainingSeconds = safeSeconds % 60;
  return `${minutes}:${remainingSeconds.toString().padStart(2, "0")}`;
}

function maskSentence(text: string): string {
  return text
    .split(" ")
    .map((word, index) => {
      const cleanWord = word.replace(/[^A-Za-z0-9]/g, "");
      if (cleanWord.length <= 2 || index % 3 !== 1) {
        return word;
      }
      return word.replace(cleanWord, "_".repeat(Math.min(cleanWord.length, 10)));
    })
    .join(" ");
}
