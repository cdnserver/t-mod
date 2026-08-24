export const atlasOverlayStages = [
  "initializing",
  "idle",
  "listening",
  "transcribing",
  "searching",
  "thinking",
  "answer",
  "error",
] as const;

export type AtlasOverlayStage = (typeof atlasOverlayStages)[number];

export type AtlasOverlayAnchor = "top-right" | "right" | "bottom-right";
export type AtlasOverlayResponseMode = "quick" | "balanced";
export type AtlasOverlaySpeechProvider = "ai" | "system";
export type AtlasOverlayIdleStyle = "orb" | "bar" | "full";
export type AtlasOverlayTheme = "cosmos" | "graphite" | "emerald" | "amber" | "crimson";
export type AtlasOverlayMotion = "cinematic" | "balanced" | "minimal";
export type AtlasOverlayAnswerHold = "brief" | "auto" | "pinned";
export type AtlasOverlayPttPhase = "down" | "up" | "cancel";

export interface AtlasOverlayCharacter {
  id: string;
  name: string;
  staticId: string;
  serverCode: string;
  factionCode: string;
  factionName?: string;
}

export interface AtlasOverlayServer {
  code: string;
  name: string;
  number?: number;
}

export interface AtlasOverlayFaction {
  code: string;
  name: string;
  serverCode: string;
  kind?: "government" | "media" | "medical" | "other";
}

export interface AtlasOverlayCatalog {
  characters: AtlasOverlayCharacter[];
  servers: AtlasOverlayServer[];
  factions: AtlasOverlayFaction[];
}

export interface AtlasOverlayBootstrapProjection {
  allowed: boolean;
  characters: Array<{
    id: number | string;
    nickname: string;
    static_id: string;
    server_code?: string;
    faction_code?: string;
    faction_name?: string;
  }>;
  selected_character?: Record<string, unknown> | null;
  catalog: {
    servers: Array<Record<string, unknown>>;
    factions: Array<Record<string, unknown>>;
  };
}

export interface AtlasOverlayConfig {
  enabled: boolean;
  showGameStatus: boolean;
  captureInRecordings: boolean;
  hotkey: string;
  serverCode: string;
  factionCode: string;
  characterId: string | null;
  characterName: string;
  speakAnswers: boolean;
  speechProvider: AtlasOverlaySpeechProvider;
  speechVoice: string;
  speechRate: number;
  speechVolume: number;
  microphoneId: string;
  responseMode: AtlasOverlayResponseMode;
  anchor: AtlasOverlayAnchor;
  opacity: number;
  scale: number;
  panelWidth: number;
  answerHeight: number;
  idleStyle: AtlasOverlayIdleStyle;
  theme: AtlasOverlayTheme;
  motion: AtlasOverlayMotion;
  initializationAnimation: boolean;
  showCitations: boolean;
  showLatency: boolean;
  answerHold: AtlasOverlayAnswerHold;
  cueVolume: number;
  /** Enables the in-game calibration HUD. This is opt-in and only active over GTA. */
  calibrationMode: boolean;
  /** Independent typography scale for a legible field overlay. */
  fontScale: number;
  positionX: number;
  positionY: number;
  screenContextEnabled: boolean;
}

export const DEFAULT_ATLAS_OVERLAY_CONFIG: Readonly<AtlasOverlayConfig> = {
  enabled: false,
  showGameStatus: true,
  captureInRecordings: true,
  hotkey: "Control+Shift+A",
  serverCode: "phoenix-15",
  factionCode: "lspd",
  characterId: null,
  characterName: "",
  speakAnswers: true,
  speechProvider: "ai",
  speechVoice: "",
  speechRate: 1.08,
  speechVolume: 0.86,
  microphoneId: "",
  responseMode: "quick",
  anchor: "right",
  opacity: 0.94,
  scale: 0.88,
  panelWidth: 430,
  answerHeight: 132,
  idleStyle: "bar",
  theme: "cosmos",
  motion: "cinematic",
  initializationAnimation: true,
  showCitations: true,
  showLatency: false,
  answerHold: "auto",
  cueVolume: 0.58,
  calibrationMode: false,
  fontScale: 1,
  positionX: 1,
  positionY: 0.5,
  screenContextEnabled: false,
};

