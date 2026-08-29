import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  nativeTheme,
  powerMonitor,
  screen,
  session,
  shell,
  WebContentsView,
} from "electron";
import type { WebContents } from "electron";
import { fileURLToPath } from "node:url";
import path from "node:path";
import log from "electron-log/main";
import electronUpdater from "electron-updater";
import { AtlasOverlayController } from "./atlas-overlay-controller";
import {
  isServiceId,
  isTrustedTModUrl,
} from "../shared/services";
import type {
  BootstrapResult,
  DesktopBootstrap,
  DesktopLoginCredentials,
  DesktopLoginResult,
  DesktopLockReason,
  DesktopService,
  DesktopShellPreferences,
  DesktopState,
  DesktopUpdateState,
  ServiceId,
} from "../shared/contracts";

const { autoUpdater } = electronUpdater;

// The startup signature is part of the local application shell and must not
// depend on a first click in Chromium. Remote service views remain muted by
// their own permission boundary and cannot use this switch to request media.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
// GTA keeps the overlay renderer permanently unfocused. Chromium's default
// occlusion policy may otherwise reduce CSS/WebAudio animation cadence even
// though the native window is visible over the game.
app.commandLine.appendSwitch("disable-backgrounding-occluded-windows");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("disable-background-timer-throttling");

// electron-vite injects its own `__dirname` shim into the ESM bundle.
// A distinct name avoids a duplicate top-level declaration in packaged builds.
const bundleDirectory = path.dirname(fileURLToPath(import.meta.url));
const SHELL_HEADER_HEIGHT = 70;
const SHELL_SIDEBAR_WIDTH = 286;
const SHELL_SIDEBAR_COLLAPSED_WIDTH = 78;
const DESKTOP_PARTITION = "persist:tmod-desktop-v1";
const BOOTSTRAP_URLS = [
  "https://tvr.lat/api/desktop/v1/bootstrap",
  "https://reactor.tvr.lat/api/desktop/v1/bootstrap",
] as const;
const LOGIN_URL = "https://tvr.lat/login?next=/reactor";
const AUTH_LOGIN_URL = "https://tvr.lat/auth/login?client=desktop";
const AUTH_LOGIN_URLS = [
  AUTH_LOGIN_URL,
  "https://reactor.tvr.lat/auth/login?client=desktop",
] as const;
const LOGOUT_URL = "https://tvr.lat/logout";
const RELEASE_URL = "https://github.com/cdnserver/t-mod-releases/releases/latest";
const ATLAS_OVERLAY_SETTINGS_URL = "https://tvr.lat/desktop/atlas-overlay-settings";
const UPDATE_INTERVAL_MS = 30 * 60 * 1_000;
const BOOTSTRAP_WAVES = 3;
const BOOTSTRAP_TIMEOUT_MS = 7_000;
const LOGIN_ATTEMPTS = 3;
const LOGIN_TIMEOUT_MS = 10_000;
const SERVICE_RETRY_DELAYS = [700, 1_800, 4_000] as const;
const RETRYABLE_NETWORK_ERRORS = new Set([-2, -7, -21, -101, -102, -105, -106, -118, -324]);
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

let mainWindow: BrowserWindow | null = null;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let shellOverlayOpen = false;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let idleLockTimer: ReturnType<typeof setInterval> | undefined;
let desktopLocked = false;
let serviceManifest = new Map<Exclude<ServiceId, "home">, DesktopService>();
let lastSuccessfulBootstrap: DesktopBootstrap | undefined;
let bootstrapRevision = 0;
let bootstrapInFlight: {
  revision: number;
  promise: Promise<BootstrapResult>;
} | undefined;
let lastSuccessfulBootstrapAt: string | undefined;
let shellPreferences = { ...DEFAULT_PREFERENCES };
let serviceRetryAttempt = 0;
let serviceRetryTimer: ReturnType<typeof setTimeout> | undefined;
let lastMainFrameHttpStatus = 0;
let atlasOverlay: AtlasOverlayController | undefined;
let updateState: DesktopUpdateState = {
  phase: app.isPackaged ? "idle" : "development",
  currentVersion: app.getVersion(),
  channel: "beta",
};

app.enableSandbox();
nativeTheme.themeSource = "dark";

function desktopSession() {
  return session.fromPartition(DESKTOP_PARTITION, { cache: true });
}

function clearServiceManifest(): void {
  serviceManifest = new Map();
}

