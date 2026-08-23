export const atlasOverlayStages = [
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
  hotkey: string;
  serverCode: string;
  factionCode: string;
  characterId: string | null;
  characterName: string;
  speakAnswers: boolean;
  speechRate: number;
  speechVolume: number;
  responseMode: AtlasOverlayResponseMode;
  anchor: AtlasOverlayAnchor;
  opacity: number;
  screenContextEnabled: boolean;
}

export const DEFAULT_ATLAS_OVERLAY_CONFIG: Readonly<AtlasOverlayConfig> = {
  enabled: false,
  hotkey: "Control+Shift+A",
  serverCode: "phoenix-15",
  factionCode: "lspd",
  characterId: null,
  characterName: "",
  speakAnswers: true,
  speechRate: 1.08,
  speechVolume: 0.86,
  responseMode: "quick",
  anchor: "right",
  opacity: 0.94,
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

export interface AtlasOverlayApi {
  getConfig(): Promise<AtlasOverlayConfig>;
  getCatalog(): Promise<AtlasOverlayCatalog>;
  saveConfig(patch: Partial<AtlasOverlayConfig>): Promise<AtlasOverlayConfig>;
  submitAudio(input: AtlasOverlayAudioInput): Promise<AtlasOverlaySubmitResult>;
  submitText(question: string): Promise<AtlasOverlaySubmitResult>;
  cancel(): Promise<void>;
  hide(): Promise<void>;
  openAtlas(): Promise<void>;
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
  return {
    enabled: source.enabled === true,
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
    speechRate: Number.isFinite(speechRate) ? Math.max(0.75, Math.min(1.45, speechRate)) : 1.08,
    speechVolume: Number.isFinite(speechVolume)
      ? Math.max(0, Math.min(1, speechVolume))
      : 0.86,
    responseMode: source.responseMode === "balanced" ? "balanced" : "quick",
    anchor: ["top-right", "right", "bottom-right"].includes(String(source.anchor))
      ? source.anchor as AtlasOverlayAnchor
      : "right",
    opacity: Number.isFinite(opacity) ? Math.max(0.68, Math.min(1, opacity)) : 0.94,
    screenContextEnabled: source.screenContextEnabled === true,
  };
}

declare global {
  interface Window {
    tmodAtlasOverlay?: AtlasOverlayApi;
  }
}
