import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import {
  DEFAULT_ATLAS_OVERLAY_CONFIG,
  initialAtlasOverlayState,
  reduceAtlasOverlayState,
} from "../../shared/atlas-overlay";
import type {
  AtlasOverlayConfig,
  AtlasOverlayCraftPlan,
  AtlasOverlayCraftSnapshot,
  AtlasOverlayEvent,
  AtlasOverlayPttPhase,
} from "../../shared/atlas-overlay";
import { IncrementalRussianSpeech, normalizeRussianKeyboardInput } from "./overlaySpeech";
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
      <circle className="atlas-mark-globe" cx="28" cy="29" r="17" fill="none" stroke="url(#atlas-overlay-globe)" strokeWidth="2.2" />
      <ellipse className="atlas-mark-meridian" cx="28" cy="29" rx="8" ry="17" fill="none" stroke="currentColor" strokeOpacity=".55" />
      <path className="atlas-mark-grid" d="M11 29h34M14.7 20.5h26.6M14.7 37.5h26.6" fill="none" stroke="currentColor" strokeOpacity=".45" />
      <path className="atlas-mark-star" d="m43 7 1.8 4.6L49 13.5l-4.2 1.8-1.8 4.6-1.8-4.6-4.2-1.8 4.2-1.9L43 7Z" fill="#dff7ff" />
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
      <b /><b /><b /><b />
      <span />
    </div>
  );
}

function InitializationField() {
  return (
    <div className="atlas-initialization-field" aria-hidden="true">
      <div className="atlas-init-sky"><i/><i/><i/><i/><i/></div>
      <span className="atlas-init-axis" />
      <span className="atlas-init-sweep" />
      <span className="atlas-init-glyph"><AtlasMark /></span>
      <div className="atlas-init-sequence"><b/><b/><b/><b/></div>
    </div>
  );
}

const craftStageLabels: Record<string, string> = {
  procurement: "Подготовка материалов",
  crafting: "Производство",
  awaiting_output: "Заберите результат",
  listing: "Подготовка к продаже",
  selling: "На продаже",
};

function craftCountdown(dueAt: string | undefined, now: number): string {
  if (!dueAt) return "ОЖИДАЕТ ЗАПУСКА";
  const seconds = Math.ceil((Date.parse(dueAt) - now) / 1_000);
  if (!Number.isFinite(seconds)) return "ВРЕМЯ УТОЧНЯЕТСЯ";
  if (seconds <= 0) return "ЦИКЛ ЗАВЕРШЁН";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function CraftPlanCard({ plan, now }: { plan: AtlasOverlayCraftPlan; now: number }) {
  const batch = plan.active_batch || null;
  const progress = plan.attempts_total > 0
    ? Math.min(100, Math.round(plan.attempts_completed / plan.attempts_total * 100))
    : 0;
  return <article className={`atlas-craft-plan${plan.needs_next_batch ? " needs-action" : ""}`}>
    <header>
      <span><small>ПЛАН #{plan.id}</small><strong>{plan.product_name}</strong></span>
      {plan.mine && <em>МОЙ</em>}
    </header>
    <div className="atlas-craft-clock">
      <small>{plan.needs_next_batch ? "ПОРА СТАВИТЬ СЛЕДУЮЩИЙ" : craftStageLabels[plan.stage] || "Крафт"}</small>
      <strong>{plan.needs_next_batch ? "ГОТОВ К НОВОМУ ЦИКЛУ" : craftCountdown(batch?.due_at, now)}</strong>
    </div>
    <div className="atlas-craft-progress"><i style={{ width: `${progress}%` }} /></div>
    <footer>
      <span>{plan.attempts_completed} / {plan.attempts_total} завершено</span>
      <span>{batch ? `${batch.quantity} шт. в работе` : `${plan.remaining_to_queue} осталось поставить`}</span>
    </footer>
  </article>;
}

function playCraftAlarm(level: number): void {
  if (typeof AudioContext === "undefined" || level <= 0) return;
  const context = new AudioContext();
  const master = context.createGain();
  const now = context.currentTime;
  master.gain.setValueAtTime(.0001, now);
  master.gain.exponentialRampToValueAtTime(.075 * clamp(level, 0, 1), now + .04);
  master.gain.exponentialRampToValueAtTime(.0001, now + 2.2);
  master.connect(context.destination);
  [261.63, 392, 523.25, 659.25].forEach((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const start = now + index * .24;
    oscillator.type = index % 2 ? "triangle" : "sine";
    oscillator.frequency.setValueAtTime(frequency, start);
    oscillator.frequency.exponentialRampToValueAtTime(frequency * 1.012, start + .65);
    gain.gain.setValueAtTime(.0001, start);
    gain.gain.exponentialRampToValueAtTime(.8 / (index + 1), start + .03);
    gain.gain.exponentialRampToValueAtTime(.0001, start + .78);
    oscillator.connect(gain).connect(master);
    oscillator.start(start);
    oscillator.stop(start + .82);
  });
  void context.resume().catch(() => undefined);
  window.setTimeout(() => void context.close(), 2_500);
}

function captureError(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "Разрешите T-Mod доступ к микрофону.";
  }
  if (error instanceof Error && error.message) return error.message;
  return "Не удалось записать голосовую команду.";
}

