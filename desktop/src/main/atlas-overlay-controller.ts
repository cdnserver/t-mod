import {
  BrowserWindow,
  desktopCapturer,
  globalShortcut,
  screen,
  shell,
  type Session,
} from "electron";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import type {
  AtlasOverlayApi,
  AtlasOverlayAudioInput,
  AtlasOverlayBootstrapProjection,
  AtlasOverlayCatalog,
  AtlasOverlayConfig,
  AtlasOverlayEvent,
  AtlasOverlaySubmitResult,
} from "../shared/atlas-overlay";
import {
  DEFAULT_ATLAS_OVERLAY_CONFIG,
  normalizeAtlasOverlayConfig,
} from "../shared/atlas-overlay";
import {
  parseServerSentEventJson,
  ServerSentEventDecoder,
  type ServerSentEvent,
} from "../shared/atlas-overlay-sse";

const ATLAS_BOOTSTRAP_URL = "https://atlas.tvr.lat/api/atlas/bootstrap";
const ATLAS_STREAM_URL = "https://atlas.tvr.lat/api/atlas/chat/stream";
const ATLAS_TRANSCRIBE_URL = "https://atlas.tvr.lat/api/atlas/overlay/transcribe";
const MAX_AUDIO_BYTES = 6 * 1024 * 1024;
const MAX_AUDIO_DURATION_MS = 25_000;
const MAX_SCREEN_CONTEXT_BYTES = 1_200_000;
const OVERLAY_WIDTH = 640;
const OVERLAY_HEIGHT = 480;

interface AtlasBootstrapPayload {
  viewer?: { csrf_token?: string; atlas_access?: boolean };
  catalog?: {
    servers?: Array<Record<string, unknown>>;
    factions?: Array<Record<string, unknown>>;
  };
}

interface AtlasOverlayControllerOptions {
  networkSession: () => Session;
  preloadPath: string;
  rendererUrl?: string;
  rendererFile: string;
  userDataPath: string;
  hotkeyHelperPath: string;
  onLog?: (message: string, details?: unknown) => void;
}

function cleanCode(value: unknown, fallback: string): string {
  const selected = String(value || "").trim().toLowerCase();
  return /^[a-z0-9][a-z0-9-]{0,47}$/.test(selected) ? selected : fallback;
}

function asCatalog(
  projection: AtlasOverlayBootstrapProjection | undefined,
  fallback?: AtlasBootstrapPayload["catalog"],
): AtlasOverlayCatalog {
  const servers = projection?.catalog?.servers || fallback?.servers || [];
  const factions = projection?.catalog?.factions || fallback?.factions || [];
  return {
    characters: (projection?.characters || []).map((character) => ({
      id: String(character.id),
      name: String(character.nickname || "Персонаж"),
      staticId: String(character.static_id || ""),
      serverCode: cleanCode(character.server_code, "phoenix-15"),
      factionCode: cleanCode(character.faction_code, "lspd"),
      factionName: String(character.faction_name || "") || undefined,
    })),
    servers: servers
      .filter((item) => item.enabled !== false)
      .map((item) => ({
        code: cleanCode(item.code, "phoenix-15"),
        name: String(item.label || item.name || item.code || "Phoenix"),
        ...(Number.isFinite(Number(item.number)) ? { number: Number(item.number) } : {}),
      })),
    factions: factions
      .filter((item) => item.enabled !== false)
      .map((item) => ({
        code: cleanCode(item.code, "lspd"),
        name: String(item.label || item.short_name || item.name || item.code || "LSPD"),
        serverCode: cleanCode(item.server_code, "phoenix-15"),
        kind: String(item.kind || "other") as "government" | "media" | "medical" | "other",
      })),
  };
}

