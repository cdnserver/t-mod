import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  DEFAULT_ATLAS_OVERLAY_CONFIG,
  initialAtlasOverlayState,
  reduceAtlasOverlayState,
} from "../../shared/atlas-overlay";
import type {
  AtlasOverlayConfig,
  AtlasOverlayEvent,
  AtlasOverlayPttPhase,
} from "../../shared/atlas-overlay";
import { IncrementalRussianSpeech } from "./overlaySpeech";
import { OverlayVoiceCapture } from "./voiceCapture";
import "./atlas-overlay.css";

function AtlasMark() {
  return (
    <svg viewBox="0 0 56 56" aria-hidden="true">
      <defs>
        <linearGradient id="atlas-overlay-globe" x1="7" y1="7" x2="48" y2="49">
          <stop stopColor="#95dfff" />
          <stop offset=".52" stopColor="#567dff" />
          <stop offset="1" stopColor="#9f67ff" />
        </linearGradient>
      </defs>
      <circle cx="28" cy="29" r="17" fill="none" stroke="url(#atlas-overlay-globe)" strokeWidth="2.2" />
      <ellipse cx="28" cy="29" rx="8" ry="17" fill="none" stroke="currentColor" strokeOpacity=".55" />
      <path d="M11 29h34M14.7 20.5h26.6M14.7 37.5h26.6" fill="none" stroke="currentColor" strokeOpacity=".45" />
      <path d="m43 7 1.8 4.6L49 13.5l-4.2 1.8-1.8 4.6-1.8-4.6-4.2-1.8 4.2-1.9L43 7Z" fill="#dff7ff" />
    </svg>
  );
}

function VoiceField({ active }: { active: boolean }) {
  return (
    <div className={`atlas-voice-field${active ? " is-active" : ""}`} aria-hidden="true">
      {Array.from({ length: 13 }, (_, index) => (
        <i key={index} style={{ "--bar": index } as CSSProperties} />
      ))}
    </div>
  );
}

function ThinkingField() {
  return (
    <div className="atlas-thinking-field" aria-hidden="true">
      <i /><i /><i />
      <span />
    </div>
  );
}

function captureError(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "Разрешите T-Mod доступ к микрофону.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Не удалось записать голосовую команду.";
}

function playOverlayCue(kind: "listen" | "release" | "ready" | "error"): void {
  if (typeof AudioContext === "undefined") return;
  const context = new AudioContext();
  const now = context.currentTime;
  const master = context.createGain();
  master.gain.setValueAtTime(.0001, now);
  master.gain.exponentialRampToValueAtTime(kind === "error" ? .035 : .048, now + .025);
  master.gain.exponentialRampToValueAtTime(.0001, now + .46);
  master.connect(context.destination);
  const notes = kind === "listen"
    ? [392, 587.33]
    : kind === "release"
      ? [493.88]
      : kind === "ready"
        ? [523.25, 783.99]
        : [220, 174.61];
  notes.forEach((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const offset = index * .075;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, now + offset);
    gain.gain.setValueAtTime(.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(1 / (index + 1), now + offset + .018);
    gain.gain.exponentialRampToValueAtTime(.0001, now + offset + .3);
    oscillator.connect(gain).connect(master);
    oscillator.start(now + offset);
    oscillator.stop(now + offset + .32);
  });
  void context.resume().catch(() => undefined);
  window.setTimeout(() => void context.close(), 550);
}