function playOverlayCue(kind: "listen" | "release" | "ready" | "error", level = 0.58): void {
  if (typeof AudioContext === "undefined") return;
  const context = new AudioContext();
  const now = context.currentTime;
  const master = context.createGain();
  master.gain.setValueAtTime(.0001, now);
  const volume = clamp(level, 0, 1);
  master.gain.exponentialRampToValueAtTime((kind === "error" ? .035 : .048) * volume, now + .025);
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

type OverlayExperienceConfig = AtlasOverlayConfig & {
  calibrationMode?: boolean;
  fontScale?: number;
};

type OverlayExperiencePatch = Partial<AtlasOverlayConfig> & {
  calibrationMode?: boolean;
  fontScale?: number;
};

type CalibrationDrag = {
  pointerId: number;
  startX: number;
  startY: number;
  offsetX: number;
  offsetY: number;
};

type AtlasSpeechEvent = Extract<AtlasOverlayEvent, { type: "speech" }>;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function isTextEntryTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    (target instanceof HTMLElement && target.isContentEditable);
}

function manualTextFromEvent(event: Event): string {
  const detail = (event as CustomEvent<unknown>).detail;
  if (!detail || typeof detail !== "object") return "";
  const source = detail as Record<string, unknown>;
  return String(source.text || source.initialText || "").slice(0, 4_000);
}

