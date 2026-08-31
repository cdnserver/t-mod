import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import type {
  BootstrapResult,
  DesktopLoginCredentials,
  DesktopLoginResult,
  DesktopLockReason,
  DesktopNotification,
  DesktopShellPreferences,
  DesktopState,
  DesktopUpdateState,
  ServiceId,
} from "../shared/contracts";
import type {
  AtlasOverlayCatalog,
  AtlasOverlayConfig,
  AtlasOverlayVoiceCatalog,
} from "../shared/atlas-overlay";
import {
  DEFAULT_ATLAS_OVERLAY_CONFIG,
  normalizeAtlasOverlayConfig,
} from "../shared/atlas-overlay";
import {
  resolveNotificationServiceId,
  serviceById,
  services,
} from "../shared/services";
import {
  CinematicLaunch,
  VaultScreen,
  playIgnitionSound,
  playVaultSound,
} from "./cinematics";

type IconName = ServiceId | "search" | "bell" | "refresh" | "back" | "forward" |
  "command" | "lock" | "download" | "logout" | "shield" | "minimize" |
  "maximize" | "close" | "settings" | "menu" | "link" | "external";

const PREFERENCES_KEY = "tmod-desktop-preferences-v1";
const DEFAULT_PREFERENCES: DesktopShellPreferences = {
  preferredName: "",
  sidebarCollapsed: false,
  compactMode: false,
  reduceMotion: false,
  solidSurfaces: false,
  serviceZoom: 1,
  idleLockMinutes: 10,
  lockSound: true,
  updateChannel: "beta",
};

function loadPreferences(): DesktopShellPreferences {
  try {
    const stored = JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}") as Partial<DesktopShellPreferences>;
    const zoom = Number(stored.serviceZoom);
    return {
      preferredName: typeof stored.preferredName === "string" ? stored.preferredName.slice(0, 24) : "",
      sidebarCollapsed: stored.sidebarCollapsed === true,
      compactMode: stored.compactMode === true,
      reduceMotion: stored.reduceMotion === true,
      solidSurfaces: stored.solidSurfaces === true,
      serviceZoom: [0.9, 1, 1.1].includes(zoom) ? zoom : 1,
      idleLockMinutes: [0, 5, 10, 15, 30].includes(Number(stored.idleLockMinutes))
        ? Number(stored.idleLockMinutes)
        : 10,
      lockSound: stored.lockSound !== false,
      updateChannel: stored.updateChannel === "dev" ? "dev" : "beta",
    };
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
}