function safeError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : String(error || fallback);
  const known: Record<string, string> = {
    atlas_overlay_disabled: "Atlas Overlay выключен в настройках.",
    atlas_overlay_access_required: "Для Atlas Overlay нужен доступ к Atlas AI.",
    atlas_overlay_character_required: "Выберите персонажа в настройках Atlas Overlay.",
    atlas_overlay_audio_invalid: "Не удалось прочитать запись микрофона.",
    atlas_overlay_transcript_empty: "Речь не распознана. Попробуйте произнести запрос чуть чётче.",
    atlas_overlay_session_expired: "Сессия T-Mod устарела. Откройте приложение и войдите снова.",
    csrf_failed: "Защитная сессия Atlas обновилась. Повторите запрос.",
  };
  return known[message] || message.slice(0, 420) || fallback;
}

export class AtlasOverlayController {
  private readonly options: AtlasOverlayControllerOptions;
  private readonly configPath: string;
  private window: BrowserWindow | null = null;
  private windowLoad?: Promise<void>;
  private windowReady = false;
  private pendingPttUp = false;
  private config: AtlasOverlayConfig = { ...DEFAULT_ATLAS_OVERLAY_CONFIG };
  private catalog: AtlasOverlayCatalog = { characters: [], servers: [], factions: [] };
  private projection?: AtlasOverlayBootstrapProjection;
  private csrfToken = "";
  private activeRequest?: AbortController;
  private activeRequestId?: string;
  private activeThreadId?: number;
  private hotkeyHelper?: ChildProcess;
  private fallbackHotkey = "";
  private fallbackListening = false;
  private bindingDirty = true;
  private screenContext?: Promise<string | undefined>;
  private hideTimer?: ReturnType<typeof setTimeout>;

  constructor(options: AtlasOverlayControllerOptions) {
    this.options = options;
    this.configPath = path.join(options.userDataPath, "atlas-overlay.json");
  }

  async initialize(): Promise<void> {
    try {
      const saved = JSON.parse(await readFile(this.configPath, "utf8")) as Partial<AtlasOverlayConfig>;
      this.config = normalizeAtlasOverlayConfig(saved);
    } catch {
      this.config = { ...DEFAULT_ATLAS_OVERLAY_CONFIG };
    }
    if (this.config.enabled) await this.ensureWindow();
    this.bindPtt();
  }

  api(): Pick<
    AtlasOverlayApi,
    "getConfig" | "getCatalog" | "saveConfig" | "submitAudio" | "submitText" |
    "cancel" | "hide" | "openAtlas"
  > {
    return {
      getConfig: async () => this.getConfig(),
      getCatalog: async () => this.getCatalog(),
      saveConfig: async (patch) => this.saveConfig(patch),
      submitAudio: async (input) => this.submitAudio(input),
      submitText: async (question) => this.submitText(question),
      cancel: async () => this.cancel(),
      hide: async () => this.hide(),
      openAtlas: async () => this.openAtlas(),
    };
  }

  getConfig(): AtlasOverlayConfig {
    return { ...this.config };
  }

  getCatalog(): AtlasOverlayCatalog {
    return {
      characters: this.catalog.characters.map((item) => ({ ...item })),
      servers: this.catalog.servers.map((item) => ({ ...item })),
      factions: this.catalog.factions.map((item) => ({ ...item })),
    };
  }

  ownsSender(webContentsId: number): boolean {
    return Boolean(
      this.window &&
      !this.window.isDestroyed() &&
      this.window.webContents.id === webContentsId,
    );
  }

  async applyBootstrap(projection: AtlasOverlayBootstrapProjection | undefined): Promise<void> {
    if (!projection) {
      this.csrfToken = "";
      this.activeThreadId = undefined;
      this.bindingDirty = true;
    }
    this.projection = projection;
    this.catalog = asCatalog(projection);
    const projected = projection?.selected_character;
    if (!this.config.characterId && projected && projected.id) {
      const character = this.catalog.characters.find((item) => item.id === String(projected.id));
      if (character) {
        this.config = {
          ...this.config,
          characterId: character.id,
          characterName: character.name,
          serverCode: cleanCode(projected.server_code, character.serverCode),
          factionCode: cleanCode(projected.faction_code, character.factionCode),
          speakAnswers: projected.voice_reply_enabled !== false,
          screenContextEnabled: projected.screen_context_enabled === true,
        };
        this.bindingDirty = false;
        await this.persistConfig();
      }
    }
    const selected = this.catalog.characters.find((item) => item.id === this.config.characterId);
    if (this.config.characterId && !selected) {
      this.config = { ...this.config, characterId: null, characterName: "" };
      this.activeThreadId = undefined;
      await this.persistConfig();
    } else if (selected && selected.name !== this.config.characterName) {
      this.config = { ...this.config, characterName: selected.name };
      await this.persistConfig();
    }
    if (!projection?.allowed) {
      this.cancel();
      this.hide();
    } else if (this.config.enabled) {
      await this.ensureWindow();
    }
  }