export interface AtlasOverlayCitation {
  index?: number;
  title: string;
  url?: string;
  pinpoint?: string;
}

export type AtlasOverlayEvent =
  | { type: "show" | "hide" | "idle" }
  | { type: "initialized"; name: string }
  | { type: "config"; config: AtlasOverlayConfig }
  | { type: "manual-query" }
  | { type: "speech"; audio?: ArrayBuffer; mimeType?: string; fallbackText?: string }
  | {
      type: "progress";
      stage: "transcribing" | "searching" | "reasoning";
      label?: string;
    }
  | { type: "transcript"; text: string }
  | { type: "delta"; text: string }
  | {
      type: "done";
      answer: string;
      citations?: AtlasOverlayCitation[];
      latencyMs?: number;
    }
  | { type: "error"; message: string; retryable?: boolean };

export interface AtlasOverlayAudioInput {
  audio: ArrayBuffer;
  mimeType: string;
  durationMs: number;
}

export interface AtlasOverlaySubmitResult {
  accepted: boolean;
  requestId?: string;
  error?: string;
}

export interface AtlasOverlayVoice {
  id: string;
  name: string;
  description: string;
  provider: "ai" | "system";
}

export interface AtlasOverlayVoiceCatalog {
  configured: boolean;
  provider: "ai" | "system";
  defaultVoice: string;
  voices: AtlasOverlayVoice[];
  availability?: {
    state: "ready" | "unconfigured" | "recovering" | "degraded";
    reason?: string;
  };
}

export interface AtlasOverlaySpeechResult {
  audio?: ArrayBuffer;
  mimeType?: string;
  fallback: boolean;
  error?: string;
}

export interface AtlasOverlayApi {
  getConfig(): Promise<AtlasOverlayConfig>;
  getCatalog(): Promise<AtlasOverlayCatalog>;
  saveConfig(patch: Partial<AtlasOverlayConfig>): Promise<AtlasOverlayConfig>;
  moveBy(deltaX: number, deltaY: number): Promise<AtlasOverlayConfig>;
  getVoices(): Promise<AtlasOverlayVoiceCatalog>;
  previewVoice(voice?: string): Promise<AtlasOverlaySpeechResult>;
  submitAudio(input: AtlasOverlayAudioInput): Promise<AtlasOverlaySubmitResult>;
  submitText(question: string): Promise<AtlasOverlaySubmitResult>;
  cancel(): Promise<void>;
  hide(): Promise<void>;
  openAtlas(): Promise<void>;
  reportSpeech(active: boolean): Promise<void>;
  onEvent(listener: (event: AtlasOverlayEvent) => void): () => void;
  onPtt(listener: (phase: AtlasOverlayPttPhase) => void): () => void;
}

export interface AtlasOverlayViewState {
  visible: boolean;
  stage: AtlasOverlayStage;
  statusLabel: string;
  transcript: string;
  answer: string;
  citations: AtlasOverlayCitation[];
  latencyMs?: number;
  error?: string;
  retryable: boolean;
}

export const initialAtlasOverlayState: Readonly<AtlasOverlayViewState> = {
  visible: false,
  stage: "idle",
  statusLabel: "Atlas готов",
  transcript: "",
  answer: "",
  citations: [],
  retryable: false,
};

const stageLabels: Record<AtlasOverlayStage, string> = {
  initializing: "Atlas инициализирован",
  idle: "Atlas готов",
  listening: "Слушаю",
  transcribing: "Распознаю речь",
  searching: "Проверяю базу",
  thinking: "Формирую ответ",
  answer: "Ответ Atlas",
  error: "Нужна повторная попытка",
};

