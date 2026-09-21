import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  nativeTheme,
  Notification,
  powerMonitor,
  safeStorage,
  screen,
  session,
  shell,
  WebContentsView,
} from "electron";
import type { WebContents } from "electron";
import { fileURLToPath } from "node:url";
import { createHash, randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
import { chmod, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import log from "electron-log/main";
import electronUpdater from "electron-updater";
import { AtlasOverlayController } from "./atlas-overlay-controller";
import {
  isServiceId,
  resolveNotificationServiceId,
  isTModAuthenticationUrl,
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
import { desktopProduct } from "../shared/product";

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
const DESKTOP_PARTITION = desktopProduct.partition;
const ACCOUNT_SESSION_COOKIE = "tmod_account_session";
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
const RELEASE_URL = desktopProduct.releaseUrl;
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
  notificationDelivery: "both",
  notificationSound: true,
  updateChannel: desktopProduct.updateChannel,
};

let mainWindow: BrowserWindow | null = null;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let shellOverlayOpen = false;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let forcedUpdateInstallTimer: ReturnType<typeof setTimeout> | undefined;
let idleLockTimer: ReturnType<typeof setInterval> | undefined;
let desktopLocked = false;
let serviceManifest = new Map<Exclude<ServiceId, "home">, DesktopService>();
let lastSuccessfulBootstrap: DesktopBootstrap | undefined;
let lastKnownBan: BootstrapResult["ban"];
let bootstrapRevision = 0;
let bootstrapInFlight: {
  revision: number;
  promise: Promise<BootstrapResult>;
} | undefined;
let lastSuccessfulBootstrapAt: string | undefined;
let shellPreferences = { ...DEFAULT_PREFERENCES };
let serviceRetryAttempt = 0;
let serviceRetryTimer: ReturnType<typeof setTimeout> | undefined;
let authProjectionTimer: ReturnType<typeof setTimeout> | undefined;
let authCookieObserverInstalled = false;
let desktopInstallToken = "";
let desktopDeviceFingerprint = "";
let lastMainFrameHttpStatus = 0;
let atlasOverlay: AtlasOverlayController | undefined;
let updateState: DesktopUpdateState = {
  phase: app.isPackaged ? "idle" : "development",
  currentVersion: app.getVersion(),
  channel: desktopProduct.updateChannel,
};
let forceUpdateRequired = false;
let notificationStreamInitialized = false;
const knownNotificationIds = new Set<number>();

app.enableSandbox();
nativeTheme.themeSource = "dark";

function desktopSession() {
  return session.fromPartition(DESKTOP_PARTITION, { cache: true });
}

const INSTALL_TOKEN_PATTERN = /^[A-Za-z0-9_-]{43,128}$/;

async function loadOrCreateDesktopInstallToken(): Promise<string> {
  const directory = app.getPath("userData");
  const destination = path.join(directory, desktopProduct.installationFile);
  try {
    const record = JSON.parse(await readFile(destination, "utf8")) as {
      protected?: boolean;
      value?: string;
    };
    const stored = String(record.value || "");
    const token = record.protected
      ? safeStorage.decryptString(Buffer.from(stored, "base64"))
      : Buffer.from(stored, "base64").toString("utf8");
    if (INSTALL_TOKEN_PATTERN.test(token)) return token;
  } catch {
    // Missing, damaged or no longer decryptable local state is replaced with
    // a new random installation credential. No hardware identifier is read.
  }
  const token = randomBytes(32).toString("base64url");
  const protect = safeStorage.isEncryptionAvailable();
  const value = protect
    ? safeStorage.encryptString(token).toString("base64")
    : Buffer.from(token, "utf8").toString("base64");
  try {
    await mkdir(directory, { recursive: true });
    const temporary = `${destination}.${process.pid}.tmp`;
    await writeFile(
      temporary,
      JSON.stringify({ version: 1, protected: protect, value }),
      { encoding: "utf8", mode: 0o600 },
    );
    await rename(temporary, destination);
    await chmod(destination, 0o600).catch(() => undefined);
  } catch (error) {
    // A read-only userData folder must not prevent the desktop shell starting.
    // The device fingerprint still enforces bans across installation resets.
    log.warn("Desktop installation credential could not be persisted", error);
  }
  return token;
}

function execFileText(command: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      command,
      args,
      { encoding: "utf8", timeout: 4_000, windowsHide: true, maxBuffer: 64 * 1024 },
      (error, stdout) => error ? reject(error) : resolve(String(stdout || "")),
    );
  });
}