  async saveConfig(patch: Partial<AtlasOverlayConfig>): Promise<AtlasOverlayConfig> {
    const previous = { ...this.config };
    const previousThreadId = this.activeThreadId;
    const previousBindingDirty = this.bindingDirty;
    const next = normalizeAtlasOverlayConfig({ ...this.config, ...patch });
    const character = this.catalog.characters.find((item) => item.id === next.characterId);
    next.characterId = character?.id || null;
    next.characterName = character?.name || "";
    if (!this.catalog.servers.some((item) => item.code === next.serverCode)) {
      next.serverCode = this.catalog.servers[0]?.code || "phoenix-15";
    }
    if (!this.catalog.factions.some((item) => item.code === next.factionCode)) {
      next.factionCode = this.catalog.factions[0]?.code || "lspd";
    }
    this.config = next;
    if (
      previous.characterId !== next.characterId ||
      previous.serverCode !== next.serverCode ||
      previous.factionCode !== next.factionCode
    ) {
      this.activeThreadId = undefined;
    }
    this.bindingDirty = this.bindingDirty || (
      previous.characterId !== next.characterId ||
      previous.serverCode !== next.serverCode ||
      previous.factionCode !== next.factionCode ||
      previous.speakAnswers !== next.speakAnswers ||
      previous.screenContextEnabled !== next.screenContextEnabled
    );
    await this.persistConfig();
    try {
      if (next.characterId && this.projection?.allowed && this.bindingDirty) {
        await this.syncBinding();
      }
    } catch (error) {
      this.config = previous;
      this.activeThreadId = previousThreadId;
      this.bindingDirty = previousBindingDirty;
      await this.persistConfig();
      throw error;
    }
    if (previous.hotkey !== next.hotkey || previous.enabled !== next.enabled) this.bindPtt();
    if (previous.anchor !== next.anchor) this.positionWindow();
    this.emit({ type: "idle" });
    if (next.enabled && this.projection?.allowed) await this.ensureWindow();
    else this.hide();
    return this.getConfig();
  }

  async submitAudio(input: AtlasOverlayAudioInput): Promise<AtlasOverlaySubmitResult> {
    const audio = input?.audio;
    if (!(audio instanceof ArrayBuffer) || audio.byteLength < 80 || audio.byteLength > MAX_AUDIO_BYTES) {
      return { accepted: false, error: "atlas_overlay_audio_invalid" };
    }
    if (Number(input.durationMs) > MAX_AUDIO_DURATION_MS) {
      return {
        accepted: false,
        error: "Голосовая команда может длиться не более 25 секунд.",
      };
    }
    const ready = this.guardReady();
    if (ready) return { accepted: false, error: ready };
    const requestId = randomUUID();
    this.beginRequest(requestId);
    void this.processAudio(requestId, input).catch((error) => this.failRequest(requestId, error));
    return { accepted: true, requestId };
  }

  async submitText(question: string): Promise<AtlasOverlaySubmitResult> {
    const clean = String(question || "").trim().slice(0, 4_000);
    if (clean.length < 2) return { accepted: false, error: "Введите вопрос для Atlas." };
    const ready = this.guardReady();
    if (ready) return { accepted: false, error: ready };
    const requestId = randomUUID();
    this.beginRequest(requestId);
    void this.streamQuestion(requestId, clean).catch((error) => this.failRequest(requestId, error));
    return { accepted: true, requestId };
  }