export function AtlasOverlay() {
  const api = window.tmodAtlasOverlay;
  const previewParams = new URLSearchParams(location.search);
  const preview = (import.meta.env.DEV || location.protocol === "file:" || ["localhost", "127.0.0.1"].includes(location.hostname))
    ? previewParams.get("preview")
    : null;
  const previewEnabled = Boolean(preview && !api);
  const craftPreviewState = previewParams.get("craftState") === "active" ? "active" : "alert";
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
      if (preview === "crafts") {
        return reduceAtlasOverlayState(
          reduceAtlasOverlayState(initial, { type: "show" }),
          { type: "idle" },
        );
      }
      if (preview === "initializing") {
        return reduceAtlasOverlayState(initial, { type: "initialized", name: "Иван" });
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
        idleStyle: (["orb", "bar", "full"].includes(String(previewParams.get("idle")))
          ? previewParams.get("idle")
          : "bar") as AtlasOverlayConfig["idleStyle"],
        theme: (["cosmos", "graphite", "emerald", "amber", "crimson"].includes(String(previewParams.get("theme")))
          ? previewParams.get("theme")
          : "cosmos") as AtlasOverlayConfig["theme"],
        workspaceMode: preview === "crafts" ? "crafts" : "assistant",
      }
    : { ...DEFAULT_ATLAS_OVERLAY_CONFIG });
  const capture = useMemo(() => new OverlayVoiceCapture(), []);
  const speech = useMemo(() => new IncrementalRussianSpeech(), []);
  const mounted = useRef(true);
  const configRef = useRef(config);
  configRef.current = config;
  const aiAudio = useRef<{ audio: HTMLAudioElement; url: string } | undefined>(undefined);
  const aiAudioQueue = useRef<AtlasSpeechEvent[]>([]);
  const aiSpeechActive = useRef(false);
  const aiAudioGeneration = useRef(0);
  const playNextAiAudioRef = useRef<() => void>(() => undefined);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const calibrationDrag = useRef<CalibrationDrag | undefined>(undefined);
  const [manualQueryOpen, setManualQueryOpen] = useState(false);
  const [manualQuery, setManualQuery] = useState("");
  const [manualQueryError, setManualQueryError] = useState("");
  const [manualSubmitting, setManualSubmitting] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const [calibrationFeedback, setCalibrationFeedback] = useState("");
  const [crafts, setCrafts] = useState<AtlasOverlayCraftSnapshot | null>(() => preview === "crafts" ? {
    server_time: new Date().toISOString(),
    revision: "preview-crafts",
    attention_count: craftPreviewState === "alert" ? 1 : 0,
    plans: craftPreviewState === "alert" ? [
      {
        id: 74, product_name: "Бронепластины", stage: "crafting", responsible: "Роберт",
        mine: true, attempts_total: 120, attempts_queued: 40, attempts_completed: 40,
        remaining_to_queue: 80, product_stock: 40, materials: [], active_batch: null,
        needs_next_batch: true, alarm_key: "preview-next",
      },
      {
        id: 71, product_name: "Промышленные металлы", stage: "crafting", responsible: "Команда",
        mine: false, attempts_total: 90, attempts_queued: 60, attempts_completed: 45,
        remaining_to_queue: 30, product_stock: 45, materials: [],
        active_batch: { id: 9, quantity: 15, due_at: new Date(Date.now() + 12 * 60_000 + 34_000).toISOString() },
        needs_next_batch: false,
      },
    ] : [
      {
        id: 71, product_name: "Промышленные металлы", stage: "crafting", responsible: "Роберт",
        mine: true, attempts_total: 90, attempts_queued: 60, attempts_completed: 45,
        remaining_to_queue: 30, product_stock: 45, materials: [],
        active_batch: { id: 9, quantity: 15, due_at: new Date(Date.now() + 12 * 60_000 + 34_000).toISOString() },
        needs_next_batch: false,
      },
    ],
  } : null);
  const [craftAlarmKey, setCraftAlarmKey] = useState(
    preview === "crafts" && craftPreviewState === "alert" ? "preview-next" : "",
  );
  const [clock, setClock] = useState(() => Date.now());

  const stopAiAudio = useCallback(() => {
    aiAudioGeneration.current += 1;
    aiAudioQueue.current = [];
    aiSpeechActive.current = false;
    void api?.reportSpeech(false);
    const current = aiAudio.current;
    if (!current) return;
    current.audio.onended = null;
    current.audio.onerror = null;
    current.audio.pause();
    current.audio.removeAttribute("src");
    current.audio.load();
    URL.revokeObjectURL(current.url);
    aiAudio.current = undefined;
  }, [api]);

  const playNextAiAudio = useCallback(() => {
    if (aiSpeechActive.current) return;
    const event = aiAudioQueue.current.shift();
    if (!event) return;
    const currentConfig = configRef.current;
    if (
      !currentConfig.speakAnswers ||
      currentConfig.speechProvider !== "ai"
    ) {
      aiAudioQueue.current = [];
      return;
    }

    aiSpeechActive.current = true;
    const advance = () => {
      aiSpeechActive.current = false;
      if (aiAudioQueue.current.length) playNextAiAudioRef.current();
      else void api?.reportSpeech(false);
    };
    if (!event.audio || !event.mimeType) {
      if (event.fallbackText) {
        void Promise.resolve(speech.speakFallback(event.fallbackText)).finally(advance);
      } else {
        advance();
      }
      return;
    }

    const generation = aiAudioGeneration.current;
    const url = URL.createObjectURL(new Blob([event.audio], { type: event.mimeType }));
    const audio = new Audio(url);
    let settled = false;
    const release = () => {
      if (aiAudio.current?.audio === audio) {
        URL.revokeObjectURL(url);
        aiAudio.current = undefined;
      }
    };
    const settle = (useFallback: boolean) => {
      if (settled) return;
      settled = true;
      if (generation !== aiAudioGeneration.current || aiAudio.current?.audio !== audio) return;
      release();
      const nextConfig = configRef.current;
      if (
        useFallback &&
        nextConfig.speakAnswers &&
        nextConfig.speechProvider === "ai" &&
        event.fallbackText
      ) {
        void Promise.resolve(speech.speakFallback(event.fallbackText)).finally(advance);
      } else {
        advance();
      }
    };

    audio.preload = "auto";
    audio.setAttribute("playsinline", "");
    audio.volume = clamp(currentConfig.speechVolume, 0, 1);
    audio.onended = () => settle(false);
    audio.onerror = () => settle(true);
    aiAudio.current = { audio, url };
    void audio.play().catch(() => settle(true));
  }, [api, speech]);

  playNextAiAudioRef.current = playNextAiAudio;

  const playAiAudio = useCallback((event: AtlasSpeechEvent): boolean => {
    const currentConfig = configRef.current;
    if (
      !currentConfig.speakAnswers ||
      currentConfig.speechProvider !== "ai" ||
      (!event.audio && !event.fallbackText)
    ) return false;

    // A request can now deliver a sentence at a time. The queue ensures that
    // sentence N never cuts off N-1, regardless of AI or explicit fallback.
    if (!aiSpeechActive.current && !aiAudioQueue.current.length) speech.cancel();
    void api?.reportSpeech(true);
    aiAudioQueue.current.push(event);
    playNextAiAudioRef.current();
    return true;
  }, [api, speech]);

  useEffect(() => {
    speech.setActivityListener((active) => void api?.reportSpeech(active));
    return () => speech.setActivityListener(undefined);
  }, [api, speech]);

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
  }, [api, capture, speech, stopAiAudio]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    speech.configure({
      enabled: config.speakAnswers && config.speechProvider === "system",
      rate: config.speechRate,
      volume: config.speechVolume,
      voiceName: config.speechVoice,
    });
    if (!config.speakAnswers || config.speechProvider !== "ai") {
      stopAiAudio();
    } else if (aiAudio.current) {
      aiAudio.current.audio.volume = clamp(config.speechVolume, 0, 1);
    }
  }, [
    config.speakAnswers,
    config.speechProvider,
    config.speechRate,
    config.speechVolume,
    config.speechVoice,
    speech,
    stopAiAudio,
  ]);

  const experienceConfig = config as OverlayExperienceConfig;
  const calibrationMode = experienceConfig.calibrationMode === true;
  const fontScale = clamp(Number(experienceConfig.fontScale) || 1, 0.82, 1.4);
  const normalizedManualQuery = useMemo(
    () => normalizeRussianKeyboardInput(manualQuery),
    [manualQuery],
  );

  const openManualQuery = useCallback((initialText = "") => {
    capture.cancel();
    speech.cancel();
    stopAiAudio();
    setManualQuery(initialText.slice(0, 4_000));
    setManualQueryError("");
    setManualSubmitting(false);
    setManualQueryOpen(true);
    dispatch({ type: "show" });
  }, [capture, speech, stopAiAudio]);

  const closeManualQuery = useCallback(() => {
    if (manualSubmitting) return;
    setManualQueryOpen(false);
    setManualQueryError("");
    void api?.cancel();
  }, [api, manualSubmitting]);

  const submitManualQuery = useCallback(async () => {
    const question = normalizedManualQuery.trim();
    if (question.length < 2) {
      setManualQueryError("Введите вопрос для Atlas.");
      return;
    }
    if (!api) {
      setManualQueryError("Ручной запрос доступен в приложении T-Mod.");
      return;
    }

    capture.cancel();
    speech.cancel();
    stopAiAudio();
    setManualSubmitting(true);
    setManualQueryError("");
    try {
      const result = await api.submitText(question);
      if (!result.accepted) {
        setManualQueryError(result.error || "Atlas не принял запрос.");
        return;
      }
      dispatch({ type: "transcript", text: question });
      setManualQuery("");
      setManualQueryOpen(false);
      playOverlayCue("release", configRef.current.cueVolume);
    } catch {
      setManualQueryError("Не удалось отправить вопрос. Проверьте подключение Atlas.");
    } finally {
      if (mounted.current) setManualSubmitting(false);
    }
  }, [api, capture, normalizedManualQuery, speech, stopAiAudio]);

  const saveCalibration = useCallback(async (patch: OverlayExperiencePatch) => {
    const previous = configRef.current as OverlayExperienceConfig;
    const optimistic = { ...previous, ...patch } as AtlasOverlayConfig;
    setConfig(optimistic);
    setCalibrationFeedback("");
    if (!api) return;
    try {
      const saved = await api.saveConfig(patch as Partial<AtlasOverlayConfig>);
      if (mounted.current) setConfig(saved);
    } catch {
      if (mounted.current) {
        setConfig(previous);
        setCalibrationFeedback("Не удалось сохранить настройку");
      }
    }
  }, [api]);

  const beginCalibrationDrag = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (!calibrationMode || event.button !== 0) return;
    if (isTextEntryTarget(event.target)) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    calibrationDrag.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      offsetX: 0,
      offsetY: 0,
    };
    setDragOffset({ x: 0, y: 0 });
  }, [calibrationMode]);

  const moveCalibrationDrag = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = calibrationDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const x = clamp(event.clientX - drag.startX, -260, 260);
    const y = clamp(event.clientY - drag.startY, -180, 180);
    drag.offsetX = x;
    drag.offsetY = y;
    setDragOffset({ x, y });
  }, []);

  const finishCalibrationDrag = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = calibrationDrag.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    calibrationDrag.current = undefined;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setDragOffset({ x: 0, y: 0 });
    if (Math.abs(drag.offsetX) < 2 && Math.abs(drag.offsetY) < 2) return;
    if (!api) {
      void saveCalibration({
        positionX: clamp(configRef.current.positionX + drag.offsetX / Math.max(360, window.innerWidth), 0, 1),
        positionY: clamp(configRef.current.positionY + drag.offsetY / Math.max(240, window.innerHeight), 0, 1),
      });
      return;
    }
    setCalibrationFeedback("");
    void api.moveBy(drag.offsetX, drag.offsetY).then((saved) => {
      if (mounted.current) setConfig(saved);
    }).catch(() => {
      if (mounted.current) setCalibrationFeedback("Не удалось сохранить позицию");
    });
  }, [api, saveCalibration]);

  useEffect(() => {
    if (!manualQueryOpen) return undefined;
    const focusTimer = window.setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 0);
    return () => window.clearTimeout(focusTimer);
  }, [manualQueryOpen]);

  useEffect(() => {
    const receiveManualRequest = (event: Event) => openManualQuery(manualTextFromEvent(event));
    const keyboardFallback = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (manualQueryOpen && event.key === "Escape") {
        event.preventDefault();
        closeManualQuery();
        return;
      }
      if (
        event.code === "Space" &&
        event.ctrlKey &&
        event.altKey &&
        !event.repeat &&
        !isTextEntryTarget(event.target)
      ) {
        event.preventDefault();
        openManualQuery();
      }
    };
    window.addEventListener("atlas:manual-query", receiveManualRequest);
    window.addEventListener("keydown", keyboardFallback);
    return () => {
      window.removeEventListener("atlas:manual-query", receiveManualRequest);
      window.removeEventListener("keydown", keyboardFallback);
    };
  }, [closeManualQuery, manualQueryOpen, openManualQuery]);

  useEffect(() => {
    if (!api) return undefined;
    const receive = (event: AtlasOverlayEvent) => {
      if (event.type === "config") {
        setConfig(event.config);
        return;
      }
      if (event.type === "crafts" || event.type === "craft-alert") {
        setCrafts(event.snapshot);
        if (event.type === "craft-alert") {
          setCraftAlarmKey(event.alarmKey);
          playCraftAlarm(configRef.current.craftAlertVolume);
          window.setTimeout(() => setCraftAlarmKey((current) => current === event.alarmKey ? "" : current), 18_000);
        }
        return;
      }
      if (event.type === "manual-query") {
        openManualQuery();
        return;
      }
      if (event.type === "speech") {
        const currentConfig = configRef.current;
        if (!currentConfig.speakAnswers || currentConfig.speechProvider !== "ai") return;
        // A no-audio speech event is the explicit fallback contract from the
        // main process. The same queue preserves sentence order for both
        // audio and Windows fallback without starting local speech early.
        playAiAudio(event);
        return;
      }
      const currentConfig = configRef.current;
      if (event.type === "delta" && currentConfig.speakAnswers && currentConfig.speechProvider === "system") {
        speech.append(event.text);
      } else if (event.type === "done") {
        if (currentConfig.speakAnswers && currentConfig.speechProvider === "system") speech.finish();
        playOverlayCue("ready", currentConfig.cueVolume);
      } else if (["hide", "idle", "error"].includes(event.type)) {
        speech.cancel();
        stopAiAudio();
        if (event.type === "hide") {
          setManualQueryOpen(false);
          setManualQueryError("");
        }
        if (event.type === "error") playOverlayCue("error", currentConfig.cueVolume);
      }
      dispatch(event);
    };
    return api.onEvent(receive);
  }, [api, openManualQuery, playAiAudio, speech, stopAiAudio]);

  useEffect(() => {
    if (!api) return undefined;
    const ptt = async (phase: AtlasOverlayPttPhase) => {
      dispatch({ type: "ptt", phase });
      if (phase === "down") {
        setManualQueryOpen(false);
        setManualQueryError("");
        speech.cancel();
        stopAiAudio();
        playOverlayCue("listen", configRef.current.cueVolume);
        try {
          await capture.start(configRef.current.microphoneId);
        } catch (error) {
          dispatch({ type: "error", message: captureError(error), retryable: true });
        }
        return;
      }
      if (phase === "cancel") {
        capture.cancel();
        speech.cancel();
        stopAiAudio();
        // The main process already aborted the request before broadcasting
        // this phase. Calling cancel back through IPC would create a loop.
        return;
      }
      try {
        playOverlayCue("release", configRef.current.cueVolume);
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
  }, [api, capture, speech, stopAiAudio]);

  const stageBusy = ["transcribing", "searching", "thinking"].includes(state.stage);
  const craftPlans = (crafts?.plans || []).filter((plan) => config.craftShowAll || plan.mine);
  const visibleCraftPlans = (craftPlans.length ? craftPlans : crafts?.plans || []).slice(0, 3);
  const craftMode = !manualQueryOpen && state.stage === "idle" && (
    (config.craftAutoExpand && Boolean(craftAlarmKey)) ||
    config.workspaceMode === "crafts" ||
    (config.workspaceMode === "auto" && Boolean(craftAlarmKey || crafts?.attention_count))
  );
  const style = {
    "--overlay-opacity": config.opacity,
    "--overlay-scale": config.scale,
    "--overlay-font-scale": fontScale,
    "--overlay-panel-width": config.panelWidth + "px",
    "--overlay-answer-height": config.answerHeight + "px",
    "--calibration-drag-x": dragOffset.x + "px",
    "--calibration-drag-y": dragOffset.y + "px",
  } as CSSProperties;
  const horizontal = config.positionX < 0.34 ? "left" : config.positionX > 0.66 ? "right" : "center";
  const vertical = config.positionY < 0.34 ? "top" : config.positionY > 0.66 ? "bottom" : "center";

  return (
    <main
      className={[
        "atlas-overlay",
        "atlas-overlay--" + config.anchor,
        state.visible && "is-visible",
        calibrationMode && "is-calibrating",
        manualQueryOpen && "is-manual-query",
        (dragOffset.x || dragOffset.y) && "is-dragging",
      ].filter(Boolean).join(" ")}
      data-stage={state.stage}
      data-idle-style={config.idleStyle}
      data-theme={config.theme}
      data-motion={config.motion}
      data-workspace={craftMode ? "crafts" : "assistant"}
      data-craft-alert={craftAlarmKey ? "true" : "false"}
      data-horizontal={horizontal}
      data-vertical={vertical}
      style={style}
    >
      <section
        className="atlas-overlay-card"
        aria-live={manualQueryOpen ? "off" : "polite"}
        aria-atomic="false"
        onPointerDown={beginCalibrationDrag}
        onPointerMove={moveCalibrationDrag}
        onPointerUp={finishCalibrationDrag}
        onPointerCancel={finishCalibrationDrag}
      >
        <div className="atlas-overlay-aurora" aria-hidden="true" />
        <header className="atlas-overlay-head">
          <div className="atlas-overlay-brand">
            <span className="atlas-overlay-mark"><AtlasMark /></span>
            <span>
              <strong>{craftMode ? "ATLAS CRAFT" : "ATLAS"}</strong>
              <small>{craftMode ? "PRODUCTION CONTROL" : "LIVE INTELLIGENCE"}</small>
            </span>
          </div>
          <div className="atlas-overlay-status">
            <i />
            <span>{craftMode ? (crafts?.attention_count ? "Требуется новый цикл" : "Крафты синхронизированы") : state.statusLabel}</span>
            {config.speakAnswers && (
              <em className={config.speechProvider === "ai" ? "is-ai" : ""}>
                {config.speechProvider === "ai" ? "AI VOICE" : "LOCAL"}
              </em>
            )}
          </div>
        </header>

        {calibrationMode && (
          <aside
            className="atlas-overlay-calibration"
            onPointerDown={(event) => event.stopPropagation()}
            aria-label="Настройка Atlas Overlay"
          >
            <span className="atlas-overlay-calibration-title"><i /> Режим настройки</span>
            <span className="atlas-overlay-calibration-hint">Стрелки — позиция · +/− — размер · [ ] — ширина · Enter — готово</span>
            <div className="atlas-overlay-calibration-keys"><kbd>← ↑ ↓ →</kbd><kbd>+ −</kbd><kbd>[ ]</kbd><kbd>ENTER</kbd></div>
            {calibrationFeedback && <span className="atlas-overlay-calibration-feedback">{calibrationFeedback}</span>}
          </aside>
        )}

        <div className="atlas-overlay-body">
          {manualQueryOpen ? (
            <form
              className="atlas-overlay-manual"
              onSubmit={(event) => {
                event.preventDefault();
                void submitManualQuery();
              }}
              onPointerDown={(event) => event.stopPropagation()}
            >
              <header className="atlas-overlay-manual-head">
                <span><i /> Текстовый запрос</span>
                <button type="button" aria-label="Закрыть текстовый запрос" onClick={closeManualQuery}>×</button>
              </header>
              <textarea
                ref={inputRef}
                value={manualQuery}
                maxLength={4_000}
                rows={3}
                spellCheck={false}
                placeholder="Напишите вопрос Atlas…"
                aria-label="Вопрос для Atlas"
                onChange={(event) => {
                  setManualQuery(event.target.value);
                  setManualQueryError("");
                }}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    closeManualQuery();
                  } else if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void submitManualQuery();
                  }
                }}
              />
              {normalizedManualQuery !== manualQuery && (
                <button
                  type="button"
                  className="atlas-overlay-layout-hint"
                  onClick={() => setManualQuery(normalizedManualQuery)}
                >
                  <span>Раскладка распознана</span>
                  <strong>{normalizedManualQuery}</strong>
                </button>
              )}
              {manualQueryError && <p className="atlas-overlay-manual-error" role="alert">{manualQueryError}</p>}
              <footer className="atlas-overlay-manual-foot">
                <span>{config.hotkey.replaceAll("+", " · ") + " · Space"}</span>
                <small>Enter — отправить</small>
                <button type="submit" disabled={manualSubmitting || manualQuery.trim().length < 2}>
                  {manualSubmitting ? "Отправляю…" : "Спросить"}
                </button>
              </footer>
            </form>
          ) : (
            <>
          {state.stage === "initializing" && (
            <div className="atlas-overlay-initializing">
              <InitializationField />
              <div>
                <small>SECURE FIELD LINK · ONLINE</small>
                <strong>{state.transcript || "Atlas"}, система готова</strong>
                <span>{config.serverCode.toUpperCase()} · {config.factionCode.toUpperCase()} · Atlas подключён к полевому контуру</span>
              </div>
            </div>
          )}

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
              {config.showCitations && state.citations.length > 0 && (
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

          {state.stage === "idle" && craftMode && (
            <div className="atlas-craft-deck">
              <div className="atlas-craft-deck-head">
                <span><small>LIVE QUEUE</small><strong>{crafts?.attention_count ? `${crafts.attention_count} требует действия` : "Производственный контур"}</strong></span>
                <time>{new Date(clock).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</time>
              </div>
              {visibleCraftPlans.length
                ? <div className="atlas-craft-plans">{visibleCraftPlans.map((plan) => <CraftPlanCard key={plan.id} plan={plan} now={clock} />)}</div>
                : <div className="atlas-craft-empty"><i/><strong>Активных крафтов нет</strong><span>Atlas сообщит, когда появится новый цикл.</span></div>}
            </div>
          )}

          {state.stage === "idle" && !craftMode && (
            <div className="atlas-overlay-idle">
              <i className="atlas-idle-signal"><b /><em /></i>
              <span><strong>ATLAS</strong><small>{config.characterName || "Готов к работе"}</small></span>
              <kbd>{config.hotkey.replaceAll("+", "  +  ")}</kbd>
            </div>
          )}
            </>
          )}
        </div>

        <footer className="atlas-overlay-foot">
          <span>{craftMode ? `КРАФТЫ · ${visibleCraftPlans.length} АКТИВНО` : `${config.serverCode.toUpperCase()} · ${config.factionCode.toUpperCase()}`}</span>
          {config.showLatency && state.latencyMs !== undefined && <span>{Math.max(0, state.latencyMs / 1_000).toFixed(1)} s</span>}
          <span className="atlas-overlay-mode">{craftMode ? config.craftHotkey.replaceAll("+", " · ") : "T-MOD DESKTOP"}</span>
        </footer>
      </section>
    </main>
  );
}
