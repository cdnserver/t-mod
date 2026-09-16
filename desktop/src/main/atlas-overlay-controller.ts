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
  AtlasOverlayCraftSnapshot,
  AtlasOverlayEvent,
  AtlasOverlaySubmitResult,
  AtlasOverlaySpeechResult,
  AtlasOverlayRuntimeStatus,
  AtlasOverlayVoiceCatalog,
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
import { splitAtlasOverlaySpeech } from "../shared/atlas-overlay-speech-segments";
import {
  parseAtlasOverlayForegroundProbe,
  resolveAtlasOverlayDisplayArea,
  resolveAtlasOverlayForegroundGame,
  resolveAtlasOverlayWindowBounds,
  type AtlasOverlayActiveGameWindow,
  type AtlasOverlayForegroundProbe,
} from "./atlas-overlay-foreground";

const ATLAS_BOOTSTRAP_URL = "https://dash.tvr.lat/api/atlas/bootstrap";
const ATLAS_STREAM_URL = "https://dash.tvr.lat/api/atlas/chat/stream";
const ATLAS_TRANSCRIBE_URL = "https://dash.tvr.lat/api/atlas/overlay/transcribe";
const ATLAS_TTS_VOICES_URL = "https://dash.tvr.lat/api/atlas/overlay/tts/voices";
const ATLAS_TTS_PREVIEW_URL = "https://dash.tvr.lat/api/atlas/overlay/tts/preview";
const ATLAS_TTS_SYNTHESIZE_URL = "https://dash.tvr.lat/api/atlas/overlay/tts/synthesize";
const ATLAS_CRAFTS_URL = "https://dash.tvr.lat/api/atlas/overlay/crafts";
const MAX_AUDIO_BYTES = 6 * 1024 * 1024;
const MAX_AUDIO_DURATION_MS = 25_000;
const MAX_SCREEN_CONTEXT_BYTES = 1_200_000;
const OVERLAY_WIDTH = 760;
const OVERLAY_HEIGHT = 620;
const GAME_SCREEN_SOURCE_PATTERN = /(?:grand theft auto(?:\s*v)?|gta\s*5|gta5|rage\s*(?:multiplayer|mp)|ragemp|majestic)/i;
const FOREGROUND_PROBE_POLL_MS = 140;
const FOREGROUND_PROBE_WATCHDOG_MS = 1_800;
const FOREGROUND_PROBE_RESTART_MIN_MS = 850;
const FOREGROUND_PROBE_RESTART_MAX_MS = 12_000;
const OVERLAY_VISIBILITY_HEAL_MS = 1_200;
const FALLBACK_GAME_SCAN_MS = 1_500;
const GAME_WINDOW_SOURCE_PATTERN = /^(?:grand theft auto(?:\s*v)?|gta\s*5|rage\s*(?:multiplayer|mp)|ragemp|majestic(?:\s*rp)?)(?:\s|$|[—–-])/i;
const NON_GAME_WINDOW_SOURCE_PATTERN = /(?:chrome|edge|firefox|yandex|opera|browser|браузер)/i;
// Full-screen GTA can briefly report an empty/transition HWND while switching
// render surfaces, especially on laptops with hybrid graphics. Atlas remains
// fail-closed, but an active request gets a larger grace window so a harmless
// DWM transition cannot cut off the visible or spoken answer.
const FOREGROUND_LOSS_GRACE_MS = 1_100;
const FOREGROUND_BUSY_LOSS_GRACE_MS = 4_200;
const POST_SPEECH_HOLD_MS = 4_500;
const INITIALIZATION_VISIBLE_MS = 3_300;
const MANUAL_INPUT_TIMEOUT_MS = 35_000;
const MAX_STREAMED_AI_PHRASES = 8;
const CRAFT_POLL_MS = 5_000;
const CRAFT_AUTH_RETRY_MS = 30_000;
const CRAFT_ACCESS_RETRY_MS = 300_000;

interface AtlasOverlaySpeechSession {
  requestId: string;
  generation: number;
  streamedText: string;
  remainder: string;
  phrasesQueued: number;
}

/*
 * Electron deliberately has no cross-platform API for the native foreground
 * HWND.  A short-lived `desktopCapturer` listing can tell us that GTA exists,
 * but not whether the player alt-tabbed away.  This helper runs only on
 * Windows, owns no UI and reports the foreground HWND's process/title and the
 * monitor work area.  Any launch, parse or native API failure is fail-closed
 * in the controller below.
 */