function applyServiceManifest(data: DesktopBootstrap): boolean {
  if (data.protocol_version !== 1 || !Array.isArray(data.services)) return false;
  const next = new Map<Exclude<ServiceId, "home">, DesktopService>();
  for (const service of data.services) {
    if (
      isServiceId(service.id) &&
      typeof service.url === "string" &&
      isTrustedTModUrl(service.url)
    ) {
      next.set(service.id, service);
    }
  }
  serviceManifest = next;
  return next.size > 0;
}

function state(): DesktopState {
  return {
    activeService,
    loading: serviceLoading,
    canGoBack: Boolean(serviceView?.webContents.navigationHistory.canGoBack()),
    canGoForward: Boolean(serviceView?.webContents.navigationHistory.canGoForward()),
    url: serviceView?.webContents.getURL() || undefined,
    error: lastServiceError,
  };
}

function emitState(): void {
  if (mainWindow && !mainWindow.webContents.isDestroyed()) {
    mainWindow.webContents.send("desktop:state", state());
  }
}

function setUpdateState(next: Partial<DesktopUpdateState>): DesktopUpdateState {
  updateState = {
    ...updateState,
    ...next,
    currentVersion: app.getVersion(),
    channel: shellPreferences.updateChannel,
  };
  if (mainWindow && !mainWindow.webContents.isDestroyed()) {
    mainWindow.webContents.send("desktop:update", updateState);
  }
  return updateState;
}

async function checkForUpdates(): Promise<DesktopUpdateState> {
  if (!app.isPackaged) {
    return setUpdateState({
      phase: "development",
      message: "Автообновление включается в установленной версии.",
    });
  }
  if (["checking", "downloading"].includes(updateState.phase)) return updateState;
  setUpdateState({ phase: "checking", message: undefined, percent: undefined });
  try {
    await autoUpdater.checkForUpdates();
  } catch (error) {
    setUpdateState({
      phase: "error",
      message: error instanceof Error ? error.message : "Не удалось проверить обновления.",
      checkedAt: new Date().toISOString(),
    });
  }
  return updateState;
}