  cancel(): void {
    this.activeRequest?.abort();
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.screenContext = undefined;
    this.pendingPttUp = false;
    this.emitPtt("cancel");
    this.emit({ type: "idle" });
  }

  hide(): void {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    this.hideTimer = undefined;
    this.emit({ type: "hide" });
    this.window?.hide();
  }

  async openAtlas(): Promise<void> {
    await shell.openExternal("https://atlas.tvr.lat/");
  }

  dispose(): void {
    this.cancel();
    this.unbindPtt();
    if (this.hideTimer) clearTimeout(this.hideTimer);
    if (this.window && !this.window.isDestroyed()) this.window.destroy();
    this.window = null;
  }

  onDisplaysChanged(): void {
    this.positionWindow();
  }

  private guardReady(): string | undefined {
    if (!this.config.enabled) return "atlas_overlay_disabled";
    if (!this.projection?.allowed) return "atlas_overlay_access_required";
    if (!this.config.characterId) return "atlas_overlay_character_required";
    return undefined;
  }

  private async persistConfig(): Promise<void> {
    await mkdir(path.dirname(this.configPath), { recursive: true });
    await writeFile(this.configPath, JSON.stringify(this.config, null, 2), "utf8");
  }

  private async ensureWindow(): Promise<void> {
    if (this.window && !this.window.isDestroyed()) {
      if (this.windowLoad) await this.windowLoad;
      return;
    }
    const overlayWindow = new BrowserWindow({
      width: OVERLAY_WIDTH,
      height: OVERLAY_HEIGHT,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      focusable: false,
      skipTaskbar: true,
      hasShadow: false,
      webPreferences: {
        preload: this.options.preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
      },
    });
    this.window = overlayWindow;
    this.windowReady = false;
    overlayWindow.setAlwaysOnTop(true, "screen-saver", 1);
    overlayWindow.setIgnoreMouseEvents(true, { forward: true });
    overlayWindow.setContentProtection(true);
    overlayWindow.setFocusable(false);
    overlayWindow.setMenuBarVisibility(false);
    overlayWindow.on("closed", () => {
      if (this.window === overlayWindow) {
        this.window = null;
        this.windowReady = false;
        this.windowLoad = undefined;
      }
    });
    this.positionWindow();
    let load: Promise<void>;
    if (this.options.rendererUrl) {
      const url = new URL(this.options.rendererUrl);
      url.searchParams.set("atlasOverlay", "1");
      load = overlayWindow.loadURL(url.toString());
    } else {
      load = overlayWindow.loadFile(this.options.rendererFile, { query: { atlasOverlay: "1" } });
    }
    this.windowLoad = load;
    try {
      await load;
      if (this.window === overlayWindow && !overlayWindow.isDestroyed()) this.windowReady = true;
    } catch (error) {
      if (!overlayWindow.isDestroyed()) overlayWindow.destroy();
      throw error;
    } finally {
      if (this.window === overlayWindow) this.windowLoad = undefined;
    }
  }

  private positionWindow(): void {
    if (!this.window || this.window.isDestroyed()) return;
    const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    const area = display.workArea;
    const margin = 24;
    const x = area.x + area.width - OVERLAY_WIDTH - margin;
    const y = this.config.anchor === "top-right"
      ? area.y + margin
      : this.config.anchor === "bottom-right"
        ? area.y + area.height - OVERLAY_HEIGHT - margin
        : area.y + Math.round((area.height - OVERLAY_HEIGHT) / 2);
    this.window.setBounds({ x, y, width: OVERLAY_WIDTH, height: OVERLAY_HEIGHT }, false);
  }

  private show(): void {
    if (!this.window || this.window.isDestroyed()) return;
    // A timeout belongs only to the answer that scheduled it. It must never
    // hide a later recording or response that started before it expired.
    this.clearHideTimer();
    this.positionWindow();
    this.window.showInactive();
    this.window.moveTop();
    this.emit({ type: "show" });
  }

