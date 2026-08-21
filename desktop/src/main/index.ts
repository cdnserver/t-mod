import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  nativeTheme,
  powerMonitor,
  session,
  shell,
  WebContentsView,
} from "electron";
import type { WebContents } from "electron";
import { fileURLToPath } from "node:url";
import path from "node:path";
import log from "electron-log/main";
import electronUpdater from "electron-updater";
import {
  isServiceId,
  isTrustedTModUrl,
} from "../shared/services";
import type {
  BootstrapResult,
  DesktopBootstrap,
  DesktopLoginCredentials,
  DesktopLoginResult,
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
const LOGOUT_URL = "https://tvr.lat/logout";
const RELEASE_URL = "https://github.com/cdnserver/t-mod-releases/releases/latest";
const UPDATE_INTERVAL_MS = 30 * 60 * 1_000;
const BOOTSTRAP_ATTEMPTS = 4;
const BOOTSTRAP_TIMEOUT_MS = 12_000;
const SERVICE_RETRY_DELAYS = [700, 1_800, 4_000] as const;
const RETRYABLE_NETWORK_ERRORS = new Set([-2, -7, -21, -101, -102, -105, -106, -118, -324]);
const DEFAULT_PREFERENCES: DesktopShellPreferences = {
  sidebarCollapsed: false,
  compactMode: false,
  reduceMotion: false,
  solidSurfaces: false,
  serviceZoom: 1,
};

let mainWindow: BrowserWindow | null = null;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let shellOverlayOpen = false;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let serviceManifest = new Map<Exclude<ServiceId, "home">, DesktopService>();
let lastSuccessfulBootstrap: DesktopBootstrap | undefined;
let bootstrapInFlight: Promise<BootstrapResult> | undefined;
let lastSuccessfulBootstrapAt: string | undefined;
let shellPreferences = { ...DEFAULT_PREFERENCES };
let serviceRetryAttempt = 0;
let serviceRetryTimer: ReturnType<typeof setTimeout> | undefined;
let lastMainFrameHttpStatus = 0;
let updateState: DesktopUpdateState = {
  phase: app.isPackaged ? "idle" : "development",
  currentVersion: app.getVersion(),
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
  autoUpdater.allowPrerelease = false;

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
  return {
    sidebarCollapsed: candidate.sidebarCollapsed === true,
    compactMode: candidate.compactMode === true,
    reduceMotion: candidate.reduceMotion === true,
    solidSurfaces: candidate.solidSurfaces === true,
    serviceZoom: [0.9, 1, 1.1].includes(zoom) ? zoom : 1,
  };
}

function applyPreferences(value: unknown): DesktopShellPreferences {
  shellPreferences = normalizePreferences(value);
  positionViews();
  serviceView?.webContents.setZoomFactor(shellPreferences.serviceZoom);
  return shellPreferences;
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
    activeService !== "home" &&
    !lastServiceError,
  );
}

function secureContents(contents: WebContents, options: { local: boolean }): void {
  const { local } = options;
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    if (local) return { action: "deny" };
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

async function performBootstrap(): Promise<BootstrapResult> {
  let lastError = "network_unavailable";
  for (let attempt = 0; attempt < BOOTSTRAP_ATTEMPTS; attempt += 1) {
    if (attempt > 0) await wait(attempt === 1 ? 350 : attempt === 2 ? 1_000 : 2_200);
    try {
      const endpoint = BOOTSTRAP_URLS[attempt % BOOTSTRAP_URLS.length];
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
      if (response.status === 401) {
        lastSuccessfulBootstrap = undefined;
        clearServiceManifest();
        return { authenticated: false, online: true, error: "login_required" };
      }
      if (
        response.status === 408 ||
        response.status === 425 ||
        response.status === 429 ||
        response.status >= 500
      ) {
        lastError = `bootstrap_http_${response.status}`;
        continue;
      }
      if (!response.ok) {
        return {
          authenticated: false,
          online: true,
          error: `bootstrap_http_${response.status}`,
        };
      }
      const data = await response.json() as DesktopBootstrap;
      if (!applyServiceManifest(data)) {
        clearServiceManifest();
        return {
          authenticated: false,
          online: true,
          error: "desktop_protocol_invalid",
        };
      }
      lastSuccessfulBootstrap = data;
      lastSuccessfulBootstrapAt = new Date().toISOString();
      return {
        authenticated: true,
        online: true,
        data,
        lastSuccessfulAt: lastSuccessfulBootstrapAt,
      };
    } catch {
      lastError = "network_unavailable";
    }
  }
  return bootstrapUnavailable(lastError);
}

async function bootstrap(): Promise<BootstrapResult> {
  if (bootstrapInFlight) return bootstrapInFlight;
  const request = performBootstrap();
  bootstrapInFlight = request;
  try {
    return await request;
  } finally {
    if (bootstrapInFlight === request) bootstrapInFlight = undefined;
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
  try {
    const form = new URLSearchParams({ login: loginValue, pin });
    const response = await desktopSession().fetch(AUTH_LOGIN_URL, {
      method: "POST",
      redirect: "manual",
      credentials: "include",
      headers: {
        Accept: "text/html,application/xhtml+xml",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: form.toString(),
    });
    if (response.status === 403) return { ok: false, error: "banned" };
    if (response.status >= 500) return { ok: false, error: "network_unavailable" };
    const error = loginErrorFromLocation(response.headers.get("location") || response.url);
    if (error) return { ok: false, error };
    const result = await bootstrap();
    if (!result.authenticated) return { ok: false, error: "login_failed" };
    activeService = "home";
    serviceLoading = false;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
    return { ok: true };
  } catch {
    return { ok: false, error: "network_unavailable" };
  }
}

async function logout(): Promise<boolean> {
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
}

async function createWindow(): Promise<void> {
  log.info("Creating T-Mod desktop window");
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1080,
    minHeight: 700,
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
  networkSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  networkSession.setPermissionCheckHandler(() => false);

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
});