function configureAutoUpdater(): void {
  if (!app.isPackaged) {
    setUpdateState({ phase: "development" });
    return;
  }

  log.initialize();
  log.transports.file.level = "info";
  autoUpdater.logger = log;
  autoUpdater.setFeedURL({
    provider: "github",
    owner: "cdnserver",
    repo: "t-mod-releases",
  });
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.autoRunAppAfterInstall = true;
  autoUpdater.channel = shellPreferences.updateChannel === "dev" ? "dev" : "latest";
  autoUpdater.allowPrerelease = shellPreferences.updateChannel === "dev";
  autoUpdater.allowDowngrade = false;

  autoUpdater.on("checking-for-update", () => {
    setUpdateState({ phase: "checking", message: undefined, percent: undefined });
  });
  autoUpdater.on("update-available", (info) => {
    setUpdateState({
      phase: "available",
      version: info.version,
      percent: 0,
      message: "Новая версия найдена. Загружаем её в фоне.",
    });
  });
  autoUpdater.on("download-progress", (progress) => {
    setUpdateState({
      phase: "downloading",
      percent: Math.max(0, Math.min(100, Math.round(progress.percent))),
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    setUpdateState({
      phase: "ready",
      version: info.version,
      percent: 100,
      message: "Обновление загружено и готово к установке.",
      checkedAt: new Date().toISOString(),
    });
  });
  autoUpdater.on("update-not-available", (info) => {
    setUpdateState({
      phase: "current",
      version: info.version,
      percent: undefined,
      message: "Установлена актуальная версия T-Mod.",
      checkedAt: new Date().toISOString(),
    });
  });
  autoUpdater.on("error", (error) => {
    setUpdateState({
      phase: "error",
      message: "Автообновление временно недоступно. Можно открыть страницу загрузки.",
      checkedAt: new Date().toISOString(),
    });
  });

  setTimeout(() => void checkForUpdates(), 5_000);
  updateTimer = setInterval(() => void checkForUpdates(), UPDATE_INTERVAL_MS);
  powerMonitor.on("resume", () => void checkForUpdates());
}

function positionViews(): void {
  if (!mainWindow || !serviceView) return;
  const [width, height] = mainWindow.getContentSize();
  const sidebarWidth = shellPreferences.sidebarCollapsed
    ? SHELL_SIDEBAR_COLLAPSED_WIDTH
    : SHELL_SIDEBAR_WIDTH;
  serviceView.setBounds({
    x: sidebarWidth,
    y: SHELL_HEADER_HEIGHT,
    width: Math.max(1, width - sidebarWidth),
    height: Math.max(1, height - SHELL_HEADER_HEIGHT),
  });
}

function normalizePreferences(value: unknown): DesktopShellPreferences {
  const candidate = value && typeof value === "object"
    ? value as Partial<DesktopShellPreferences>
    : {};
  const zoom = Number(candidate.serviceZoom);
  const idleLockMinutes = Number(candidate.idleLockMinutes);
  const preferredName = typeof candidate.preferredName === "string"
    ? candidate.preferredName.trim().slice(0, 24)
    : "";
  return {
    preferredName,
    sidebarCollapsed: candidate.sidebarCollapsed === true,
    compactMode: candidate.compactMode === true,
    reduceMotion: candidate.reduceMotion === true,
    solidSurfaces: candidate.solidSurfaces === true,
    serviceZoom: [0.9, 1, 1.1].includes(zoom) ? zoom : 1,
    idleLockMinutes: [0, 5, 10, 15, 30].includes(idleLockMinutes) ? idleLockMinutes : 10,
    lockSound: candidate.lockSound !== false,
    updateChannel: candidate.updateChannel === "dev" ? "dev" : "beta",
  };
}

function applyPreferences(value: unknown): DesktopShellPreferences {
  const previousChannel = shellPreferences.updateChannel;
  shellPreferences = normalizePreferences(value);
  positionViews();
  serviceView?.webContents.setZoomFactor(shellPreferences.serviceZoom);
  if (app.isPackaged && previousChannel !== shellPreferences.updateChannel) {
    autoUpdater.channel = shellPreferences.updateChannel === "dev" ? "dev" : "latest";
    autoUpdater.allowPrerelease = shellPreferences.updateChannel === "dev";
    autoUpdater.allowDowngrade = false;
    setUpdateState({
      phase: "idle",
      version: undefined,
      percent: undefined,
      message: shellPreferences.updateChannel === "dev"
        ? "Канал Dev включён. Проверяем экспериментальные сборки."
        : "Канал Beta включён. Вы получаете только проверенные выпуски.",
    });
    setTimeout(() => void checkForUpdates(), 350);
  } else {
    setUpdateState({});
  }
  return shellPreferences;
}

function lockDesktop(reason: DesktopLockReason): boolean {
  if (desktopLocked || !mainWindow || mainWindow.isDestroyed()) return desktopLocked;
  desktopLocked = true;
  syncServiceVisibility();
  mainWindow.webContents.send("desktop:lock-requested", reason);
  mainWindow.webContents.focus();
  return true;
}

function unlockDesktop(): boolean {
  if (!desktopLocked) return true;
  desktopLocked = false;
  syncServiceVisibility();
  if (!shellOverlayOpen && serviceView?.getVisible()) serviceView.webContents.focus();
  return true;
}

function startIdleLockMonitor(): void {
  if (idleLockTimer) clearInterval(idleLockTimer);
  idleLockTimer = setInterval(() => {
    const minutes = shellPreferences.idleLockMinutes;
    if (!desktopLocked && minutes > 0 && powerMonitor.getSystemIdleTime() >= minutes * 60) {
      lockDesktop("idle");
    }
  }, 5_000);
}

function clearServiceRetry(resetAttempt = true): void {
  if (serviceRetryTimer) clearTimeout(serviceRetryTimer);
  serviceRetryTimer = undefined;
  if (resetAttempt) serviceRetryAttempt = 0;
}

function scheduleServiceRetry(code: number, description: string): boolean {
  if (!serviceView || activeService === "home") return false;
  const retryable = code >= 500 || RETRYABLE_NETWORK_ERRORS.has(code);
  const delay = SERVICE_RETRY_DELAYS[serviceRetryAttempt];
  if (!retryable || delay === undefined) return false;
  clearServiceRetry(false);
  serviceRetryAttempt += 1;
  serviceLoading = true;
  lastServiceError = undefined;
  syncServiceVisibility();
  emitState();
  serviceRetryTimer = setTimeout(() => {
    serviceRetryTimer = undefined;
    const target = serviceManifest.get(activeService as Exclude<ServiceId, "home">)?.url;
    if (!target || !serviceView) return;
    void serviceView.webContents.loadURL(target).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error || description);
      if (!scheduleServiceRetry(code, message)) {
        serviceLoading = false;
        lastServiceError = message;
        syncServiceVisibility();
        emitState();
      }
    });
  }, delay);
  return true;
}

function syncServiceVisibility(): void {
  if (!serviceView) return;
  serviceView.setVisible(
    !shellOverlayOpen &&
    !desktopLocked &&
    activeService !== "home" &&
    !lastServiceError,
  );
}

function isAtlasOverlaySettingsUrl(value: string): boolean {
  return value === ATLAS_OVERLAY_SETTINGS_URL;
}