  private emit(event: AtlasOverlayEvent): void {
    if (this.window && !this.window.webContents.isDestroyed()) {
      this.window.webContents.send("atlas-overlay:event", event);
    }
  }

  private emitPtt(phase: "down" | "up" | "cancel"): void {
    if (this.window && !this.window.webContents.isDestroyed()) {
      this.window.webContents.send("atlas-overlay:ptt", phase);
    }
  }

  private beginPttWhenReady(): void {
    this.pendingPttUp = false;
    void this.ensureWindow().then(() => {
      this.show();
      this.emitPtt("down");
      if (this.pendingPttUp) {
        this.pendingPttUp = false;
        this.emitPtt("up");
      }
    }).catch((error) => {
      this.options.onLog?.("Atlas overlay window unavailable", error);
      this.emit({
        type: "error",
        message: "Не удалось открыть Atlas Overlay. Перезапустите T-Mod.",
        retryable: true,
      });
    });
  }

  private releasePttWhenReady(): void {
    if (!this.windowReady || this.windowLoad) {
      this.pendingPttUp = true;
      return;
    }
    this.emitPtt("up");
  }

  private beginRequest(requestId: string): void {
    this.activeRequest?.abort();
    this.activeRequest = new AbortController();
    this.activeRequestId = requestId;
    this.show();
  }

