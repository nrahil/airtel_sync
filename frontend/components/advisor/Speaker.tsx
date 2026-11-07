"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { type Locale, type Messages, bcp47 } from "@/lib/i18n";

/**
 * Spoken replies (stretch S1): the browser's own text-to-speech reads each new advisor
 * message aloud in the chosen language. Nothing is sent anywhere by us. The choice is
 * remembered on this device only.
 *
 * A Hindi sentence read by an English voice is gibberish, so if the phone has no voice
 * for the language we say so instead of speaking.
 */
const PREF_KEY = "udyam.speak";

function synth(): SpeechSynthesis | null {
  return typeof window !== "undefined" && "speechSynthesis" in window ? window.speechSynthesis : null;
}

/** A voice for this language ("hi-IN", else any "hi-*"), or null. */
function voiceFor(voices: SpeechSynthesisVoice[], locale: Locale): SpeechSynthesisVoice | null {
  const norm = (lang: string) => lang.toLowerCase().replace("_", "-");
  const exact = norm(bcp47(locale));
  return (
    voices.find((v) => norm(v.lang) === exact) ?? voices.find((v) => norm(v.lang).split("-")[0] === locale) ?? null
  );
}

function readPref(): boolean {
  try {
    return localStorage.getItem(PREF_KEY) === "1";
  } catch {
    return false;
  }
}

function writePref(on: boolean): void {
  try {
    localStorage.setItem(PREF_KEY, on ? "1" : "0");
  } catch {
    /* private mode: just not remembered */
  }
}

export interface Speaker {
  supported: boolean;
  on: boolean;
  /** The phone has no voice for this language (only known once voices have loaded). */
  noVoice: boolean;
  toggle: (latest?: string[]) => void;
  speak: (texts: string[]) => void;
  stop: () => void;
}

export function useSpeaker(locale: Locale): Speaker {
  const [supported, setSupported] = useState(false);
  const [on, setOn] = useState(false);
  const [voice, setVoice] = useState<SpeechSynthesisVoice | null>(null);
  const [voicesLoaded, setVoicesLoaded] = useState(false);
  const onRef = useRef(false);
  const voiceRef = useRef<SpeechSynthesisVoice | null>(null);

  useEffect(() => {
    const s = synth();
    if (!s) return;
    setSupported(true);
    const pref = readPref();
    setOn(pref);
    onRef.current = pref;
    const load = () => {
      const voices = s.getVoices();
      if (!voices.length) return; // Chrome fills the list a moment later
      const found = voiceFor(voices, locale);
      voiceRef.current = found;
      setVoice(found);
      setVoicesLoaded(true);
    };
    load();
    s.addEventListener?.("voiceschanged", load);
    // Some browsers never fire the event and never list voices: treat that as "none".
    const giveUp = window.setTimeout(() => setVoicesLoaded(true), 2000);
    return () => {
      s.removeEventListener?.("voiceschanged", load);
      window.clearTimeout(giveUp);
      s.cancel();
    };
  }, [locale]);

  const stop = useCallback(() => synth()?.cancel(), []);

  const say = useCallback((texts: string[]) => {
    const s = synth();
    const v = voiceRef.current;
    if (!s || !v) return;
    s.cancel(); // never talk over the owner's next step
    for (const text of texts) {
      const trimmed = text.trim();
      if (!trimmed) continue;
      const u = new SpeechSynthesisUtterance(trimmed);
      u.voice = v;
      u.lang = v.lang;
      u.rate = 0.95;
      s.speak(u);
    }
  }, []);

  const speak = useCallback(
    (texts: string[]) => {
      if (onRef.current) say(texts);
    },
    [say],
  );

  const toggle = useCallback(
    (latest: string[] = []) => {
      const next = !onRef.current;
      onRef.current = next;
      setOn(next);
      writePref(next);
      if (next) say(latest);
      else stop();
    },
    [say, stop],
  );

  return { supported, on, noVoice: voicesLoaded && voice === null, toggle, speak, stop };
}

export function SpeakerToggle({ speaker, t, latest }: { speaker: Speaker; t: Messages; latest: () => string[] }) {
  if (!speaker.supported) return null;
  return (
    <div className="flex flex-col gap-1">
      <button
        type="button"
        data-testid="speaker"
        aria-pressed={speaker.on}
        onClick={() => speaker.toggle(latest())}
        className={`self-start rounded-full px-3 py-1.5 text-sm ring-1 ${
          speaker.on
            ? "bg-red-700 text-white ring-red-700"
            : "ring-neutral-300 dark:ring-neutral-600"
        }`}
      >
        <span aria-hidden="true">{speaker.on ? "🔊" : "🔈"}</span> {speaker.on ? t.advisor.speakStop : t.advisor.speak}
      </button>
      {speaker.on && speaker.noVoice && (
        <p role="status" data-testid="speaker-no-voice" className="text-sm text-amber-800 dark:text-amber-200">
          {t.advisor.speakNoVoice}
        </p>
      )}
    </div>
  );
}