function secureContents(contents: WebContents, options: { local: boolean }): void {
  const { local } = options;
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    if (local) return { action: "deny" };
    if (isAtlasOverlaySettingsUrl(url)) {
      mainWindow?.webContents.send("desktop:open-atlas-overlay-settings");
      return { action: "deny" };
    }
    if (isTrustedTModUrl(url)) {
      void contents.loadURL(url);
    } else if (/^https:\/\/(?:discord\.com|support\.discord\.com)\//i.test(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    if (!local && !isTrustedTModUrl(url)) {
      event.preventDefault();
    }
  });
}

async function navigate(serviceId: ServiceId): Promise<DesktopState> {
  if (!serviceView || !mainWindow) return state();
  clearServiceRetry();
  const retryAfterError = Boolean(lastServiceError);
  activeService = serviceId;
  lastServiceError = undefined;

  if (serviceId === "home") {
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
    return state();
  }

  const remote = serviceManifest.get(serviceId);
  const target = remote?.url;
  if (!remote || !remote.enabled) {
    lastServiceError = remote?.reason || "service_access_denied";
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
    return state();
  }
  if (!target || !isTrustedTModUrl(target)) {
    lastServiceError = "service_route_invalid";
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
    return state();
  }

  syncServiceVisibility();
  serviceLoading = true;
  emitState();
  const current = serviceView.webContents.getURL();
  if (current !== target || retryAfterError) {
    try {
      await serviceView.webContents.loadURL(target);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!scheduleServiceRetry(-2, message)) {
        lastServiceError = message;
        serviceLoading = false;
        syncServiceVisibility();
        emitState();
      }
    }
  } else {
    serviceLoading = false;
    emitState();
  }
  return state();
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function bootstrapUnavailable(error: string): BootstrapResult {
  if (lastSuccessfulBootstrap) {
    return {
      authenticated: true,
      online: false,
      data: lastSuccessfulBootstrap,
      error,
      lastSuccessfulAt: lastSuccessfulBootstrapAt,
    };
  }
  return { authenticated: false, online: false, error: "network_unavailable" };
}

async function applyAtlasOverlayBootstrapSafely(
  projection: DesktopBootstrap["atlas_overlay"],
): Promise<void> {
  try {
    await atlasOverlay?.applyBootstrap(projection);
  } catch (error) {
    // Atlas Overlay is optional. A local transparent-window/helper failure
    // cannot be allowed to masquerade as a network outage for all of Desktop.
    log.warn("Atlas overlay bootstrap failed without blocking Desktop", error);
  }
}

function retryableBootstrapStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

async function fetchBootstrapCandidate(): Promise<Response | undefined> {
  const requests = BOOTSTRAP_URLS.map(async (endpoint) => {
    const response = await desktopSession().fetch(endpoint, {
      method: "GET",
      cache: "no-store",
      credentials: "include",
      headers: {
        Accept: "application/json",
        "X-TMod-Desktop-Version": app.getVersion(),
      },
      signal: AbortSignal.timeout(BOOTSTRAP_TIMEOUT_MS),
    });
    // A dead contour must not delay a healthy mirror. Promise.any resolves as
    // soon as one endpoint returns an authoritative response.
    if (retryableBootstrapStatus(response.status)) {
      throw new Error(`bootstrap_http_${response.status}`);
    }
    return response;
  });
  try {
    return await Promise.any(requests);
  } catch {
    return undefined;
  }
}

async function performBootstrap(revision: number): Promise<BootstrapResult> {
  let lastError = "network_unavailable";
  for (let attempt = 0; attempt < BOOTSTRAP_WAVES; attempt += 1) {
    if (attempt > 0) await wait(attempt === 1 ? 450 : 1_250);
    try {
      const response = await fetchBootstrapCandidate();
      if (!response) continue;
      // A login/logout transaction superseded this request. Its result may be
      // returned to the old caller, but must never mutate the current session.
      const current = revision === bootstrapRevision;
      if (response.status === 401) {
        if (current) {
          lastSuccessfulBootstrap = undefined;
          clearServiceManifest();
          await applyAtlasOverlayBootstrapSafely(undefined);
        }
        return { authenticated: false, online: true, error: "login_required" };
      }
      if (!response.ok) {
        return {
          authenticated: false,
          online: true,
          error: `bootstrap_http_${response.status}`,
        };
      }
      const data = await response.json() as DesktopBootstrap;
      if (data.protocol_version !== 1 || !Array.isArray(data.services)) {
        if (current) clearServiceManifest();
        return {
          authenticated: false,
          online: true,
          error: "desktop_protocol_invalid",
        };
      }
      if (current) {
        if (!applyServiceManifest(data)) {
          clearServiceManifest();
          return {
            authenticated: false,
            online: true,
            error: "desktop_protocol_invalid",
          };
        }
        await applyAtlasOverlayBootstrapSafely(data.atlas_overlay);
        lastSuccessfulBootstrap = data;
        lastSuccessfulBootstrapAt = new Date().toISOString();
      }
      return {
        authenticated: true,
        online: true,
        data,
        lastSuccessfulAt: current ? lastSuccessfulBootstrapAt : new Date().toISOString(),
      };
    } catch {
      lastError = "network_unavailable";
    }
  }
  return bootstrapUnavailable(lastError);
}