const WINDOWS_FOREGROUND_PROBE_SCRIPT = String.raw`
$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class TModAtlasForegroundProbe {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct MONITORINFO {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
    }

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int maxCount);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern IntPtr MonitorFromWindow(IntPtr hWnd, uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFO info);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(
        IntPtr process, uint flags, StringBuilder path, ref uint size
    );

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static string GetProcessName(uint processId) {
        const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        IntPtr process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, processId);
        if (process == IntPtr.Zero) return "";
        try {
            uint size = 1024;
            StringBuilder path = new StringBuilder((int)size);
            if (!QueryFullProcessImageName(process, 0, path, ref size)) return "";
            return System.IO.Path.GetFileNameWithoutExtension(path.ToString());
        } finally {
            CloseHandle(process);
        }
    }
}
"@

function New-TModAtlasForegroundProbe {
    $empty = [ordered]@{
        available = $true
        title = ""
        processName = ""
        processId = 0
        visible = $false
        minimized = $true
    }
    $hwnd = [TModAtlasForegroundProbe]::GetForegroundWindow()
    if ($hwnd -eq [IntPtr]::Zero) { return $empty }

    $visible = [TModAtlasForegroundProbe]::IsWindowVisible($hwnd)
    $minimized = [TModAtlasForegroundProbe]::IsIconic($hwnd)
    if (-not $visible -or $minimized) { return $empty }

    $caption = New-Object System.Text.StringBuilder 512
    [void][TModAtlasForegroundProbe]::GetWindowText($hwnd, $caption, $caption.Capacity)
    $processId = [uint32]0
    [void][TModAtlasForegroundProbe]::GetWindowThreadProcessId($hwnd, [ref]$processId)
    $processName = ""
    try { $processName = (Get-Process -Id $processId -ErrorAction Stop).ProcessName } catch {}
    if (-not $processName -and $processId -gt 0) {
        try { $processName = [TModAtlasForegroundProbe]::GetProcessName($processId) } catch {}
    }

    $result = [ordered]@{
        available = $true
        title = $caption.ToString().Trim()
        processName = $processName
        processId = $processId
        windowHandle = $hwnd.ToInt64().ToString()
        visible = $true
        minimized = $false
    }
    $monitor = [TModAtlasForegroundProbe]::MonitorFromWindow($hwnd, 2)
    if ($monitor -ne [IntPtr]::Zero) {
        $info = New-Object TModAtlasForegroundProbe+MONITORINFO
        $info.cbSize = [Runtime.InteropServices.Marshal]::SizeOf($info)
        if ([TModAtlasForegroundProbe]::GetMonitorInfo($monitor, [ref]$info)) {
            $result.workArea = [ordered]@{
                x = $info.rcWork.Left
                y = $info.rcWork.Top
                width = $info.rcWork.Right - $info.rcWork.Left
                height = $info.rcWork.Bottom - $info.rcWork.Top
            }
        }
    }
    return $result
}

try {
    while ($true) {
        $probe = New-TModAtlasForegroundProbe
        [Console]::Out.WriteLine(($probe | ConvertTo-Json -Compress -Depth 3))
        [Console]::Out.Flush()
        Start-Sleep -Milliseconds ${FOREGROUND_PROBE_POLL_MS}
    }
} catch {
    [Console]::Out.WriteLine('{"available":false}')
    [Console]::Out.Flush()
    exit 1
}
`;
const WINDOWS_FOREGROUND_PROBE_COMMAND = Buffer
  .from(WINDOWS_FOREGROUND_PROBE_SCRIPT, "utf16le")
  .toString("base64");

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
    atlas_overlay_game_not_active: "Atlas Overlay доступен только когда GTA V / RAGE MP в фокусе.",
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
  private pttStartToken = 0;
  private config: AtlasOverlayConfig = { ...DEFAULT_ATLAS_OVERLAY_CONFIG };
  private catalog: AtlasOverlayCatalog = { characters: [], servers: [], factions: [] };
  private projection?: AtlasOverlayBootstrapProjection;
  private csrfToken = "";
  private activeRequest?: AbortController;
  private activeRequestId?: string;
  private activeThreadId?: number;
  private hotkeyHelper?: ChildProcess;
  private fallbackHotkey = "";
  private craftHotkey = "";
  private fallbackListening = false;
  private bindingDirty = true;
  private screenContext?: Promise<string | undefined>;
  private hideTimer?: ReturnType<typeof setTimeout>;
  private foregroundProbe?: ChildProcess;
  private foregroundProbeWatchdog?: ReturnType<typeof setTimeout>;
  private foregroundProbeRestartTimer?: ReturnType<typeof setTimeout>;
  private foregroundProbeRestartAttempts = 0;
  private fallbackGameScanTimer?: ReturnType<typeof setTimeout>;
  private fallbackGameScanInFlight = false;
  private foregroundLossTimer?: ReturnType<typeof setTimeout>;
  private overlayWindowRecoveryTimer?: ReturnType<typeof setTimeout>;
  private lastOverlayVisibilityHealAt = 0;
  private lastNativeZOrderErrorAt = 0;
  private lastFallbackGameSeenAt = 0;
  private initializationTimer?: ReturnType<typeof setTimeout>;
  /** One cinematic handshake per T-Mod process, regardless of GTA HWND/PID changes. */
  private initializationPresented = false;
  private initializationInFlight = false;
  private activeGameWindow?: AtlasOverlayActiveGameWindow;
  private overlayInputEnabled = false;
  private overlayFocusable = false;
  private manualInputUntil = 0;
  private manualInputTimer?: ReturnType<typeof setTimeout>;
  private responseVisibleUntil = 0;
  private speechGeneration = 0;
  private speechSession?: AtlasOverlaySpeechSession;
  private speechQueue: Promise<void> = Promise.resolve();
  private speechPlaybackActive = false;
  private speechSynthesisPending = 0;
  private settleAfterSpeech = false;
  private craftPollTimer?: ReturnType<typeof setTimeout>;
  private craftPollInFlight = false;
  private craftEtag = "";
  private craftSnapshot?: AtlasOverlayCraftSnapshot;
  private readonly acknowledgedCraftAlarms = new Set<string>();
  private disposed = false;

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
    this.scheduleCraftPoll(250);
  }

  api(): Pick<
    AtlasOverlayApi,
    "getConfig" | "getCatalog" | "getStatus" | "saveConfig" | "moveBy" | "getVoices" | "previewVoice" |
    "submitAudio" | "submitText" |
    "cancel" | "hide" | "openAtlas" | "reportSpeech"
  > {
    return {
      getConfig: async () => this.getConfig(),
      getCatalog: async () => this.getCatalog(),
      getStatus: async () => this.getStatus(),
      saveConfig: async (patch) => this.saveConfig(patch),
      moveBy: async (deltaX, deltaY) => this.moveBy(deltaX, deltaY),
      getVoices: async () => this.getVoices(),
      previewVoice: async (voice) => this.previewVoice(voice),
      submitAudio: async (input) => this.submitAudio(input),
      submitText: async (question) => this.submitText(question),
      cancel: async () => this.cancel(),
      hide: async () => this.hide(),
      openAtlas: async () => this.openAtlas(),
      reportSpeech: async (active) => this.reportSpeech(active),
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

  getStatus(): AtlasOverlayRuntimeStatus {
    const game = this.activeGameWindow;
    const windowVisible = Boolean(this.window && !this.window.isDestroyed() && this.window.isVisible());
    if (!this.config.enabled) {
      return { mode: "disabled", gameDetected: false, foregroundVerified: false, windowReady: this.windowReady, windowVisible, message: "Оверлей выключен" };
    }
    if (!this.projection?.allowed) {
      return { mode: "denied", gameDetected: false, foregroundVerified: false, windowReady: this.windowReady, windowVisible, message: "Нет доступа к Atlas AI" };
    }
    if (process.platform !== "win32") {
      return { mode: "unsupported", gameDetected: false, foregroundVerified: false, windowReady: this.windowReady, windowVisible, message: "Игровой оверлей доступен в Windows" };
    }
    if (game) {
      return {
        mode: game.foregroundVerified ? "native" : "compatibility",
        gameDetected: true,
        foregroundVerified: game.foregroundVerified,
        windowReady: this.windowReady,
        windowVisible,
        display: { ...game.workArea },
        message: game.foregroundVerified ? "GTA в фокусе · точный режим" : "GTA найдена · режим совместимости",
      };
    }
    const recovering = !this.foregroundProbe && Boolean(this.foregroundProbeRestartTimer || this.fallbackGameScanTimer);
    return {
      mode: recovering ? "recovering" : "waiting",
      gameDetected: false,
      foregroundVerified: false,
      windowReady: this.windowReady,
      windowVisible,
      message: recovering ? "Восстанавливаю детектор GTA" : "Ожидаю GTA V / RAGE MP",
    };
  }

  async getVoices(): Promise<AtlasOverlayVoiceCatalog> {
    try {
      const response = await this.options.networkSession().fetch(ATLAS_TTS_VOICES_URL, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
      if (!response.ok) throw new Error(String(payload.error || `Atlas TTS ${response.status}`));
      const rawVoices = Array.isArray(payload.voices) ? payload.voices : [];
      const rawAvailability = payload.availability && typeof payload.availability === "object"
        ? payload.availability as Record<string, unknown>
        : undefined;
      const availabilityState = String(rawAvailability?.state || "");
      const availability = ["ready", "unconfigured", "recovering", "degraded"].includes(availabilityState)
        ? {
            state: availabilityState as "ready" | "unconfigured" | "recovering" | "degraded",
            ...(String(rawAvailability?.reason || "").trim()
              ? { reason: String(rawAvailability?.reason || "").trim().slice(0, 220) }
              : {}),
          }
        : undefined;
      return {
        configured: payload.configured === true,
        provider: payload.configured === true ? "ai" : "system",
        defaultVoice: String(payload.default_voice || ""),
        ...(availability ? { availability } : {}),
        voices: rawVoices.slice(0, 24).map((item) => {
          const voice = item && typeof item === "object" ? item as Record<string, unknown> : {};
          return {
            id: String(voice.id || "").slice(0, 80),
            name: String(voice.name || voice.id || "Atlas").slice(0, 80),
            description: String(voice.description || "AI-голос Atlas").slice(0, 160),
            provider: "ai" as const,
          };
        }).filter((voice) => voice.id),
      };
    } catch (error) {
      this.options.onLog?.("Atlas AI voice catalog unavailable; using system voices", error);
      return {
        configured: false,
        provider: "system",
        defaultVoice: "",
        availability: { state: "degraded", reason: "Не удалось проверить AI-озвучку." },
        voices: [],
      };
    }
  }

  async previewVoice(voice?: string): Promise<AtlasOverlaySpeechResult> {
    try {
      const csrf = await this.ensureAtlasSession();
      return await this.requestSpeech(ATLAS_TTS_PREVIEW_URL, {
        voice: String(voice || this.config.speechVoice || ""),
      }, csrf);
    } catch (error) {
      return { fallback: true, error: safeError(error, "AI-голос временно недоступен.") };
    }
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
      this.stopGameDetection();
      this.hide();
      this.stopCraftPolling();
    } else if (this.config.enabled) {
      await this.ensureWindow();
      // Desktop refreshes its bootstrap periodically. Reusing the healthy
      // probe avoids a hide/restart flash every 45 seconds and preserves an
      // answer that is still visible or being spoken.
      if (!this.foregroundProbe) this.startForegroundProbe();
      else if (this.activeGameWindow) this.healOverlayVisibility();
      this.scheduleCraftPoll(0);
    }
  }

  invalidateAccountSession(): void {
    // The HttpOnly account cookie was replaced or removed. Never let an old
    // user's CSRF token, thread or in-flight answer cross the new SSO boundary.
    this.activeRequest?.abort();
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.csrfToken = "";
    this.activeThreadId = undefined;
    this.bindingDirty = true;
    this.craftEtag = "";
    this.craftSnapshot = undefined;
    this.stopCraftPolling();
    this.cancelSpeechDelivery();
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
    if (
      previous.hotkey !== next.hotkey ||
      previous.craftHotkey !== next.craftHotkey ||
      previous.enabled !== next.enabled
    ) this.bindPtt();
    if (
      previous.anchor !== next.anchor ||
      previous.positionX !== next.positionX ||
      previous.positionY !== next.positionY
    ) this.positionWindow();
    if (previous.captureInRecordings !== next.captureInRecordings) {
      this.window?.setContentProtection(!next.captureInRecordings);
    }
    this.emit({ type: "config", config: this.getConfig() });
    if (previous.workspaceMode !== next.workspaceMode && this.craftSnapshot) {
      this.emit({ type: "crafts", snapshot: this.craftSnapshot });
    }
    if (next.enabled && this.projection?.allowed) {
      await this.ensureWindow();
      this.scheduleCraftPoll(0);
      if (!this.foregroundProbe) {
        this.syncGameDetection();
      } else if (this.activeGameWindow) {
        if (next.calibrationMode) this.show();
        else if (this.shouldKeepIdleVisible()) this.showIdle();
        else this.hide();
      } else {
        this.hide();
      }
    } else {
      this.stopCraftPolling();
      this.stopGameDetection();
      this.hide();
    }
    return this.getConfig();
  }

  /** Applies a physical drag delta against the validated GTA monitor, not the
   * overlay's fixed 640×480 viewport. */
  async moveBy(deltaX: number, deltaY: number): Promise<AtlasOverlayConfig> {
    if (!this.config.calibrationMode || !this.activeGameWindow) {
      throw new Error("atlas_overlay_game_not_active");
    }
    const x = Number(deltaX);
    const y = Number(deltaY);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw new Error("atlas_overlay_position_invalid");
    }
    const area = this.activeGameWindow.workArea;
    const firstPosition = resolveAtlasOverlayWindowBounds(area, 0, 0);
    const lastPosition = resolveAtlasOverlayWindowBounds(area, 1, 1);
    const availableWidth = Math.max(1, lastPosition.x - firstPosition.x);
    const availableHeight = Math.max(1, lastPosition.y - firstPosition.y);
    const next = normalizeAtlasOverlayConfig({
      ...this.config,
      positionX: this.config.positionX + Math.max(-360, Math.min(360, x)) / availableWidth,
      positionY: this.config.positionY + Math.max(-260, Math.min(260, y)) / availableHeight,
    });
    this.config = next;
    await this.persistConfig();
    this.positionWindow();
    this.emit({ type: "config", config: this.getConfig() });
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
    if (this.initializationTimer) clearTimeout(this.initializationTimer);
    this.initializationTimer = undefined;
    this.pttStartToken += 1;
    this.cancelSpeechDelivery();
    this.activeRequest?.abort();
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.screenContext = undefined;
    this.pendingPttUp = false;
    this.responseVisibleUntil = 0;
    this.speechPlaybackActive = false;
    this.speechSynthesisPending = 0;
    this.settleAfterSpeech = false;
    this.endManualInput();
    this.emitPtt("cancel");
    this.settleOverlay();
  }

  hide(): void {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    this.hideTimer = undefined;
    this.responseVisibleUntil = 0;
    this.speechPlaybackActive = false;
    this.speechSynthesisPending = 0;
    this.settleAfterSpeech = false;
    this.cancelSpeechDelivery();
    this.endManualInput();
    this.updateOverlayInputMode(false);
    this.emit({ type: "hide" });
    // A stable BrowserWindow handle is enough for OBS discovery; keeping an
    // actually visible transparent window outside the game is not safe.
    this.window?.hide();
  }

  async openAtlas(): Promise<void> {
    await shell.openExternal("https://dash.tvr.lat/");
  }

  reportSpeech(active: boolean): void {
    const wasActive = this.speechPlaybackActive;
    this.speechPlaybackActive = active === true;
    if (this.speechPlaybackActive) {
      this.settleAfterSpeech = false;
      this.clearHideTimer();
      return;
    }
    if (!wasActive || this.speechSynthesisPending > 0) return;
    this.resumeResponseTimeoutAfterSpeech();
  }

  dispose(): void {
    this.disposed = true;
    this.cancel();
    this.unbindPtt();
    this.stopGameDetection();
    this.stopCraftPolling();
    if (this.hideTimer) clearTimeout(this.hideTimer);
    if (this.manualInputTimer) clearTimeout(this.manualInputTimer);
    if (this.foregroundLossTimer) clearTimeout(this.foregroundLossTimer);
    if (this.fallbackGameScanTimer) clearTimeout(this.fallbackGameScanTimer);
    if (this.initializationTimer) clearTimeout(this.initializationTimer);
    if (this.overlayWindowRecoveryTimer) clearTimeout(this.overlayWindowRecoveryTimer);
    if (this.window && !this.window.isDestroyed()) this.window.destroy();
    this.window = null;
  }

  onDisplaysChanged(): void {
    if (this.activeGameWindow) this.positionWindow();
  }

  private guardReady(): string | undefined {
    if (!this.config.enabled) return "atlas_overlay_disabled";
    if (!this.projection?.allowed) return "atlas_overlay_access_required";
    if (!this.config.characterId) return "atlas_overlay_character_required";
    if (!this.activeGameWindow) return "atlas_overlay_game_not_active";
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
      title: "T-Mod Atlas Overlay",
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
      alwaysOnTop: true,
      skipTaskbar: true,
      hasShadow: false,
      webPreferences: {
        preload: this.options.preloadPath,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false,
        // Audio returned by Atlas is delivered after a background network
        // stream, not a renderer click.  Permit that trusted overlay document
        // to start its validated response audio without falling back to SAPI.
        autoplayPolicy: "no-user-gesture-required",
      },
    });
    this.window = overlayWindow;
    this.windowReady = false;
    overlayWindow.webContents.setZoomFactor(1);
    void overlayWindow.webContents.setVisualZoomLevelLimits(1, 1);
    overlayWindow.setAlwaysOnTop(true, "screen-saver", 1);
    overlayWindow.setIgnoreMouseEvents(true, { forward: true });
    overlayWindow.setContentProtection(!this.config.captureInRecordings);
    overlayWindow.setFocusable(false);
    overlayWindow.setMenuBarVisibility(false);
    if (process.platform === "darwin") {
      overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    }
    overlayWindow.on("closed", () => {
      if (this.window === overlayWindow) {
        this.window = null;
        this.windowReady = false;
        this.windowLoad = undefined;
      }
    });
    overlayWindow.on("unresponsive", () => {
      this.scheduleOverlayWindowRecovery(overlayWindow, new Error("renderer_unresponsive"));
    });
    overlayWindow.on("always-on-top-changed", (_event, isAlwaysOnTop) => {
      if (!isAlwaysOnTop && this.activeGameWindow && !overlayWindow.isDestroyed()) {
        overlayWindow.setAlwaysOnTop(true, "screen-saver", 1);
      }
    });
    overlayWindow.webContents.on("render-process-gone", (_event, details) => {
      this.scheduleOverlayWindowRecovery(
        overlayWindow,
        new Error(`renderer_process_gone:${details.reason}`),
      );
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
      if (this.window === overlayWindow && !overlayWindow.isDestroyed()) {
        this.windowReady = true;
        this.emit({ type: "config", config: this.getConfig() });
        if (this.shouldKeepIdleVisible()) this.showIdle();
        else if (this.config.calibrationMode && this.activeGameWindow) this.show();
      }
    } catch (error) {
      if (!overlayWindow.isDestroyed()) overlayWindow.destroy();
      throw error;
    } finally {
      if (this.window === overlayWindow) this.windowLoad = undefined;
    }
  }

  private positionWindow(): void {
    if (!this.window || this.window.isDestroyed()) return;
    // Never anchor to the cursor: it may be on a secondary monitor while GTA
    // is focused elsewhere.  The foreground probe gives us the game's own
    // monitor work area, and no probe means no placement/visibility.
    const area = this.activeGameWindow?.workArea;
    if (!area) return;
    this.window.setBounds(
      resolveAtlasOverlayWindowBounds(area, this.config.positionX, this.config.positionY),
      false,
    );
  }

  private show(): void {
    if (!this.window || this.window.isDestroyed()) return;
    if (!this.activeGameWindow) {
      this.hide();
      return;
    }
    // A timeout belongs only to the answer that scheduled it. It must never
    // hide a later recording or response that started before it expired.
    this.clearHideTimer();
    this.positionWindow();
    // Re-assert the native z-order on every transition. Windows may demote an
    // always-on-top window when GTA switches render surfaces or GPUs.
    this.window.setAlwaysOnTop(true, "screen-saver", 1);
    this.window.showInactive();
    this.raiseOverlayAboveGame();
    this.window.webContents.invalidate();
    this.updateOverlayInputMode();
    this.emit({ type: "show" });
  }

  private showIdle(): void {
    this.show();
    this.emit({ type: "idle" });
  }

  /**
   * Calibration remains click-through and is driven by the native keyboard
   * helper, so GTA never has to release its cursor. Text input is the only
   * short, explicit focus exception and is verified against this exact window.
   */
  private updateOverlayInputMode(interactiveOverride?: boolean): void {
    const overlayWindow = this.window;
    if (!overlayWindow || overlayWindow.isDestroyed()) {
      this.overlayInputEnabled = false;
      this.overlayFocusable = false;
      return;
    }
    const manualInputActive = this.isManualInputActive();
    const interactive = interactiveOverride ?? Boolean(
      manualInputActive &&
      this.activeGameWindow &&
      overlayWindow.isVisible(),
    );
    const focusable = interactive && manualInputActive;
    if (interactive === this.overlayInputEnabled && focusable === this.overlayFocusable) return;
    try {
      if (!focusable && this.overlayFocusable) overlayWindow.blur();
      overlayWindow.setFocusable(focusable);
      if (interactive) overlayWindow.setIgnoreMouseEvents(false);
      else overlayWindow.setIgnoreMouseEvents(true, { forward: true });
      this.overlayInputEnabled = interactive;
      this.overlayFocusable = focusable;
    } catch (error) {
      // A failed native transition must not leave the overlay interactive
      // while the foreground state is uncertain.
      this.overlayInputEnabled = false;
      this.overlayFocusable = false;
      this.options.onLog?.("Atlas overlay input mode update failed", error);
    }
  }

  private isManualInputActive(): boolean {
    return Boolean(
      this.manualInputUntil > Date.now() &&
      this.activeGameWindow &&
      this.window &&
      !this.window.isDestroyed() &&
      this.window.isVisible(),
    );
  }

  private beginManualInput(): void {
    const overlayWindow = this.window;
    if (!overlayWindow || overlayWindow.isDestroyed() || !this.activeGameWindow) return;
    const expiration = Date.now() + MANUAL_INPUT_TIMEOUT_MS;
    this.manualInputUntil = expiration;
    if (this.manualInputTimer) clearTimeout(this.manualInputTimer);
    this.manualInputTimer = setTimeout(() => {
      if (this.manualInputUntil !== expiration) return;
      this.manualInputUntil = 0;
      this.manualInputTimer = undefined;
      this.updateOverlayInputMode();
      this.settleOverlay();
    }, MANUAL_INPUT_TIMEOUT_MS);
    this.updateOverlayInputMode();
    try {
      overlayWindow.focus();
    } catch (error) {
      this.options.onLog?.("Atlas overlay text input could not take focus", error);
    }
  }

  private endManualInput(): void {
    if (this.manualInputTimer) clearTimeout(this.manualInputTimer);
    this.manualInputTimer = undefined;
    if (!this.manualInputUntil) return;
    this.manualInputUntil = 0;
    this.updateOverlayInputMode();
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
    const token = ++this.pttStartToken;
    this.pendingPttUp = false;
    void this.ensureWindow().then(() => {
      if (token !== this.pttStartToken || !this.activeGameWindow) {
        this.hide();
        return;
      }
      this.show();
      if (token !== this.pttStartToken || !this.activeGameWindow) return;
      this.emitPtt("down");
      if (this.pendingPttUp && token === this.pttStartToken) {
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
    this.cancelSpeechDelivery();
    this.speechSession = {
      requestId,
      generation: this.speechGeneration,
      streamedText: "",
      remainder: "",
      phrasesQueued: 0,
    };
    this.endManualInput();
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
        response_mode: this.config.responseMode === "quick" ? "strict" : "balanced",
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
      const text = String(item.text || "");
      this.emit({ type: "delta", text });
      this.appendStreamedAiSpeech(requestId, text);
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
    const answer = String(item.answer || "");
    this.finishStreamedAiSpeech(requestId, answer);
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    const hideDelay = this.answerVisibleDelay(answer);
    this.responseVisibleUntil = Date.now() + hideDelay;
    this.scheduleHide(hideDelay);
    return true;
  }

  private async syncBinding(csrf?: string): Promise<void> {
    if (!this.config.characterId) throw new Error("atlas_overlay_character_required");
    const token = csrf || await this.ensureAtlasSession();
    const response = await this.options.networkSession().fetch(
      "https://dash.tvr.lat/api/atlas/overlay/context",
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

  private async requestSpeech(
    url: string,
    payload: Record<string, unknown>,
    csrf: string,
  ): Promise<AtlasOverlaySpeechResult> {
    const response = await this.options.networkSession().fetch(url, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        Accept: "audio/mpeg, audio/*;q=.9, application/json;q=.5",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "X-Idempotency-Key": `overlay-tts-${randomUUID()}`,
      },
      body: JSON.stringify(payload),
    });
    if (response.status === 204 || response.headers.get("x-atlas-tts-fallback") === "system") {
      return { fallback: true };
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as Record<string, unknown>;
      throw new Error(String(body.error || body.message || `Atlas TTS ${response.status}`));
    }
    const mimeType = String(response.headers.get("content-type") || "").split(";", 1)[0].trim();
    if (!/^audio\//i.test(mimeType)) throw new Error("atlas_tts_audio_invalid");
    const audio = await response.arrayBuffer();
    if (audio.byteLength < 128 || audio.byteLength > 8 * 1024 * 1024) {
      throw new Error("atlas_tts_audio_invalid");
    }
    return { audio, mimeType, fallback: false };
  }

  private cancelSpeechDelivery(): void {
    this.speechGeneration += 1;
    this.speechSession = undefined;
    this.speechSynthesisPending = 0;
    // Detach a new request from a slow, obsolete provider call.  Each queued
    // task still checks its generation before it can emit renderer audio.
    this.speechQueue = Promise.resolve();
  }

  private appendStreamedAiSpeech(requestId: string, text: string): void {
    const session = this.speechSession;
    if (!session || session.requestId !== requestId || !text) return;
    session.streamedText += text;
    if (!this.config.speakAnswers || this.config.speechProvider !== "ai") return;
    session.remainder += text;
    this.flushStreamedAiSpeech(session, false);
  }

  private finishStreamedAiSpeech(requestId: string, answer: string): void {
    const session = this.speechSession;
    if (!session || session.requestId !== requestId) {
      if (this.config.speakAnswers && this.config.speechProvider === "ai" && answer.trim()) {
        this.enqueueAiSpeech(answer, this.speechGeneration);
      }
      return;
    }
    if (this.config.speakAnswers && this.config.speechProvider === "ai") {
      if (!session.streamedText.trim()) {
        session.remainder = answer;
      } else if (answer.startsWith(session.streamedText)) {
        // Streams normally repeat the accumulated deltas in `done`.  Append
        // only the final suffix so an already spoken sentence is never read a
        // second time.
        session.remainder += answer.slice(session.streamedText.length);
      } else if (!session.phrasesQueued && answer.trim()) {
        // A provider is allowed to replace an incomplete stream at `done`.
        // Prefer that authoritative answer before any phrase has escaped.
        session.remainder = answer;
      }
      this.flushStreamedAiSpeech(session, true);
    }
    if (this.speechSession === session) this.speechSession = undefined;
  }

  private flushStreamedAiSpeech(session: AtlasOverlaySpeechSession, flush: boolean): void {
    if (
      session.generation !== this.speechGeneration ||
      !this.config.speakAnswers ||
      this.config.speechProvider !== "ai" ||
      !session.remainder
    ) return;
    const slots = MAX_STREAMED_AI_PHRASES - session.phrasesQueued;
    if (slots <= 0) return;
    const split = splitAtlasOverlaySpeech(session.remainder, {
      flush,
      maximumChars: 320,
      maximumPhrases: slots,
    });
    session.remainder = split.remainder;
    for (const phrase of split.phrases) {
      session.phrasesQueued += 1;
      this.enqueueAiSpeech(phrase, session.generation);
    }
  }

  private enqueueAiSpeech(text: string, generation: number): void {
    const phrase = text.trim();
    if (!phrase) return;
    if (generation !== this.speechGeneration) return;
    this.speechSynthesisPending += 1;
    this.clearHideTimer();
    const previous = this.speechQueue;
    this.speechQueue = previous
      .catch((error) => this.options.onLog?.("Atlas overlay speech queue recovered", error))
      .then(async () => {
        if (
          generation !== this.speechGeneration ||
          !this.config.speakAnswers ||
          this.config.speechProvider !== "ai"
        ) return;
        await this.speakAnswer(phrase, generation);
      })
      .finally(() => {
        if (generation !== this.speechGeneration) return;
        this.speechSynthesisPending = Math.max(0, this.speechSynthesisPending - 1);
        if (!this.speechPlaybackActive && this.speechSynthesisPending === 0) {
          this.resumeResponseTimeoutAfterSpeech();
        }
      });
  }

  private answerVisibleDelay(answer: string): number {
    const words = String(answer || "").trim().split(/\s+/).filter(Boolean).length;
    if (this.config.answerHold === "brief") return Math.max(8_000, 4_000 + words * 160);
    if (this.config.answerHold === "pinned") return 15 * 60_000;
    return Math.max(14_000, Math.min(32_000, 7_000 + words * 300));
  }

  private resumeResponseTimeoutAfterSpeech(): void {
    if (this.responseVisibleUntil <= 0) return;
    const visibleUntil = this.config.answerHold === "pinned"
      ? Math.max(this.responseVisibleUntil, Date.now() + 15 * 60_000)
      : Math.max(this.responseVisibleUntil, Date.now() + POST_SPEECH_HOLD_MS);
    this.settleAfterSpeech = false;
    this.scheduleHideUntil(visibleUntil);
  }

  private extendResponseVisibility(text: string): void {
    const words = String(text || "").trim().split(/\s+/).filter(Boolean).length;
    const until = Math.max(
      this.responseVisibleUntil,
      Date.now() + Math.max(12_000, Math.min(30_000, 5_000 + words * 520)),
    );
    this.scheduleHideUntil(until);
  }

  private async speakAnswer(text: string, generation: number): Promise<void> {
    try {
      const csrf = await this.ensureAtlasSession();
      const result = await this.requestSpeech(ATLAS_TTS_SYNTHESIZE_URL, {
        text,
        voice: this.config.speechVoice || undefined,
        speed: Math.max(0.8, Math.min(1.25, this.config.speechRate)),
      }, csrf);
      if (
        generation !== this.speechGeneration ||
        !this.config.speakAnswers ||
        this.config.speechProvider !== "ai"
      ) return;
      this.extendResponseVisibility(text);
      this.emit(result.fallback
        ? { type: "speech", fallbackText: text }
        : {
            type: "speech",
            audio: result.audio,
            mimeType: result.mimeType,
            fallbackText: text,
          });
    } catch (error) {
      this.options.onLog?.("Atlas AI voice unavailable; using local speech", error);
      if (
        generation === this.speechGeneration &&
        this.config.speakAnswers &&
        this.config.speechProvider === "ai"
      ) {
        this.extendResponseVisibility(text);
        this.emit({ type: "speech", fallbackText: text });
      }
    }
  }

  private async captureScreenContext(): Promise<string | undefined> {
    try {
      const game = this.activeGameWindow;
      if (!game) return undefined;
      const display = screen.getDisplayNearestPoint({
        x: game.workArea.x + Math.round(game.workArea.width / 2),
        y: game.workArea.y + Math.round(game.workArea.height / 2),
      });
      const sources = await desktopCapturer.getSources({
        types: ["window", "screen"],
        thumbnailSize: { width: 1280, height: 720 },
        fetchWindowIcons: false,
      });
      const gameWindow = sources.find((item) =>
        GAME_SCREEN_SOURCE_PATTERN.test(item.name) &&
        !item.thumbnail.isEmpty(),
      );
      const selected = gameWindow ||
        sources.find((item) => item.display_id === String(display.id) && !item.thumbnail.isEmpty());
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
    this.cancelSpeechDelivery();
    if (error instanceof DOMException && error.name === "AbortError") return;
    this.options.onLog?.("Atlas overlay request failed", error);
    this.emit({ type: "error", message: safeError(error, "Atlas временно недоступен."), retryable: true });
    this.activeRequest = undefined;
    this.activeRequestId = undefined;
    this.csrfToken = /session|csrf|сесси|защитн/i.test(String(error)) ? "" : this.csrfToken;
    this.responseVisibleUntil = Date.now() + 10_000;
    this.scheduleHide(12_000);
  }

  private scheduleHide(delay = 24_000): void {
    this.scheduleHideUntil(Date.now() + delay);
  }

  private scheduleHideUntil(until: number): void {
    this.clearHideTimer();
    this.responseVisibleUntil = until;
    this.hideTimer = setTimeout(() => {
      this.hideTimer = undefined;
      if (this.speechPlaybackActive || this.speechSynthesisPending > 0) {
        this.settleAfterSpeech = true;
        return;
      }
      this.responseVisibleUntil = 0;
      this.cancelSpeechDelivery();
      this.settleOverlay();
    }, Math.max(0, until - Date.now()));
  }

  private clearHideTimer(): void {
    if (this.hideTimer) clearTimeout(this.hideTimer);
    this.hideTimer = undefined;
  }

  private shouldKeepIdleVisible(): boolean {
    return Boolean(
      this.config.enabled &&
      this.projection?.allowed &&
      (this.config.showGameStatus || this.config.workspaceMode !== "assistant") &&
      this.activeGameWindow?.foregroundVerified,
    );
  }

  private shouldKeepCalibrationVisible(): boolean {
    return Boolean(
      this.config.enabled &&
      this.projection?.allowed &&
      this.config.calibrationMode &&
      this.activeGameWindow,
    );
  }

  private settleOverlay(): void {
    if (this.shouldKeepIdleVisible()) this.showIdle();
    else if (this.shouldKeepCalibrationVisible()) this.show();
    else this.hide();
  }

  private syncGameDetection(): void {
    this.stopGameDetection();
    this.hide();
    if (!this.config.enabled || !this.projection?.allowed) {
      return;
    }
    if (process.platform !== "win32") {
      this.options.onLog?.("Atlas overlay foreground guard is only available on Windows; overlay stays hidden");
      return;
    }
    this.startForegroundProbe();
  }

  private startForegroundProbe(): void {
    if (
      this.foregroundProbe ||
      !this.config.enabled ||
      !this.projection?.allowed ||
      process.platform !== "win32"
    ) return;
    try {
      const helper = spawn(
        "powershell.exe",
        [
          "-NoLogo",
          "-NoProfile",
          "-NonInteractive",
          "-ExecutionPolicy",
          "Bypass",
          "-EncodedCommand",
          WINDOWS_FOREGROUND_PROBE_COMMAND,
        ],
        { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
      );
      this.foregroundProbe = helper;
      let pending = "";
      let stderr = "";
      helper.stdout?.setEncoding("utf8");
      helper.stdout?.on("data", (chunk: string) => {
        if (this.foregroundProbe !== helper) return;
        pending += chunk;
        const lines = pending.split(/\r?\n/);
        pending = lines.pop() || "";
        for (const line of lines) this.handleForegroundProbeLine(helper, line);
      });
      helper.stderr?.setEncoding("utf8");
      helper.stderr?.on("data", (chunk: string) => {
        stderr = `${stderr}${chunk}`.slice(-1_000);
      });
      helper.once("error", (error) => this.failForegroundProbe(helper, error));
      helper.once("exit", (code, signal) => {
        if (this.foregroundProbe !== helper) return;
        const details = stderr.trim() || `exit=${code ?? "unknown"}, signal=${signal ?? "none"}`;
        this.failForegroundProbe(helper, new Error(`Windows foreground helper stopped (${details})`));
      });
      this.armForegroundProbeWatchdog(helper);
    } catch (error) {
      this.options.onLog?.("Atlas foreground helper could not start; enabling compatibility detection", error);
      this.setActiveGameWindow(undefined);
      this.hide();
      this.scheduleForegroundProbeRestart(error);
      this.startFallbackGameScan();
    }
  }

  private stopGameDetection(resetRestart = true): void {
    if (this.foregroundProbeWatchdog) clearTimeout(this.foregroundProbeWatchdog);
    this.foregroundProbeWatchdog = undefined;
    if (this.foregroundLossTimer) clearTimeout(this.foregroundLossTimer);
    this.foregroundLossTimer = undefined;
    if (this.foregroundProbeRestartTimer) clearTimeout(this.foregroundProbeRestartTimer);
    this.foregroundProbeRestartTimer = undefined;
    if (this.fallbackGameScanTimer) clearTimeout(this.fallbackGameScanTimer);
    this.fallbackGameScanTimer = undefined;
    this.fallbackGameScanInFlight = false;
    if (resetRestart) this.foregroundProbeRestartAttempts = 0;
    const helper = this.foregroundProbe;
    this.foregroundProbe = undefined;
    if (helper && !helper.killed) helper.kill();
    this.setActiveGameWindow(undefined);
  }

  private armForegroundProbeWatchdog(helper: ChildProcess): void {
    if (this.foregroundProbeWatchdog) clearTimeout(this.foregroundProbeWatchdog);
    this.foregroundProbeWatchdog = setTimeout(() => {
      if (this.foregroundProbe !== helper) return;
      this.failForegroundProbe(helper, new Error("Windows foreground helper timed out"));
    }, FOREGROUND_PROBE_WATCHDOG_MS);
  }

  private handleForegroundProbeLine(helper: ChildProcess, line: string): void {
    if (this.foregroundProbe !== helper) return;
    const probe = parseAtlasOverlayForegroundProbe(line);
    if (!probe) {
      this.failForegroundProbe(helper, new Error("Windows foreground helper returned malformed data"));
      return;
    }
    this.foregroundProbeRestartAttempts = 0;
    this.stopFallbackGameScan();
    this.armForegroundProbeWatchdog(helper);
    const detected = resolveAtlasOverlayForegroundGame(probe);
    const next = detected
      ? {
          ...detected,
          workArea: this.nativeWorkAreaToDip(detected.workArea),
        }
      : undefined;
    if (!next && this.isOwnManualInputForeground(probe)) {
      // The text field is allowed to own focus briefly after a confirmed GTA
      // foreground. Keep the last game monitor as the placement target; a
      // foreign app, a stale window or any other title still follows the
      // fail-closed branch below.
      return;
    }
    if (!next) {
      if (!this.foregroundLossTimer) {
        this.foregroundLossTimer = setTimeout(() => {
          this.foregroundLossTimer = undefined;
          this.setActiveGameWindow(undefined);
          this.cancel();
        }, this.foregroundLossGraceMs());
      }
      return;
    }
    if (this.foregroundLossTimer) clearTimeout(this.foregroundLossTimer);
    this.foregroundLossTimer = undefined;
    const changed = !this.sameGameWindow(this.activeGameWindow, next);
    this.setActiveGameWindow(next);
    if (!changed) {
      this.healOverlayVisibility();
      return;
    }
    this.positionWindow();
    const initialize = this.config.initializationAnimation &&
      !this.initializationPresented &&
      !this.initializationInFlight;
    if (initialize) this.initializationInFlight = true;
    if (!initialize && !this.shouldKeepIdleVisible() && !this.shouldKeepCalibrationVisible()) return;
    void this.ensureWindow().then(() => {
      if (!this.sameGameWindow(this.activeGameWindow, next)) {
        if (initialize) this.initializationInFlight = false;
        return;
      }
      if (initialize) {
        this.initializationInFlight = false;
        this.initializationPresented = true;
        this.showInitialization();
      }
      else if (this.shouldKeepIdleVisible()) this.showIdle();
      else if (this.shouldKeepCalibrationVisible()) this.show();
    }).catch((error) => {
      if (initialize) this.initializationInFlight = false;
      this.options.onLog?.("Atlas overlay window unavailable after foreground change", error);
      this.hide();
    });
  }

  private failForegroundProbe(helper: ChildProcess, error: unknown): void {
    if (this.foregroundProbe !== helper) return;
    this.foregroundProbe = undefined;
    if (this.foregroundProbeWatchdog) clearTimeout(this.foregroundProbeWatchdog);
    this.foregroundProbeWatchdog = undefined;
    if (!helper.killed) helper.kill();
    this.setActiveGameWindow(undefined);
    this.cancel();
    this.options.onLog?.("Atlas foreground probe unavailable; enabling compatibility detection", error);
    this.scheduleForegroundProbeRestart(error);
    this.startFallbackGameScan();
  }

  private foregroundLossGraceMs(): number {
    return (
      this.activeRequestId ||
      this.speechPlaybackActive ||
      this.speechSynthesisPending > 0 ||
      this.responseVisibleUntil > Date.now()
    ) ? FOREGROUND_BUSY_LOSS_GRACE_MS : FOREGROUND_LOSS_GRACE_MS;
  }

  private scheduleForegroundProbeRestart(error: unknown): void {
    if (
      this.foregroundProbeRestartTimer ||
      !this.config.enabled ||
      !this.projection?.allowed ||
      process.platform !== "win32"
    ) return;
    const delay = Math.min(
      FOREGROUND_PROBE_RESTART_MAX_MS,
      FOREGROUND_PROBE_RESTART_MIN_MS * 2 ** Math.min(this.foregroundProbeRestartAttempts, 4),
    );
    this.foregroundProbeRestartAttempts += 1;
    this.options.onLog?.(`Atlas foreground probe will restart in ${delay} ms`, error);
    this.foregroundProbeRestartTimer = setTimeout(() => {
      this.foregroundProbeRestartTimer = undefined;
      this.startForegroundProbe();
    }, delay);
  }

  private healOverlayVisibility(): void {
    const now = Date.now();
    if (now - this.lastOverlayVisibilityHealAt < OVERLAY_VISIBILITY_HEAL_MS) return;
    this.lastOverlayVisibilityHealAt = now;
    const shouldShow = Boolean(
      this.activeRequestId ||
      this.speechPlaybackActive ||
      this.speechSynthesisPending > 0 ||
      this.responseVisibleUntil > now ||
      this.shouldKeepIdleVisible() ||
      this.shouldKeepCalibrationVisible()
    );
    if (!shouldShow) return;
    void this.ensureWindow().then(() => {
      if (!this.activeGameWindow) return;
      if (!this.window?.isVisible()) {
        if (
          !this.activeRequestId &&
          !this.speechPlaybackActive &&
          this.speechSynthesisPending === 0 &&
          this.responseVisibleUntil <= Date.now() &&
          this.shouldKeepIdleVisible()
        ) this.showIdle();
        else this.show();
      } else {
        this.positionWindow();
        this.window.setAlwaysOnTop(true, "screen-saver", 1);
        this.raiseOverlayAboveGame();
        this.window.webContents.invalidate();
      }
    }).catch((error) => {
      this.options.onLog?.("Atlas overlay visibility self-heal failed", error);
    });
  }

  private scheduleOverlayWindowRecovery(overlayWindow: BrowserWindow, error: unknown): void {
    if (this.window !== overlayWindow || this.overlayWindowRecoveryTimer) return;
    this.options.onLog?.("Atlas overlay renderer will be recovered", error);
    this.windowReady = false;
    this.windowLoad = undefined;
    if (!overlayWindow.isDestroyed()) overlayWindow.destroy();
    this.overlayWindowRecoveryTimer = setTimeout(() => {
      this.overlayWindowRecoveryTimer = undefined;
      if (!this.activeGameWindow || !this.config.enabled || !this.projection?.allowed) return;
      void this.ensureWindow().then(() => {
        if (this.activeGameWindow) this.settleOverlay();
      }).catch((recoveryError) => {
        this.options.onLog?.("Atlas overlay renderer recovery failed", recoveryError);
      });
    }, 450);
  }

  private setActiveGameWindow(next: AtlasOverlayActiveGameWindow | undefined): void {
    this.activeGameWindow = next;
    this.updateOverlayInputMode();
  }

  private nativeWorkAreaToDip(area: AtlasOverlayForegroundProbe["workArea"]): NonNullable<AtlasOverlayForegroundProbe["workArea"]> {
    if (!area) return { ...screen.getPrimaryDisplay().workArea };
    try {
      const converted = process.platform === "win32"
        ? screen.screenToDipRect(null, area)
        : area;
      if (converted.width >= 200 && converted.height >= 120) return converted;
    } catch (error) {
      this.options.onLog?.("Atlas overlay DPI conversion failed; using display matcher", error);
    }
    return resolveAtlasOverlayDisplayArea(
      area,
      screen.getAllDisplays().map((display) => ({ ...display.workArea })),
    );
  }

  private raiseOverlayAboveGame(): void {
    const overlayWindow = this.window;
    if (!overlayWindow || overlayWindow.isDestroyed()) return;
    const mediaSourceId = this.activeGameWindow?.mediaSourceId;
    if (mediaSourceId) {
      try {
        overlayWindow.moveAbove(mediaSourceId);
        return;
      } catch (error) {
        const now = Date.now();
        if (now - this.lastNativeZOrderErrorAt > 30_000) {
          this.lastNativeZOrderErrorAt = now;
          this.options.onLog?.("Atlas could not attach above the GTA window; using global topmost", error);
        }
      }
    }
    overlayWindow.moveTop();
  }

  /**
   * Corporate PowerShell policies can block Add-Type on otherwise supported
   * Windows PCs. In that case DesktopCapturer still exposes GTA's exact HWND
   * and display id. This fallback is intentionally used only while the native
   * foreground helper is unavailable.
   */
  private startFallbackGameScan(): void {
    if (
      this.fallbackGameScanTimer ||
      this.fallbackGameScanInFlight ||
      this.foregroundProbe ||
      !this.config.enabled ||
      !this.projection?.allowed ||
      process.platform !== "win32"
    ) return;
    const scan = async () => {
      this.fallbackGameScanTimer = undefined;
      if (this.foregroundProbe || !this.config.enabled || !this.projection?.allowed) return;
      this.fallbackGameScanInFlight = true;
      try {
        const sources = await desktopCapturer.getSources({
          types: ["window"],
          thumbnailSize: { width: 0, height: 0 },
          fetchWindowIcons: false,
        });
        const source = sources.find((candidate) => {
          const name = candidate.name.trim();
          return GAME_WINDOW_SOURCE_PATTERN.test(name) && !NON_GAME_WINDOW_SOURCE_PATTERN.test(name);
        });
        if (!source) {
          if (this.activeGameWindow && !this.activeGameWindow.foregroundVerified) {
            this.setActiveGameWindow(undefined);
            this.cancel();
          }
        } else {
          const display = screen.getAllDisplays().find((candidate) => String(candidate.id) === source.display_id)
            || screen.getPrimaryDisplay();
          const next: AtlasOverlayActiveGameWindow = {
            title: source.name,
            processName: "desktop-capturer-fallback",
            processId: 0,
            mediaSourceId: source.id,
            foregroundVerified: false,
            workArea: { ...display.workArea },
          };
          this.lastFallbackGameSeenAt = Date.now();
          const changed = !this.sameGameWindow(this.activeGameWindow, next);
          this.setActiveGameWindow(next);
          if (changed) this.positionWindow();
          this.healOverlayVisibility();
        }
      } catch (error) {
        this.options.onLog?.("Atlas fallback game scan failed", error);
      } finally {
        this.fallbackGameScanInFlight = false;
        if (!this.foregroundProbe && this.config.enabled && this.projection?.allowed) {
          this.fallbackGameScanTimer = setTimeout(scan, FALLBACK_GAME_SCAN_MS);
        }
      }
    };
    void scan();
  }

  private stopFallbackGameScan(): void {
    if (this.fallbackGameScanTimer) clearTimeout(this.fallbackGameScanTimer);
    this.fallbackGameScanTimer = undefined;
  }

  private showInitialization(): void {
    if (this.initializationTimer) clearTimeout(this.initializationTimer);
    this.show();
    this.emit({
      type: "initialized",
      name: this.config.characterName || "Atlas",
    });
    this.initializationTimer = setTimeout(() => {
      this.initializationTimer = undefined;
      if (this.activeRequestId || this.speechPlaybackActive) return;
      this.settleOverlay();
    }, INITIALIZATION_VISIBLE_MS);
  }

  private sameGameWindow(
    first: AtlasOverlayActiveGameWindow | undefined,
    second: AtlasOverlayActiveGameWindow | undefined,
  ): boolean {
    if (!first || !second) return first === second;
    return (
      first.processName === second.processName &&
      first.processId === second.processId &&
      first.mediaSourceId === second.mediaSourceId &&
      first.foregroundVerified === second.foregroundVerified &&
      first.title === second.title &&
      first.workArea.x === second.workArea.x &&
      first.workArea.y === second.workArea.y &&
      first.workArea.width === second.workArea.width &&
      first.workArea.height === second.workArea.height
    );
  }

  private isOwnManualInputForeground(probe: AtlasOverlayForegroundProbe): boolean {
    const overlayWindow = this.window;
    if (
      !this.isManualInputActive() ||
      !this.overlayFocusable ||
      !overlayWindow ||
      overlayWindow.isDestroyed() ||
      !overlayWindow.isFocused() ||
      !this.isActiveGameProcessAlive()
    ) return false;
    const title = String(probe.title || "").trim();
    const nativeTitle = String(overlayWindow.getTitle() || "").trim();
    return Boolean(
      title &&
      (title === nativeTitle || title === "Atlas Overlay" || title === "T-Mod Atlas Overlay"),
    );
  }

  private isActiveGameProcessAlive(): boolean {
    const processId = this.activeGameWindow?.processId;
    if (
      this.activeGameWindow &&
      !this.activeGameWindow.foregroundVerified &&
      this.activeGameWindow.mediaSourceId &&
      Date.now() - this.lastFallbackGameSeenAt < FALLBACK_GAME_SCAN_MS * 3
    ) return true;
    if (!Number.isSafeInteger(processId) || !processId || processId < 1) return false;
    try {
      process.kill(processId, 0);
      return true;
    } catch {
      return false;
    }
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
      this.registerCraftHotkey();
      return;
    }
    this.registerToggleFallback();
    this.registerCraftHotkey();
  }

  private handlePttLine(line: string): void {
    if (line === "down") {
      if (!this.activeGameWindow) {
        this.hide();
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
      this.beginPttWhenReady();
    } else if (line === "text") {
      if (!this.activeGameWindow) {
        this.hide();
        return;
      }
      const ready = this.guardReady();
      if (ready) {
        this.show();
        this.emit({ type: "error", message: safeError(new Error(ready), ready) });
        return;
      }
      this.cancel();
      const manualToken = ++this.pttStartToken;
      void this.ensureWindow().then(() => {
        if (manualToken !== this.pttStartToken || !this.activeGameWindow) {
          this.hide();
          return;
        }
        this.show();
        this.emit({ type: "manual-query" });
        this.beginManualInput();
      }).catch((error) => {
        this.options.onLog?.("Atlas overlay manual query unavailable", error);
        this.hide();
      });
    } else if (line === "up") {
      this.releasePttWhenReady();
    } else if (line === "edit") {
      if (!this.activeGameWindow) return;
      this.cancel();
      void this.saveConfig({ calibrationMode: true }).catch((error) => {
        this.options.onLog?.("Atlas overlay keyboard calibration unavailable", error);
      });
    } else if (line === "edit-done") {
      void this.saveConfig({ calibrationMode: false }).catch((error) => {
        this.options.onLog?.("Atlas overlay keyboard calibration could not close", error);
      });
    } else if (line.startsWith("move:")) {
      const movement: Record<string, [number, number]> = {
        "move:left": [-28, 0],
        "move:right": [28, 0],
        "move:up": [0, -24],
        "move:down": [0, 24],
      };
      const delta = movement[line];
      if (delta) void this.moveBy(...delta).catch((error) => this.options.onLog?.("Atlas overlay keyboard move failed", error));
    } else if (line === "scale:up" || line === "scale:down") {
      const delta = line === "scale:up" ? .05 : -.05;
      void this.saveConfig({ scale: this.config.scale + delta }).catch((error) => this.options.onLog?.("Atlas overlay keyboard resize failed", error));
    } else if (line === "width:up" || line === "width:down") {
      const delta = line === "width:up" ? 24 : -24;
      void this.saveConfig({ panelWidth: this.config.panelWidth + delta }).catch((error) => this.options.onLog?.("Atlas overlay keyboard width failed", error));
    } else if (line === "cancel") {
      this.cancel();
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
      if (!this.activeGameWindow) {
        this.hide();
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

  private registerCraftHotkey(): void {
    if (!this.config.enabled || this.config.craftHotkey === this.config.hotkey) return;
    if (!globalShortcut.register(this.config.craftHotkey, () => {
      const next = this.config.workspaceMode === "crafts" ? "assistant" : "crafts";
      void this.saveConfig({ workspaceMode: next }).then(() => {
        if (!this.activeGameWindow) return;
        this.show();
        if (this.craftSnapshot) this.emit({ type: "crafts", snapshot: this.craftSnapshot });
      }).catch((error) => this.options.onLog?.("Atlas craft mode toggle failed", error));
    })) {
      this.options.onLog?.("Atlas craft hotkey registration failed", this.config.craftHotkey);
      return;
    }
    this.craftHotkey = this.config.craftHotkey;
  }

  private unbindPtt(): void {
    if (this.fallbackHotkey) globalShortcut.unregister(this.fallbackHotkey);
    if (this.craftHotkey) globalShortcut.unregister(this.craftHotkey);
    this.fallbackHotkey = "";
    this.craftHotkey = "";
    this.fallbackListening = false;
    if (this.hotkeyHelper && !this.hotkeyHelper.killed) this.hotkeyHelper.kill();
    this.hotkeyHelper = undefined;
  }

  private stopCraftPolling(): void {
    if (this.craftPollTimer) clearTimeout(this.craftPollTimer);
    this.craftPollTimer = undefined;
  }

  private scheduleCraftPoll(delay = CRAFT_POLL_MS): void {
    if (this.craftPollTimer) clearTimeout(this.craftPollTimer);
    this.craftPollTimer = undefined;
    if (this.disposed || !this.config.enabled || !this.projection?.allowed) return;
    this.craftPollTimer = setTimeout(() => {
      this.craftPollTimer = undefined;
      void this.refreshCraftSnapshot();
    }, Math.max(0, delay));
  }

  private async refreshCraftSnapshot(): Promise<void> {
    if (this.craftPollInFlight || !this.config.enabled || !this.projection?.allowed) {
      this.scheduleCraftPoll();
      return;
    }
    this.craftPollInFlight = true;
    let nextPollDelay = CRAFT_POLL_MS;
    try {
      const response = await this.options.networkSession().fetch(ATLAS_CRAFTS_URL, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: {
          Accept: "application/json",
          ...(this.craftEtag ? { "If-None-Match": this.craftEtag } : {}),
        },
      });
      if (response.status === 304) return;
      if (response.status === 401) {
        this.csrfToken = "";
        nextPollDelay = CRAFT_AUTH_RETRY_MS;
        return;
      }
      if (response.status === 403) {
        // Atlas can also be granted to non-members. They do not have the craft
        // contour, so avoid hammering a forbidden endpoint every five seconds.
        nextPollDelay = CRAFT_ACCESS_RETRY_MS;
        return;
      }
      if (!response.ok) throw new Error(`Atlas crafts ${response.status}`);
      const payload = await response.json() as AtlasOverlayCraftSnapshot;
      if (!payload || !Array.isArray(payload.plans) || typeof payload.revision !== "string") {
        throw new Error("atlas_crafts_payload_invalid");
      }
      this.craftEtag = String(response.headers.get("etag") || "");
      this.craftSnapshot = payload;
      this.emit({ type: "crafts", snapshot: payload });
      const alarm = payload.plans.find((plan) =>
        plan.needs_next_batch &&
        Boolean(plan.alarm_key) &&
        !this.acknowledgedCraftAlarms.has(String(plan.alarm_key)),
      );
      if (alarm?.alarm_key) {
        const key = String(alarm.alarm_key);
        this.acknowledgedCraftAlarms.add(key);
        while (this.acknowledgedCraftAlarms.size > 80) {
          const oldest = this.acknowledgedCraftAlarms.values().next().value;
          if (typeof oldest !== "string") break;
          this.acknowledgedCraftAlarms.delete(oldest);
        }
        if (this.config.craftAlerts) {
          if (this.config.craftAutoExpand && this.activeGameWindow) this.show();
          this.emit({ type: "craft-alert", snapshot: payload, alarmKey: key });
        }
      }
    } catch (error) {
      this.options.onLog?.("Atlas craft synchronization failed", error);
    } finally {
      this.craftPollInFlight = false;
      this.scheduleCraftPoll(nextPollDelay);
    }
  }
}