async function stableSystemIdentifier(): Promise<string> {
  if (process.platform === "win32") {
    const output = await execFileText("reg.exe", [
      "QUERY",
      "HKLM\\SOFTWARE\\Microsoft\\Cryptography",
      "/v",
      "MachineGuid",
    ]);
    const match = output.match(/MachineGuid\s+REG_\w+\s+([^\r\n]+)/i);
    if (match?.[1]) return match[1].trim();
  } else if (process.platform === "darwin") {
    const output = await execFileText("/usr/sbin/ioreg", [
      "-rd1",
      "-c",
      "IOPlatformExpertDevice",
    ]);
    const match = output.match(/"IOPlatformUUID"\s*=\s*"([^"]+)"/i);
    if (match?.[1]) return match[1].trim();
  } else {
    const machineId = (await readFile("/etc/machine-id", "utf8")).trim();
    if (machineId) return machineId;
  }
  throw new Error("stable_system_identifier_unavailable");
}

async function buildDesktopDeviceFingerprint(): Promise<string> {
  try {
    const systemIdentifier = await stableSystemIdentifier();
    return createHash("sha256")
      .update(`tmod-desktop-hwid-v1\0${process.platform}\0${systemIdentifier}`, "utf8")
      .digest("hex");
  } catch (error) {
    // Never mislabel an installation identifier as a machine binding.
    log.warn("Stable system identifier unavailable; using installation binding", error);
    return "";
  }
}

function desktopIdentityHeaders(): Record<string, string> {
  return desktopInstallToken
    ? {
        "X-TMod-Install-Token": desktopInstallToken,
        "X-TMod-Desktop-Platform": process.platform,
        "X-TMod-Desktop-Version": app.getVersion(),
        "X-TMod-Desktop-Edition": desktopProduct.edition,
        ...(desktopDeviceFingerprint
          ? { "X-TMod-Device-Fingerprint": desktopDeviceFingerprint }
          : {}),
      }
    : {
        "X-TMod-Desktop-Version": app.getVersion(),
        "X-TMod-Desktop-Edition": desktopProduct.edition,
      };
}