async function bootstrap(): Promise<BootstrapResult> {
  const revision = bootstrapRevision;
  if (bootstrapInFlight?.revision === revision) return bootstrapInFlight.promise;
  const request = performBootstrap(revision);
  const entry = { revision, promise: request };
  bootstrapInFlight = entry;
  try {
    return await request;
  } finally {
    if (bootstrapInFlight === entry) bootstrapInFlight = undefined;
  }
}

function loginErrorFromLocation(location: string | null): DesktopLoginResult["error"] {
  if (!location) return undefined;
  try {
    const error = new URL(location, LOGIN_URL).searchParams.get("error");
    if (
      error === "invalid" ||
      error === "locked" ||
      error === "reset_required" ||
      error === "character_required" ||
      error === "atlas_access"
    ) {
      return error;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

async function login(credentials: DesktopLoginCredentials): Promise<DesktopLoginResult> {
  const loginValue = String(credentials?.login || "").trim();
  const pin = String(credentials?.pin || "").trim();
  if (!/^[A-Za-z0-9._-]{3,32}$/.test(loginValue) || !/^\d{8}$/.test(pin)) {
    return { ok: false, error: "invalid_input" };
  }
  // Invalidate an older periodic bootstrap before changing the session. This
  // closes the race where its delayed 401 overwrote a successful login.
  bootstrapRevision += 1;
  try {
    const form = new URLSearchParams({ login: loginValue, pin });
    let response: Response | undefined;
    for (let attempt = 0; attempt < LOGIN_ATTEMPTS; attempt += 1) {
      if (attempt > 0) await wait(attempt === 1 ? 450 : 1_250);
      try {
        const candidate = await desktopSession().fetch(
          AUTH_LOGIN_URLS[attempt % AUTH_LOGIN_URLS.length],
          {
            method: "POST",
            redirect: "manual",
            credentials: "include",
            headers: {
              Accept: "text/html,application/xhtml+xml",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            body: form.toString(),
            signal: AbortSignal.timeout(LOGIN_TIMEOUT_MS),
          },
        );
        if (retryableBootstrapStatus(candidate.status)) continue;
        response = candidate;
        break;
      } catch {
        // Retry short DNS, TLS and service-restart gaps inside this same login
        // operation so the user never has to submit the PIN twice.
      }
    }
    if (!response) return { ok: false, error: "network_unavailable" };
    if (response.status === 403) return { ok: false, error: "banned" };
    const error = loginErrorFromLocation(response.headers.get("location") || response.url);
    if (error) return { ok: false, error };
    let result: BootstrapResult | undefined;
    for (const delay of [120, 450, 1_100]) {
      await wait(delay);
      result = await bootstrap();
      if (result.authenticated) break;
    }
    if (!result?.authenticated) {
      return {
        ok: false,
        error: result?.online ? "login_failed" : "network_unavailable",
      };
    }
    activeService = "home";
    serviceLoading = false;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
    return { ok: true, bootstrap: result };
  } catch {
    return { ok: false, error: "network_unavailable" };
  }
}

async function logout(): Promise<boolean> {
  bootstrapRevision += 1;
  try {
    await desktopSession().fetch(LOGOUT_URL, {
      redirect: "manual",
      credentials: "include",
    });
  } catch {
    // The dedicated desktop partition contains only T-Mod sessions. Clearing
    // it locally still guarantees logout when the network is unavailable.
  }
  try {
    await desktopSession().clearStorageData({ storages: ["cookies"] });
  } catch {
    return false;
  }
  clearServiceManifest();
  await applyAtlasOverlayBootstrapSafely(undefined);
  lastSuccessfulBootstrap = undefined;
  lastSuccessfulBootstrapAt = undefined;
  activeService = "home";
  serviceLoading = false;
  lastServiceError = undefined;
  syncServiceVisibility();
  emitState();
  return true;
}

function registerIpc(): void {
  const trusted = (event: Electron.IpcMainInvokeEvent): boolean =>
    Boolean(mainWindow && event.sender.id === mainWindow.webContents.id);
  const trustedOverlay = (event: Electron.IpcMainInvokeEvent): boolean =>
    Boolean(atlasOverlay?.ownsSender(event.sender.id));
  const trustedOverlayOrShell = (event: Electron.IpcMainInvokeEvent): boolean =>
    trusted(event) || trustedOverlay(event);

  ipcMain.handle("desktop:bootstrap", (event) =>
    trusted(event)
      ? bootstrap()
      : ({ authenticated: false, online: false, error: "untrusted_sender" } satisfies BootstrapResult),
  );
  ipcMain.handle("desktop:login", (event, credentials: DesktopLoginCredentials) =>
    trusted(event)
      ? login(credentials)
      : ({ ok: false, error: "login_failed" } satisfies DesktopLoginResult),
  );
  ipcMain.handle("desktop:logout", (event) => trusted(event) ? logout() : false);
  ipcMain.handle("desktop:navigate", (event, serviceId: unknown) => {
    if (!trusted(event) || !isServiceId(serviceId)) return state();
    return navigate(serviceId);
  });
  ipcMain.handle("desktop:shell-overlay", (event, open: unknown) => {
    if (!trusted(event)) return;
    shellOverlayOpen = open === true;
    syncServiceVisibility();
    if (shellOverlayOpen) mainWindow?.webContents.focus();
    else if (serviceView?.getVisible()) serviceView.webContents.focus();
  });
  ipcMain.handle("desktop:preferences", (event, preferences: unknown) =>
    trusted(event) ? applyPreferences(preferences) : DEFAULT_PREFERENCES,
  );
  ipcMain.handle("desktop:copy-current-link", (event) => {
    if (!trusted(event) || !serviceView) return false;
    const url = serviceView.webContents.getURL();
    if (!isTrustedTModUrl(url)) return false;
    clipboard.writeText(url);
    return true;
  });
  ipcMain.handle("desktop:open-current-link", (event) => {
    if (!trusted(event) || !serviceView) return false;
    const url = serviceView.webContents.getURL();
    if (!isTrustedTModUrl(url)) return false;
    void shell.openExternal(url);
    return true;
  });
  ipcMain.handle("desktop:lock", (event) => trusted(event) ? lockDesktop("manual") : false);
  ipcMain.handle("desktop:unlock", (event) => trusted(event) ? unlockDesktop() : false);
  ipcMain.handle("desktop:open-login", async (event) => {
    if (!trusted(event) || !serviceView) return state();
    clearServiceRetry();
    activeService = "reactor";
    syncServiceVisibility();
    serviceLoading = true;
    emitState();
    await serviceView.webContents.loadURL(LOGIN_URL);
    return state();
  });
  ipcMain.handle("desktop:reload", (event) => {
    if (!trusted(event) || !serviceView) return;
    clearServiceRetry();
    lastServiceError = undefined;
    serviceLoading = true;
    syncServiceVisibility();
    emitState();
    serviceView.webContents.reload();
  });
  ipcMain.handle("desktop:back", (event) => {
    if (trusted(event) && serviceView?.webContents.navigationHistory.canGoBack()) {
      serviceView.webContents.navigationHistory.goBack();
    }
  });
  ipcMain.handle("desktop:forward", (event) => {
    if (trusted(event) && serviceView?.webContents.navigationHistory.canGoForward()) {
      serviceView.webContents.navigationHistory.goForward();
    }
  });
  ipcMain.handle("desktop:minimize", (event) => {
    if (trusted(event)) mainWindow?.minimize();
  });
  ipcMain.handle("desktop:maximize", (event) => {
    if (!trusted(event) || !mainWindow) return;
    mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
  });
  ipcMain.handle("desktop:close", (event) => {
    if (trusted(event)) mainWindow?.close();
  });
  ipcMain.handle("desktop:update-check", (event) =>
    trusted(event) ? checkForUpdates() : updateState,
  );
  ipcMain.handle("desktop:update-install", (event) => {
    if (!trusted(event) || updateState.phase !== "ready") return false;
    setImmediate(() => autoUpdater.quitAndInstall(false, true));
    return true;
  });
  ipcMain.handle("desktop:update-open-release", (event) => {
    if (!trusted(event)) return false;
    void shell.openExternal(RELEASE_URL);
    return true;
  });
  ipcMain.handle("atlas-overlay:get-config", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getConfig() : undefined,
  );
  ipcMain.handle("atlas-overlay:get-catalog", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getCatalog() : undefined,
  );
  ipcMain.handle("atlas-overlay:get-voices", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getVoices() : undefined,
  );
  ipcMain.handle("atlas-overlay:preview-voice", (event, voice: unknown) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.previewVoice(String(voice || "")) : undefined,
  );
  ipcMain.handle("atlas-overlay:save-config", (event, patch: unknown) => {
    if (!trustedOverlayOrShell(event) || !atlasOverlay || !patch || typeof patch !== "object") {
      return undefined;
    }
    return atlasOverlay.saveConfig(patch);
  });
  ipcMain.handle("atlas-overlay:move-by", (event, deltaX: unknown, deltaY: unknown) => {
    if (!trustedOverlayOrShell(event) || !atlasOverlay) return undefined;
    return atlasOverlay.moveBy(Number(deltaX), Number(deltaY));
  });
  ipcMain.handle("atlas-overlay:submit-audio", (event, input: unknown) =>
    trustedOverlayOrShell(event) && atlasOverlay
      ? atlasOverlay.submitAudio(input as Parameters<AtlasOverlayController["submitAudio"]>[0])
      : ({ accepted: false, error: "untrusted_sender" }),
  );
  ipcMain.handle("atlas-overlay:submit-text", (event, question: unknown) =>
    trustedOverlayOrShell(event) && atlasOverlay
      ? atlasOverlay.submitText(String(question || ""))
      : ({ accepted: false, error: "untrusted_sender" }),
  );
  ipcMain.handle("atlas-overlay:cancel", (event) => {
    if (trustedOverlayOrShell(event)) atlasOverlay?.cancel();
  });
  ipcMain.handle("atlas-overlay:hide", (event) => {
    if (trustedOverlayOrShell(event)) atlasOverlay?.hide();
  });
  ipcMain.handle("atlas-overlay:open-atlas", (event) => {
    if (trustedOverlayOrShell(event)) return atlasOverlay?.openAtlas();
  });
  ipcMain.handle("atlas-overlay:report-speech", (event, active: unknown) => {
    if (trustedOverlay(event)) atlasOverlay?.reportSpeech(active === true);
  });
}

async function createWindow(): Promise<void> {
  log.info("Creating T-Mod desktop window");
  const { workArea } = screen.getPrimaryDisplay();
  const width = Math.min(workArea.width, Math.max(960, Math.round(workArea.width * .96)));
  const height = Math.min(workArea.height, Math.max(640, Math.round(workArea.height * .94)));
  mainWindow = new BrowserWindow({
    x: workArea.x + Math.round((workArea.width - width) / 2),
    y: workArea.y + Math.round((workArea.height - height) / 2),
    width,
    height,
    minWidth: 960,
    minHeight: 640,
    show: false,
    frame: false,
    backgroundColor: "#07090f",
    title: "T-Mod",
    webPreferences: {
      preload: path.join(bundleDirectory, "../preload/index.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });
  serviceView = new WebContentsView({
    webPreferences: {
      partition: DESKTOP_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
    },
  });

  secureContents(mainWindow.webContents, { local: true });
  secureContents(serviceView.webContents, { local: false });
  serviceView.webContents.setUserAgent(
    `${serviceView.webContents.getUserAgent()} TModDesktop/${app.getVersion()}`,
  );
  const networkSession = desktopSession();
  const desktopUserAgent = `${networkSession.getUserAgent()} TModDesktop/${app.getVersion()}`;
  networkSession.setUserAgent(desktopUserAgent);
  networkSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  networkSession.setPermissionCheckHandler(() => false);

  atlasOverlay = new AtlasOverlayController({
    networkSession: desktopSession,
    preloadPath: path.join(bundleDirectory, "../preload/overlay.cjs"),
    rendererUrl: process.env.ELECTRON_RENDERER_URL
      ? new URL("overlay.html", `${process.env.ELECTRON_RENDERER_URL}/`).toString()
      : undefined,
    rendererFile: path.join(bundleDirectory, "../renderer/overlay.html"),
    userDataPath: app.getPath("userData"),
    hotkeyHelperPath: app.isPackaged
      ? path.join(process.resourcesPath, "atlas-overlay-hotkey.ps1")
      : path.join(app.getAppPath(), "resources", "atlas-overlay-hotkey.ps1"),
    onLog: (message, details) => log.warn(message, details),
  });
  const localSession = session.defaultSession;
  localSession.setPermissionCheckHandler((webContents, permission) =>
    permission === "media" && Boolean(
      webContents && (
        atlasOverlay?.ownsSender(webContents.id) ||
        mainWindow?.webContents.id === webContents.id
      ),
    ),
  );
  localSession.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const mediaTypes = "mediaTypes" in details && Array.isArray(details.mediaTypes)
      ? details.mediaTypes
      : [];
    const microphoneOnly = !mediaTypes.length || (
      mediaTypes.includes("audio") && !mediaTypes.includes("video")
    );
    callback(
      permission === "media" &&
      microphoneOnly &&
      Boolean(
        atlasOverlay?.ownsSender(webContents.id) ||
        mainWindow?.webContents.id === webContents.id
      ),
    );
  });
  try {
    await atlasOverlay.initialize();
  } catch (error) {
    // The overlay is an optional companion surface. If its transparent window,
    // renderer or hotkey helper cannot start on a particular machine, keep the
    // authenticated Desktop shell usable and allow the user to retry next run.
    log.warn("Atlas overlay initialization failed without blocking Desktop", error);
    try {
      atlasOverlay.dispose();
    } catch (disposeError) {
      log.warn("Atlas overlay cleanup failed", disposeError);
    }
    atlasOverlay = undefined;
  }

  mainWindow.contentView.addChildView(serviceView);
  syncServiceVisibility();
  positionViews();

  serviceView.webContents.on("did-start-loading", () => {
    lastMainFrameHttpStatus = 0;
    serviceLoading = true;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-stop-loading", () => {
    if (serviceRetryTimer) return;
    if (lastMainFrameHttpStatus >= 500) {
      if (scheduleServiceRetry(lastMainFrameHttpStatus, `HTTP ${lastMainFrameHttpStatus}`)) return;
      serviceLoading = false;
      lastServiceError = `service_http_${lastMainFrameHttpStatus}`;
      syncServiceVisibility();
      emitState();
      return;
    }
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
    if (isTrustedTModUrl(serviceView?.webContents.getURL() || "")) {
      mainWindow?.webContents.send("desktop:auth-changed");
    }
  });
  serviceView.webContents.on("did-finish-load", () => {
    if (lastMainFrameHttpStatus >= 500) return;
    clearServiceRetry();
    serviceLoading = false;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-fail-load", (_event, code, description, _url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    if (scheduleServiceRetry(code, description)) return;
    serviceLoading = false;
    lastServiceError = description || `load_error_${code}`;
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-navigate", (_event, _url, httpResponseCode) => {
    lastMainFrameHttpStatus = Number(httpResponseCode || 0);
    emitState();
  });
  serviceView.webContents.on("did-navigate-in-page", emitState);
  serviceView.webContents.on("before-input-event", (event, input) => {
    if ((input.control || input.meta) && input.key.toLowerCase() === "k") {
      event.preventDefault();
      mainWindow?.webContents.send("desktop:command-palette");
    }
  });

  mainWindow.on("resize", positionViews);
  mainWindow.on("maximize", positionViews);
  mainWindow.on("unmaximize", positionViews);
  mainWindow.on("closed", () => {
    clearServiceRetry();
    if (serviceView && !serviceView.webContents.isDestroyed()) {
      serviceView.webContents.close();
    }
    serviceView = null;
    mainWindow = null;
    shellOverlayOpen = false;
    desktopLocked = false;
    atlasOverlay?.dispose();
    atlasOverlay = undefined;
  });

  const rendererUrl = process.env.ELECTRON_RENDERER_URL;
  if (rendererUrl) {
    await mainWindow.loadURL(rendererUrl);
  } else {
    await mainWindow.loadFile(path.join(bundleDirectory, "../renderer/index.html"));
  }
  log.info("T-Mod desktop shell loaded");
  mainWindow.show();
  mainWindow.focus();
  log.info("T-Mod desktop window shown");
}

const ownsInstanceLock = app.requestSingleInstanceLock();
if (!ownsInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

app.whenReady().then(async () => {
  if (!ownsInstanceLock) return;
  app.setAppUserModelId("lat.tvr.tmod.desktop");
  app.setAsDefaultProtocolClient("tmod");
  registerIpc();
  configureAutoUpdater();
  await createWindow();
  screen.on("display-added", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-removed", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-metrics-changed", () => atlasOverlay?.onDisplaysChanged());
  startIdleLockMonitor();
  setUpdateState({});

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void createWindow();
  });
}).catch((error: unknown) => {
  const message = error instanceof Error ? `${error.name}: ${error.message}\n${error.stack || ""}` : String(error);
  log.error("T-Mod startup failed", message);
  console.error("T-Mod startup failed", message);
  dialog.showErrorBox(
    "T-Mod не удалось запустить",
    "Приложение не смогло открыть главное окно. Переустановите последнюю версию или отправьте журнал разработчику.",
  );
  app.quit();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (updateTimer) clearInterval(updateTimer);
  if (idleLockTimer) clearInterval(idleLockTimer);
  atlasOverlay?.dispose();
});
