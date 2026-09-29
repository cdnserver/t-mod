import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
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
import { NotificationPopup } from "./notification-popup";
import { notificationChannel } from "../shared/notification-policy";
import { csrfTokenFromAccountSnapshot } from "../shared/account-response";
import { BootstrapProtocolError, isFreshLoginProjection, loginPayloadError, parseBootstrapResponse, selectBootstrapCandidate } from "../shared/auth-network";
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
import { normalizeIntroStyle, serviceBounds } from "../shared/shell-layout";

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
const BLACKBIRD_UPDATE_FEED = "https://tvr.lat/api/desktop/v1/updates/blackbird";
const ATLAS_OVERLAY_SETTINGS_URL = "https://tvr.lat/desktop/atlas-overlay-settings";
const UPDATE_INTERVAL_MS = 30 * 60 * 1_000;
const BOOTSTRAP_WAVES = 3;
const BOOTSTRAP_TIMEOUT_MS = 7_000;
const LOGIN_ATTEMPTS = 3;
const LOGIN_TIMEOUT_MS = 10_000;
const SERVICE_RETRY_DELAYS = [700, 1_800, 4_000] as const;
const RETRYABLE_NETWORK_ERRORS = new Set([-2, -7, -21, -101, -102, -105, -106, -118, -324]);
const DEFAULT_PREFERENCES: DesktopShellPreferences = {
  introStyle: "letters",
  controlBar: "horizontal",
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
let communicateWindow: BrowserWindow | null = null;
let pendingCommunicateShare = "";
let notificationPopup: NotificationPopup | undefined;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let shellOverlayOpen = false;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let notificationPollTimer: ReturnType<typeof setInterval> | undefined;
let notificationPollInFlight = false;
let forcedUpdateInstallTimer: ReturnType<typeof setTimeout> | undefined;
let idleLockTimer: ReturnType<typeof setInterval> | undefined;
let desktopLockReason: DesktopLockReason = "manual";
let desktopLocked = false;
let serviceManifest = new Map<Exclude<ServiceId, "home">, DesktopService>();
let lastSuccessfulBootstrap: DesktopBootstrap | undefined;
let lastKnownBan: BootstrapResult["ban"];
let bootstrapRevision = 0;
let loginInFlight = false;
let loginAbortController: AbortController | undefined;
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
    notificationPopup?.clear();
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
    locked: desktopLocked,
    lockReason: desktopLockReason,
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
  else if (desktopProduct.privateEdition && app.isPackaged && !updateState.checkedAt) void checkForUpdates();
}