function scheduleAuthProjectionRefresh(options: { sessionRemoved?: boolean } = {}): void {
  if (options.sessionRemoved) {
    bootstrapRevision += 1;
    lastSuccessfulBootstrap = undefined;
    lastSuccessfulBootstrapAt = undefined;
    notificationStreamInitialized = false;
    knownNotificationIds.clear();
    clearServiceManifest();
    void applyAtlasOverlayBootstrapSafely(undefined);
    activeService = "home";
    serviceLoading = false;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
  }
  if (authProjectionTimer) clearTimeout(authProjectionTimer);
  authProjectionTimer = setTimeout(() => {
    authProjectionTimer = undefined;
    if (mainWindow && !mainWindow.webContents.isDestroyed()) {
      mainWindow.webContents.send("desktop:auth-changed");
    }
  }, options.sessionRemoved ? 0 : 180);
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

function reconcileActiveServiceAccess(): void {
  if (activeService === "home") return;
  const access = serviceManifest.get(activeService as Exclude<ServiceId, "home">);
  if (access?.enabled) return;
  clearServiceRetry();
  activeService = "home";
  serviceLoading = false;
  lastServiceError = undefined;
  syncServiceVisibility();
  emitState();
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

function applyClientUpdatePolicy(data: DesktopBootstrap): void {
  const policy = data.client_update;
  forceUpdateRequired = Boolean(policy?.required);
  setUpdateState({
    required: forceUpdateRequired,
    minimumVersion: policy?.minimum_version || undefined,
    releaseUrl: policy?.release_url || RELEASE_URL,
    message: forceUpdateRequired
      ? policy?.message || `Для продолжения требуется обновление ${desktopProduct.fullName}.`
      : updateState.message,
  });
  if (forceUpdateRequired) void checkForUpdates();
}

async function checkForUpdates(): Promise<DesktopUpdateState> {
  if (desktopProduct.privateEdition) {
    return setUpdateState({
      phase: "current",
      message: "Приватная редакция получает только персонально опубликованные сборки.",
      checkedAt: new Date().toISOString(),
    });
  }
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
  if (desktopProduct.privateEdition) {
    setUpdateState({
      phase: "current",
      message: "BLACKBIRD подключён к закрытому каналу выпусков.",
      checkedAt: new Date().toISOString(),
    });
    return;
  }
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
    if (forceUpdateRequired && !forcedUpdateInstallTimer) {
      forcedUpdateInstallTimer = setTimeout(() => {
        forcedUpdateInstallTimer = undefined;
        autoUpdater.quitAndInstall(false, true);
      }, 2_500);
    }
  });
  autoUpdater.on("update-not-available", (info) => {
    if (forceUpdateRequired) {
      setUpdateState({
        phase: "error",
        version: info.version,
        percent: undefined,
        message: "Обязательная версия ещё не найдена в канале обновлений. Откройте установщик.",
        checkedAt: new Date().toISOString(),
      });
      return;
    }
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
  powerMonitor.on("resume", () => {
    void checkForUpdates();
    // A suspended laptop can wake after the account session or section grants
    // changed. Refresh the shared projection immediately instead of waiting
    // for the renderer's periodic poll.
    scheduleAuthProjectionRefresh();
  });
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
    notificationDelivery: ["both", "in-app", "system", "off"].includes(String(candidate.notificationDelivery))
      ? candidate.notificationDelivery as DesktopShellPreferences["notificationDelivery"]
      : "both",
    notificationSound: candidate.notificationSound !== false,
    updateChannel: desktopProduct.privateEdition
      ? "private"
      : candidate.updateChannel === "dev" ? "dev" : "beta",
  };
}

function syncNativeNotifications(items: DesktopBootstrap["notifications"]["items"]): void {
  const currentIds = new Set(items.map((item) => item.id));
  if (!notificationStreamInitialized) {
    currentIds.forEach((id) => knownNotificationIds.add(id));
    notificationStreamInitialized = true;
    return;
  }
  const delivery = shellPreferences.notificationDelivery;
  for (const item of items.slice().reverse()) {
    if (knownNotificationIds.has(item.id)) continue;
    knownNotificationIds.add(item.id);
    if (!["both", "system"].includes(delivery) || !Notification.isSupported()) continue;
    const notification = new Notification({
      title: item.title,
      body: item.body,
      silent: !shellPreferences.notificationSound,
      urgency: item.severity === "critical" ? "critical" : "normal",
    });
    notification.on("click", () => {
      const target = resolveNotificationServiceId(item.route);
      if (target) void navigate(target);
      mainWindow?.show();
      mainWindow?.focus();
    });
    notification.show();
  }
  for (const id of [...knownNotificationIds]) {
    if (!currentIds.has(id) && knownNotificationIds.size > 500) knownNotificationIds.delete(id);
  }
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
  if (lastKnownBan) {
    return {
      authenticated: false,
      online: false,
      error: "globally_banned",
      ban: lastKnownBan,
    };
  }
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
        ...desktopIdentityHeaders(),
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
    const never = new Promise<Response>(() => undefined);
    const healthy = Promise.any(
      requests.map(async (request) => {
        const response = await request;
        if (!response.ok) throw new Error(`bootstrap_non_success_${response.status}`);
        return response;
      }),
    ).catch(() => never);
    const fallback = Promise.any(requests).then(async (response) => {
      // During a rolling restart one contour can briefly reject a valid shared
      // session while its healthy mirror already accepts it. Give a successful
      // mirror a short priority window without making real login failures slow.
      if (!response.ok) await wait(400);
      return response;
    });
    return await Promise.race([healthy, fallback]);
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
          lastKnownBan = undefined;
          lastSuccessfulBootstrap = undefined;
          clearServiceManifest();
          reconcileActiveServiceAccess();
          await applyAtlasOverlayBootstrapSafely(undefined);
        }
        return { authenticated: false, online: true, error: "login_required" };
      }
      if (response.status === 403 && desktopProduct.privateEdition) {
        if (current) {
          lastSuccessfulBootstrap = undefined;
          lastSuccessfulBootstrapAt = undefined;
          clearServiceManifest();
          reconcileActiveServiceAccess();
          await applyAtlasOverlayBootstrapSafely(undefined);
        }
        return {
          authenticated: false,
          online: true,
          error: "private_access_required",
        };
      }
      if (response.status === 423) {
        let decision = { reason: "Решение администратора T-Mod.", reference: "—" };
        try {
          const payload = await response.json() as Partial<typeof decision>;
          decision = {
            reason: String(payload.reason || decision.reason),
            reference: String(payload.reference || decision.reference),
          };
        } catch {
          // A valid 423 is authoritative even if a proxy stripped its body.
        }
        if (current) {
          lastKnownBan = decision;
          lastSuccessfulBootstrap = undefined;
          lastSuccessfulBootstrapAt = undefined;
          clearServiceManifest();
          activeService = "home";
          serviceLoading = false;
          lastServiceError = undefined;
          syncServiceVisibility();
          await applyAtlasOverlayBootstrapSafely(undefined);
          emitState();
        }
        return {
          authenticated: false,
          online: true,
          error: "globally_banned",
          ban: decision,
        };
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
        if (current) {
          clearServiceManifest();
          reconcileActiveServiceAccess();
        }
        return {
          authenticated: false,
          online: true,
          error: "desktop_protocol_invalid",
        };
      }
      if (current) {
        applyClientUpdatePolicy(data);
        lastKnownBan = undefined;
        if (!applyServiceManifest(data)) {
          clearServiceManifest();
          reconcileActiveServiceAccess();
          return {
            authenticated: false,
            online: true,
            error: "desktop_protocol_invalid",
          };
        }
        reconcileActiveServiceAccess();
        await applyAtlasOverlayBootstrapSafely(data.atlas_overlay);
        lastSuccessfulBootstrap = data;
        lastSuccessfulBootstrapAt = new Date().toISOString();
        syncNativeNotifications(data.notifications.items || []);
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
              ...desktopIdentityHeaders(),
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
    if (response.status === 403 || response.status === 423) {
      if (response.status === 423) await bootstrap();
      if (response.status === 403 && desktopProduct.privateEdition) {
        return { ok: false, error: "private_access_required" };
      }
      return { ok: false, error: "banned" };
    }
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
        error: result?.error === "private_access_required"
          ? "private_access_required"
          : result?.online ? "login_failed" : "network_unavailable",
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
    void shell.openExternal(updateState.releaseUrl || RELEASE_URL);
    return true;
  });
  ipcMain.handle("atlas-overlay:get-config", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getConfig() : undefined,
  );
  ipcMain.handle("atlas-overlay:get-catalog", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getCatalog() : undefined,
  );
  ipcMain.handle("atlas-overlay:get-status", (event) =>
    trustedOverlayOrShell(event) ? atlasOverlay?.getStatus() : undefined,
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
  log.info(`Creating ${desktopProduct.fullName} window`);
  desktopInstallToken = await loadOrCreateDesktopInstallToken();
  desktopDeviceFingerprint = await buildDesktopDeviceFingerprint();
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
    title: desktopProduct.fullName,
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
    `${serviceView.webContents.getUserAgent()} TModDesktop/${app.getVersion()} ${desktopProduct.name}/${app.getVersion()}`,
  );
  const networkSession = desktopSession();
  const desktopUserAgent = `${networkSession.getUserAgent()} TModDesktop/${app.getVersion()} ${desktopProduct.name}/${app.getVersion()}`;
  networkSession.setUserAgent(desktopUserAgent);
  networkSession.webRequest.onBeforeSendHeaders(
    { urls: ["https://tvr.lat/*", "https://*.tvr.lat/*"] },
    (details, callback) => {
      callback({
        requestHeaders: {
          ...details.requestHeaders,
          ...desktopIdentityHeaders(),
        },
      });
    },
  );
  networkSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  networkSession.setPermissionCheckHandler(() => false);
  if (!authCookieObserverInstalled) {
    authCookieObserverInstalled = true;
    networkSession.cookies.on("changed", (_event, cookie, cause, removed) => {
      if (cookie.name !== ACCOUNT_SESSION_COOKIE) return;
      const domain = String(cookie.domain || "").replace(/^\./, "").toLowerCase();
      if (domain !== "tvr.lat" && !domain.endsWith(".tvr.lat")) return;
      // All service WebContents share this partition. Cookie changes are the
      // authoritative signal for login, logout and expiry across every contour.
      // An overwrite is a renewal, not a logout.
      atlasOverlay?.invalidateAccountSession();
      scheduleAuthProjectionRefresh({ sessionRemoved: removed && cause !== "overwrite" });
    });
  }

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
  serviceView.webContents.on("did-navigate", (_event, url, httpResponseCode) => {
    lastMainFrameHttpStatus = Number(httpResponseCode || 0);
    if (
      lastMainFrameHttpStatus === 401 ||
      lastMainFrameHttpStatus === 423 ||
      isTModAuthenticationUrl(url)
    ) {
      scheduleAuthProjectionRefresh();
    }
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
  log.info(`${desktopProduct.fullName} shell loaded`);
  mainWindow.show();
  mainWindow.focus();
  log.info(`${desktopProduct.fullName} window shown`);
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
  app.setAppUserModelId(desktopProduct.appId);
  app.setAsDefaultProtocolClient(desktopProduct.protocol);
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
  log.error(`${desktopProduct.fullName} startup failed`, message);
  console.error(`${desktopProduct.fullName} startup failed`, message);
  dialog.showErrorBox(
    `${desktopProduct.name} не удалось запустить`,
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
  if (authProjectionTimer) clearTimeout(authProjectionTimer);
  if (forcedUpdateInstallTimer) clearTimeout(forcedUpdateInstallTimer);
  atlasOverlay?.dispose();
});