export function reduceAtlasOverlayState(
  state: AtlasOverlayViewState,
  event: AtlasOverlayEvent | { type: "ptt"; phase: AtlasOverlayPttPhase },
): AtlasOverlayViewState {
  switch (event.type) {
    case "show":
      return { ...state, visible: true };
    case "hide":
      return { ...initialAtlasOverlayState };
    case "idle":
      return {
        ...initialAtlasOverlayState,
        visible: state.visible,
      };
    case "initialized":
      return {
        ...initialAtlasOverlayState,
        visible: true,
        stage: "initializing",
        statusLabel: stageLabels.initializing,
        transcript: event.name.trim(),
      };
    case "config":
      return state;
    case "speech":
      return state;
    case "manual-query":
      return {
        ...initialAtlasOverlayState,
        visible: true,
        statusLabel: "Введите запрос для Atlas",
      };
    case "ptt":
      if (event.phase === "down") {
        return {
          ...initialAtlasOverlayState,
          visible: true,
          stage: "listening",
          statusLabel: stageLabels.listening,
        };
      }
      if (event.phase === "cancel") {
        return { ...initialAtlasOverlayState, visible: state.visible };
      }
      return state.stage === "listening"
        ? { ...state, stage: "transcribing", statusLabel: stageLabels.transcribing }
        : state;
    case "progress": {
      const stage = event.stage === "reasoning" ? "thinking" : event.stage;
      return {
        ...state,
        visible: true,
        stage,
        statusLabel: event.label?.trim() || stageLabels[stage],
        error: undefined,
        retryable: false,
      };
    }
    case "transcript":
      return {
        ...state,
        visible: true,
        stage: "thinking",
        statusLabel: stageLabels.thinking,
        transcript: event.text.trim(),
      };
    case "delta":
      return {
        ...state,
        visible: true,
        stage: "answer",
        statusLabel: stageLabels.answer,
        answer: state.answer + event.text,
      };
    case "done":
      return {
        ...state,
        visible: true,
        stage: "answer",
        statusLabel: stageLabels.answer,
        answer: event.answer || state.answer,
        citations: event.citations || [],
        latencyMs: event.latencyMs,
        error: undefined,
        retryable: false,
      };
    case "error":
      return {
        ...state,
        visible: true,
        stage: "error",
        statusLabel: stageLabels.error,
        error: event.message,
        retryable: event.retryable === true,
      };
  }
}

const ELECTRON_ACCELERATOR_KEY = /^(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4])|space|tab|insert|delete|home|end|pageup|pagedown|up|down|left|right)$/i;
const ELECTRON_ACCELERATOR_MODIFIERS = new Set([
  "alt",
  "option",
  "altgr",
  "shift",
  "super",
  "command",
  "cmd",
  "control",
  "ctrl",
  "commandorcontrol",
  "cmdorctrl",
]);

export function isValidAtlasOverlayHotkey(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 64) return false;
  const parts = value.split("+").map((part) => part.trim()).filter(Boolean);
  if (parts.length < 1 || parts.length > 4) return false;
  const key = parts.at(-1) || "";
  const modifiers = parts.slice(0, -1).map((part) => part.toLowerCase());
  return (
    ELECTRON_ACCELERATOR_KEY.test(key) &&
    modifiers.every((modifier) => ELECTRON_ACCELERATOR_MODIFIERS.has(modifier)) &&
    new Set(modifiers).size === modifiers.length
  );
}