async function checkForUpdates(): Promise<DesktopUpdateState> {
  if (!app.isPackaged) {
    return setUpdateState({
      phase: "development",
      message: "Автообновление включается в установленной версии.",
    });
  }
  if (["checking", "downloading"].includes(updateState.phase)) return updateState;
  if (desktopProduct.privateEdition) {
    // electron-updater uses its own HTTP executor, not Electron's account
    // partition. Forward only this first-party session cookie to our own feed.
    try {
      const cookies = await desktopSession().cookies.get({ url: "https://tvr.lat", name: ACCOUNT_SESSION_COOKIE });
      const cookie = cookies.find(item => item.name === ACCOUNT_SESSION_COOKIE)?.value;
      if (!cookie) return setUpdateState({
        phase: "idle",
        message: "Войдите в аккаунт, чтобы проверить обновления Blackbird.",
      });
      autoUpdater.requestHeaders = { Cookie: `${ACCOUNT_SESSION_COOKIE}=${cookie}` };
    } catch {
      return setUpdateState({ phase: "error", message: "Не удалось проверить сессию обновлений." });
    }
  }
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
  autoUpdater.setFeedURL(desktopProduct.privateEdition
    ? { provider: "generic", url: BLACKBIRD_UPDATE_FEED }
    : { provider: "github", owner: "cdnserver", repo: "t-mod-releases" });
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.autoRunAppAfterInstall = true;
  autoUpdater.channel = !desktopProduct.privateEdition && shellPreferences.updateChannel === "dev" ? "dev" : "latest";
  autoUpdater.allowPrerelease = desktopProduct.privateEdition || shellPreferences.updateChannel === "dev";
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
      message: `Установлена актуальная версия ${desktopProduct.name}.`,
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
  serviceView.setBounds(serviceBounds(width, height, desktopProduct.privateEdition, shellPreferences.controlBar === "vertical", shellPreferences.sidebarCollapsed));
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
    introStyle: normalizeIntroStyle(candidate.introStyle),
    controlBar: candidate.controlBar === "vertical" ? "vertical" : "horizontal",
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
    if (desktopProduct.privateEdition) {
      for (const item of items.slice().reverse()) {
        if (!item.read_at && item.kind.startsWith("orl:"))
          notificationPopup?.show(desktopLocked ? { ...item, kind: "orl:toast", title: "Новое событие", body: "Разблокируйте Blackbird, чтобы прочитать." } : item,
            shellPreferences.notificationSound, shellPreferences.reduceMotion);
      }
    }
    return;
  }
  const delivery = shellPreferences.notificationDelivery;
  for (const item of items.slice().reverse()) {
    if (knownNotificationIds.has(item.id)) continue;
    knownNotificationIds.add(item.id);
    if (item.read_at) continue;
    // Operator messages are an explicit surface request, independent of the
    // adaptive Windows/in-app delivery choice. Never display their content on
    // a locked workstation.
    if (desktopProduct.privateEdition && item.kind.startsWith("orl:")) {
      notificationPopup?.show(desktopLocked ? { ...item, kind: "orl:toast", title: "Новое событие", body: "Разблокируйте Blackbird, чтобы прочитать." } : item,
        shellPreferences.notificationSound, shellPreferences.reduceMotion);
      continue;
    }
    const channel = desktopProduct.privateEdition
      ? notificationChannel(delivery, Boolean(mainWindow?.isFocused()))
      : ["both", "system"].includes(delivery) ? "system" : "none";
    if (channel === "custom") {
      notificationPopup?.show(desktopLocked ? { ...item, title: "Новое событие", body: "Откройте Blackbird, чтобы прочитать." } : item, shellPreferences.notificationSound, shellPreferences.reduceMotion); continue;
    }
    if (channel !== "system" || !Notification.isSupported()) continue;
    const notification = new Notification({
      title: item.title,
      body: desktopLocked ? "Новое событие. Откройте Blackbird, чтобы прочитать." : item.body,
      silent: !shellPreferences.notificationSound,
      urgency: item.severity === "critical" ? "critical" : "normal",
    });
    notification.on("click", () => {
      void markNotificationsRead([item.id]);
      const target = resolveNotificationServiceId(item.route);
      if (target) void navigate(target);
      else mainWindow?.webContents.send("desktop:open-notifications");
      if (mainWindow?.isMinimized()) mainWindow.restore();
      mainWindow?.show();
      mainWindow?.focus();
    });
    notification.show();
  }
  for (const id of [...knownNotificationIds]) {
    if (!currentIds.has(id) && knownNotificationIds.size > 500) knownNotificationIds.delete(id);
  }
}

async function pollNotifications(): Promise<void> {
  if (notificationPollInFlight || !lastSuccessfulBootstrap || !desktopProduct.privateEdition) return;
  const revision = bootstrapRevision;
  const account = lastSuccessfulBootstrap.viewer.id;
  notificationPollInFlight = true;
  try {
    const response = await desktopSession().fetch("https://reactor.tvr.lat/api/reactor/notifications?unread=1", {
      credentials: "include",
      headers: { ...desktopIdentityHeaders(), Accept: "application/json" },
      signal: AbortSignal.timeout(6000),
    });
    if (!response.ok) return;
    const data = await response.json() as { viewer?: { id?: number }; items?: DesktopBootstrap["notifications"]["items"] };
    if (revision !== bootstrapRevision || data.viewer?.id !== account || !Array.isArray(data.items)) return;
    const fresh = data.items.some(item => !knownNotificationIds.has(item.id));
    syncNativeNotifications(data.items);
    if (fresh) mainWindow?.webContents.send("desktop:auth-changed");
  } catch (error) {
    log.debug("Notification refresh unavailable", error);
  } finally {
    notificationPollInFlight = false;
  }
}