export function AtlasOverlay() {
  const api = window.tmodAtlasOverlay;
  const preview = (import.meta.env.DEV || location.protocol === "file:" || ["localhost", "127.0.0.1"].includes(location.hostname))
    ? new URLSearchParams(location.search).get("preview")
    : null;
  const previewEnabled = Boolean(preview && !api);
  const [state, dispatch] = useReducer(
    reduceAtlasOverlayState,
    initialAtlasOverlayState,
    (initial) => {
      if (!previewEnabled) return initial;
      if (preview === "listening") {
        return reduceAtlasOverlayState(initial, { type: "ptt", phase: "down" });
      }
      if (preview === "thinking") {
        const withQuestion = reduceAtlasOverlayState(initial, {
          type: "transcript",
          text: "Могу ли я проводить обыск без ордера?",
        });
        return reduceAtlasOverlayState(withQuestion, {
          type: "progress",
          stage: "searching",
          label: "Сверяю основания и исключения",
        });
      }
      if (preview === "idle") {
        return reduceAtlasOverlayState(
          reduceAtlasOverlayState(initial, { type: "show" }),
          { type: "idle" },
        );
      }
      const withQuestion = reduceAtlasOverlayState(initial, {
        type: "transcript",
        text: "Могу ли я проводить обыск без ордера?",
      });
      return reduceAtlasOverlayState(withQuestion, {
        type: "done",
        answer: "Только при наличии предусмотренного законом исключения. Зафиксируйте конкретное основание, разъясните его участнику и не выходите за пределы необходимого досмотра.",
        citations: [
          { index: 1, title: "Процессуальный кодекс · порядок обыска" },
          { index: 2, title: "Закон о правоохранительных органах" },
        ],
        latencyMs: 1_840,
      });
    },
  );
  const [config, setConfig] = useState<AtlasOverlayConfig>(() => previewEnabled
    ? {
        ...DEFAULT_ATLAS_OVERLAY_CONFIG,
        enabled: true,
        characterName: "S. Goodman",
        factionCode: "gov",
      }
    : { ...DEFAULT_ATLAS_OVERLAY_CONFIG });
  const capture = useMemo(() => new OverlayVoiceCapture(), []);
  const speech = useMemo(() => new IncrementalRussianSpeech(), []);
  const mounted = useRef(true);
  const aiAudio = useRef<{ audio: HTMLAudioElement; url: string } | undefined>(undefined);

  const stopAiAudio = () => {
    const current = aiAudio.current;
    if (!current) return;
    current.audio.pause();
    URL.revokeObjectURL(current.url);
    aiAudio.current = undefined;
  };

  useEffect(() => {
    mounted.current = true;
    void api?.getConfig().then((next) => {
      if (mounted.current) setConfig(next);
    });
    return () => {
      mounted.current = false;
      capture.cancel();
      speech.cancel();
      stopAiAudio();
    };
  }, [api, capture, speech]);

  useEffect(() => {
    speech.configure({
      enabled: config.speakAnswers && config.speechProvider === "system",
      rate: config.speechRate,
      volume: config.speechVolume,
      voiceName: config.speechVoice,
    });
  }, [config.speakAnswers, config.speechProvider, config.speechRate, config.speechVolume, config.speechVoice, speech]);

  useEffect(() => {
    if (!api) return undefined;
    const receive = (event: AtlasOverlayEvent) => {
      if (event.type === "config") {
        setConfig(event.config);
        return;
      }
      if (event.type === "speech") {
        if (event.audio && event.mimeType) {
          stopAiAudio();
          const url = URL.createObjectURL(new Blob([event.audio], { type: event.mimeType }));
          const audio = new Audio(url);
          audio.volume = config.speechVolume;
          audio.onended = audio.onerror = () => {
            if (aiAudio.current?.audio === audio) {
              URL.revokeObjectURL(url);
              aiAudio.current = undefined;
            }
          };
          aiAudio.current = { audio, url };
          void audio.play().catch(() => {
            stopAiAudio();
            if (event.fallbackText) speech.speakNow(event.fallbackText);
          });
        } else if (event.fallbackText) {
          speech.speakNow(event.fallbackText);
        }
        return;
      }
      if (event.type === "delta" && config.speechProvider === "system") speech.append(event.text);
      else if (event.type === "done") {
        if (config.speechProvider === "system") speech.finish();
        playOverlayCue("ready");
      } else if (["hide", "idle", "error"].includes(event.type)) {
        speech.cancel();
        if (event.type === "error") playOverlayCue("error");
      }
      dispatch(event);
    };
    return api.onEvent(receive);
  }, [api, config.speechProvider, config.speechVolume, speech]);

  useEffect(() => {
    if (!api) return undefined;
    const ptt = async (phase: AtlasOverlayPttPhase) => {
      dispatch({ type: "ptt", phase });
      if (phase === "down") {
        speech.cancel();
        stopAiAudio();
        playOverlayCue("listen");
        try {
          await capture.start(config.microphoneId);
        } catch (error) {
          dispatch({ type: "error", message: captureError(error), retryable: true });
        }
        return;
      }
      if (phase === "cancel") {
        capture.cancel();
        // The main process already aborted the request before broadcasting
        // this phase. Calling cancel back through IPC would create a loop.
        return;
      }
      try {
        playOverlayCue("release");
        const recording = await capture.stop();
        if (!recording) {
          dispatch({ type: "error", message: "Голос не записан. Удерживайте клавиши чуть дольше.", retryable: true });
          return;
        }
        const result = await api.submitAudio(recording);
        if (!result.accepted) {
          dispatch({
            type: "error",
            message: result.error || "Atlas не принял запись.",
            retryable: true,
          });
        }
      } catch (error) {
        dispatch({ type: "error", message: captureError(error), retryable: true });
      }
    };
    return api.onPtt((phase) => void ptt(phase));
  }, [api, capture, config.microphoneId, speech]);

  const stageBusy = ["transcribing", "searching", "thinking"].includes(state.stage);
  const style = {
    "--overlay-opacity": config.opacity,
    "--overlay-scale": config.scale,
  } as CSSProperties;
  const horizontal = config.positionX < 0.34 ? "left" : config.positionX > 0.66 ? "right" : "center";
  const vertical = config.positionY < 0.34 ? "top" : config.positionY > 0.66 ? "bottom" : "center";

  return (
    <main
      className={`atlas-overlay atlas-overlay--${config.anchor}${state.visible ? " is-visible" : ""}`}
      data-stage={state.stage}
      data-horizontal={horizontal}
      data-vertical={vertical}
      style={style}
    >
      <section className="atlas-overlay-card" aria-live="polite" aria-atomic="false">
        <div className="atlas-overlay-aurora" aria-hidden="true" />
        <header className="atlas-overlay-head">
          <div className="atlas-overlay-brand">
            <span className="atlas-overlay-mark"><AtlasMark /></span>
            <span>
              <strong>ATLAS</strong>
              <small>LIVE INTELLIGENCE</small>
            </span>
          </div>
          <div className="atlas-overlay-status">
            <i />
            <span>{state.statusLabel}</span>
          </div>
        </header>

        <div className="atlas-overlay-body">
          {state.stage === "listening" && (
            <div className="atlas-overlay-listening">
              <VoiceField active />
              <strong>Говорите свободно</strong>
              <span>Отпустите {config.hotkey.replaceAll("+", " · ")}, когда закончите</span>
            </div>
          )}

          {stageBusy && (
            <div className="atlas-overlay-thinking">
              <ThinkingField />
              {state.transcript && <p>«{state.transcript}»</p>}
              <span>{state.stage === "transcribing" ? "Выделяю речь" : "Сверяю нормы и контекст"}</span>
            </div>
          )}

          {state.stage === "answer" && (
            <article className="atlas-overlay-answer">
              {state.transcript && <p className="atlas-overlay-question">{state.transcript}</p>}
              <p className="atlas-overlay-copy">{state.answer}<span className="atlas-overlay-caret" /></p>
              {state.citations.length > 0 && (
                <div className="atlas-overlay-sources">
                  {state.citations.slice(0, 2).map((citation, index) => (
                    <span key={`${citation.index || index}-${citation.title}`}>
                      {citation.index || index + 1}. {citation.title}
                    </span>
                  ))}
                </div>
              )}
            </article>
          )}

          {state.stage === "error" && (
            <div className="atlas-overlay-error">
              <span className="atlas-overlay-error-mark">!</span>
              <div><strong>Связь с Atlas прервана</strong><p>{state.error}</p></div>
            </div>
          )}

          {state.stage === "idle" && (
            <div className="atlas-overlay-idle">
              <span>{config.characterName || "Atlas готов к работе"}</span>
              <kbd>{config.hotkey.replaceAll("+", "  +  ")}</kbd>
            </div>
          )}
        </div>

        <footer className="atlas-overlay-foot">
          <span>{config.serverCode.toUpperCase()} · {config.factionCode.toUpperCase()}</span>
          {state.latencyMs !== undefined && <span>{Math.max(0, state.latencyMs / 1_000).toFixed(1)} s</span>}
          <span className="atlas-overlay-mode">T-MOD DESKTOP</span>
        </footer>
      </section>
    </main>
  );
}
