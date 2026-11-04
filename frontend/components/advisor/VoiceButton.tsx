"use client";

import { useEffect, useRef, useState } from "react";

import { type Locale, type Messages, bcp47 } from "@/lib/i18n";

/**
 * Voice input via the browser's Web Speech API (stretch, ARCHITECTURE §5.6).
 * Speech becomes text in the input box; the owner reviews it before sending.
 * No audio is recorded or uploaded by us. Hidden where the browser lacks support.
 */
interface Recognition {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start(): void;
  stop(): void;
}

type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function VoiceButton({ locale, t, onText }: { locale: Locale; t: Messages; onText: (text: string) => void }) {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const ref = useRef<Recognition | null>(null);

  useEffect(() => setSupported(recognitionCtor() !== null), []);
  useEffect(() => () => ref.current?.stop(), []);

  if (!supported) return null;

  const toggle = () => {
    if (listening) {
      ref.current?.stop();
      return;
    }
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    const rec = new Ctor();
    rec.lang = bcp47(locale);
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript;
      if (transcript) onText(transcript);
    };
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);
    ref.current = rec;
    setListening(true);
    rec.start();
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={listening}
      className="h-12 rounded-xl border border-neutral-300 px-3 text-sm dark:border-neutral-600"
    >
      {listening ? t.advisor.listening : t.advisor.voice}
    </button>
  );
}