async function markNotificationsRead(value: unknown): Promise<boolean> {
  if (!Array.isArray(value) || !lastSuccessfulBootstrap) return false;
  const known = new Set(lastSuccessfulBootstrap.notifications.items.map(item => item.id));
  const ids = [...new Set(value.filter((id): id is number => Number.isSafeInteger(id) && id > 0 && known.has(id)))].slice(0, 100);
  if (!ids.length) return false; // Empty IDs mean "all" on the server; never send them.
  const revision = bootstrapRevision;
  const account = lastSuccessfulBootstrap.viewer.id;
  try {
    const host = "https://reactor.tvr.lat";
    const response = await desktopSession().fetch(`${host}/api/reactor/home`, {
      credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.timeout(7000),
    });
    if (!response.ok) return false;
    const home = await response.json() as { viewer?: { id: number; csrf_token: string } };
    const viewer = home.viewer;
    if (revision !== bootstrapRevision || !viewer || viewer.id !== account || !viewer.csrf_token) return false;
    const update = await desktopSession().fetch(`${host}/api/reactor/notifications/read`, {
      method: "POST", credentials: "include", signal: AbortSignal.timeout(7000),
      headers: { ...desktopIdentityHeaders(), "Content-Type": "application/json", "X-CSRF-Token": viewer.csrf_token },
      body: JSON.stringify({ ids }),
    });
    if (!update.ok) return false;
    mainWindow?.webContents.send("desktop:auth-changed");
    return true;
  } catch { return false; }
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
  communicateWindow?.close();
  desktopLockReason = reason;
  notificationPopup?.clear();
  syncServiceVisibility();
  mainWindow.webContents.send("desktop:lock-requested", reason);
  emitState();
  if (mainWindow.isFocused()) mainWindow.webContents.focus();
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
    } else if (/^https?:\/\//i.test(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  contents.on("will-navigate", (event, url) => {
    if (!local && !isTrustedTModUrl(url)) {
      event.preventDefault();
    }
  });
  if (!local) contents.on("context-menu", (_event, params) => {
    if (!/^https?:\/\//i.test(params.linkURL || "")) return;
    const url = params.linkURL;
    Menu.buildFromTemplate([
      { label: "Скопировать ссылку", click: () => clipboard.writeText(url) },
      { label: "Открыть в браузере", click: () => void shell.openExternal(url) },
    ]).popup({ window: mainWindow || undefined });
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
  return { authenticated: false, online: false, error };
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

async function fetchBootstrapCandidate() {
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
    return parseBootstrapResponse(response);
  });
  return selectBootstrapCandidate(requests);
}

async function performBootstrap(revision: number): Promise<BootstrapResult> {
  let lastError = "network_unavailable";
  for (let attempt = 0; attempt < BOOTSTRAP_WAVES; attempt += 1) {
    if (attempt > 0) await wait(attempt === 1 ? 450 : 1_250);
    try {
      const candidate = await fetchBootstrapCandidate();
      const { response } = candidate;
      if (revision !== bootstrapRevision) return { authenticated: false, online: false, error: "session_superseded" };
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
        let reason = "access_denied";
        try {
          const payload = await response.clone().json() as { error?: string };
          if (String(payload.error || "").endsWith("_private_access_required")) reason = "private_access_required";
        } catch { /* A plain-text 403 is still a real access denial. */ }
        return {
          authenticated: false,
          online: true,
          error: reason,
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
        if (revision !== bootstrapRevision) return { authenticated: false, online: false, error: "session_superseded" };
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
      const data = candidate.data!;
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
        lastSuccessfulBootstrap = data;
        lastSuccessfulBootstrapAt = new Date().toISOString();
        syncNativeNotifications(data.notifications.items || []);
        await applyAtlasOverlayBootstrapSafely(data.atlas_overlay);
        if (revision !== bootstrapRevision) return { authenticated: false, online: false, error: "session_superseded" };
      }
      return {
        authenticated: true,
        online: true,
        data,
        lastSuccessfulAt: current ? lastSuccessfulBootstrapAt : new Date().toISOString(),
      };
    } catch (error) {
      lastError = error instanceof BootstrapProtocolError ? "desktop_protocol_invalid" : "network_unavailable";
      log.warn("Account projection retry failed", { attempt: attempt + 1, reason: lastError });
    }
  }
  return revision === bootstrapRevision ? bootstrapUnavailable(lastError) : { authenticated: false, online: false, error: "session_superseded" };
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
  if (loginInFlight) return { ok: false, error: "login_in_progress" };
  loginInFlight = true;
  const controller = new AbortController();
  loginAbortController = controller;
  try { return await performLogin(credentials, controller.signal); }
  finally { loginInFlight = false; if (loginAbortController === controller) loginAbortController = undefined; }
}

async function performLogin(credentials: DesktopLoginCredentials, cancellation: AbortSignal): Promise<DesktopLoginResult> {
  const loginValue = String(credentials?.login || "").trim();
  const pin = String(credentials?.pin || "");
  if (!/^[A-Za-z0-9._-]{3,32}$/.test(loginValue) || !pin || pin.length > 128) {
    return { ok: false, error: "invalid_input" };
  }
  // Invalidate an older periodic bootstrap before changing the session. This
  // closes the race where its delayed 401 overwrote a successful login.
  bootstrapRevision += 1;
  const revision = bootstrapRevision;
  // Cached identity is useful for an outage, never as proof of a new login.
  lastSuccessfulBootstrap = undefined;
  lastSuccessfulBootstrapAt = undefined;
  notificationStreamInitialized = false;
  knownNotificationIds.clear();
  notificationPopup?.clear();
  try {
    const form = new URLSearchParams({ login: loginValue, pin });
    if (credentials.code) form.set("code", credentials.code.slice(0, 32));
    if (credentials.challenge) form.set("challenge", credentials.challenge.slice(0, 64));
    let response: Response | undefined;
    for (let attempt = 0; attempt < LOGIN_ATTEMPTS; attempt += 1) {
      if (cancellation.aborted || revision !== bootstrapRevision) return { ok: false, error: "login_failed" };
      if (attempt > 0) await wait(attempt === 1 ? 450 : 1_250);
      try {
        const candidate = await desktopSession().fetch(
          AUTH_LOGIN_URLS[attempt % AUTH_LOGIN_URLS.length],
          {
            method: "POST",
            // Electron rejects manual redirects with "Redirect was cancelled"
            // rather than exposing a 303 Response. Follow legacy server flows;
            // the final URL and fresh bootstrap still decide authentication.
            redirect: "follow",
            credentials: "include",
            headers: {
              Accept: "text/html,application/xhtml+xml",
              "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
              ...desktopIdentityHeaders(),
            },
            body: form.toString(),
            signal: AbortSignal.any([cancellation, AbortSignal.timeout(LOGIN_TIMEOUT_MS)]),
          },
        );
        // A JSON 429 is an explicit login lockout, not a transport failure.
        if (retryableBootstrapStatus(candidate.status) && !(candidate.status === 429 && candidate.headers.get("content-type")?.includes("application/json"))) {
          log.warn("Account login retry: server unavailable", { attempt: attempt + 1, status: candidate.status });
          continue;
        }
        response = candidate;
        break;
      } catch {
        log.warn("Account login retry: transport interrupted", { attempt: attempt + 1 });
        // Retry short DNS, TLS and service-restart gaps inside this same login
        // operation so the user never has to submit the PIN twice.
      }
    }
    if (revision !== bootstrapRevision) return { ok: false, error: "login_failed" };
    if (!response) return { ok: false, error: "network_unavailable" };
    if (response.status === 202) {
      const factor = await response.json() as { challenge?: string; method?: string; delivery_failed?: boolean };
      return { ok: false, error: "two_factor_required", challenge: factor.challenge, method: factor.method, deliveryFailed: factor.delivery_failed };
    }
    if (response.headers.get("content-type")?.includes("application/json")) {
      let payload: unknown;
      try { payload = await response.json(); } catch { return { ok: false, error: "server_response_invalid" }; }
      const error = loginPayloadError(payload);
      if (error) return { ok: false, error };
      if (!response.ok) return { ok: false, error: "server_response_invalid" };
    }
    if (!response.ok && ![301, 302, 303, 307, 308, 403, 423].includes(response.status)) return { ok: false, error: "login_failed" };
    if (response.status === 403 || response.status === 423) {
      if (response.status === 423) await bootstrap();
      return { ok: false, error: "banned" };
    }
    const error = loginErrorFromLocation(response.headers.get("location") || response.url);
    if (error) return { ok: false, error };
    let result: BootstrapResult | undefined;
    for (const delay of [120, 450, 1_100]) {
      await wait(delay);
      result = await bootstrap();
      if (revision !== bootstrapRevision) return { ok: false, error: "login_failed" };
      if (isFreshLoginProjection(result)) break;
      if (!result.online) break;
      if (["private_access_required", "globally_banned", "desktop_protocol_invalid"].includes(result.error || "")) break;
    }
    if (!isFreshLoginProjection(result)) {
      return {
        ok: false,
        error: result?.error === "private_access_required"
          ? "private_access_required"
          : result?.error === "globally_banned" ? "banned"
          : result?.error === "desktop_protocol_invalid" ? "server_response_invalid"
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
  loginAbortController?.abort();
  communicateWindow?.close();
  if (desktopProduct.privateEdition) autoUpdater.requestHeaders = null;
  notificationPopup?.clear();
  notificationStreamInitialized = false;
  knownNotificationIds.clear();
  bootstrapRevision += 1;
  try {
    await desktopSession().fetch(LOGOUT_URL, {
      redirect: "manual",
      credentials: "include",
      signal: AbortSignal.timeout(5000),
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

async function accountRequest(action: string, data?: Record<string, string>): Promise<Record<string, unknown>> {
  const blackbirdAction = action.startsWith("characters") || action.startsWith("communicate");
  const host = blackbirdAction ? "https://reactor.tvr.lat" : "https://tvr.lat";
  const endpoint = blackbirdAction ? `/api/blackbird/${action.split("-")[0]}`
    : `/api/account/${action === "billing" ? "billing" : "security"}`;
  const revision = bootstrapRevision;
  const snapshot = await desktopSession().fetch(`${host}${endpoint}`, {
    credentials: "include", headers: desktopIdentityHeaders(), signal: AbortSignal.timeout(12_000),
  });
  if (!snapshot.ok) throw new Error(snapshot.status === 401 ? "session_expired" : "account_unavailable");
  const result = await snapshot.json() as Record<string, unknown>;
  if (revision !== bootstrapRevision) throw new Error("session_changed");
  if (action !== "update" && !action.endsWith("-update")) return result;
  const csrfToken = csrfTokenFromAccountSnapshot(result);
  if (!data || typeof data !== "object" || Array.isArray(data) || JSON.stringify(data).length > 4096 || typeof csrfToken !== "string") throw new Error("invalid_request");
  const response = await desktopSession().fetch(`${host}${endpoint}`, {
    method: "POST", credentials: "include", headers: { ...desktopIdentityHeaders(), "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(data), signal: AbortSignal.timeout(20_000),
  });
  const body = await response.json() as Record<string, unknown>;
  if (revision !== bootstrapRevision) throw new Error("session_changed");
  if (!response.ok) throw new Error(typeof body.error === "string" ? body.error : "account_unavailable");
  return body;
}

function shareableLink(url: string): string | null {
  try {
    const parsed = new URL(url);
    if (!isTrustedTModUrl(parsed.href)) return null;
    for (const key of [...parsed.searchParams.keys()]) {
      if (/(token|code|state|secret|password|session|auth|key)/i.test(key)) parsed.searchParams.delete(key);
    }
    parsed.username = ""; parsed.password = "";
    return parsed.href;
  } catch { return null; }
}

function validExternalLink(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false;
  try { return ["http:", "https:"].includes(new URL(value).protocol); }
  catch { return false; }
}

async function openCommunicateWindow(sharedUrl?: string): Promise<boolean> {
  if (!desktopProduct.privateEdition || !lastSuccessfulBootstrap || desktopLocked) return false;
  if (communicateWindow && !communicateWindow.isDestroyed()) {
    communicateWindow.show(); communicateWindow.focus();
    if (sharedUrl) communicateWindow.webContents.send("blackbird:share-link", sharedUrl);
    return true;
  }
  pendingCommunicateShare = sharedUrl || "";
  const owner = mainWindow;
  communicateWindow = new BrowserWindow({
    width: 970, height: 660, minWidth: 760, minHeight: 520,
    parent: owner || undefined, show: false, frame: false, backgroundColor: "#15191d",
    title: "Blackbird Communicate",
    webPreferences: { preload: path.join(bundleDirectory, "../preload/communicate.cjs"), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webSecurity: true, backgroundThrottling: false },
  });
  secureContents(communicateWindow.webContents, { local: true });
  communicateWindow.on("closed", () => { communicateWindow = null; });
  const rendererUrl = process.env.ELECTRON_RENDERER_URL;
  if (rendererUrl) await communicateWindow.loadURL(new URL("communicate.html", rendererUrl).href);
  else await communicateWindow.loadFile(path.join(bundleDirectory, "../renderer/communicate.html"));
  communicateWindow.show(); communicateWindow.focus();
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
  ipcMain.handle("desktop:account-request", async (event, action: unknown, data: unknown) => {
    if (!trusted(event) || !["security", "billing", "update", "characters", "characters-update", "communicate", "communicate-update"].includes(String(action))) throw new Error("untrusted_request");
    return accountRequest(String(action), data as Record<string, string> | undefined);
  });
  const trustedCommunicate = (event: Electron.IpcMainInvokeEvent) => Boolean(communicateWindow && event.sender.id === communicateWindow.webContents.id);
  ipcMain.handle("desktop:communicate-open", event => trusted(event) ? openCommunicateWindow() : false);
  ipcMain.handle("desktop:communicate-share", event => {
    if (!trusted(event) || !serviceView) return false;
    const url = shareableLink(serviceView.webContents.getURL());
    return url ? openCommunicateWindow(url) : false;
  });
  ipcMain.handle("blackbird:communicate-context", event => {
    if (!trustedCommunicate(event)) throw new Error("untrusted_request");
    const sharedUrl = pendingCommunicateShare;
    pendingCommunicateShare = "";
    return { viewerId: lastSuccessfulBootstrap?.viewer.id || 0, authenticated: Boolean(lastSuccessfulBootstrap), sharedUrl };
  });
  ipcMain.handle("blackbird:communicate-request", (event, action: unknown, data: unknown) => {
    if (!trustedCommunicate(event) || !["communicate", "communicate-update"].includes(String(action))) throw new Error("untrusted_request");
    return accountRequest(String(action), data as Record<string, string> | undefined);
  });
  ipcMain.handle("blackbird:communicate-close", event => { if (trustedCommunicate(event)) communicateWindow?.close(); });
  ipcMain.handle("blackbird:communicate-minimize", event => { if (trustedCommunicate(event)) communicateWindow?.minimize(); });
  ipcMain.handle("blackbird:communicate-open-link", (event, value: unknown) => {
    if (!trustedCommunicate(event) || !validExternalLink(value)) return false;
    void shell.openExternal(value); return true;
  });
  ipcMain.handle("blackbird:communicate-copy-link", (event, value: unknown) => {
    if (!trustedCommunicate(event) || !validExternalLink(value)) return false;
    clipboard.writeText(value); return true;
  });
  ipcMain.handle("desktop:billing-open", async event => {
    if (!trusted(event)) return false;
    await shell.openExternal("https://atlas.tvr.lat/account");
    return true;
  });
  ipcMain.handle("desktop:account-create", async event => {
    if (!trusted(event)) return false;
    // Fixed verified registration destination; no arbitrary URL from the renderer.
    await shell.openExternal("https://discord.gg/5vAKXdX5sw");
    return true;
  });
  ipcMain.handle("desktop:notification-preview", event => {
    if (!trusted(event) || !desktopProduct.privateEdition) return false;
    notificationPopup?.show({ id: -Date.now(), severity: "info", kind: "preview", title: "Blackbird на связи",
      body: "Так будут появляться ваши уведомления. Нажмите, чтобы открыть приложение.", route: null, read_at: null, created_at: new Date().toISOString() }, shellPreferences.notificationSound, shellPreferences.reduceMotion);
    return true;
  });
  ipcMain.handle("desktop:notifications-read", (event, ids: unknown) => trusted(event) ? markNotificationsRead(ids) : false);
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
      backgroundThrottling: false,
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
  mainWindow.on("focus", emitState);
  mainWindow.on("restore", emitState);
  mainWindow.on("closed", () => {
    notificationPopup?.dispose();
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
  if (desktopProduct.privateEdition) notificationPopup = new NotificationPopup(bundleDirectory, item => {
    if (item.id > 0) void markNotificationsRead([item.id]);
    if (item.kind === "communicate" && !desktopLocked) {
      if (mainWindow?.isMinimized()) mainWindow.restore();
      void openCommunicateWindow(); return;
    }
    const target = resolveNotificationServiceId(item.route);
    if (mainWindow?.isMinimized()) mainWindow.restore();
    mainWindow?.show(); mainWindow?.focus();
    if (target) void navigate(target);
    else mainWindow?.webContents.send("desktop:open-notifications");
  }, () => atlasOverlay?.getStatus().display || mainWindow?.getBounds());
  configureAutoUpdater();
  await createWindow();
  screen.on("display-added", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-removed", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-metrics-changed", () => atlasOverlay?.onDisplaysChanged());
  startIdleLockMonitor();
  if (desktopProduct.privateEdition) notificationPollTimer = setInterval(() => void pollNotifications(), 6_000);
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
  if (notificationPollTimer) clearInterval(notificationPollTimer);
  if (idleLockTimer) clearInterval(idleLockTimer);
  if (authProjectionTimer) clearTimeout(authProjectionTimer);
  if (forcedUpdateInstallTimer) clearTimeout(forcedUpdateInstallTimer);
  atlasOverlay?.dispose();
  notificationPopup?.dispose();
});