  private async processAudio(requestId: string, input: AtlasOverlayAudioInput): Promise<void> {
    this.emit({ type: "progress", stage: "transcribing", label: "Распознаю запрос" });
    const csrf = await this.ensureAtlasSession();
    const form = new FormData();
    form.append(
      "audio",
      new Blob([input.audio], { type: String(input.mimeType || "audio/webm") }),
      "atlas-overlay.webm",
    );
    form.append("duration_ms", String(Math.max(0, Math.round(input.durationMs || 0))));
    const response = await this.options.networkSession().fetch(ATLAS_TRANSCRIBE_URL, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "X-CSRF-Token": csrf,
        "X-Idempotency-Key": requestId,
      },
      body: form,
      signal: this.activeRequest?.signal,
    });
    const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) throw new Error(String(payload.error || payload.message || `STT ${response.status}`));
    const transcript = String(payload.transcript || payload.text || "").trim();
    if (!transcript) throw new Error("atlas_overlay_transcript_empty");
    if (this.activeRequestId !== requestId) return;
    this.emit({ type: "transcript", text: transcript });
    await this.streamQuestion(requestId, transcript, csrf);
  }

  private async streamQuestion(requestId: string, question: string, csrf?: string): Promise<void> {
    const token = csrf || await this.ensureAtlasSession();
    if (this.activeRequestId !== requestId) return;
    if (this.bindingDirty) await this.syncBinding(token);
    this.emit({ type: "progress", stage: "searching", label: "Сверяю с Atlas" });
    const screenContext = this.config.screenContextEnabled
      ? await (this.screenContext || this.captureScreenContext())
      : undefined;
    this.screenContext = undefined;
    const response = await this.options.networkSession().fetch(ATLAS_STREAM_URL, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        "X-CSRF-Token": token,
        "X-Idempotency-Key": requestId,
      },
      body: JSON.stringify({
        question,
        thread_id: this.activeThreadId,
        character_id: this.config.characterId,
        server_code: this.config.serverCode,
        faction_code: this.config.factionCode,
        response_mode: this.config.responseMode,
        latency_mode: "overlay",
        model: "atlas-tvr-a",
        ...(screenContext ? { screen_context: screenContext } : {}),
      }),
      signal: this.activeRequest?.signal,
    });
    if (!response.ok || !response.body) {
      const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
      throw new Error(String(payload.error || payload.message || `Atlas ${response.status}`));
    }
    const decoder = new ServerSentEventDecoder();
    const reader = response.body.getReader();
    let completed = false;
    for (;;) {
      const result = await reader.read();
      if (result.done) break;
      for (const event of decoder.push(result.value)) {
        completed = this.consumeSse(requestId, event) || completed;
      }
    }
    for (const event of decoder.finish()) {
      completed = this.consumeSse(requestId, event) || completed;
    }
    if (!completed && this.activeRequestId === requestId) {
      throw new Error("Atlas завершил поток без ответа.");
    }
  }

  private consumeSse(requestId: string, event: ServerSentEvent): boolean {
    if (this.activeRequestId !== requestId) return false;
    const payload = parseServerSentEventJson(event);
    if (!payload || typeof payload !== "object") return false;
    const item = payload as Record<string, unknown>;
    const type = String(item.type || "");
    if (type === "delta") {
      this.emit({ type: "delta", text: String(item.text || "") });
      return false;
    }
    if (type === "progress") {
      const phase = String(item.phase || "");
      this.emit({
        type: "progress",
        stage: phase === "planning" || phase === "plan" ? "reasoning" : "searching",
        label: String(item.title || item.label || "") || undefined,
      });
      return false;
    }
    if (type === "error") {
      throw new Error(String(item.message || item.error || "Atlas временно недоступен."));
    }
    if (type !== "done") return false;
    if (Number.isFinite(Number(item.thread_id))) this.activeThreadId = Number(item.thread_id);
    const rawCitations = Array.isArray(item.citations) ? item.citations : [];
    this.emit({
      type: "done",
      answer: String(item.answer || ""),
      citations: rawCitations.slice(0, 5).map((citation) => {
        const source = citation && typeof citation === "object"
          ? citation as Record<string, unknown>
          : {};
        return {
          index: Number(source.index) || undefined,
          title: String(source.title || "Источник"),
          url: String(source.url || "") || undefined,
          pinpoint: Array.isArray(source.pinpoints)
            ? source.pinpoints.map(String).join(", ")
            : undefined,
        };
      }),
      latencyMs: Number(item.latency_ms) || undefined,
    });
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.scheduleHide();
    return true;
  }

  private async syncBinding(csrf?: string): Promise<void> {
    if (!this.config.characterId) throw new Error("atlas_overlay_character_required");
    const token = csrf || await this.ensureAtlasSession();
    const response = await this.options.networkSession().fetch(
      "https://atlas.tvr.lat/api/atlas/overlay/context",
      {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
          "X-Idempotency-Key": `overlay-bind-${randomUUID()}`,
        },
        body: JSON.stringify({
          character_id: Number(this.config.characterId),
          server_code: this.config.serverCode,
          faction_code: this.config.factionCode,
          voice_reply_enabled: this.config.speakAnswers,
          screen_context_enabled: this.config.screenContextEnabled,
        }),
        signal: this.activeRequest?.signal,
      },
    );
    const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) {
      throw new Error(String(payload.error || payload.message || `Atlas ${response.status}`));
    }
    this.bindingDirty = false;
  }

  private async ensureAtlasSession(): Promise<string> {
    if (this.csrfToken) return this.csrfToken;
    const response = await this.options.networkSession().fetch(ATLAS_BOOTSTRAP_URL, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: this.activeRequest?.signal,
    });
    if (response.status === 401) throw new Error("atlas_overlay_session_expired");
    const payload = await response.json().catch(() => ({})) as AtlasBootstrapPayload;
    if (!response.ok || !payload.viewer?.atlas_access) {
      throw new Error("atlas_overlay_access_required");
    }
    this.csrfToken = String(payload.viewer.csrf_token || "");
    if (!this.csrfToken) throw new Error("atlas_overlay_session_expired");
    if (!this.catalog.servers.length || !this.catalog.factions.length) {
      this.catalog = asCatalog(this.projection, payload.catalog);
    }
    return this.csrfToken;
  }

  private async captureScreenContext(): Promise<string | undefined> {
    try {
      const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
      const sources = await desktopCapturer.getSources({
        types: ["window", "screen"],
        thumbnailSize: { width: 1280, height: 720 },
        fetchWindowIcons: false,
      });
      const gameWindow = sources.find((item) =>
        /(?:grand theft auto(?: v)?|majestic|rage multiplayer)/i.test(item.name) &&
        !item.thumbnail.isEmpty(),
      );
      const selected = gameWindow ||
        sources.find((item) => item.display_id === String(display.id)) ||
        sources.find((item) => !item.thumbnail.isEmpty());
      if (!selected || selected.thumbnail.isEmpty()) return undefined;
      const encoded = selected.thumbnail.toJPEG(68).toString("base64");
      if (encoded.length > MAX_SCREEN_CONTEXT_BYTES) return undefined;
      return `data:image/jpeg;base64,${encoded}`;
    } catch (error) {
      this.options.onLog?.("Atlas overlay screen context unavailable", error);
      return undefined;
    }
  }

  private failRequest(requestId: string, error: unknown): void {
    if (this.activeRequestId !== requestId) return;
    if (error instanceof DOMException && error.name === "AbortError") return;
    this.options.onLog?.("Atlas overlay request failed", error);
    this.emit({ type: "error", message: safeError(error, "Atlas временно недоступен."), retryable: true });
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.csrfToken = /session|csrf|сесси|защитн/i.test(String(error)) ? "" : this.csrfToken;
    this.scheduleHide(12_000);
  }

  private scheduleHide(delay = 24_000): void {
    this.clearHideTimer();
    this.hideTimer = setTimeout(() => this.hide(), delay);
  }

  private clearHideTimer(): void {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    this.hideTimer = undefined;
  }

  private bindPtt(): void {
    this.unbindPtt();
    if (!this.config.enabled) return;
    if (process.platform === "win32" && existsSync(this.options.hotkeyHelperPath)) {
      const helper = spawn(
        "powershell.exe",
        [
          "-NoLogo",
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy",
          "Bypass",
          "-File",
          this.options.hotkeyHelperPath,
          "-Hotkey",
          this.config.hotkey,
        ],
        { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
      );
      this.hotkeyHelper = helper;
      let pending = "";
      helper.stdout?.setEncoding("utf8");
      helper.stdout?.on("data", (chunk: string) => {
        pending += chunk;
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() || "";
        for (const line of lines) this.handlePttLine(line.trim());
      });
      helper.once("exit", () => {
        if (this.hotkeyHelper === helper) {
          this.hotkeyHelper = undefined;
          this.registerToggleFallback();
        }
      });
      return;
    }
    this.registerToggleFallback();
  }

  private handlePttLine(line: string): void {
    if (line === "down") {
      const ready = this.guardReady();
      if (ready) {
        this.show();
        this.emit({ type: "error", message: safeError(new Error(ready), ready) });
        return;
      }
      this.cancel();
      if (this.config.screenContextEnabled) this.screenContext = this.captureScreenContext();
      this.beginPttWhenReady();
    } else if (line === "up") {
      this.releasePttWhenReady();
    } else if (line.startsWith("error:")) {
      this.options.onLog?.("Atlas overlay hotkey helper error", line);
    }
  }

  private registerToggleFallback(): void {
    if (!globalShortcut.register(this.config.hotkey, () => {
      if (this.fallbackListening) {
        this.fallbackListening = false;
        this.releasePttWhenReady();
        return;
      }
      const ready = this.guardReady();
      if (ready) {
        this.show();
        this.emit({ type: "error", message: safeError(new Error(ready), ready) });
        return;
      }
      this.cancel();
      if (this.config.screenContextEnabled) this.screenContext = this.captureScreenContext();
      this.fallbackListening = true;
      this.beginPttWhenReady();
    })) {
      this.options.onLog?.("Atlas overlay fallback hotkey registration failed", this.config.hotkey);
      return;
    }
    this.fallbackHotkey = this.config.hotkey;
  }

  private unbindPtt(): void {
    if (this.fallbackHotkey) globalShortcut.unregister(this.fallbackHotkey);
    this.fallbackHotkey = "";
    this.fallbackListening = false;
    if (this.hotkeyHelper && !this.hotkeyHelper.killed) this.hotkeyHelper.kill();
    this.hotkeyHelper = undefined;
  }
}