function Icon({ name }: { name: IconName }) {
  const paths: Record<string, ReactNode> = {
    home: <><rect x="8" y="3.5" width="8" height="17" rx="2.5"/><path d="M9.5 8h5M12 8v8.5"/></>,
    reactor: <><path d="M7 6v12M12 3.5v17M17 6v12M4.5 8.5h15M4.5 15.5h15"/><rect x="9.5" y="8" width="5" height="8" rx="1.5"/></>,
    consensus: <><path d="M4 20h16M6 17V10M10 17V8M14 17V8M18 17v-7M4.5 7.5 12 3.5l7.5 4"/></>,
    atlas: <><circle cx="10.5" cy="13" r="7.5"/><path d="M3 13h15M5 9c3 2 8 2 11 0M5 17c3-2 8-2 11 0M10.5 5.5c-2.5 2.5-3.5 5-3.5 7.5s1 5 3.5 7.5M10.5 5.5c2.5 2.5 3.5 5 3.5 7.5s-1 5-3.5 7.5M20 2v5M17.5 4.5h5"/></>,
    sgl: <><path d="m3 9 9-5 9 5H3ZM5 11h14M4 20h16M6.5 11v9M10 11v9M14 11v9M17.5 11v9"/></>,
    ovr: <><path d="M8 3H4v4M16 3h4v4M8 21H4v-4M16 21h4v-4M3.5 12S6.5 7.5 12 7.5 20.5 12 20.5 12 17.5 16.5 12 16.5 3.5 12 3.5 12Z"/><path d="M12 9v6"/></>,
    games: <><rect x="4" y="4" width="7" height="7"/><rect x="13" y="13" width="7" height="7"/><rect x="13" y="4" width="7" height="7"/><rect x="4" y="13" width="7" height="7"/></>,
    tasks: <><path d="M4 6h16M4 12h16M4 18h16"/><path d="m5 12 3 3 10-10"/></>,
    admin: <><path d="m12 2 10 10-10 10L2 12 12 2Zm0 4.5 5.5 5.5-5.5 5.5L6.5 12 12 6.5Z"/><rect x="10" y="10" width="4" height="4" rx="1"/></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/></>,
    bell: <><path d="M18 9a6 6 0 00-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8A7 7 0 0118 6l2 6M17.9 16A7 7 0 016 18l-2-6"/></>,
    back: <path d="M15 18l-6-6 6-6"/>,
    forward: <path d="M9 18l6-6-6-6"/>,
    command: <><path d="M9 7V5.5A2.5 2.5 0 106.5 8H18M15 17v1.5a2.5 2.5 0 102.5-2.5H6"/></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 018 0v3"/></>,
    download: <><path d="M12 3v12M7.5 10.5L12 15l4.5-4.5"/><path d="M5 20h14"/></>,
    logout: <><path d="M10 4H5v16h5M14 8l4 4-4 4M8 12h10"/></>,
    shield: <><path d="M12 2.5l8 3.2v5.6c0 5.1-3.3 8.4-8 10.2-4.7-1.8-8-5.1-8-10.2V5.7z"/><path d="M9 12l2 2 4-4"/></>,
    minimize: <path d="M6 12h12"/>,
    maximize: <rect x="6" y="6" width="12" height="12" rx="1.5"/>,
    close: <path d="M7 7l10 10M17 7L7 17"/>,
    settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21h-4v-.1a1.7 1.7 0 00-1-1.5 1.7 1.7 0 00-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3v-4h.1a1.7 1.7 0 001.5-1 1.7 1.7 0 00-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 001.9.3 1.7 1.7 0 001-1.5V3h4v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 00-.3 1.9 1.7 1.7 0 001.5 1h.1v4h-.1a1.7 1.7 0 00-1.5 1z"/></>,
    menu: <><path d="M4 7h16M4 12h16M4 17h16"/></>,
    link: <><path d="M10 13a4.5 4.5 0 006.4.1l2-2a4.5 4.5 0 00-6.4-6.4l-1.1 1.1"/><path d="M14 11a4.5 4.5 0 00-6.4-.1l-2 2A4.5 4.5 0 0012 19.3l1.1-1.1"/></>,
    external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v6a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h6"/></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}

function greeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Доброй ночи, ${name}`;
  if (hour < 12) return `Доброе утро, ${name}`;
  if (hour < 18) return `Добрый день, ${name}`;
  return `Добрый вечер, ${name}`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "сейчас"
    : new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function browserApi() {
  return window.tmodDesktop;
}

function overlayApi() {
  return window.tmodAtlasOverlay;
}

function playLaunchSound(): () => void {
  if (typeof AudioContext === "undefined") return () => undefined;
  const context = new AudioContext();
  const now = context.currentTime;
  const master = context.createGain();
  master.gain.setValueAtTime(0.0001, now);
  master.gain.exponentialRampToValueAtTime(0.105, now + 0.48);
  master.gain.setValueAtTime(0.105, now + 4.72);
  master.gain.exponentialRampToValueAtTime(0.0001, now + 6.28);
  const compressor = context.createDynamicsCompressor();
  compressor.threshold.setValueAtTime(-22, now);
  compressor.knee.setValueAtTime(18, now);
  compressor.ratio.setValueAtTime(5, now);
  compressor.attack.setValueAtTime(0.018, now);
  compressor.release.setValueAtTime(0.32, now);
  master.connect(compressor).connect(context.destination);

  const tone = (
    frequency: number,
    offset: number,
    duration: number,
    volume: number,
    type: OscillatorType = "sine",
    pan = 0,
  ) => {
    const oscillator = context.createOscillator();
    const envelope = context.createGain();
    oscillator.type = type;
    oscillator.frequency.setValueAtTime(frequency, now + offset);
    envelope.gain.setValueAtTime(0.0001, now + offset);
    envelope.gain.exponentialRampToValueAtTime(volume, now + offset + Math.min(0.48, duration * 0.3));
    envelope.gain.exponentialRampToValueAtTime(0.0001, now + offset + duration);
    oscillator.connect(envelope);
    const panner = context.createStereoPanner();
    panner.pan.setValueAtTime(pan, now + offset);
    envelope.connect(panner).connect(master);
    oscillator.start(now + offset);
    oscillator.stop(now + offset + duration + 0.04);
  };

  // Spatial notes follow the glass mark as it comes forward from depth.
  // A low foundation and restrained shimmer keep the signature cinematic.
  tone(41.2, 0, 5.78, 0.34, "sine");
  tone(82.41, 0.08, 5.26, 0.18, "sine");
  tone(123.47, 0.54, 4.38, 0.09, "triangle", -0.5);
  tone(164.81, 0.82, 4.04, 0.085, "triangle", 0.5);
  tone(246.94, 1.28, 3.5, 0.068, "sine");
  tone(329.63, 2.18, 2.78, 0.052, "sine", -0.24);
  tone(493.88, 3.22, 1.88, 0.039, "sine", 0.26);
  tone(987.77, 4.58, 1.12, 0.024, "sine");

  const sub = context.createOscillator();
  const subGain = context.createGain();
  sub.type = "sine";
  sub.frequency.setValueAtTime(54, now);
  sub.frequency.exponentialRampToValueAtTime(36, now + 5.45);
  subGain.gain.setValueAtTime(0.0001, now);
  subGain.gain.exponentialRampToValueAtTime(0.2, now + 0.38);
  subGain.gain.exponentialRampToValueAtTime(0.0001, now + 5.62);
  sub.connect(subGain).connect(master);
  sub.start(now);
  sub.stop(now + 5.7);

  const noiseLength = Math.floor(context.sampleRate * 3.4);
  const noiseBuffer = context.createBuffer(1, noiseLength, context.sampleRate);
  const noise = noiseBuffer.getChannelData(0);
  for (let index = 0; index < noise.length; index += 1) {
    noise[index] = (Math.random() * 2 - 1) * (1 - index / noise.length);
  }
  const noiseSource = context.createBufferSource();
  const noiseFilter = context.createBiquadFilter();
  const noiseGain = context.createGain();
  noiseSource.buffer = noiseBuffer;
  noiseFilter.type = "lowpass";
  noiseFilter.frequency.setValueAtTime(180, now);
  noiseFilter.frequency.exponentialRampToValueAtTime(2_100, now + 3.25);
  noiseGain.gain.setValueAtTime(0.0001, now);
  noiseGain.gain.exponentialRampToValueAtTime(0.024, now + 0.92);
  noiseGain.gain.exponentialRampToValueAtTime(0.0001, now + 3.4);
  noiseSource.connect(noiseFilter).connect(noiseGain).connect(master);
  noiseSource.start(now);
  noiseSource.stop(now + 3.4);

  void context.resume().catch(() => undefined);
  const closeTimer = window.setTimeout(() => void context.close(), 6_650);
  return () => {
    window.clearTimeout(closeTimer);
    if (context.state !== "closed") void context.close();
  };
}

function LaunchSequence({ reduced }: { reduced: boolean }) {
  return (
    <section className={`launch-sequence ${reduced ? "reduced" : ""}`} aria-label="T-Mod запускается" aria-live="polite">
      <div className="launch-noise" aria-hidden="true"/>
      <div className="launch-letterbox" aria-hidden="true"><i/><b/></div>
      <div className="launch-cinema-depth" aria-hidden="true"><i/><i/><i/><i/></div>
      <div className="launch-volumetric" aria-hidden="true"><i/><i/><i/></div>
      <div className="launch-motes" aria-hidden="true"><i/><i/><i/><i/><i/><i/></div>
      <div className="launch-aperture" aria-hidden="true"><i/><b/></div>
      <div className="launch-cube-scene" aria-hidden="true">
        <i className="launch-cube-shadow"/>
        <div className="launch-cube-rig">
          <div className="launch-glass-cube">
            <i className="glass-back"/>
            <i className="glass-top"/>
            <i className="glass-side"/>
            <i className="glass-core"/>
            <i className="glass-front"/>
            <i className="glass-scan"/>
            <span>T</span>
            <b/>
          </div>
        </div>
        <i className="launch-cube-reflection"/>
      </div>
      <div className="launch-copy">
        <h1 aria-label="T-Mod"><span>T‑MOD</span></h1>
        <p>ЕДИНАЯ ЭКОСИСТЕМА</p>
        <small><i/> SYSTEM CORE READY</small>
      </div>
      <div className="launch-cinematic-release" aria-hidden="true"><i/><b/><em/></div>
    </section>
  );
}

function playLockSound(kind: "lock" | "unlock", enabled: boolean): () => void {
  if (!enabled || typeof AudioContext === "undefined") return () => undefined;
  const context = new AudioContext();
  const now = context.currentTime;
  const master = context.createGain();
  master.gain.setValueAtTime(0.0001, now);
  master.gain.exponentialRampToValueAtTime(kind === "lock" ? 0.085 : 0.095, now + 0.08);
  master.gain.exponentialRampToValueAtTime(0.0001, now + (kind === "lock" ? 2.08 : 1.72));
  const compressor = context.createDynamicsCompressor();
  compressor.threshold.setValueAtTime(-20, now);
  compressor.ratio.setValueAtTime(4, now);
  master.connect(compressor).connect(context.destination);
  const notes = kind === "lock" ? [246.94, 164.81, 82.41] : [164.81, 246.94, 329.63, 659.25];
  notes.forEach((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const offset = index * (kind === "lock" ? 0.16 : 0.12);
    oscillator.type = index === 0 ? "sine" : "triangle";
    oscillator.frequency.setValueAtTime(frequency, now + offset);
    gain.gain.setValueAtTime(0.0001, now + offset);
    gain.gain.exponentialRampToValueAtTime(0.2 / Math.sqrt(index + 1), now + 0.12 + offset);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + (kind === "lock" ? 1.48 : 1.05) + offset);
    oscillator.connect(gain).connect(master);
    oscillator.start(now + offset);
    oscillator.stop(now + (kind === "lock" ? 1.62 : 1.18) + offset);
  });
  const impact = context.createOscillator();
  const impactGain = context.createGain();
  impact.type = "sine";
  impact.frequency.setValueAtTime(kind === "lock" ? 72 : 96, now);
  impact.frequency.exponentialRampToValueAtTime(kind === "lock" ? 38 : 148, now + 0.7);
  impactGain.gain.setValueAtTime(kind === "lock" ? 0.25 : 0.16, now);
  impactGain.gain.exponentialRampToValueAtTime(0.0001, now + 1.1);
  impact.connect(impactGain).connect(master);
  impact.start(now);
  impact.stop(now + 1.15);
  void context.resume().catch(() => undefined);
  const timer = window.setTimeout(() => void context.close(), 2_350);
  return () => {
    window.clearTimeout(timer);
    if (context.state !== "closed") void context.close();
  };
}

function LockScreen({
  name,
  reason,
  reduced,
  unlocking,
}: {
  name: string;
  reason: DesktopLockReason;
  reduced: boolean;
  unlocking: boolean;
}) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const time = new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(now);
  const date = new Intl.DateTimeFormat("ru", { weekday: "long", day: "numeric", month: "long" }).format(now);
  return (
    <section
      className={`lock-screen ${reduced ? "reduced" : ""} ${unlocking ? "unlocking" : ""}`}
      role="dialog"
      aria-modal="true"
      aria-label="T-Mod заблокирован"
    >
      <div className="lock-aurora" aria-hidden="true"><i/><i/><i/></div>
      <div className="lock-stars" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/><i/></div>
      <div className="lock-horizon" aria-hidden="true"><i/><b/><em/></div>
      <header className="lock-header"><span className="lock-mini-mark"><Icon name="shield"/></span><strong>T‑MOD</strong><small>{reason === "idle" ? "СЕАНС ПРИОСТАНОВЛЕН" : "КОНТУР ЗАБЛОКИРОВАН"}</small></header>
      <div className="lock-time"><strong>{time}</strong><span>{date}</span></div>
      <div className="lock-focus">
        <small>{unlocking ? "ВОССТАНАВЛИВАЕМ СЕАНС" : `С ВОЗВРАЩЕНИЕМ, ${name.toUpperCase()}`}</small>
        <h1>T‑MOD</h1>
        <p>{unlocking ? "Контур открыт" : "Нажмите любую клавишу, чтобы продолжить"}</p>
        <div className="lock-keyboard-prompt" aria-hidden="true"><kbd>{unlocking ? "✓" : "ANY KEY"}</kbd><i/><span>{unlocking ? "ДОСТУП ПОДТВЕРЖДЁН" : "ТОЛЬКО КЛАВИАТУРА"}</span></div>
      </div>
      <footer><span><i/> Локальная защита активна</span><small>{time} · T‑Mod Desktop</small></footer>
    </section>
  );
}

export function App() {
  const cinematicQaEnabled = import.meta.env.DEV
    || globalThis.location.hostname === "127.0.0.1"
    || globalThis.location.hostname === "localhost";
  const cinematicParams = new URLSearchParams(globalThis.location.search);
  const cinematicQa = cinematicQaEnabled ? cinematicParams.get("cinematic") : null;
  const atlasSettingsQa = cinematicQaEnabled && cinematicParams.get("settings-preview") === "atlas";
  const cinematicHold = cinematicQaEnabled && cinematicParams.get("hold") === "1";
  const cinematicPreviewName = cinematicQaEnabled
    ? String(cinematicParams.get("name") || "").trim()
    : "";
  const bridgeAvailable = Boolean(browserApi());
  const [bootstrap, setBootstrap] = useState<BootstrapResult>({
    authenticated: false,
    online: bridgeAvailable,
    error: bridgeAvailable ? "loading" : "desktop_bridge_unavailable",
  });
  const [bootstrapLoading, setBootstrapLoading] = useState(bridgeAvailable);
  const [desktopState, setDesktopState] = useState<DesktopState>({
    activeService: atlasSettingsQa ? "atlas" : "home",
    loading: false,
    canGoBack: false,
    canGoForward: false,
  });
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [atlasSettingsOpen, setAtlasSettingsOpen] = useState(atlasSettingsQa);
  const [atlasSettingsFocusRequest, setAtlasSettingsFocusRequest] = useState(0);
  const [preferences, setPreferences] = useState<DesktopShellPreferences>(loadPreferences);
  const [overlayConfig, setOverlayConfig] = useState<AtlasOverlayConfig>({
    ...DEFAULT_ATLAS_OVERLAY_CONFIG,
    ...(atlasSettingsQa ? {
      enabled: true,
      characterId: "qa-1",
      characterName: "S. Goodman",
      factionCode: "lspd",
    } : {}),
  });
  const [overlayCatalog, setOverlayCatalog] = useState<AtlasOverlayCatalog>(atlasSettingsQa ? {
    characters: [{ id: "qa-1", name: "S. Goodman", staticId: "263345", serverCode: "phoenix-15", factionCode: "lspd" }],
    servers: [{ code: "phoenix-15", name: "Phoenix", number: 15 }],
    factions: [{ code: "lspd", name: "LSPD", serverCode: "phoenix-15", kind: "government" }, { code: "gov", name: "GOV", serverCode: "phoenix-15", kind: "government" }],
  } : { characters: [], servers: [], factions: [] });
  const [overlayBusy, setOverlayBusy] = useState(false);
  const [overlayError, setOverlayError] = useState<string>();
  const [toast, setToast] = useState<string>();
  const [updateState, setUpdateState] = useState<DesktopUpdateState>({
    phase: "development",
    currentVersion: "—",
    channel: "beta",
  });
  const [dismissedUpdate, setDismissedUpdate] = useState<string>();
  const [launchVisible, setLaunchVisible] = useState(!atlasSettingsQa && cinematicQa !== "lock");
  const [locked, setLocked] = useState(cinematicQa === "lock");
  const [unlocking, setUnlocking] = useState(false);
  const [lockReason, setLockReason] = useState<DesktopLockReason>("idle");
  const searchRef = useRef<HTMLInputElement>(null);
  const bootstrapInFlight = useRef(false);
  const bootstrapRefreshPending = useRef(false);
  const bootstrapRevision = useRef(0);
  const hasLoadedBootstrap = useRef(false);
  const unlockInFlight = useRef(false);
  const unlockTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const stopSound = playIgnitionSound();
    if (cinematicHold) return stopSound;
    const timer = window.setTimeout(
      () => setLaunchVisible(false),
      preferences.reduceMotion ? 1_420 : 8_200,
    );
    return () => {
      window.clearTimeout(timer);
      stopSound();
    };
    // Launch preferences are intentionally sampled once. A settings change
    // must not replay the startup sequence in an already opened application.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadBootstrap = useCallback(async () => {
    if (bootstrapInFlight.current) {
      // Cookie, resume and service-navigation events can arrive while an older
      // projection is in flight. Never lose the newest refresh request.
      bootstrapRefreshPending.current = true;
      return;
    }
    const api = browserApi();
    if (!api) {
      setBootstrapLoading(false);
      setBootstrap({
        authenticated: false,
        online: false,
        error: "desktop_bridge_unavailable",
      });
      return;
    }
    bootstrapInFlight.current = true;
    if (!hasLoadedBootstrap.current) setBootstrapLoading(true);
    try {
      do {
        bootstrapRefreshPending.current = false;
        const revision = ++bootstrapRevision.current;
        const result = await api.bootstrap();
        if (bootstrapRevision.current === revision) setBootstrap(result);
      } while (bootstrapRefreshPending.current);
    } finally {
      hasLoadedBootstrap.current = true;
      bootstrapInFlight.current = false;
      setBootstrapLoading(false);
    }
  }, []);

  const loadOverlay = useCallback(async () => {
    const api = overlayApi();
    if (!api) return;
    try {
      const [config, catalog] = await Promise.all([api.getConfig(), api.getCatalog()]);
      if (config) setOverlayConfig(normalizeAtlasOverlayConfig(config));
      if (catalog) setOverlayCatalog(catalog);
      setOverlayError(undefined);
    } catch (error) {
      setOverlayError(error instanceof Error ? error.message : "Не удалось открыть настройки Overlay.");
    }
  }, []);

  useEffect(() => {
    void loadBootstrap();
    const api = browserApi();
    if (!api) return;
    const unsubscribeState = api.onState(setDesktopState);
    const unsubscribeAuth = api.onAuthChanged(loadBootstrap);
    const unsubscribePalette = api.onCommandPalette(() => setPaletteOpen(true));
    const unsubscribeOverlaySettings = api.onAtlasOverlaySettings(() => {
      setPaletteOpen(false);
      setNotificationsOpen(false);
      setSettingsOpen(false);
      setAtlasSettingsFocusRequest((value) => value + 1);
      setAtlasSettingsOpen(true);
    });
    const refresh = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadBootstrap();
    }, 45_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") void loadBootstrap();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      unsubscribeState();
      unsubscribeAuth();
      unsubscribePalette();
      unsubscribeOverlaySettings();
      window.clearInterval(refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [loadBootstrap]);

  useEffect(() => {
    if (bootstrap.authenticated) void loadOverlay();
  }, [bootstrap.authenticated, bootstrap.data?.generated_at, loadOverlay]);

  useEffect(() => {
    if (bootstrap.online) return;
    const refresh = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadBootstrap();
    }, 7_500);
    return () => window.clearInterval(refresh);
  }, [bootstrap.online, loadBootstrap]);

  useEffect(() => {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences));
    void browserApi()?.applyPreferences(preferences);
  }, [preferences]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(undefined), 2_400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    const api = browserApi();
    if (!api) return;
    const unsubscribeUpdate = api.onUpdate(setUpdateState);
    const unsubscribeLock = api.onLockRequested((reason) => {
      if (unlockTimer.current) window.clearTimeout(unlockTimer.current);
      setPaletteOpen(false);
      setNotificationsOpen(false);
      setSettingsOpen(false);
      setAtlasSettingsOpen(false);
      setLockReason(reason);
      setUnlocking(false);
      setLocked(true);
    });
    return () => {
      if (unlockTimer.current) window.clearTimeout(unlockTimer.current);
      unsubscribeUpdate();
      unsubscribeLock();
    };
  }, []);

  const unlock = useCallback(() => {
    const api = browserApi();
    if (!locked || unlockInFlight.current || !api) return;
    unlockInFlight.current = true;
    void api.unlock().then((ok) => {
      if (ok !== false) {
        setUnlocking(true);
        playVaultSound("unlock", preferences.lockSound);
        unlockTimer.current = window.setTimeout(() => {
          setLocked(false);
          setUnlocking(false);
          unlockTimer.current = undefined;
        }, preferences.reduceMotion ? 80 : 920);
      }
    }).finally(() => {
      unlockInFlight.current = false;
    });
  }, [locked, preferences.lockSound, preferences.reduceMotion]);

  const lockNow = useCallback(() => {
    setPaletteOpen(false);
    setNotificationsOpen(false);
    setSettingsOpen(false);
    setAtlasSettingsOpen(false);
    void browserApi()?.lock().then((ok) => {
      if (ok) {
        if (unlockTimer.current) window.clearTimeout(unlockTimer.current);
        setLockReason("manual");
        setUnlocking(false);
        setLocked(true);
      }
    });
  }, []);

  useEffect(() => {
    if (!locked) return;
    const stopSound = playVaultSound("lock", preferences.lockSound);
    const release = (event: KeyboardEvent) => {
      if (event.repeat || unlocking) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      unlock();
    };
    window.addEventListener("keydown", release, true);
    return () => {
      stopSound();
      window.removeEventListener("keydown", release, true);
    };
  }, [locked, preferences.lockSound, unlock, unlocking]);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setNotificationsOpen(false);
        setSettingsOpen(false);
        setAtlasSettingsOpen(false);
      }
      if ((event.metaKey || event.ctrlKey) && /^[1-4]$/.test(event.key)) {
        const target = services[Number(event.key) - 1];
        if (target) void selectService(target.id);
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  });

  useEffect(() => {
    if (paletteOpen) window.setTimeout(() => searchRef.current?.focus(), 40);
  }, [paletteOpen]);

  useEffect(() => {
    void browserApi()?.setShellOverlayOpen(paletteOpen || notificationsOpen || settingsOpen || atlasSettingsOpen || locked);
  }, [paletteOpen, notificationsOpen, settingsOpen, atlasSettingsOpen, locked]);

  useEffect(() => () => {
    void browserApi()?.setShellOverlayOpen(false);
  }, []);

  const access = useMemo(
    () => new Map(bootstrap.data?.services.map((service) => [service.id, service]) || []),
    [bootstrap.data],
  );
  const visibleServices = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return services.filter((service) =>
      !normalized || `${service.title} ${service.eyebrow} ${service.description}`.toLowerCase().includes(normalized),
    );
  }, [query]);
  const activeDefinition = serviceById[desktopState.activeService];

  const selectService = async (serviceId: ServiceId) => {
    const remote = serviceId === "home" ? undefined : access.get(serviceId);
    if (serviceId !== "home" && (!bootstrap.authenticated || !remote?.enabled)) return;
    setPaletteOpen(false);
    setNotificationsOpen(false);
    setSettingsOpen(false);
    setAtlasSettingsOpen(false);
    const api = browserApi();
    if (api) setDesktopState(await api.navigate(serviceId));
    else setDesktopState((current) => ({ ...current, activeService: serviceId }));
  };

  const notifications = bootstrap.data?.notifications.items || [];
  const unread = bootstrap.data?.notifications.unread || 0;
  const userName = cinematicPreviewName
    || preferences.preferredName.trim()
    || bootstrap.data?.viewer.name
    || "T-Mod";
  const connectionState = bootstrap.online
    ? "online"
    : bootstrap.authenticated
      ? "reconnecting"
      : "offline";
  const connectionLabel = bootstrap.online
    ? "Контур на связи"
    : bootstrap.authenticated
      ? "Восстанавливаем связь"
      : "Нет соединения";
  const style = { "--active-accent": activeDefinition?.accent || "#8ea4ff" } as CSSProperties;
  const updateBusy = ["checking", "available", "downloading"].includes(updateState.phase);
  const updateLabel = updateState.phase === "ready"
    ? `Установить ${updateState.version ? `v${updateState.version}` : "обновление"}`
    : updateState.phase === "downloading" || updateState.phase === "available"
      ? `Загрузка ${updateState.percent || 0}%`
      : updateState.phase === "checking"
        ? "Проверяем версию"
        : updateState.phase === "error"
          ? "Скачать обновление"
        : `v${updateState.currentVersion}`;

  const runUpdateAction = () => {
    const api = browserApi();
    if (!api || updateBusy) return;
    if (updateState.phase === "ready") void api.installUpdate();
    else if (updateState.phase === "error") void api.openReleasePage();
    else void api.checkForUpdates().then(setUpdateState);
  };

  const login = async (credentials: DesktopLoginCredentials): Promise<DesktopLoginResult> => {
    const api = browserApi();
    if (!api) return { ok: false, error: "login_failed" };
    // Any periodic bootstrap that started before this click is now stale.
    // Its late offline result must not replace the authenticated projection.
    bootstrapRevision.current += 1;
    const result = await api.login(credentials);
    if (result.ok && result.bootstrap) {
      hasLoadedBootstrap.current = true;
      setBootstrapLoading(false);
      setBootstrap(result.bootstrap);
    } else if (result.ok) {
      await loadBootstrap();
    }
    return result;
  };

  const logout = async () => {
    const api = browserApi();
    if (!api) return;
    await api.logout();
    setSettingsOpen(false);
    setAtlasSettingsOpen(false);
    setBootstrap({ authenticated: false, online: true, error: "login_required" });
    setDesktopState((current) => ({ ...current, activeService: "home", error: undefined }));
  };

  const saveOverlay = async (patch: Partial<AtlasOverlayConfig>) => {
    const api = overlayApi();
    if (!api || overlayBusy) return;
    const previous = overlayConfig;
    setOverlayConfig((current) => normalizeAtlasOverlayConfig({ ...current, ...patch }));
    setOverlayBusy(true);
    setOverlayError(undefined);
    try {
      const saved = await api.saveConfig(patch);
      if (saved) setOverlayConfig(normalizeAtlasOverlayConfig(saved));
      setOverlayCatalog(await api.getCatalog());
    } catch (error) {
      setOverlayConfig(previous);
      setOverlayError(error instanceof Error ? error.message : "Настройки Overlay не сохранены.");
    } finally {
      setOverlayBusy(false);
    }
  };

  const previewOverlay = (patch: Partial<AtlasOverlayConfig>) => {
    setOverlayConfig((current) => normalizeAtlasOverlayConfig({ ...current, ...patch }));
  };

  const openAtlasSettings = () => {
    setPaletteOpen(false);
    setNotificationsOpen(false);
    setSettingsOpen(false);
    setAtlasSettingsFocusRequest((value) => value + 1);
    setAtlasSettingsOpen(true);
  };

  const desktopClasses = [
    "desktop",
    preferences.sidebarCollapsed ? "sidebar-collapsed" : "",
    preferences.compactMode ? "compact-mode" : "",
    preferences.reduceMotion ? "reduce-motion" : "",
    preferences.solidSurfaces ? "solid-surfaces" : "",
  ].filter(Boolean).join(" ");

  const copyCurrentLink = async () => {
    if (await browserApi()?.copyCurrentLink()) setToast("Ссылка на раздел скопирована");
  };

  return (
    <div className={desktopClasses} style={style}>
      <div className="aurora" aria-hidden="true"><i/><i/><i/></div>
      <aside className="sidebar">
        <button
          className="sidebar-toggle"
          onClick={() => setPreferences((current) => ({ ...current, sidebarCollapsed: !current.sidebarCollapsed }))}
          aria-label={preferences.sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"}
          title={preferences.sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"}
        ><Icon name="menu"/></button>
        <button className="brand" onClick={() => void selectService("home")} aria-label="T-Mod — домой">
          <span className="brand-mark"><span>T</span></span>
          <span><strong>T-Mod</strong><small>desktop system</small></span>
        </button>

        <button className="quick-search" onClick={() => setPaletteOpen(true)}>
          <Icon name="search"/><span>Найти или открыть</span><kbd>⌘ K</kbd>
        </button>

        <nav className="nav-list" aria-label="Сервисы T-Mod">
          {services.map((service) => {
            const remote = service.id === "home" ? undefined : access.get(service.id);
            const locked = service.id !== "home" && (
              !bootstrap.authenticated || !remote || !remote.enabled
            );
            const active = desktopState.activeService === service.id;
            return (
              <button
                key={service.id}
                className={`nav-item ${active ? "active" : ""} ${locked ? "locked" : ""}`}
                style={{ "--service-accent": service.accent } as CSSProperties}
                onClick={() => void selectService(service.id)}
                title={locked ? remote?.reason || "Войдите в T-Mod Account" : service.description}
              >
                <span className="nav-icon"><Icon name={locked ? "lock" : service.id}/></span>
                <span className="nav-copy"><strong>{service.title}</strong><small>{service.eyebrow}</small></span>
                {service.shortcut && <kbd>{service.shortcut.replace("⌘ ", "")}</kbd>}
                {active && <span className="active-line"/>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className={`connection ${connectionState}`}>
            <span className="connection-dot"/><span>{connectionLabel}</span>
          </div>
          <div className="identity-row">
          <button className="identity" onClick={() => bootstrap.authenticated ? void selectService("reactor") : void selectService("home")}>
            <span className="avatar">{userName.slice(0, 1).toUpperCase()}</span>
            <span><strong>{bootstrap.authenticated ? userName : "Войти в T-Mod"}</strong><small>{bootstrap.data?.viewer.account_tier === "administrator" ? "Администратор" : bootstrap.data?.viewer.guild_member ? "Товарищество" : "Единый аккаунт"}</small></span>
            <span className="identity-arrow">›</span>
          </button>
          {bootstrap.authenticated && <button className="logout-button" onClick={() => void logout()} title="Выйти из аккаунта"><Icon name="logout"/></button>}
          </div>
        </div>
      </aside>

      <header className="topbar">
        <div className="browser-tools">
          <button disabled={!desktopState.canGoBack} onClick={() => void browserApi()?.goBack()}><Icon name="back"/></button>
          <button disabled={!desktopState.canGoForward} onClick={() => void browserApi()?.goForward()}><Icon name="forward"/></button>
          <div className="surface-title"><span style={{ background: activeDefinition.accent }}/><strong>{activeDefinition.title}</strong><small>{activeDefinition.eyebrow}</small></div>
        </div>
        <div className="top-actions">
          {browserApi() && (
            <button
              className={`update-pill ${updateState.phase}`}
              onClick={runUpdateAction}
              disabled={updateBusy}
              title={updateState.message || "Проверить обновления T-Mod"}
            >
              <Icon name={updateState.phase === "ready" ? "download" : "refresh"}/>
              <span>{updateLabel}</span>
              {(updateState.phase === "downloading" || updateState.phase === "available") && (
                <i style={{ width: `${updateState.percent || 0}%` }}/>
              )}
            </button>
          )}
          <button className={`channel-badge ${preferences.updateChannel}`} onClick={() => { setNotificationsOpen(false); setAtlasSettingsOpen(false); setSettingsOpen(true); }} title="Канал обновлений">{preferences.updateChannel.toUpperCase()}</button>
          {desktopState.activeService === "atlas" && <button className={`atlas-overlay-shortcut ${overlayConfig.enabled ? "active" : ""} ${atlasSettingsOpen ? "selected" : ""}`} onClick={openAtlasSettings} title="Настройки Atlas"><Icon name="atlas"/><span>Настройки Atlas</span><i/></button>}
          <button className="circle-action" onClick={lockNow} title="Заблокировать T-Mod"><Icon name="lock"/></button>
          {desktopState.activeService !== "home" && <button className="circle-action" onClick={() => void browserApi()?.reload()} title="Обновить"><Icon name="refresh"/></button>}
          {desktopState.activeService !== "home" && <button className="circle-action" onClick={() => void copyCurrentLink()} title="Скопировать ссылку"><Icon name="link"/></button>}
          {desktopState.activeService !== "home" && <button className="circle-action" onClick={() => void browserApi()?.openCurrentLink()} title="Открыть в браузере"><Icon name="external"/></button>}
          <button className={`circle-action ${unread ? "has-unread" : ""}`} onClick={() => { setSettingsOpen(false); setAtlasSettingsOpen(false); setNotificationsOpen((open) => !open); }} title="Уведомления"><Icon name="bell"/>{unread > 0 && <b>{Math.min(unread, 99)}</b>}</button>
          <button className={`circle-action ${settingsOpen ? "active" : ""}`} onClick={() => { setNotificationsOpen(false); setAtlasSettingsOpen(false); setSettingsOpen((open) => !open); }} title="Настройки приложения"><Icon name="settings"/></button>
          <div className="window-actions">
            <button aria-label="Свернуть" title="Свернуть" onClick={() => void browserApi()?.minimize()}><Icon name="minimize"/></button>
            <button aria-label="Развернуть" title="Развернуть" onClick={() => void browserApi()?.toggleMaximize()}><Icon name="maximize"/></button>
            <button className="close" aria-label="Закрыть" title="Закрыть" onClick={() => void browserApi()?.close()}><Icon name="close"/></button>
          </div>
        </div>
        {desktopState.loading && <div className="load-line"/>}
      </header>

      <main className={`content ${desktopState.activeService !== "home" ? "service-open" : ""} ${atlasSettingsOpen ? "atlas-settings-open" : ""}`}>
        {atlasSettingsOpen ? (
          <AtlasSettingsPage
            overlayConfig={overlayConfig}
            overlayCatalog={overlayCatalog}
            overlayAllowed={atlasSettingsQa || bootstrap.data?.atlas_overlay?.allowed === true}
            overlayBusy={overlayBusy}
            overlayError={overlayError}
            onOverlayChange={saveOverlay}
            onOverlayPreview={previewOverlay}
            onClose={() => setAtlasSettingsOpen(false)}
            focusRequest={atlasSettingsFocusRequest}
          />
        ) : desktopState.activeService === "home" ? (
          <Home
            name={userName}
            bootstrap={bootstrap}
            loading={bootstrapLoading}
            bridgeAvailable={bridgeAvailable}
            notifications={notifications}
            access={access}
            onOpen={selectService}
            onLogin={login}
            onRetry={loadBootstrap}
            overlayConfig={overlayConfig}
            overlayAllowed={bootstrap.data?.atlas_overlay?.allowed === true}
            onOverlaySettings={openAtlasSettings}
          />
        ) : desktopState.error ? (
          <section className="service-error-stage">
            <span><Icon name="refresh"/></span>
            <p className="kicker">СЕРВИС НЕДОСТУПЕН</p>
            <h1>{activeDefinition.title} не открылся</h1>
            <p>Соединение могло прерваться или доступ изменился. Ваши данные не потеряны.</p>
            <div><button className="primary" onClick={() => void selectService(desktopState.activeService)}>Повторить <b>↻</b></button><button onClick={() => void selectService("home")}>Вернуться в Центр</button></div>
          </section>
        ) : (
          <div className="service-underlay"><div className="service-orbit"/><p>Открываем {activeDefinition.title}</p></div>
        )}
      </main>

      {notificationsOpen && (
        <Notifications items={notifications} unread={unread} onClose={() => setNotificationsOpen(false)} onOpen={selectService}/>
      )}
      {settingsOpen && (
        <SettingsDrawer
          preferences={preferences}
          online={bootstrap.online}
          lastSuccessfulAt={bootstrap.lastSuccessfulAt}
          updateState={updateState}
          onChange={setPreferences}
          onClose={() => setSettingsOpen(false)}
          onReconnect={loadBootstrap}
          onLock={lockNow}
        />
      )}
      {paletteOpen && (
        <CommandPalette
          query={query}
          setQuery={setQuery}
          items={visibleServices}
          access={access}
          authenticated={bootstrap.authenticated}
          inputRef={searchRef}
          onClose={() => setPaletteOpen(false)}
          onOpen={selectService}
        />
      )}
      {updateState.phase === "ready" && dismissedUpdate !== updateState.version && (
        <aside className="update-toast" role="status">
          <span className="update-toast-icon"><Icon name="download"/></span>
          <div><small>T-Mod готов к обновлению</small><strong>Версия {updateState.version}</strong><p>Перезапуск займёт несколько секунд.</p></div>
          <button className="update-later" onClick={() => setDismissedUpdate(updateState.version)}>Позже</button>
          <button className="update-install" onClick={() => void browserApi()?.installUpdate()}>Перезапустить</button>
        </aside>
      )}
      {toast && <div className="desktop-toast" role="status">{toast}</div>}
      {locked && (
        <VaultScreen
          name={userName}
          reason={lockReason}
          reduced={preferences.reduceMotion}
          unlocking={unlocking}
          onMinimize={() => void browserApi()?.minimize()}
        />
      )}
      {launchVisible && <CinematicLaunch name={userName} reduced={preferences.reduceMotion}/>}
    </div>
  );
}

function Home({
  name,
  bootstrap,
  loading,
  bridgeAvailable,
  notifications,
  access,
  onOpen,
  onLogin,
  onRetry,
  overlayConfig,
  overlayAllowed,
  onOverlaySettings,
}: {
  name: string;
  bootstrap: BootstrapResult;
  loading: boolean;
  bridgeAvailable: boolean;
  notifications: DesktopNotification[];
  access: Map<string, { enabled: boolean; reason: string | null }>;
  onOpen: (id: ServiceId) => Promise<void>;
  onLogin: (credentials: DesktopLoginCredentials) => Promise<DesktopLoginResult>;
  onRetry: () => Promise<void>;
  overlayConfig: AtlasOverlayConfig;
  overlayAllowed: boolean;
  onOverlaySettings: () => void;
}) {
  const [loginValue, setLoginValue] = useState("");
  const [pin, setPin] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<DesktopLoginResult["error"]>();

  if (loading) {
    return (
      <section className="session-stage" aria-live="polite">
        <div className="session-orbit"><span>T</span><i/><i/></div>
        <p className="kicker">T-MOD ACCOUNT</p>
        <h1>Восстанавливаем<br/>защищённую сессию</h1>
        <p>Проверяем аккаунт, доступные пространства и актуальную версию клиента.</p>
        <div className="session-progress"><i/></div>
      </section>
    );
  }

  if (!bootstrap.authenticated) {
    const messages: Record<NonNullable<DesktopLoginResult["error"]>, string> = {
      invalid: "Логин или PIN не подошли. Проверьте данные и повторите вход.",
      locked: "Слишком много попыток. Подождите несколько минут и попробуйте снова.",
      reset_required: "PIN заблокирован. Напишите T-Mod команду /reset в Discord.",
      character_required: "Сначала добавьте персонажа через /account в Discord.",
      atlas_access: "Для этой учётной записи ещё не выдан доступ к Atlas.",
      banned: "Доступ к экосистеме T-Mod заблокирован.",
      network_unavailable: "Соединение пока восстанавливается. T-Mod уже повторяет попытку — немного подождите и нажмите вход ещё раз.",
      login_failed: "Вход принят, но подтверждение сессии задержалось. Повторите нажатие — PIN вводить заново не потребуется.",
      invalid_input: "Логин — от 3 символов, PIN — ровно 8 цифр.",
    };
    const submit = async (event: FormEvent) => {
      event.preventDefault();
      if (loginBusy || !bridgeAvailable) return;
      setLoginBusy(true);
      setLoginError(undefined);
      try {
        const result = await onLogin({ login: loginValue, pin });
        if (!result.ok) setLoginError(result.error || "login_failed");
      } finally {
        setLoginBusy(false);
      }
    };
    return (
      <section className="login-stage">
        <div className="login-visual">
          <div className="login-sigil"><span>T</span><i/><i/><i/></div>
          <p className="kicker">ЕДИНЫЙ КОНТУР</p>
          <h1>Один вход.<br/>Вся экосистема.</h1>
          <p>Ваши права, сервисы и сессия синхронизируются через защищённый T-Mod Account.</p>
          <div className="login-assurances"><span><Icon name="shield"/><b>HttpOnly-сессия</b></span><span><i/>Все домены tvr.lat</span></div>
        </div>
        <form className="desktop-login-form" onSubmit={(event) => void submit(event)}>
          <header><p>T·ID</p><h2>Войти в T-Mod</h2><span>Данные задаются через <b>/account</b> в личных сообщениях боту.</span></header>
          <label><span>Логин</span><input value={loginValue} onChange={(event) => setLoginValue(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={3} maxLength={32} placeholder="ваш.логин" disabled={loginBusy}/></label>
          <label><span>PIN · 8 цифр</span><input value={pin} onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 8))} autoComplete="current-password" inputMode="numeric" type="password" minLength={8} maxLength={8} placeholder="••••••••" disabled={loginBusy}/></label>
          {loginError && <output className="desktop-login-error">{messages[loginError]}</output>}
          {!bridgeAvailable && <output className="desktop-login-error">Компонент приложения не загрузился. Переустановите T-Mod из последнего релиза.</output>}
          <button className={`primary ${loginBusy ? "busy" : ""}`} type="submit" disabled={loginBusy || !bridgeAvailable} aria-busy={loginBusy}>{loginBusy ? "Устанавливаем защищённую сессию…" : "Войти в T-Mod"}<span>{loginBusy ? "•••" : "→"}</span></button>
          <footer className={bootstrap.online ? "online" : "reconnecting"}><i/><span>{bootstrap.online ? "Сервер T-Mod доступен" : "Восстанавливаем соединение с T-Mod…"}</span>{!bootstrap.online && <button type="button" onClick={() => void onRetry()} aria-label="Повторить подключение"><Icon name="refresh"/></button>}</footer>
        </form>
      </section>
    );
  }

  const tier = bootstrap.data?.viewer.administrator ? "Полный контур" : bootstrap.data?.viewer.guild_member ? "Контур Товарищества" : "Базовый контур";
  return (
    <div className="home-scroll">
      <section className="welcome">
        <div>
          <p className="kicker">{tier}</p>
          <h1>{greeting(name)}</h1>
          <p>Здесь собраны пространства, решения и события, которым сегодня нужно ваше внимание.</p>
        </div>
        <div className="time-block"><strong>{new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(new Date())}</strong><span>время Товарищества</span></div>
      </section>

      <section className="overview-grid">
        <article className={`reactor-card ${access.get("reactor")?.enabled ? "" : "locked"}`} onClick={() => access.get("reactor")?.enabled && void onOpen("reactor")}>
          <div className="reactor-visual"><i/><i/><i/><span>T</span></div>
          <div className="reactor-copy"><p>Личный Reactor</p><h2>{access.get("reactor")?.enabled ? <>Ваш мандат<br/>в активном состоянии</> : <>Доступ откроется<br/>после вступления</>}</h2><span>{access.get("reactor")?.enabled ? <>Открыть пространство <b>→</b></> : access.get("reactor")?.reason}</span></div>
          <div className="reactor-noise"/>
        </article>
        <article className="attention-card">
          <div className="section-heading"><span><Icon name="bell"/></span><div><p>Центр внимания</p><h2>{notifications.length ? `${notifications.length} важных события` : "Всё спокойно"}</h2></div></div>
          <div className="mini-events">
            {notifications.slice(0, 3).map((item) => (
              <button key={item.id} onClick={() => { const target = resolveNotificationServiceId(item.route); if (target) void onOpen(target); }}>
                <i className={item.severity}/><span><strong>{item.title}</strong><small>{item.body}</small></span><time>{formatTime(item.created_at)}</time>
              </button>
            ))}
            {!notifications.length && <div className="empty-events">Новых событий нет. Можно сосредоточиться на текущей работе.</div>}
          </div>
        </article>
      </section>

      {overlayAllowed && (
        <button className={`overlay-home-card ${overlayConfig.enabled ? "active" : ""}`} onClick={onOverlaySettings}>
          <span className="overlay-home-orbit"><Icon name="atlas"/><i/><i/></span>
          <span className="overlay-home-copy"><small>ATLAS · FIELD MODE</small><strong>{overlayConfig.enabled ? "Игровой оверлей готов" : "Подключить Atlas к GTA V"}</strong><p>{overlayConfig.enabled ? `${overlayConfig.characterName || "Персонаж"} · ${overlayConfig.factionCode.toUpperCase()} · удерживать ${overlayConfig.hotkey.replaceAll("+", " + ")}` : "Голосовой вопрос, мгновенный ответ, источники и озвучка прямо поверх игры."}</p></span>
          <span className="overlay-home-state"><i/>{overlayConfig.enabled ? "АКТИВЕН" : "НАСТРОИТЬ"}<b>→</b></span>
        </button>
      )}

      <section className="spaces-section">
        <div className="section-title"><div><p className="kicker">Пространства</p><h2>Продолжить работу</h2></div><button onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}><Icon name="command"/> Командная строка</button></div>
        <div className="space-grid">
          {services.filter((service) => !["home", "reactor"].includes(service.id)).map((service) => {
            const remote = access.get(service.id);
            const locked = !remote || !remote.enabled;
            return (
              <button key={service.id} className={`space-card ${locked ? "locked" : ""}`} style={{ "--service-accent": service.accent } as CSSProperties} onClick={() => !locked && void onOpen(service.id)}>
                <span className="space-icon"><Icon name={locked ? "lock" : service.id}/></span>
                <span className="space-meta"><small>{service.eyebrow}</small><strong>{service.title}</strong><p>{locked ? remote?.reason : service.description}</p></span>
                <span className="space-arrow">↗</span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function Notifications({ items, unread, onClose, onOpen }: { items: DesktopNotification[]; unread: number; onClose: () => void; onOpen: (id: ServiceId) => Promise<void> }) {
  return <><button className="scrim clear" onClick={onClose} aria-label="Закрыть"/><aside className="notification-drawer"><header><div><p className="kicker">Поток T-Mod</p><h2>Уведомления</h2></div><span>{unread} новых</span></header><div className="notification-list">{items.map((item) => <button key={item.id} onClick={() => { const target = resolveNotificationServiceId(item.route); if (target) void onOpen(target); }}><i className={item.severity}/><span><strong>{item.title}</strong><p>{item.body}</p><small>{formatTime(item.created_at)}</small></span></button>)}{!items.length && <div className="drawer-empty"><Icon name="bell"/><p>В центре уведомлений тихо.</p></div>}</div></aside></>;
}

function SettingsDrawer({
  preferences,
  online,
  lastSuccessfulAt,
  updateState,
  onChange,
  onClose,
  onReconnect,
  onLock,
}: {
  preferences: DesktopShellPreferences;
  online: boolean;
  lastSuccessfulAt?: string;
  updateState: DesktopUpdateState;
  onChange: (preferences: DesktopShellPreferences) => void;
  onClose: () => void;
  onReconnect: () => Promise<void>;
  onLock: () => void;
}) {
  const toggle = (key: keyof Pick<DesktopShellPreferences, "compactMode" | "reduceMotion" | "solidSurfaces" | "lockSound">) =>
    onChange({ ...preferences, [key]: !preferences[key] });

  return <><button className="scrim clear" onClick={onClose} aria-label="Закрыть"/><aside className="settings-drawer">
    <header><div><p className="kicker">T-MOD DESKTOP</p><h2>Настройки</h2></div><button onClick={onClose} aria-label="Закрыть">×</button></header>
    <div className="settings-scroll">
      <section><p className="settings-label">Обращение</p>
        <label className="preferred-name-setting">
          <span><strong>Как вас называть</strong><small>Это имя используется во всей оболочке T‑Mod на этом устройстве</small></span>
          <div><input value={preferences.preferredName} maxLength={24} autoComplete="off" spellCheck={false} placeholder="Например, Иван" onChange={(event) => onChange({ ...preferences, preferredName: event.target.value })}/><small>{preferences.preferredName.length}/24</small></div>
        </label>
      </section>
      <section><p className="settings-label">Интерфейс</p>
        <SettingToggle label="Компактный режим" hint="Больше информации на одном экране" active={preferences.compactMode} onClick={() => toggle("compactMode")}/>
        <SettingToggle label="Спокойные анимации" hint="Минимум движения и эффектов" active={preferences.reduceMotion} onClick={() => toggle("reduceMotion")}/>
        <SettingToggle label="Плотные поверхности" hint="Меньше прозрачности, выше контраст" active={preferences.solidSurfaces} onClick={() => toggle("solidSurfaces")}/>
        <div className="setting-row zoom-setting"><span><strong>Масштаб сервисов</strong><small>Применяется ко всем пространствам</small></span><div>{[0.9, 1, 1.1].map((zoom) => <button key={zoom} className={preferences.serviceZoom === zoom ? "active" : ""} onClick={() => onChange({ ...preferences, serviceZoom: zoom })}>{Math.round(zoom * 100)}%</button>)}</div></div>
      </section>
      <section><p className="settings-label">Экран блокировки</p>
        <div className="setting-row lock-delay-setting"><span><strong>Автоблокировка</strong><small>После отсутствия активности</small></span><div>{[0, 5, 10, 15, 30].map((minutes) => <button key={minutes} className={preferences.idleLockMinutes === minutes ? "active" : ""} onClick={() => onChange({ ...preferences, idleLockMinutes: minutes })}>{minutes ? `${minutes}м` : "Выкл"}</button>)}</div></div>
        <SettingToggle label="Звук блокировки" hint="Кинематографичный сигнал входа и выхода" active={preferences.lockSound} onClick={() => toggle("lockSound")}/>
        <button className="lock-now-setting" onClick={onLock}><Icon name="lock"/><span><strong>Заблокировать сейчас</strong><small>Разблокировка — только клавиатурой</small></span><b>›</b></button>
      </section>
      <section><p className="settings-label">Обновления</p>
        <div className="update-channel-setting"><div><button className={preferences.updateChannel === "beta" ? "active" : ""} onClick={() => onChange({ ...preferences, updateChannel: "beta" })}><strong>Beta</strong><small>Проверенные версии</small></button><button className={preferences.updateChannel === "dev" ? "active dev" : "dev"} onClick={() => onChange({ ...preferences, updateChannel: "dev" })}><strong>Dev</strong><small>Самые новые функции</small></button></div><p>{preferences.updateChannel === "dev" ? "Экспериментальные сборки могут меняться чаще. Вернуться в Beta можно в любой момент." : "Основной канал. Обновления выходят реже и проходят полный цикл проверки."}</p></div>
      </section>
      <section><p className="settings-label">Диагностика</p><div className="diagnostic-card"><div><i className={online ? "online" : ""}/><span><strong>{online ? "T-Mod на связи" : "Восстанавливаем соединение"}</strong><small>{lastSuccessfulAt ? `Последняя синхронизация: ${formatTime(lastSuccessfulAt)}` : "Ожидаем первую синхронизацию"}</small></span></div><button onClick={() => void onReconnect()}><Icon name="refresh"/> Проверить</button></div><div className="diagnostic-line"><span>Версия приложения</span><b>{updateState.currentVersion}</b></div><div className="diagnostic-line"><span>Канал обновлений</span><b className={`channel-text ${preferences.updateChannel}`}>{preferences.updateChannel.toUpperCase()}</b></div></section>
      <button className="reset-preferences" onClick={() => onChange({ ...DEFAULT_PREFERENCES })}>Вернуть настройки по умолчанию</button>
    </div>
  </aside></>;
}

function AtlasSettingsPage({
  onClose,
  overlayConfig,
  overlayCatalog,
  overlayAllowed,
  overlayBusy,
  overlayError,
  onOverlayChange,
  onOverlayPreview,
  focusRequest,
}: {
  onClose: () => void;
  overlayConfig: AtlasOverlayConfig;
  overlayCatalog: AtlasOverlayCatalog;
  overlayAllowed: boolean;
  overlayBusy: boolean;
  overlayError?: string;
  onOverlayChange: (patch: Partial<AtlasOverlayConfig>) => Promise<void>;
  onOverlayPreview: (patch: Partial<AtlasOverlayConfig>) => void;
  focusRequest: number;
}) {
  const [voices, setVoices] = useState<SpeechSynthesisVoice[]>([]);
  const [aiVoices, setAiVoices] = useState<AtlasOverlayVoiceCatalog>({
    configured: false,
    provider: "system",
    defaultVoice: "",
    voices: [],
    availability: {
      state: "recovering",
      reason: "Проверяем подключение к AI-голосу Atlas.",
    },
  });
  const [microphones, setMicrophones] = useState<MediaDeviceInfo[]>([]);
  const [microphoneStatus, setMicrophoneStatus] = useState<"idle" | "testing" | "ready" | "silent" | "error">("idle");
  const aiVoiceAvailability = aiVoices.availability?.state ?? (aiVoices.configured ? "ready" : "unconfigured");
  const aiVoiceStatusLabel = aiVoiceAvailability === "ready"
    ? "AI-голос готов"
    : aiVoiceAvailability === "recovering"
      ? "AI-голос восстанавливается"
      : aiVoiceAvailability === "degraded"
        ? "AI-голос временно недоступен"
        : "AI-голосу нужны учётные данные";
  const aiVoiceStatusHint = aiVoices.availability?.reason === "credentials_or_endpoint_missing"
    ? "Добавьте ключ и HTTPS-адрес AI-провайдера в настройки сервера Atlas."
    : aiVoices.availability?.reason
      || (aiVoiceAvailability === "ready"
        ? "Ответы будут озвучиваться голосом Atlas."
        : "До восстановления Atlas автоматически использует системный голос Windows.");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      document.querySelector<HTMLElement>(".content.atlas-settings-open")?.scrollTo({ top: 0, behavior: "smooth" });
    }, 40);
    return () => window.clearTimeout(timer);
  }, [focusRequest]);

  useEffect(() => {
    if (!globalThis.speechSynthesis) return undefined;
    const updateVoices = () => {
      const next = globalThis.speechSynthesis.getVoices()
        .slice()
        .sort((left, right) => Number(/^ru(?:-|_)/i.test(right.lang)) - Number(/^ru(?:-|_)/i.test(left.lang)) || left.name.localeCompare(right.name));
      setVoices(next);
    };
    updateVoices();
    globalThis.speechSynthesis.addEventListener("voiceschanged", updateVoices);
    return () => globalThis.speechSynthesis.removeEventListener("voiceschanged", updateVoices);
  }, []);

  useEffect(() => {
    void overlayApi()?.getVoices().then((catalog) => {
      if (catalog) setAiVoices(catalog);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    void navigator.mediaDevices?.enumerateDevices().then((devices) => {
      setMicrophones(devices.filter((device) => device.kind === "audioinput"));
    }).catch(() => undefined);
  }, []);

  const previewSystemVoice = () => {
    if (!globalThis.speechSynthesis) return;
    globalThis.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance("Atlas на связи. Голос готов к работе.");
    utterance.lang = "ru-RU";
    utterance.rate = overlayConfig.speechRate;
    utterance.volume = overlayConfig.speechVolume;
    utterance.voice = voices.find((voice) =>
      voice.voiceURI === overlayConfig.speechVoice || voice.name === overlayConfig.speechVoice,
    ) || voices.find((voice) => /^ru(?:-|_)/i.test(voice.lang)) || null;
    globalThis.speechSynthesis.speak(utterance);
  };

  const previewVoice = async () => {
    if (overlayConfig.speechProvider !== "ai" || !aiVoices.configured) {
      previewSystemVoice();
      return;
    }
    try {
      const result = await overlayApi()?.previewVoice(overlayConfig.speechVoice || aiVoices.defaultVoice);
      if (!result?.audio || !result.mimeType || result.fallback) {
        previewSystemVoice();
        return;
      }
      const url = URL.createObjectURL(new Blob([result.audio], { type: result.mimeType }));
      const audio = new Audio(url);
      audio.volume = overlayConfig.speechVolume;
      audio.onended = audio.onerror = () => URL.revokeObjectURL(url);
      await audio.play();
    } catch {
      previewSystemVoice();
    }
  };

  const testMicrophone = async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setMicrophoneStatus("error");
      return;
    }
    setMicrophoneStatus("testing");
    let stream: MediaStream | undefined;
    let context: AudioContext | undefined;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          ...(overlayConfig.microphoneId ? { deviceId: { exact: overlayConfig.microphoneId } } : {}),
          echoCancellation: true,
          noiseSuppression: true,
        },
        video: false,
      });
      const devices = await navigator.mediaDevices.enumerateDevices();
      setMicrophones(devices.filter((device) => device.kind === "audioinput"));
      context = new AudioContext();
      const analyser = context.createAnalyser();
      analyser.fftSize = 256;
      context.createMediaStreamSource(stream).connect(analyser);
      const samples = new Uint8Array(analyser.frequencyBinCount);
      let peak = 0;
      const deadline = performance.now() + 900;
      while (performance.now() < deadline) {
        analyser.getByteFrequencyData(samples);
        peak = Math.max(peak, ...samples);
        await new Promise((resolve) => window.setTimeout(resolve, 55));
      }
      setMicrophoneStatus(peak > 4 ? "ready" : "silent");
    } catch {
      setMicrophoneStatus("error");
    } finally {
      stream?.getTracks().forEach((track) => track.stop());
      void context?.close();
    }
  };
  const scrollTo = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  return <section className="atlas-settings-page" aria-label="Настройки Atlas Overlay">
    <header className="atlas-settings-page-hero">
      <div className="atlas-settings-page-orbit" aria-hidden="true"><i/><i/><span><Icon name="atlas"/></span></div>
      <div><p className="kicker">ATLAS · FIELD SYSTEM</p><h1>Пульт полевого интерфейса</h1><p>Настройте поведение Atlas поверх GTA V: от формы ожидания и голоса до управления без курсора.</p></div>
      <div className="atlas-settings-page-state"><i className={overlayConfig.enabled && overlayAllowed ? "active" : ""}/><span><small>СОСТОЯНИЕ</small><strong>{overlayConfig.enabled && overlayAllowed ? "Готов к игре" : "Отключён"}</strong></span></div>
      <button className="atlas-settings-page-close" onClick={onClose}><Icon name="back"/> Вернуться в Atlas</button>
    </header>
    <div className="atlas-settings-telemetry" aria-label="Сводка Atlas Overlay">
      <span><small>ПРОФИЛЬ</small><strong>{overlayConfig.responseMode === "quick" ? "Точный полевой" : "Контекстный"}</strong></span>
      <span><small>ФОРМА</small><strong>{{ orb: "Импульс", bar: "Строка", full: "Панель" }[overlayConfig.idleStyle]}</strong></span>
      <span><small>ДВИЖЕНИЕ</small><strong>{{ cinematic: "Кинематографично", balanced: "Сбалансировано", minimal: "Производительно" }[overlayConfig.motion]}</strong></span>
      <span><small>ОТВЕТ</small><strong>{{ brief: "Кратко", auto: "До конца речи", pinned: "Закреплён" }[overlayConfig.answerHold]}</strong></span>
    </div>
    <div className="atlas-settings-page-layout">
      <nav className="atlas-settings-page-nav" aria-label="Разделы настроек">
        <p>Конфигурация</p>
        <button onClick={() => scrollTo("atlas-settings-core")}><span>01</span><b>Основа</b><small>Запуск и статус</small></button>
        <button onClick={() => scrollTo("atlas-settings-persona")}><span>02</span><b>Контекст</b><small>Персонаж и фракция</small></button>
        <button onClick={() => scrollTo("atlas-settings-controls")}><span>03</span><b>Управление</b><small>Голос и клавиатура</small></button>
        <button onClick={() => scrollTo("atlas-settings-voice")}><span>04</span><b>Голос Atlas</b><small>Микрофон и озвучка</small></button>
        <button onClick={() => scrollTo("atlas-settings-visual")}><span>05</span><b>Внешний вид</b><small>Форма, тема, размер</small></button>
        <button onClick={() => scrollTo("atlas-settings-intelligence")}><span>06</span><b>Интеллект</b><small>Ответы и приватность</small></button>
        <div className="atlas-settings-keymap"><small>БЕЗ КУРСОРА В GTA</small><strong>{overlayConfig.hotkey.replaceAll("Control", "CTRL")} + TAB</strong><span>Стрелки · + − · [ ] · Enter</span></div>
      </nav>
      <main className="atlas-settings-workspace">
      <section id="atlas-settings-core" className={`overlay-settings atlas-settings-work-card ${overlayBusy ? "is-busy" : ""}`}>
        <p className="settings-label">Atlas Overlay · GTA V</p>
        <div className="overlay-setting-hero">
          <span className="overlay-setting-globe"><Icon name="atlas"/><i/></span>
          <div><small>FIELD INTELLIGENCE</small><strong>Atlas поверх игры</strong><p>Удерживайте клавиши, задайте вопрос и получите короткий ответ с источниками и озвучкой.</p></div>
          <i className={overlayConfig.enabled && overlayAllowed ? "active" : ""}/>
        </div>
        {!overlayAllowed ? (
          <div className="overlay-access-note"><Icon name="lock"/><span><strong>Доступ пока не выдан</strong><small>Atlas Overlay включается вместе с доступом к Atlas AI.</small></span></div>
        ) : !overlayCatalog.characters.length ? (
          <div className="overlay-access-note warning"><Icon name="atlas"/><span><strong>Добавьте персонажа</strong><small>Создайте хотя бы одного персонажа через T‑Mod Account, затем обновите приложение.</small></span></div>
        ) : (
          <fieldset disabled={overlayBusy}>
            <div className="atlas-settings-section-heading"><span>01</span><div><strong>Запуск и присутствие</strong><small>Когда Atlas появляется поверх игры</small></div></div>
            <SettingToggle label="Оверлей в игре" hint="Запускается вместе с T‑Mod и не забирает управление у GTA" active={overlayConfig.enabled} onClick={() => void onOverlayChange({ enabled: !overlayConfig.enabled })}/>
            <SettingToggle label="Показывать статус в игре" hint="Компактная строка появляется при запуске GTA V, Majestic или RAGE Multiplayer" active={overlayConfig.showGameStatus} onClick={() => void onOverlayChange({ showGameStatus: !overlayConfig.showGameStatus })}/>
            <SettingToggle label="Инициализация при входе в игру" hint="Показывается только один раз за запуск T-Mod — при первом фокусе GTA V" active={overlayConfig.initializationAnimation} onClick={() => void onOverlayChange({ initializationAnimation: !overlayConfig.initializationAnimation })}/>
            <SettingToggle label="Показывать в записи и трансляции" hint="Сохраняет стабильный источник Atlas Overlay для OBS" active={overlayConfig.captureInRecordings} onClick={() => void onOverlayChange({ captureInRecordings: !overlayConfig.captureInRecordings })}/>
            <div className="atlas-settings-section-heading"><span>02</span><div><strong>Игровой контекст</strong><small>Кто обращается к Atlas и в каком контуре</small></div></div>
            <div id="atlas-settings-persona" className="overlay-setting-block atlas-settings-anchor">
              <span className="overlay-setting-title">Персонаж</span>
              <div className="overlay-character-grid">
                {overlayCatalog.characters.map((character) => (
                  <button
                    key={character.id}
                    className={overlayConfig.characterId === character.id ? "active" : ""}
                    onClick={() => void onOverlayChange({
                      characterId: character.id,
                      characterName: character.name,
                      serverCode: character.serverCode || overlayConfig.serverCode,
                      factionCode: character.factionCode || overlayConfig.factionCode,
                    })}
                  >
                    <span>{character.name.slice(0, 1).toUpperCase()}</span>
                    <b>{character.name}</b>
                    <small>#{character.staticId}</small>
                  </button>
                ))}
              </div>
            </div>
            <div className="overlay-setting-block">
              <span className="overlay-setting-title">Организация · Phoenix 15</span>
              <div className="overlay-faction-grid">
                {overlayCatalog.factions.map((faction) => (
                  <button
                    key={`${faction.serverCode}-${faction.code}`}
                    className={overlayConfig.factionCode === faction.code ? "active" : ""}
                    onClick={() => void onOverlayChange({ serverCode: faction.serverCode, factionCode: faction.code })}
                  >{faction.name}</button>
                ))}
              </div>
            </div>
            <div id="atlas-settings-controls" className="overlay-setting-block split atlas-settings-anchor">
              <div><span className="overlay-setting-title">Клавиша голоса</span><small>Удерживать в Windows</small></div>
              <div className="overlay-preset-grid overlay-hotkey-grid">
                {["Control+Shift+A", "F9", "F10"].map((hotkey) => (
                  <button key={hotkey} className={overlayConfig.hotkey === hotkey ? "active" : ""} onClick={() => void onOverlayChange({ hotkey })}>{hotkey.replaceAll("Control", "CTRL").replaceAll("+", " + ")}</button>
                ))}
                <input
                  key={overlayConfig.hotkey}
                  className="overlay-hotkey-input"
                  defaultValue={overlayConfig.hotkey}
                  maxLength={64}
                  spellCheck={false}
                  aria-label="Своя комбинация клавиш"
                  onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
                  onBlur={(event) => void onOverlayChange({ hotkey: event.currentTarget.value })}
                />
              </div>
            </div>
            <div className="atlas-settings-section-heading"><span>03</span><div><strong>Голос и управление</strong><small>Запрос без курсора и спокойная озвучка</small></div></div>
            <div id="atlas-settings-voice" className="overlay-setting-block split overlay-device-row atlas-settings-anchor">
              <div><span className="overlay-setting-title">Микрофон</span><small>{microphoneStatus === "testing" ? "Слушаю 1 секунду…" : microphoneStatus === "ready" ? "Сигнал отличный" : microphoneStatus === "silent" ? "Сигнал слишком тихий" : microphoneStatus === "error" ? "Нет доступа к микрофону" : "Выберите вход и проверьте сигнал"}</small></div>
              <div className="overlay-device-controls">
                <select value={overlayConfig.microphoneId} onChange={(event) => void onOverlayChange({ microphoneId: event.target.value })}>
                  <option value="">Системный микрофон</option>
                  {microphones.map((device, index) => <option key={device.deviceId || index} value={device.deviceId}>{device.label || `Микрофон ${index + 1}`}</option>)}
                </select>
                <button type="button" className={microphoneStatus === "ready" ? "ready" : ""} onClick={() => void testMicrophone()} disabled={microphoneStatus === "testing"}>{microphoneStatus === "testing" ? "…" : "Тест"}</button>
              </div>
            </div>
            <div className="overlay-setting-block split">
              <div><span className="overlay-setting-title">Профиль полевого ответа</span><small>Оба режима ограничены коротким форматом; точный быстрее выходит к применимой норме</small></div>
              <div className="overlay-preset-grid compact">
                <button className={overlayConfig.responseMode === "quick" ? "active" : ""} onClick={() => void onOverlayChange({ responseMode: "quick" })}>Точный</button>
                <button className={overlayConfig.responseMode === "balanced" ? "active" : ""} onClick={() => void onOverlayChange({ responseMode: "balanced" })}>Контекст</button>
              </div>
            </div>
            <SettingToggle label="Озвучивать ответ" hint="AI‑голос включается сразу после короткого ответа; системный голос умеет читать поток" active={overlayConfig.speakAnswers} onClick={() => void onOverlayChange({ speakAnswers: !overlayConfig.speakAnswers })}/>
            <div className="overlay-setting-block split">
              <div><span className="overlay-setting-title">Источник голоса</span><small>{aiVoiceAvailability === "ready" ? "AI-голос Atlas или быстрый голос Windows" : "Atlas выбран — системный голос используется только до восстановления AI"}</small></div>
              <div className="overlay-preset-grid compact">
                <button className={overlayConfig.speechProvider === "ai" ? "active" : ""} onClick={() => void onOverlayChange({ speechProvider: "ai", speechVoice: aiVoices.defaultVoice })}>Atlas AI</button>
                <button className={overlayConfig.speechProvider === "system" ? "active" : ""} onClick={() => void onOverlayChange({ speechProvider: "system", speechVoice: "" })}>Системный</button>
              </div>
            </div>
            <div className={`atlas-voice-status ${aiVoiceAvailability}`} role="status"><i/><span><strong>{aiVoiceStatusLabel}</strong><small>{aiVoiceStatusHint}</small></span></div>
            <div className="overlay-setting-block split overlay-device-row">
              <div><span className="overlay-setting-title">Голос Atlas</span><small>{overlayConfig.speechProvider === "ai" ? "Фирменный AI‑голос с системным резервом" : "Локальный голос устройства · без дополнительной задержки"}</small></div>
              <div className="overlay-device-controls">
                <select value={overlayConfig.speechVoice} onChange={(event) => void onOverlayChange({ speechVoice: event.target.value })}>
                  <option value="">Автовыбор голоса</option>
                  {overlayConfig.speechProvider === "ai"
                    ? aiVoices.voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name} · {voice.description}</option>)
                    : voices.map((voice) => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang}</option>)}
                </select>
                <button type="button" onClick={() => void previewVoice()}>Послушать</button>
              </div>
            </div>
            <div className="overlay-setting-block split">
              <div><span className="overlay-setting-title">Громкость голоса</span><small>{Math.round(overlayConfig.speechVolume * 100)}%</small></div>
              <div className="overlay-preset-grid compact">
                {[.55, .78, 1].map((volume) => <button key={volume} className={overlayConfig.speechVolume === volume ? "active" : ""} onClick={() => void onOverlayChange({ speechVolume: volume })}>{Math.round(volume * 100)}%</button>)}
              </div>
            </div>
            <OverlayRange label="Системные сигналы" value={overlayConfig.cueVolume} min={0} max={1} step={.02} display={`${Math.round(overlayConfig.cueVolume * 100)}%`} onPreview={(value) => onOverlayPreview({ cueVolume: value })} onCommit={(value) => onOverlayChange({ cueVolume: value })}/>
            <div className="overlay-setting-block split">
              <div><span className="overlay-setting-title">Положение</span><small>На активном мониторе</small></div>
              <div className="overlay-preset-grid compact">
                {(["top-right", "right", "bottom-right"] as const).map((anchor, index) => <button key={anchor} className={overlayConfig.anchor === anchor && overlayConfig.positionX === 1 ? "active" : ""} onClick={() => void onOverlayChange({ anchor, positionX: 1, positionY: [0, .5, 1][index] })}>{["Сверху", "Центр", "Снизу"][index]}</button>)}
              </div>
            </div>
            <div className="atlas-position-presets" aria-label="Быстрый выбор положения">
              <span className="overlay-setting-title">Точка привязки на экране</span>
              <div>{([
                [0, 0, "↖"], [.5, 0, "↑"], [1, 0, "↗"],
                [0, .5, "←"], [.5, .5, "•"], [1, .5, "→"],
                [0, 1, "↙"], [.5, 1, "↓"], [1, 1, "↘"],
              ] as const).map(([x, y, label]) => <button key={`${x}-${y}`} className={Math.abs(overlayConfig.positionX - x) < .06 && Math.abs(overlayConfig.positionY - y) < .06 ? "active" : ""} onClick={() => void onOverlayChange({ positionX: x, positionY: y, anchor: y === 0 ? "top-right" : y === 1 ? "bottom-right" : "right" })}>{label}</button>)}</div>
            </div>
            <div className="atlas-settings-section-heading"><span>04</span><div><strong>Визуальный профиль</strong><small>Форма, цвет, движение и плотность интерфейса</small></div></div>
            <div id="atlas-settings-visual" className="overlay-setting-block overlay-visual-controls atlas-settings-anchor">
              <span className="overlay-setting-title">Готовые профили</span>
              <div className="atlas-visual-preset-grid">
                <button onClick={() => void onOverlayChange({ idleStyle: "orb", theme: "graphite", motion: "minimal", scale: .84, panelWidth: 410, answerHeight: 130, fontScale: 1, opacity: .98, showCitations: false, showLatency: false })}><b>Невидимый</b><small>Максимум FPS · минимум шума</small></button>
                <button onClick={() => void onOverlayChange({ idleStyle: "bar", theme: "cosmos", motion: "balanced", scale: 1, panelWidth: 480, answerHeight: 160, fontScale: 1.12, opacity: .96, showCitations: true, showLatency: false })}><b>Полевой</b><small>Чёткий ежедневный режим</small></button>
                <button onClick={() => void onOverlayChange({ idleStyle: "full", theme: "emerald", motion: "cinematic", scale: 1.08, panelWidth: 560, answerHeight: 220, fontScale: 1.24, opacity: .98, showCitations: true, showLatency: true })}><b>Командный</b><small>Крупная информативная панель</small></button>
              </div>
              <span className="overlay-setting-title">Форма в режиме ожидания</span>
              <div className="atlas-idle-style-grid">
                {([
                  ["orb", "Импульс", "Минимальный квадрат"],
                  ["bar", "Строка", "Статус и горячая клавиша"],
                  ["full", "Панель", "Всегда полный интерфейс"],
                ] as const).map(([value, label, hint]) => <button key={value} className={overlayConfig.idleStyle === value ? "active" : ""} onClick={() => void onOverlayChange({ idleStyle: value })}><i className={`idle-shape ${value}`}/><span><b>{label}</b><small>{hint}</small></span></button>)}
              </div>
              <span className="overlay-setting-title">Цветовой контур</span>
              <div className="atlas-theme-grid">
                {([
                  ["cosmos", "Космос"], ["graphite", "Графит"], ["emerald", "Изумруд"], ["amber", "Янтарь"], ["crimson", "Кармин"],
                ] as const).map(([value, label]) => <button key={value} className={`${value} ${overlayConfig.theme === value ? "active" : ""}`} onClick={() => void onOverlayChange({ theme: value })}><i/><span>{label}</span></button>)}
              </div>
              <div className="overlay-setting-block split atlas-motion-setting">
                <div><span className="overlay-setting-title">Характер движения</span><small>Кинематографичный, спокойный или статичный</small></div>
                <div className="overlay-preset-grid compact">
                  {([['cinematic', 'Кино'], ['balanced', 'Мягко'], ['minimal', 'Минимум']] as const).map(([value, label]) => <button key={value} className={overlayConfig.motion === value ? "active" : ""} onClick={() => void onOverlayChange({ motion: value })}>{label}</button>)}
                </div>
              </div>
              <OverlayRange label="Размер" value={overlayConfig.scale} min={.78} max={1.3} step={.01} display={`${Math.round(overlayConfig.scale * 100)}%`} onPreview={(value) => onOverlayPreview({ scale: value })} onCommit={(value) => onOverlayChange({ scale: value })}/>
              <OverlayRange label="Ширина панели" value={overlayConfig.panelWidth} min={380} max={620} step={10} display={`${overlayConfig.panelWidth}px`} onPreview={(value) => onOverlayPreview({ panelWidth: value })} onCommit={(value) => onOverlayChange({ panelWidth: value })}/>
              <OverlayRange label="Высота ответа" value={overlayConfig.answerHeight} min={96} max={300} step={4} display={`${overlayConfig.answerHeight}px`} onPreview={(value) => onOverlayPreview({ answerHeight: value })} onCommit={(value) => onOverlayChange({ answerHeight: value })}/>
              <OverlayRange label="Размер текста" value={overlayConfig.fontScale} min={.9} max={1.6} step={.01} display={`${Math.round(overlayConfig.fontScale * 100)}%`} onPreview={(value) => onOverlayPreview({ fontScale: value })} onCommit={(value) => onOverlayChange({ fontScale: value })}/>
              <OverlayRange label="Прозрачность" value={overlayConfig.opacity} min={.68} max={1} step={.01} display={`${Math.round(overlayConfig.opacity * 100)}%`} onPreview={(value) => onOverlayPreview({ opacity: value })} onCommit={(value) => onOverlayChange({ opacity: value })}/>
              <OverlayPlacementPreview config={overlayConfig} onChange={onOverlayChange}/>
            </div>
            <SettingToggle label="Настройка прямо в GTA" hint={`${overlayConfig.hotkey.replaceAll("Control", "CTRL")} + Tab — режим; стрелки — позиция; +/− — размер; [ ] — ширина; Enter — готово`} active={overlayConfig.calibrationMode} onClick={() => void onOverlayChange({ calibrationMode: !overlayConfig.calibrationMode })}/>
            <div className="atlas-settings-section-heading"><span>05</span><div><strong>Ответ и интеллект</strong><small>Что остаётся на экране и какие данные получает Atlas</small></div></div>
            <div id="atlas-settings-intelligence" className="atlas-settings-anchor">
              <SettingToggle label="Контекст с экрана" hint="Только один кадр при запросе; без записи, хранения и управления игрой" active={overlayConfig.screenContextEnabled} onClick={() => void onOverlayChange({ screenContextEnabled: !overlayConfig.screenContextEnabled })}/>
            </div>
            <SettingToggle label="Показывать источники" hint="До двух коротких ссылок на использованные документы под ответом" active={overlayConfig.showCitations} onClick={() => void onOverlayChange({ showCitations: !overlayConfig.showCitations })}/>
            <SettingToggle label="Показывать время ответа" hint="Техническая задержка отображается в нижней строке оверлея" active={overlayConfig.showLatency} onClick={() => void onOverlayChange({ showLatency: !overlayConfig.showLatency })}/>
            <div className="overlay-setting-block split">
              <div><span className="overlay-setting-title">Когда сворачивать ответ</span><small>Atlas в любом случае дождётся конца озвучки</small></div>
              <div className="overlay-preset-grid compact">
                <button className={overlayConfig.answerHold === "brief" ? "active" : ""} onClick={() => void onOverlayChange({ answerHold: "brief" })}>Быстро</button>
                <button className={overlayConfig.answerHold === "auto" ? "active" : ""} onClick={() => void onOverlayChange({ answerHold: "auto" })}>Авто</button>
                <button className={overlayConfig.answerHold === "pinned" ? "active" : ""} onClick={() => void onOverlayChange({ answerHold: "pinned" })}>Закрепить</button>
              </div>
            </div>
            {overlayConfig.screenContextEnabled && <div className="overlay-privacy-note"><Icon name="shield"/><p><strong>Приватный режим.</strong> Кадр уменьшается, отправляется только вместе с вашим запросом и не сохраняется T‑Mod.</p></div>}
            <div className="overlay-borderless-note"><i/><p><strong>Для GTA V выберите «Полноэкранный без рамки».</strong> Для OBS добавьте «Захват окна» → T‑Mod Atlas Overlay или используйте «Захват экрана»: обычный Game Capture GTA не видит внешние окна.</p></div>
          </fieldset>
        )}
        {overlayError && <output className="overlay-setting-error">{overlayError}</output>}
      </section>
      </main>
    </div>
  </section>;
}

function SettingToggle({ label, hint, active, onClick }: { label: string; hint: string; active: boolean; onClick: () => void }) {
  return <button className="setting-row" onClick={onClick}><span><strong>{label}</strong><small>{hint}</small></span><i className={`toggle ${active ? "active" : ""}`}><b/></i></button>;
}

function OverlayRange({
  label,
  value,
  min,
  max,
  step,
  display,
  onPreview,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  display: string;
  onPreview: (value: number) => void;
  onCommit: (value: number) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const draftRef = useRef(value);
  const dragging = useRef(false);
  const committed = useRef(value);

  useEffect(() => {
    if (dragging.current) return;
    draftRef.current = value;
    committed.current = value;
    setDraft(value);
  }, [value]);

  const update = (next: number) => {
    const safe = Math.max(min, Math.min(max, next));
    draftRef.current = safe;
    setDraft(safe);
    onPreview(safe);
  };
  const commit = () => {
    const next = draftRef.current;
    if (Math.abs(next - committed.current) < Number.EPSILON) return;
    committed.current = next;
    void onCommit(next);
  };
  const progress = ((draft - min) / Math.max(Number.EPSILON, max - min)) * 100;

  return <label className="overlay-range-row">
    <span><b>{label}</b><small>{display}</small></span>
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={draft}
      style={{ "--range-progress": `${progress}%` } as CSSProperties}
      onPointerDown={() => { dragging.current = true; }}
      onChange={(event) => update(Number(event.currentTarget.value))}
      onPointerUp={() => { dragging.current = false; commit(); }}
      onPointerCancel={() => { dragging.current = false; commit(); }}
      onKeyUp={commit}
      onBlur={() => { dragging.current = false; commit(); }}
    />
  </label>;
}

function OverlayPlacementPreview({
  config,
  onChange,
}: {
  config: AtlasOverlayConfig;
  onChange: (patch: Partial<AtlasOverlayConfig>) => Promise<void>;
}) {
  const surface = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState({ x: config.positionX, y: config.positionY });
  const draftRef = useRef(draft);
  const dragging = useRef(false);

  useEffect(() => {
    const next = { x: config.positionX, y: config.positionY };
    draftRef.current = next;
    setDraft(next);
  }, [config.positionX, config.positionY]);

  const move = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current || !surface.current) return;
    const bounds = surface.current.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    const y = Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height));
    draftRef.current = { x, y };
    setDraft(draftRef.current);
  };

  const finish = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    dragging.current = false;
    event.currentTarget.releasePointerCapture(event.pointerId);
    void onChange({ positionX: draftRef.current.x, positionY: draftRef.current.y });
  };

  return (
    <div className="overlay-placement">
      <div><strong>Предпросмотр положения</strong><small>Перетащите Atlas в любую точку активного монитора</small></div>
      <div
        ref={surface}
        className="overlay-placement-screen"
        onPointerDown={(event) => {
          dragging.current = true;
          event.currentTarget.setPointerCapture(event.pointerId);
          move(event);
        }}
        onPointerMove={move}
        onPointerUp={finish}
        onPointerCancel={finish}
      >
        <i/><i/><i/>
        <span
          className={`overlay-placement-chip theme-${config.theme} idle-${config.idleStyle}`}
          style={{
            left: `${draft.x * 100}%`,
            top: `${draft.y * 100}%`,
            transform: `translate(${-draft.x * 100}%, ${-draft.y * 100}%) scale(${config.scale})`,
            opacity: config.opacity,
            width: config.idleStyle === "orb" ? 34 : config.idleStyle === "full" ? Math.min(190, Math.max(138, config.panelWidth * .34)) : 120,
            height: config.idleStyle === "full" ? 56 : 32,
          }}
        ><Icon name="atlas"/>{config.idleStyle !== "orb" && <><b>ATLAS</b><small>{config.hotkey.replaceAll("Control", "CTRL")}</small></>}</span>
      </div>
    </div>
  );
}

function CommandPalette({ query, setQuery, items, access, authenticated, inputRef, onClose, onOpen }: { query: string; setQuery: (query: string) => void; items: typeof services; access: Map<string, { enabled: boolean; reason: string | null }>; authenticated: boolean; inputRef: RefObject<HTMLInputElement | null>; onClose: () => void; onOpen: (id: ServiceId) => Promise<void> }) {
  return <div className="palette-layer"><button className="scrim" onClick={onClose} aria-label="Закрыть"/><section className="palette"><div className="palette-input"><Icon name="search"/><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Открыть сервис, задачу или инструмент…"/><kbd>ESC</kbd></div><div className="palette-results"><p>Пространства T-Mod</p>{items.map((service) => { const remote = access.get(service.id); const locked = service.id !== "home" && (!authenticated || !remote || !remote.enabled); return <button key={service.id} disabled={locked} onClick={() => void onOpen(service.id)} style={{ "--service-accent": service.accent } as CSSProperties}><span><Icon name={locked ? "lock" : service.id}/></span><div><strong>{service.title}</strong><small>{locked ? remote?.reason || "Войдите в T-Mod Account" : service.description}</small></div><kbd>↵</kbd></button>; })}</div><footer><span><kbd>Esc</kbd> закрыть</span><span><kbd>Enter</kbd> открыть</span><span>Команды выполняются только внутри T-Mod</span></footer></section></div>;
}