export function normalizeAtlasOverlayConfig(
  value: Partial<AtlasOverlayConfig> | null | undefined,
): AtlasOverlayConfig {
  const source = value || {};
  const speechRate = Number(source.speechRate);
  const speechVolume = Number(source.speechVolume);
  const opacity = Number(source.opacity);
  const scale = Number(source.scale);
  const panelWidth = Number(source.panelWidth);
  const answerHeight = Number(source.answerHeight);
  const fontScale = Number(source.fontScale);
  const cueVolume = Number(source.cueVolume);
  const anchor = ["top-right", "right", "bottom-right"].includes(String(source.anchor))
    ? source.anchor as AtlasOverlayAnchor
    : "right";
  const fallbackPositionY = anchor === "top-right" ? 0 : anchor === "bottom-right" ? 1 : 0.5;
  const positionX = Number(source.positionX);
  const positionY = Number(source.positionY);
  return {
    enabled: source.enabled === true,
    showGameStatus: source.showGameStatus !== false,
    captureInRecordings: source.captureInRecordings !== false,
    hotkey: isValidAtlasOverlayHotkey(source.hotkey)
      ? source.hotkey
      : DEFAULT_ATLAS_OVERLAY_CONFIG.hotkey,
    serverCode: String(source.serverCode || DEFAULT_ATLAS_OVERLAY_CONFIG.serverCode)
      .trim()
      .slice(0, 48),
    factionCode: String(source.factionCode || DEFAULT_ATLAS_OVERLAY_CONFIG.factionCode)
      .trim()
      .slice(0, 48),
    characterId: source.characterId ? String(source.characterId).slice(0, 96) : null,
    characterName: String(source.characterName || "").trim().slice(0, 96),
    speakAnswers: source.speakAnswers !== false,
    speechProvider: source.speechProvider === "system" ? "system" : "ai",
    speechVoice: String(source.speechVoice || "").trim().slice(0, 160),
    speechRate: Number.isFinite(speechRate) ? Math.max(0.75, Math.min(1.45, speechRate)) : 1.08,
    speechVolume: Number.isFinite(speechVolume)
      ? Math.max(0, Math.min(1, speechVolume))
      : 0.86,
    microphoneId: String(source.microphoneId || "").trim().slice(0, 256),
    responseMode: source.responseMode === "balanced" ? "balanced" : "quick",
    anchor,
    opacity: Number.isFinite(opacity) ? Math.max(0.68, Math.min(1, opacity)) : 0.94,
    scale: Number.isFinite(scale) ? Math.max(0.72, Math.min(1.35, scale)) : 0.88,
    panelWidth: Number.isFinite(panelWidth)
      ? Math.max(340, Math.min(520, panelWidth))
      : DEFAULT_ATLAS_OVERLAY_CONFIG.panelWidth,
    answerHeight: Number.isFinite(answerHeight)
      ? Math.max(96, Math.min(300, answerHeight))
      : DEFAULT_ATLAS_OVERLAY_CONFIG.answerHeight,
    idleStyle: ["orb", "bar", "full"].includes(String(source.idleStyle))
      ? source.idleStyle as AtlasOverlayIdleStyle
      : DEFAULT_ATLAS_OVERLAY_CONFIG.idleStyle,
    theme: ["cosmos", "graphite", "emerald", "amber", "crimson"].includes(String(source.theme))
      ? source.theme as AtlasOverlayTheme
      : DEFAULT_ATLAS_OVERLAY_CONFIG.theme,
    motion: ["cinematic", "balanced", "minimal"].includes(String(source.motion))
      ? source.motion as AtlasOverlayMotion
      : DEFAULT_ATLAS_OVERLAY_CONFIG.motion,
    initializationAnimation: source.initializationAnimation !== false,
    showCitations: source.showCitations !== false,
    showLatency: source.showLatency === true,
    answerHold: ["brief", "auto", "pinned"].includes(String(source.answerHold))
      ? source.answerHold as AtlasOverlayAnswerHold
      : DEFAULT_ATLAS_OVERLAY_CONFIG.answerHold,
    cueVolume: Number.isFinite(cueVolume) ? Math.max(0, Math.min(1, cueVolume)) : 0.58,
    calibrationMode: source.calibrationMode === true,
    fontScale: Number.isFinite(fontScale) ? Math.max(0.82, Math.min(1.4, fontScale)) : 1,
    positionX: Number.isFinite(positionX) ? Math.max(0, Math.min(1, positionX)) : 1,
    positionY: Number.isFinite(positionY)
      ? Math.max(0, Math.min(1, positionY))
      : fallbackPositionY,
    screenContextEnabled: source.screenContextEnabled === true,
  };
}

declare global {
  interface Window {
    tmodAtlasOverlay?: AtlasOverlayApi;
  }
}
