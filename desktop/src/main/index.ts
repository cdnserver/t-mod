import {
  app,
  BrowserWindow,
  clipboard,
  dialog,
  ipcMain,
  Menu,
  nativeImage,
  nativeTheme,
  Notification,
  powerMonitor,
  safeStorage,
  screen,
  session,
  shell,
  Tray,
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
import { csrfTokenFromAccountSnapshot } from "../shared/account-response";
import { requestAccount } from "./account-request";
import { readBoundedBytes } from "./bounded-response";
import { downloadCommunicateAttachment, uploadCommunicateAttachment } from "./communicate-transfer";
import { accountIdentityKey, sameAccountIdentity } from "../shared/account-identity";
import { ConsensusInvitationGate, consensusAttendanceConfirmed, consensusViewerMatches } from "../shared/consensus-flow";
import { readableNotificationIds, startupPopupNotifications } from "../shared/notification-reliability";
import { BootstrapProtocolError, isFreshLoginProjection, loginPayloadError, parseBootstrapResponse, selectBootstrapCandidate } from "../shared/auth-network";
import {
  isServiceId,
  resolveNotificationServiceId,
  isTModAuthenticationUrl,
  isTrustedTModUrl,
} from "../shared/services";
import type {
  BootstrapResult,
  ConsensusRegistrationNotice,
  ConsensusLiveSnapshot,
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
import { enableInteractionSurfaces } from "./interaction-surface";
import { parseServiceDeepLink, serviceForDeepLink } from "../shared/service-deep-link";
import type { ServiceDeepLink } from "../shared/service-deep-link";
import { normalizeIntroStyle, serviceBounds } from "../shared/shell-layout";
import { normalizeHardwareUuid } from "../shared/device-identity";
import { mayCompleteUnlock, ProtectedAccessGate } from "../shared/protected-access";
import { navigationWasAborted, ServiceNavigation } from "../shared/service-navigation";

const { autoUpdater } = electronUpdater;
if (desktopProduct.privateEdition) enableInteractionSurfaces();

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
  notificationCorner: "bottom-right",
  notificationDurationSeconds: 9,
  updateChannel: desktopProduct.updateChannel,
};

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let communicateWindow: BrowserWindow | null = null;
let communicateOpening: Promise<boolean> | undefined;
let communicateContextReady = false;
let pendingCommunicateShare = "";
let pendingServiceDeepLink: ServiceDeepLink | null = process.argv
  .map((argument) => parseServiceDeepLink(argument, desktopProduct.protocol))
  .find((link): link is ServiceDeepLink => link !== null) || null;
let notificationPopup: NotificationPopup | undefined;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let shellOverlayOpen = false;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let notificationPollTimer: ReturnType<typeof setInterval> | undefined;
let consensusPollTimer: ReturnType<typeof setInterval> | undefined;
let consensusPollInFlight = false;
const consensusInvitations = new ConsensusInvitationGate();
let notificationPollInFlight = false;
let forcedUpdateInstallTimer: ReturnType<typeof setTimeout> | undefined;
let idleLockTimer: ReturnType<typeof setInterval> | undefined;
let desktopLockReason: DesktopLockReason = "manual";
let desktopLocked = false;
let lockedServiceDestination: string | undefined;
let unlockInFlight: Promise<boolean | "login_required"> | undefined;
let serviceClearInFlight: Promise<void> | undefined;
let serviceManifest = new Map<Exclude<ServiceId, "home">, DesktopService>();
let lastSuccessfulBootstrap: DesktopBootstrap | undefined;
let lastKnownBan: BootstrapResult["ban"];
let bootstrapRevision = 0;
let desktopLockRevision = 0;
const protectedAccess = new ProtectedAccessGate(() => ({
  revision: bootstrapRevision,
  accountId: lastSuccessfulBootstrap ? accountIdentityKey(lastSuccessfulBootstrap.viewer) : null,
  locked: desktopLocked,
  banned: Boolean(lastKnownBan),
}));
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
let desktopHardwareFingerprint = "";
const serviceNavigation = new ServiceNavigation();
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

async function buildDesktopHardwareFingerprint(): Promise<string> {
  if (process.platform !== "win32") return "";
  try {
    const output = await execFileText("powershell.exe", [
      "-NoProfile", "-NonInteractive", "-Command",
      "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID",
    ]);
    const uuid = normalizeHardwareUuid(output);
    if (!uuid) return "";
    return createHash("sha256")
      .update(`tmod-desktop-smbios-v1\0${uuid}`, "utf8")
      .digest("hex");
  } catch (error) {
    log.warn("Hardware binding unavailable; continuing with installation and OS identifiers", error);
    return "";
  }
}

function desktopIdentityHeaders(): Record<string, string> {
  return {
    ...(desktopInstallToken ? { "X-TMod-Install-Token": desktopInstallToken } : {}),
    ...(desktopDeviceFingerprint ? { "X-TMod-Device-Fingerprint": desktopDeviceFingerprint } : {}),
    ...(desktopHardwareFingerprint ? { "X-TMod-Hardware-Fingerprint": desktopHardwareFingerprint } : {}),
    "X-TMod-Desktop-Platform": process.platform,
    "X-TMod-Desktop-Version": app.getVersion(),
    "X-TMod-Desktop-Edition": desktopProduct.edition,
  };
}

function scheduleAuthProjectionRefresh(options: { sessionRemoved?: boolean } = {}): void {
  if (options.sessionRemoved) {
    protectedAccess.revoke();
    communicateWindow?.close();
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
    clearServiceRetry();
    syncServiceVisibility();
    clearSensitiveServiceView();
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
    serviceReady: serviceNavigation.ready && activeService !== "home" && !desktopLocked && !lastKnownBan,
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

async function pollConsensusRegistration(): Promise<void> {
  if (!desktopProduct.privateEdition || !lastSuccessfulBootstrap || consensusPollInFlight || desktopLocked || lastKnownBan || !mainWindow) return;
  consensusPollInFlight = true;
  const observation = consensusInvitations.observe();
  const lease = protectedAccess.capture();
  const revision = bootstrapRevision;
  const accountId = accountIdentityKey(lastSuccessfulBootstrap.viewer);
  try {
    const response = await desktopSession().fetch("https://consensus.tvr.lat/api/state?mode=live&fresh=1", {
      credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(8_000)]),
    });
    lease.assertCurrent();
    refreshAccountAfterDeniedResponse(response.status);
    if (!response.ok) return;
    const state = await response.json() as { session?: { key?: string; stage?: string; plenary_number?: number }; viewer?: { id?: number; id_exact?: string; participant?: boolean; confirmed?: boolean; csrf_token?: string; ballot_available?: boolean } };
    lease.assertCurrent();
    const session = state.session;
    const viewer = state.viewer;
    if (revision !== bootstrapRevision || !lastSuccessfulBootstrap || desktopLocked || lastKnownBan ||
        !consensusViewerMatches(viewer, lastSuccessfulBootstrap.viewer) || !viewer?.participant ||
        !session?.key || (!viewer.confirmed && !viewer.csrf_token)) return;
    if (session.stage === "finished" || session.stage === "cancelled") return;
    if (!viewer.confirmed && session.stage !== "registration") return;
    if (!consensusInvitations.accept(accountId, session.key, viewer.confirmed === true, observation)) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    mainWindow.webContents.send("desktop:consensus-registration", {
      sessionKey: session.key, plenaryNumber: Number(session.plenary_number) || 0,
      csrfToken: viewer.csrf_token || "", confirmed: viewer.confirmed === true,
    } satisfies ConsensusRegistrationNotice);
  } catch (error) { log.debug("Consensus registration poll unavailable", error); }
  finally { consensusPollInFlight = false; }
}

async function readConsensusLiveState(): Promise<ConsensusLiveSnapshot | null> {
  if (!desktopProduct.privateEdition || !lastSuccessfulBootstrap || desktopLocked || lastKnownBan) return null;
  const lease = protectedAccess.capture();
  const revision = bootstrapRevision;
  const accountId = accountIdentityKey(lastSuccessfulBootstrap.viewer);
  try {
    const response = await desktopSession().fetch("https://consensus.tvr.lat/api/state?mode=live&fresh=1", {
      credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(8_000)]),
    });
    lease.assertCurrent();
    refreshAccountAfterDeniedResponse(response.status);
    if (!response.ok) return null;
    const data = await response.json() as {
      session?: { key?: string; plenary_number?: number; stage?: string; stage_label?: string; quorum?: { confirmed?: number; invited?: number; ready?: boolean }; current_bill?: { title?: string } } | null;
      viewer?: { id?: number; id_exact?: string; confirmed?: boolean; ballot_available?: boolean };
    };
    lease.assertCurrent();
    if (!data.viewer) return null;
    if (revision !== bootstrapRevision || !lastSuccessfulBootstrap || desktopLocked || lastKnownBan ||
        !consensusViewerMatches(data.viewer, lastSuccessfulBootstrap.viewer)) return null;
    const session = data.session;
    return {
      sessionKey: session?.key || null,
      plenaryNumber: Number(session?.plenary_number) || 0,
      stage: session?.stage || "idle",
      stageLabel: session?.stage_label || "Ожидание",
      confirmed: data.viewer.confirmed === true,
      ballotAvailable: data.viewer.ballot_available === true,
      confirmedCount: Number(session?.quorum?.confirmed) || 0,
      invitedCount: Number(session?.quorum?.invited) || 0,
      quorumReady: session?.quorum?.ready === true,
      currentBillTitle: session?.current_bill?.title || null,
    };
  } catch (error) { log.debug("Consensus live state unavailable", error); return null; }
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
    reduceMotion: false,
    solidSurfaces: candidate.solidSurfaces === true,
    serviceZoom: [0.9, 1, 1.1].includes(zoom) ? zoom : 1,
    idleLockMinutes: [0, 5, 10, 15, 30].includes(idleLockMinutes) ? idleLockMinutes : 10,
    lockSound: candidate.lockSound !== false,
    notificationDelivery: candidate.notificationDelivery === "in-app" ? "both"
      : ["both", "system", "off"].includes(String(candidate.notificationDelivery))
        ? candidate.notificationDelivery as DesktopShellPreferences["notificationDelivery"] : "both",
    notificationSound: candidate.notificationSound !== false,
    notificationCorner: ["bottom-right", "bottom-left", "top-right", "top-left"].includes(String(candidate.notificationCorner))
      ? candidate.notificationCorner as DesktopShellPreferences["notificationCorner"] : "bottom-right",
    notificationDurationSeconds: [6, 9, 12, 20].includes(Number(candidate.notificationDurationSeconds))
      ? Number(candidate.notificationDurationSeconds) : 9,
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
      for (const item of startupPopupNotifications(items))
        notificationPopup?.show(desktopLocked && item.kind !== "screenban" ? { ...item, kind: "orl:toast", title: "Новое событие", body: "Разблокируйте Blackbird, чтобы прочитать." } : item,
          shellPreferences.notificationSound, shellPreferences.reduceMotion);
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
    if (desktopProduct.privateEdition && (item.kind.startsWith("orl:") || item.kind === "screenban")) {
      notificationPopup?.show(desktopLocked && item.kind !== "screenban" ? { ...item, kind: "orl:toast", title: "Новое событие", body: "Разблокируйте Blackbird, чтобы прочитать." } : item,
        shellPreferences.notificationSound, shellPreferences.reduceMotion);
      continue;
    }
    const channel = desktopProduct.privateEdition
      ? delivery === "off" ? "none" : delivery === "system" ? "system" : "custom"
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
  if (notificationPollInFlight || !desktopProduct.privateEdition) return;
  // A revoked session must be noticed even while the app is unfocused. Keep
  // checking for an administrator's later unban without exposing old views.
  if (lastKnownBan) {
    notificationPollInFlight = true;
    try {
      const previous = lastKnownBan.reference;
      const result = await bootstrap();
      if (result.error !== "globally_banned" || result.ban?.reference !== previous)
        mainWindow?.webContents.send("desktop:auth-changed");
    } catch (error) { log.debug("Ban state refresh unavailable", error); }
    finally { notificationPollInFlight = false; }
    return;
  }
  if (!lastSuccessfulBootstrap) return;
  const revision = bootstrapRevision;
  const account = lastSuccessfulBootstrap.viewer;
  notificationPollInFlight = true;
  try {
    const response = await desktopSession().fetch("https://reactor.tvr.lat/api/reactor/notifications?unread=1", {
      credentials: "include",
      headers: { ...desktopIdentityHeaders(), Accept: "application/json" },
      signal: AbortSignal.timeout(6000),
    });
    if (response.status === 423) {
      const result = await bootstrap();
      if (result.error === "globally_banned") mainWindow?.webContents.send("desktop:auth-changed");
      return;
    }
    if (!response.ok) return;
    const data = await response.json() as { viewer?: { id: number; id_exact?: string }; items?: DesktopBootstrap["notifications"]["items"] };
    if (revision !== bootstrapRevision || !sameAccountIdentity(account, data.viewer) || !Array.isArray(data.items)) return;
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
  if (!Array.isArray(value) || !lastSuccessfulBootstrap || desktopLocked || lastKnownBan) return false;
  const lease = protectedAccess.capture();
  const known = new Set([...knownNotificationIds, ...lastSuccessfulBootstrap.notifications.items.map(item => item.id)]);
  const ids = readableNotificationIds(value, known);
  if (!ids.length) return false; // Empty IDs mean "all" on the server; never send them.
  const revision = bootstrapRevision;
  const account = lastSuccessfulBootstrap.viewer;
  try {
    const host = "https://reactor.tvr.lat";
    const response = await desktopSession().fetch(`${host}/api/reactor/home`, {
      credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(7000)]),
    });
    lease.assertCurrent();
    refreshAccountAfterDeniedResponse(response.status);
    if (!response.ok) return false;
    const home = await response.json() as { viewer?: { id: number; id_exact?: string; csrf_token: string } };
    const viewer = home.viewer;
    lease.assertCurrent();
    if (revision !== bootstrapRevision || !sameAccountIdentity(account, viewer) || !viewer?.csrf_token) return false;
    const update = await desktopSession().fetch(`${host}/api/reactor/notifications/read`, {
      method: "POST", credentials: "include", signal: AbortSignal.any([lease.signal, AbortSignal.timeout(7000)]),
      headers: { ...desktopIdentityHeaders(), "Content-Type": "application/json", "X-CSRF-Token": viewer.csrf_token },
      body: JSON.stringify({ ids }),
    });
    lease.assertCurrent();
    refreshAccountAfterDeniedResponse(update.status);
    if (!update.ok) return false;
    mainWindow?.webContents.send("desktop:auth-changed");
    return true;
  } catch { return false; }
}

function applyPreferences(value: unknown): DesktopShellPreferences {
  const previousChannel = shellPreferences.updateChannel;
  shellPreferences = normalizePreferences(value);
  notificationPopup?.configure(shellPreferences.notificationCorner, shellPreferences.notificationDurationSeconds);
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

function clearSensitiveServiceView(): void {
  serviceNavigation.begin();
  clearServiceRetry();
  if (!serviceView || serviceView.webContents.isDestroyed()) return;
  serviceView.webContents.stop();
  const clearing = serviceView.webContents.loadURL("about:blank")
    .catch(error => log.warn("Could not clear a protected service view", error))
    .then(() => undefined);
  serviceClearInFlight = clearing;
  void clearing.finally(() => {
    if (serviceClearInFlight === clearing) serviceClearInFlight = undefined;
  });
}

function lockDesktop(reason: DesktopLockReason): boolean {
  if (!mainWindow || mainWindow.isDestroyed()) return desktopLocked;
  desktopLockRevision++;
  protectedAccess.revoke();
  if (desktopLocked) return true;
  const currentUrl = serviceView?.webContents.getURL();
  lockedServiceDestination = currentUrl && activeService !== "home" && serviceForDeepLink(currentUrl)?.serviceId === activeService
    ? currentUrl : undefined;
  desktopLocked = true;
  communicateWindow?.close();
  atlasOverlay?.setLocked(true);
  desktopLockReason = reason;
  notificationPopup?.clear();
  clearServiceRetry();
  serviceLoading = false;
  syncServiceVisibility();
  clearSensitiveServiceView();
  mainWindow.webContents.send("desktop:lock-requested", reason);
  emitState();
  if (mainWindow.isFocused()) mainWindow.webContents.focus();
  return true;
}

function unlockDesktop(): Promise<boolean | "login_required"> {
  if (lastKnownBan) return Promise.resolve(false);
  if (!desktopLocked) return Promise.resolve(true);
  if (unlockInFlight) return unlockInFlight;
  unlockInFlight = (async () => {
    const lockRevision = desktopLockRevision;
    if (desktopProduct.privateEdition) {
      const fresh = await bootstrap();
      if (lockRevision !== desktopLockRevision) return false;
      if (!fresh.authenticated || !fresh.online || !fresh.data || lastKnownBan) {
        mainWindow?.webContents.send("desktop:auth-changed");
        if (fresh.online && ["login_required", "private_access_required"].includes(String(fresh.error))) {
          lockedServiceDestination = undefined;
          activeService = "home";
          desktopLocked = false;
          syncServiceVisibility();
          emitState();
          return "login_required";
        }
        return false;
      }
    }
    const currentContext = () => ({ lockRevision: desktopLockRevision, sessionRevision: bootstrapRevision,
      accountId: lastSuccessfulBootstrap ? accountIdentityKey(lastSuccessfulBootstrap.viewer) : null });
    const context = currentContext();
    // A lock may still be unloading the previous page. Wait before restoring it,
    // otherwise the late about:blank navigation can replace the unlocked service.
    if (serviceClearInFlight) await serviceClearInFlight;
    if (!mayCompleteUnlock(context, currentContext(), Boolean(lastKnownBan))) return false;
    const destination = lockedServiceDestination;
    lockedServiceDestination = undefined;
    desktopLocked = false;
    atlasOverlay?.setLocked(false);
    if (activeService !== "home") await navigate(activeService, destination);
    else { syncServiceVisibility(); emitState(); }
    if (desktopLocked || !mayCompleteUnlock(context, currentContext(), Boolean(lastKnownBan))) return false;
    if (!shellOverlayOpen && serviceView?.getVisible()) serviceView.webContents.focus();
    void openPendingServiceDeepLink();
    return true;
  })().finally(() => { unlockInFlight = undefined; });
  return unlockInFlight;
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
  if (!serviceView || activeService === "home" || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || !serviceNavigation.destination) return false;
  // Electron reports one failure through both did-fail-load and loadURL's
  // rejected promise. They must not consume two retry slots.
  if (serviceRetryTimer) return true;
  const retryable = code >= 500 || RETRYABLE_NETWORK_ERRORS.has(code);
  const delay = SERVICE_RETRY_DELAYS[serviceRetryAttempt];
  if (!retryable || delay === undefined) return false;
  clearServiceRetry(false);
  serviceRetryAttempt += 1;
  serviceNavigation.ready = false;
  const ticket = serviceNavigation.ticket;
  const lease = protectedAccess.capture();
  serviceLoading = true;
  lastServiceError = undefined;
  syncServiceVisibility();
  emitState();
  serviceRetryTimer = setTimeout(() => {
    serviceRetryTimer = undefined;
    if (!serviceNavigation.current(ticket) || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) return;
    // A periodic bootstrap may refresh the same account while this timer
    // waits. Scope invalidation handles lock/logout; an identity check keeps
    // healthy refreshes from silently stranding the loading indicator.
    try { if (protectedAccess.capture().accountId !== lease.accountId) return; } catch { return; }
    // Keep the ballot/case/article URL, not the service's landing page.
    void loadServiceDestination(ticket, description);
  }, delay);
  return true;
}

function syncServiceVisibility(): void {
  if (!serviceView) return;
  serviceView.setVisible(
    !shellOverlayOpen &&
    !desktopLocked &&
    !lastKnownBan &&
    Boolean(lastSuccessfulBootstrap) &&
    activeService !== "home" &&
    (!desktopProduct.privateEdition || serviceNavigation.ready) &&
    !lastServiceError,
  );
}

function isAtlasOverlaySettingsUrl(value: string): boolean {
  return value === ATLAS_OVERLAY_SETTINGS_URL;
}

function secureContents(contents: WebContents, options: { local: boolean }): void {
  const { local } = options;
  if (desktopProduct.privateEdition && app.isPackaged) {
    contents.on("devtools-opened", () => contents.closeDevTools());
    contents.on("before-input-event", (event, input) => {
      if (input.type !== "keyDown") return;
      const key = input.key.toLowerCase();
      if (key === "f12" || ((input.control || input.meta) && (
        (input.shift && ["i", "j", "c", "k"].includes(key)) ||
        (input.alt && ["i", "j", "c"].includes(key)) ||
        key === "u"
      ))) event.preventDefault();
    });
  }
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url }) => {
    if (local) return { action: "deny" };
    if (desktopProduct.privateEdition && (desktopLocked || lastKnownBan || !lastSuccessfulBootstrap)) return { action: "deny" };
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
    if (!local && desktopProduct.privateEdition && (desktopLocked || lastKnownBan || !lastSuccessfulBootstrap)) {
      event.preventDefault();
      return;
    }
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

async function navigate(serviceId: ServiceId, requestedUrl?: string): Promise<DesktopState> {
  if (!serviceView || !mainWindow || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) return state();
  const reuseCurrentDocument = activeService === serviceId && serviceNavigation.ready;
  const ticket = serviceNavigation.begin();
  const lease = protectedAccess.capture();
  clearServiceRetry();
  if (serviceClearInFlight) await serviceClearInFlight;
  if (!serviceNavigation.current(ticket)) return state();
  try { lease.assertCurrent(); } catch { return state(); }
  if (!serviceView || !mainWindow || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) return state();
  clearServiceRetry();
  const retryAfterError = Boolean(lastServiceError);
  activeService = serviceId;
  lastServiceError = undefined;

  if (serviceId === "home") {
    serviceView.webContents.stop();
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
  const requested = requestedUrl ? serviceForDeepLink(requestedUrl) : null;
  if (!target || !isTrustedTModUrl(target) ||
    (requestedUrl && requested?.serviceId !== serviceId)) {
    lastServiceError = "service_route_invalid";
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
    return state();
  }

  const destination = requested?.url || target;
  serviceNavigation.redirect(destination);
  serviceLoading = true;
  syncServiceVisibility();
  emitState();
  const current = serviceView.webContents.getURL();
  if (current !== destination || retryAfterError || !reuseCurrentDocument || serviceView.webContents.isLoadingMainFrame()) {
    await loadServiceDestination(ticket);
  } else {
    serviceNavigation.finish(current, !isTModAuthenticationUrl(current));
    serviceLoading = false;
    syncServiceVisibility();
    emitState();
  }
  return state();
}

async function loadServiceDestination(ticket: number, fallback = "service_load_failed"): Promise<void> {
  if (!serviceView || !serviceNavigation.current(ticket)) return;
  const attempt = serviceNavigation.startAttempt();
  try { await serviceView.webContents.loadURL(serviceNavigation.destination); }
  catch (error) {
    if (!serviceNavigation.currentAttempt(ticket, attempt) || navigationWasAborted(error) ||
      desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || activeService === "home") return;
    const message = error instanceof Error ? error.message : String(error || fallback);
    if (!lastServiceError && !scheduleServiceRetry(-2, message)) {
      lastServiceError = message;
      serviceLoading = false;
      syncServiceVisibility();
      emitState();
    }
  }
}

function receiveServiceDeepLink(value: string): void {
  const parsed = parseServiceDeepLink(value, desktopProduct.protocol);
  if (!parsed) return;
  pendingServiceDeepLink = parsed;
  void openPendingServiceDeepLink();
}

async function openPendingServiceDeepLink(): Promise<void> {
  const link = pendingServiceDeepLink;
  if (!link || !mainWindow || !serviceView || desktopLocked || lastKnownBan) return;
  // Before login the link waits for a fresh service manifest. Once the
  // account is known, a denied contour must show the existing access error
  // instead of leaving the click apparently unanswered.
  if (!lastSuccessfulBootstrap) return;
  pendingServiceDeepLink = null;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  await navigate(link.serviceId, link.url);
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function discardAccountProjection(): void {
  protectedAccess.revoke();
  lastSuccessfulBootstrap = undefined;
  lastSuccessfulBootstrapAt = undefined;
  clearServiceManifest();
  clearServiceRetry();
  activeService = "home";
  serviceLoading = false;
  lastServiceError = undefined;
  communicateWindow?.close();
  notificationPopup?.clear();
  atlasOverlay?.setLocked(true);
  syncServiceVisibility();
  clearSensitiveServiceView();
  emitState();
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
  if (desktopProduct.privateEdition) {
    // Blackbird never treats a cached account projection as current access.
    // If authorization cannot be checked, close the live service surface.
    discardAccountProjection();
    return { authenticated: false, online: false, error };
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
    // A ban wins immediately. Healthy identity waits for the bounded mirror
    // checks so a stale projection cannot override another endpoint's denial.
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
          protectedAccess.revoke();
          lastKnownBan = undefined;
          atlasOverlay?.setLocked(desktopLocked);
          lastSuccessfulBootstrap = undefined;
          communicateWindow?.close();
          notificationPopup?.clear();
          clearServiceManifest();
          reconcileActiveServiceAccess();
          clearSensitiveServiceView();
          await applyAtlasOverlayBootstrapSafely(undefined);
        }
        return { authenticated: false, online: true, error: "login_required" };
      }
      if (response.status === 403 && desktopProduct.privateEdition) {
        if (current) {
          protectedAccess.revoke();
          lastKnownBan = undefined;
          atlasOverlay?.setLocked(desktopLocked);
          lastSuccessfulBootstrap = undefined;
          lastSuccessfulBootstrapAt = undefined;
          communicateWindow?.close();
          notificationPopup?.clear();
          clearServiceManifest();
          reconcileActiveServiceAccess();
          clearSensitiveServiceView();
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
          protectedAccess.revoke();
          lastKnownBan = decision;
          atlasOverlay?.setLocked(true);
          lastSuccessfulBootstrap = undefined;
          lastSuccessfulBootstrapAt = undefined;
          communicateWindow?.close();
          notificationPopup?.clear();
          clearServiceManifest();
          activeService = "home";
          serviceLoading = false;
          lastServiceError = undefined;
          clearServiceRetry();
          pendingServiceDeepLink = null;
          syncServiceVisibility();
          clearSensitiveServiceView();
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
        if (current && desktopProduct.privateEdition) discardAccountProjection();
        return {
          authenticated: false,
          online: true,
          error: `bootstrap_http_${response.status}`,
        };
      }
      const data = candidate.data!;
      if (data.protocol_version !== 1 || !Array.isArray(data.services)) {
        if (current) discardAccountProjection();
        return {
          authenticated: false,
          online: true,
          error: "desktop_protocol_invalid",
        };
      }
      if (current) {
        applyClientUpdatePolicy(data);
        if (lastSuccessfulBootstrap && !sameAccountIdentity(lastSuccessfulBootstrap.viewer, data.viewer)) {
          protectedAccess.revoke();
          communicateWindow?.close();
          notificationPopup?.clear();
          notificationStreamInitialized = false;
          knownNotificationIds.clear();
          activeService = "home";
          serviceLoading = false;
          clearSensitiveServiceView();
          syncServiceVisibility();
        }
        lastKnownBan = undefined;
        atlasOverlay?.setLocked(desktopLocked);
        if (!applyServiceManifest(data)) {
          discardAccountProjection();
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
        queueMicrotask(() => void openPendingServiceDeepLink());
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
  protectedAccess.revoke();
  bootstrapRevision += 1;
  const revision = bootstrapRevision;
  // Cached identity is useful for an outage, never as proof of a new login.
  lastSuccessfulBootstrap = undefined;
  lastSuccessfulBootstrapAt = undefined;
  notificationStreamInitialized = false;
  knownNotificationIds.clear();
  notificationPopup?.clear();
  if (desktopProduct.privateEdition) {
    clearServiceManifest();
    clearServiceRetry();
    activeService = "home";
    serviceLoading = false;
    syncServiceVisibility();
    clearSensitiveServiceView();
    emitState();
  }
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
  protectedAccess.revoke();
  loginAbortController?.abort();
  communicateWindow?.close();
  if (desktopProduct.privateEdition) autoUpdater.requestHeaders = null;
  notificationPopup?.clear();
  notificationStreamInitialized = false;
  knownNotificationIds.clear();
  consensusInvitations.reset();
  bootstrapRevision += 1;
  // Revoke the old account's live content before the remote logout or cookie
  // cleanup can stall. The local surface must never remain interactive.
  lastSuccessfulBootstrap = undefined;
  lastSuccessfulBootstrapAt = undefined;
  clearServiceManifest();
  clearServiceRetry();
  activeService = "home";
  serviceLoading = false;
  lastServiceError = undefined;
  syncServiceVisibility();
  clearSensitiveServiceView();
  emitState();
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
  await applyAtlasOverlayBootstrapSafely(undefined);
  return true;
}

function accountRequestDependencies(): Parameters<typeof requestAccount>[0] {
  return {
    access: protectedAccess, viewer: () => lastSuccessfulBootstrap?.viewer,
    fetch: (url, init) => desktopSession().fetch(url, init), headers: desktopIdentityHeaders,
    onDenied: refreshAccountAfterDeniedResponse,
    onMismatch: () => { void bootstrap().then(() => mainWindow?.webContents.send("desktop:auth-changed")).catch(error => log.debug("Account identity refresh unavailable", error)); },
  };
}

async function accountRequest(action: string, data?: Record<string, string>): Promise<Record<string, unknown>> {
  return requestAccount(accountRequestDependencies(), action, data);
}

function refreshAccountAfterDeniedResponse(status: number): void {
  if (status !== 401 && status !== 423) return;
  void bootstrap().then(() => {
    if (mainWindow && !mainWindow.webContents.isDestroyed()) mainWindow.webContents.send("desktop:auth-changed");
  }).catch(error => log.debug("Access projection refresh unavailable", error));
}

type MediaAssetKind = "avatar" | "cover";
function validMediaAssetKind(value: unknown): value is MediaAssetKind {
  return value === "avatar" || value === "cover";
}

async function mediaAsset(userId: unknown, kind: unknown): Promise<{ bytes: Uint8Array; mimeType: string; revision: string } | null> {
  if (!desktopProduct.privateEdition || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap ||
      typeof userId !== "string" || !/^\d{1,22}$/.test(userId) || !validMediaAssetKind(kind)) throw new Error("access_locked");
  const lease = protectedAccess.capture();
  const revision = bootstrapRevision;
  const response = await desktopSession().fetch(`https://reactor.tvr.lat/api/blackbird/media/assets/${userId}/${kind}`, {
    credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "image/webp" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(15_000)]),
  });
  lease.assertCurrent();
  refreshAccountAfterDeniedResponse(response.status);
  if (response.status === 404) return null;
  if (!response.ok) {
    if (response.status === 423) void bootstrap();
    throw new Error("media_asset_unavailable");
  }
  const bytes = await readBoundedBytes(response, 2 * 1024 * 1024, lease.signal);
  lease.assertCurrent();
  if (bytes.length > 2 * 1024 * 1024 || revision !== bootstrapRevision || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) throw new Error("access_locked");
  return { bytes, mimeType: "image/webp", revision: (response.headers.get("ETag") || "").replaceAll('"', "") };
}

async function mediaAssetWrite(kind: unknown, raw: unknown, remove = false): Promise<{ revision: string } | boolean> {
  if (!desktopProduct.privateEdition || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || !validMediaAssetKind(kind)) throw new Error("access_locked");
  if (!remove && !(raw instanceof Uint8Array || raw instanceof ArrayBuffer)) throw new Error("media_asset_invalid");
  const bytes = remove ? undefined : raw instanceof ArrayBuffer ? Buffer.from(raw) : Buffer.from(raw as Uint8Array);
  if (bytes && (!bytes.length || bytes.length > 8 * 1024 * 1024)) throw new Error("media_asset_size_invalid");
  const lease = protectedAccess.capture();
  const revision = bootstrapRevision;
  const snapshot = await desktopSession().fetch("https://reactor.tvr.lat/api/blackbird/media", {
    credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(12_000)]),
  });
  lease.assertCurrent();
  refreshAccountAfterDeniedResponse(snapshot.status);
  if (!snapshot.ok) {
    if (snapshot.status === 423) void bootstrap();
    throw new Error("media_unavailable");
  }
  const context = await snapshot.json() as Record<string, unknown>;
  const viewer = context.viewer as { id?: string } | undefined;
  const csrfToken = csrfTokenFromAccountSnapshot(context);
  lease.assertCurrent();
  if (revision !== bootstrapRevision || !lastSuccessfulBootstrap || viewer?.id !== accountIdentityKey(lastSuccessfulBootstrap.viewer) || !csrfToken) throw new Error("session_changed");
  const response = await desktopSession().fetch(`https://reactor.tvr.lat/api/blackbird/media/assets/${kind}`, {
    method: remove ? "DELETE" : "POST", credentials: "include", signal: AbortSignal.any([lease.signal, AbortSignal.timeout(30_000)]),
    headers: { ...desktopIdentityHeaders(), "X-CSRF-Token": csrfToken, ...(remove ? {} : { "Content-Type": "application/octet-stream" }) },
    ...(bytes ? { body: bytes } : {}),
  });
  lease.assertCurrent();
  refreshAccountAfterDeniedResponse(response.status);
  if (response.status === 423) void bootstrap();
  let payload: Record<string, unknown> = {};
  try {
    const value: unknown = await response.json();
    if (value && typeof value === "object" && !Array.isArray(value)) payload = value as Record<string, unknown>;
    else if (response.ok) throw new Error("media_response_invalid");
  } catch {
    if (response.ok) throw new Error("media_response_invalid");
    // A gateway's 500/504 or login page may be HTML, not a JSON response.
  }
  lease.assertCurrent();
  if (revision !== bootstrapRevision || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) throw new Error("session_changed");
  if (!response.ok) throw new Error(String(payload.error || "media_asset_failed"));
  return remove ? payload.removed === true : { revision: String(payload.revision || "") };
}

async function communicateUpload(input: unknown): Promise<Record<string, unknown>> {
  return uploadCommunicateAttachment(accountRequestDependencies(), input);
}

async function communicateAttachment(id: unknown): Promise<{ bytes: Uint8Array; mimeType: string; filename: string }> {
  return downloadCommunicateAttachment(accountRequestDependencies(), id);
}

async function communicateLinkPreview(value: unknown): Promise<Record<string, string> | null> {
  if (desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || !validExternalLink(value)) return null;
  const lease = protectedAccess.capture();
  const revision = bootstrapRevision;
  const viewerId = accountIdentityKey(lastSuccessfulBootstrap.viewer);
  const url = new URL("https://reactor.tvr.lat/api/blackbird/communicate/preview");
  url.searchParams.set("url", value);
  const response = await desktopSession().fetch(url.href, {
    credentials: "include", headers: { ...desktopIdentityHeaders(), Accept: "application/json" }, signal: AbortSignal.any([lease.signal, AbortSignal.timeout(8_000)]),
  });
  lease.assertCurrent();
  refreshAccountAfterDeniedResponse(response.status);
  if (!response.ok) {
    if (response.status === 423) void bootstrap();
    return null;
  }
  const payload = await response.json() as { result?: Record<string, unknown> | null };
  lease.assertCurrent();
  if (revision !== bootstrapRevision || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap ||
      viewerId !== accountIdentityKey(lastSuccessfulBootstrap.viewer)) return null;
  const result = payload.result;
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const clean: Record<string, string> = {};
  for (const key of ["title", "detail", "resource", "status"]) {
    if (typeof result[key] === "string") clean[key] = result[key].slice(0, 300);
  }
  return clean.title ? clean : null;
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
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password;
  }
  catch { return false; }
}

async function openSharedLink(value: unknown): Promise<boolean> {
  if (desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || !validExternalLink(value)) return false;
  const target = serviceForDeepLink(value);
  if (target) {
    const result = await navigate(target.serviceId, target.url);
    if (result.error || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) return false;
    if (mainWindow?.isMinimized()) mainWindow.restore();
    mainWindow?.show(); mainWindow?.focus();
    return true;
  }
  await shell.openExternal(value);
  return true;
}

async function openCommunicateWindow(sharedUrl?: string): Promise<boolean> {
  if (!desktopProduct.privateEdition || !lastSuccessfulBootstrap || desktopLocked || lastKnownBan) return false;
  if (communicateWindow && !communicateWindow.isDestroyed()) {
    if (sharedUrl) {
      if (communicateContextReady) communicateWindow.webContents.send("blackbird:share-link", sharedUrl);
      else pendingCommunicateShare = [pendingCommunicateShare, sharedUrl].filter(Boolean).join("\n");
    }
    if (communicateOpening) return communicateOpening;
    communicateWindow.show(); communicateWindow.focus();
    return true;
  }
  const lease = protectedAccess.capture();
  communicateContextReady = false;
  pendingCommunicateShare = sharedUrl || "";
  const window = new BrowserWindow({
    width: 850, height: 610, minWidth: 700, minHeight: 500, center: true,
    show: false, frame: false, autoHideMenuBar: true, backgroundColor: "#11161a",
    title: "Blackbird Communicate",
    webPreferences: { preload: path.join(bundleDirectory, "../preload/communicate.cjs"), contextIsolation: true,
      nodeIntegration: false, sandbox: true, webSecurity: true, backgroundThrottling: false,
      devTools: !(desktopProduct.privateEdition && app.isPackaged) },
  });
  communicateWindow = window;
  secureContents(window.webContents, { local: true });
  window.on("closed", () => {
    if (communicateWindow !== window) return;
    communicateWindow = null; communicateContextReady = false; communicateOpening = undefined; pendingCommunicateShare = "";
  });
  const opening = (async () => {
    try {
      const rendererUrl = process.env.ELECTRON_RENDERER_URL;
      if (rendererUrl) await window.loadURL(new URL("communicate.html", rendererUrl).href);
      else await window.loadFile(path.join(bundleDirectory, "../renderer/communicate.html"));
      if (communicateWindow !== window || window.isDestroyed() || lease.signal.aborted || desktopLocked || lastKnownBan ||
          !lastSuccessfulBootstrap || accountIdentityKey(lastSuccessfulBootstrap.viewer) !== lease.accountId) {
        if (!window.isDestroyed()) window.close();
        return false;
      }
      window.show(); window.focus();
      return true;
    } catch (error) {
      log.warn("Communicate window could not open", error);
      if (!window.isDestroyed()) window.close();
      return false;
    } finally { if (communicateWindow === window) communicateOpening = undefined; }
  })();
  communicateOpening = opening;
  return opening;
}

function registerIpc(): void {
  const trusted = (event: Electron.IpcMainInvokeEvent): boolean =>
    Boolean(mainWindow && event.sender.id === mainWindow.webContents.id);
  const trustedOverlay = (event: Electron.IpcMainInvokeEvent): boolean =>
    Boolean(atlasOverlay?.ownsSender(event.sender.id));
  const trustedOverlayOrShell = (event: Electron.IpcMainInvokeEvent): boolean =>
    !desktopLocked && !lastKnownBan && (trusted(event) || trustedOverlay(event));

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
    if (!trusted(event) || !["security", "billing", "update", "characters", "characters-update", "communicate", "communicate-update", "media", "media-update"].includes(String(action))) throw new Error("untrusted_request");
    return accountRequest(String(action), data as Record<string, string> | undefined);
  });
  ipcMain.handle("desktop:media-asset", (event, userId: unknown, kind: unknown) => {
    if (!trusted(event)) throw new Error("untrusted_request");
    return mediaAsset(userId, kind);
  });
  ipcMain.handle("desktop:media-upload", (event, kind: unknown, bytes: unknown) => {
    if (!trusted(event)) throw new Error("untrusted_request");
    return mediaAssetWrite(kind, bytes) as Promise<{ revision: string }>;
  });
  ipcMain.handle("desktop:media-remove", (event, kind: unknown) => {
    if (!trusted(event)) throw new Error("untrusted_request");
    return mediaAssetWrite(kind, undefined, true) as Promise<boolean>;
  });
  const trustedCommunicate = (event: Electron.IpcMainInvokeEvent) => Boolean(communicateWindow && event.sender.id === communicateWindow.webContents.id);
  ipcMain.handle("desktop:open-shared-link", (event, value: unknown) => trusted(event) ? openSharedLink(value) : false);
  ipcMain.handle("desktop:communicate-open", event => trusted(event) ? openCommunicateWindow() : false);
  ipcMain.handle("desktop:communicate-share", event => {
    if (!trusted(event) || !serviceView) return false;
    const url = shareableLink(serviceView.webContents.getURL());
    return url ? openCommunicateWindow(url) : false;
  });
  ipcMain.handle("blackbird:communicate-context", event => {
    if (!trustedCommunicate(event)) throw new Error("untrusted_request");
    if (desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) throw new Error("access_locked");
    communicateContextReady = true;
    const sharedUrl = pendingCommunicateShare;
    pendingCommunicateShare = "";
    return { viewerId: lastSuccessfulBootstrap ? accountIdentityKey(lastSuccessfulBootstrap.viewer) : "0", authenticated: Boolean(lastSuccessfulBootstrap), sharedUrl };
  });
  ipcMain.handle("blackbird:communicate-request", (event, action: unknown, data: unknown) => {
    if (!trustedCommunicate(event) || !["communicate", "communicate-update"].includes(String(action))) throw new Error("untrusted_request");
    return accountRequest(String(action), data as Record<string, string> | undefined);
  });
  ipcMain.handle("blackbird:communicate-upload", (event, input: unknown) => {
    if (!trustedCommunicate(event)) throw new Error("untrusted_request");
    return communicateUpload(input);
  });
  ipcMain.handle("blackbird:communicate-attachment", (event, id: unknown) => {
    if (!trustedCommunicate(event)) throw new Error("untrusted_request");
    return communicateAttachment(id);
  });
  ipcMain.handle("blackbird:communicate-link-preview", (event, url: unknown) => {
    if (!trustedCommunicate(event)) throw new Error("untrusted_request");
    return communicateLinkPreview(url);
  });
  ipcMain.handle("blackbird:communicate-close", event => { if (trustedCommunicate(event)) communicateWindow?.close(); });
  ipcMain.handle("blackbird:communicate-minimize", event => { if (trustedCommunicate(event)) communicateWindow?.minimize(); });
  ipcMain.handle("blackbird:communicate-open-link", (event, value: unknown) => trustedCommunicate(event) ? openSharedLink(value) : false);
  ipcMain.handle("blackbird:communicate-copy-link", (event, value: unknown) => {
    if (!trustedCommunicate(event) || desktopLocked || lastKnownBan || !validExternalLink(value)) return false;
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
  ipcMain.handle("desktop:notifications-read", (event, ids: unknown) => trusted(event) && !desktopLocked && !lastKnownBan ? markNotificationsRead(ids) : false);
  ipcMain.handle("desktop:navigate", (event, serviceId: unknown) => {
    if (!trusted(event) || desktopLocked || lastKnownBan || !isServiceId(serviceId)) return state();
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
    if (!trusted(event) || !serviceView || desktopLocked || lastKnownBan) return false;
    const url = serviceView.webContents.getURL();
    if (!isTrustedTModUrl(url)) return false;
    clipboard.writeText(url);
    return true;
  });
  ipcMain.handle("desktop:open-current-link", (event) => {
    if (!trusted(event) || !serviceView || desktopLocked || lastKnownBan) return false;
    const url = serviceView.webContents.getURL();
    if (!isTrustedTModUrl(url)) return false;
    void shell.openExternal(url);
    return true;
  });
  ipcMain.handle("desktop:lock", (event) => trusted(event) ? lockDesktop("manual") : false);
  ipcMain.handle("desktop:consensus-confirm", async (event, input: unknown) => {
    if (!trusted(event) || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap || !input || typeof input !== "object") return false;
    const notice = input as Partial<ConsensusRegistrationNotice>;
    if (typeof notice.sessionKey !== "string" || !notice.sessionKey || notice.sessionKey.length > 128 || typeof notice.csrfToken !== "string" || notice.csrfToken.length > 256) return false;
    const revision = bootstrapRevision;
    const accountId = accountIdentityKey(lastSuccessfulBootstrap.viewer);
    try {
      const lease = protectedAccess.capture();
      const response = await desktopSession().fetch("https://consensus.tvr.lat/api/attendance", {
        method: "POST", credentials: "include", signal: AbortSignal.any([lease.signal, AbortSignal.timeout(12_000)]),
        headers: { ...desktopIdentityHeaders(), "Content-Type": "application/json", "X-CSRF-Token": notice.csrfToken },
        body: JSON.stringify({ session_key: notice.sessionKey }),
      });
      lease.assertCurrent();
      refreshAccountAfterDeniedResponse(response.status);
      const confirmed = response.ok && consensusAttendanceConfirmed(await response.json(), notice.sessionKey!);
      lease.assertCurrent();
      const accepted = confirmed && revision === bootstrapRevision && !desktopLocked && !lastKnownBan &&
        Boolean(lastSuccessfulBootstrap && accountIdentityKey(lastSuccessfulBootstrap.viewer) === accountId);
      if (accepted) consensusInvitations.confirmed(accountId, notice.sessionKey!);
      return accepted;
    } catch (error) { log.warn("Consensus attendance confirmation unavailable", error); return false; }
  });
  ipcMain.handle("desktop:consensus-state", event => trusted(event) && !desktopLocked && !lastKnownBan ? readConsensusLiveState() : null);
  ipcMain.handle("desktop:consensus-ballot", event => trusted(event) && !desktopLocked && !lastKnownBan && desktopProduct.privateEdition
    ? navigate("consensus", "https://consensus.tvr.lat/?view=ballot") : state());
  ipcMain.handle("desktop:unlock", (event) => trusted(event) ? unlockDesktop() : false);
  ipcMain.handle("desktop:open-login", async (event) => {
    if (!trusted(event) || !serviceView || desktopLocked || lastKnownBan) return state();
    const ticket = serviceNavigation.begin(LOGIN_URL);
    if (serviceClearInFlight) await serviceClearInFlight;
    if (!serviceView || desktopLocked || lastKnownBan || !serviceNavigation.current(ticket)) return state();
    clearServiceRetry();
    activeService = "reactor";
    syncServiceVisibility();
    serviceLoading = true;
    emitState();
    await serviceView.webContents.loadURL(LOGIN_URL);
    return state();
  });
  ipcMain.handle("desktop:reload", (event) => {
    if (!trusted(event) || !serviceView || desktopLocked || lastKnownBan || !lastSuccessfulBootstrap) return;
    clearServiceRetry();
    serviceNavigation.begin(serviceView.webContents.getURL());
    lastServiceError = undefined;
    serviceLoading = true;
    syncServiceVisibility();
    emitState();
    serviceView.webContents.reload();
  });
  ipcMain.handle("desktop:back", (event) => {
    if (trusted(event) && !desktopLocked && !lastKnownBan && lastSuccessfulBootstrap && serviceView?.webContents.navigationHistory.canGoBack()) {
      serviceView.webContents.navigationHistory.goBack();
    }
  });
  ipcMain.handle("desktop:forward", (event) => {
    if (trusted(event) && !desktopLocked && !lastKnownBan && lastSuccessfulBootstrap && serviceView?.webContents.navigationHistory.canGoForward()) {
      serviceView.webContents.navigationHistory.goForward();
    }
  });
  ipcMain.handle("desktop:minimize", (event) => {
    if (!trusted(event)) return;
    if (desktopProduct.privateEdition && tray) mainWindow?.hide();
    else mainWindow?.minimize();
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
  desktopHardwareFingerprint = await buildDesktopHardwareFingerprint();
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
      devTools: !(desktopProduct.privateEdition && app.isPackaged),
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
      devTools: !(desktopProduct.privateEdition && app.isPackaged),
    },
  });

  // The service renderer has its own media-query environment. Keep the
  // application's animation policy independent of Windows accessibility flags.
  if (desktopProduct.privateEdition) {
    const contents = serviceView.webContents;
    const enforceMotion = () => {
      if (contents.isDestroyed()) return;
      try {
        if (!contents.debugger.isAttached()) contents.debugger.attach();
        void contents.debugger.sendCommand("Emulation.setEmulatedMedia", {
          features: [{ name: "prefers-reduced-motion", value: "no-preference" }],
        }).catch(error => log.warn("Service motion override unavailable", error));
      } catch (error) { log.warn("Could not attach service motion policy", error); }
    };
    enforceMotion();
    contents.on("did-finish-load", enforceMotion);
  }

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
    openAtlas: async () => { await navigate("atlas"); },
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

  const navigationActive = () => activeService !== "home" && !desktopLocked && !lastKnownBan &&
    Boolean(serviceNavigation.destination) && (!desktopProduct.privateEdition || Boolean(lastSuccessfulBootstrap));
  serviceView.webContents.on("did-start-navigation", details => {
    if (!details.isMainFrame || !navigationActive() || !isTrustedTModUrl(details.url)) return;
    if (details.isSameDocument) {
      serviceNavigation.redirect(details.url);
      emitState();
      return;
    }
    if (!serviceNavigation.matches(details.url)) {
      clearServiceRetry();
      serviceNavigation.begin(details.url);
      const route = serviceForDeepLink(details.url);
      if (route) activeService = route.serviceId;
    }
    serviceNavigation.ready = false;
    serviceNavigation.status = 0;
    serviceLoading = true;
    lastServiceError = undefined;
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-redirect-navigation", details => {
    if (!details.isMainFrame || !navigationActive() || !isTrustedTModUrl(details.url)) return;
    serviceNavigation.redirect(details.url);
  });
  serviceView.webContents.on("did-stop-loading", () => {
    if (!navigationActive() || serviceRetryTimer || serviceView?.webContents.isLoadingMainFrame()) return;
    if (serviceNavigation.status >= 500) {
      if (scheduleServiceRetry(serviceNavigation.status, `HTTP ${serviceNavigation.status}`)) return;
      serviceLoading = false;
      lastServiceError = `service_http_${serviceNavigation.status}`;
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
    if (!navigationActive() || serviceRetryTimer || lastServiceError || serviceNavigation.status >= 500) return;
    const url = serviceView?.webContents.getURL() || "";
    if (!serviceNavigation.matches(url)) return;
    clearServiceRetry();
    serviceLoading = false;
    serviceNavigation.finish(url, !desktopProduct.privateEdition || !isTModAuthenticationUrl(url));
    lastServiceError = serviceNavigation.ready ? undefined : serviceNavigation.status >= 400
      ? `service_http_${serviceNavigation.status}` : "login_required";
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    if (!navigationActive() || !serviceNavigation.matches(url)) return;
    serviceNavigation.ready = false;
    if (scheduleServiceRetry(code, description)) return;
    serviceLoading = false;
    lastServiceError = description || `load_error_${code}`;
    syncServiceVisibility();
    emitState();
  });
  serviceView.webContents.on("did-navigate", (_event, url, httpResponseCode) => {
    if (!navigationActive() || !serviceNavigation.matches(url)) return;
    serviceNavigation.status = Number(httpResponseCode || 0);
    if (
      serviceNavigation.status === 401 ||
      serviceNavigation.status === 423 ||
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
    if ((input.control || input.meta) && (input.code === "Backquote" || input.key === "`")) {
      event.preventDefault();
      mainWindow?.webContents.send("desktop:command-console");
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
  app.on("second-instance", (_event, commandLine) => {
    for (const argument of commandLine) receiveServiceDeepLink(argument);
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
  app.on("open-url", (event, url) => {
    event.preventDefault();
    receiveServiceDeepLink(url);
  });
}

app.whenReady().then(async () => {
  if (!ownsInstanceLock) return;
  app.setAppUserModelId(desktopProduct.appId);
  if (!app.setAsDefaultProtocolClient(desktopProduct.protocol)) {
    log.warn(`${desktopProduct.protocol} URL protocol registration failed`);
  }
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
  notificationPopup?.configure(shellPreferences.notificationCorner, shellPreferences.notificationDurationSeconds);
  configureAutoUpdater();
  await createWindow();
  if (desktopProduct.privateEdition && process.platform === "win32") {
    const icon = nativeImage.createFromPath(path.join(app.getAppPath(), "resources", "blackbird", "icon.ico"));
    if (!icon.isEmpty()) {
      tray = new Tray(icon);
      tray.setToolTip("Blackbird · Технологии Товарищества");
      const reveal = () => { if (mainWindow?.isMinimized()) mainWindow.restore(); mainWindow?.show(); mainWindow?.focus(); };
      tray.on("double-click", reveal);
      tray.setContextMenu(Menu.buildFromTemplate([
        { label: "Открыть Blackbird", click: reveal },
        { type: "separator" },
        { label: "Atlas", click: () => { reveal(); void navigate("atlas"); } },
        { label: "Сенат", click: () => { reveal(); void navigate("reactor"); } },
        { label: "Заблокировать", click: () => { reveal(); lockDesktop("manual"); } },
        { type: "separator" },
        { label: "Проверить обновления", click: () => { void checkForUpdates(); } },
        { label: "Выйти из Blackbird", click: () => app.quit() },
      ]));
    } else log.warn("Blackbird tray icon unavailable");
  }
  screen.on("display-added", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-removed", () => atlasOverlay?.onDisplaysChanged());
  screen.on("display-metrics-changed", () => atlasOverlay?.onDisplaysChanged());
  startIdleLockMonitor();
  if (desktopProduct.privateEdition) notificationPollTimer = setInterval(() => void pollNotifications(), 6_000);
  if (desktopProduct.privateEdition) {
    consensusPollTimer = setInterval(() => void pollConsensusRegistration(), 12_000);
    void pollConsensusRegistration();
  }
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
  tray?.destroy();
  tray = null;
  if (updateTimer) clearInterval(updateTimer);
  if (notificationPollTimer) clearInterval(notificationPollTimer);
  if (consensusPollTimer) clearInterval(consensusPollTimer);
  if (idleLockTimer) clearInterval(idleLockTimer);
  if (authProjectionTimer) clearTimeout(authProjectionTimer);
  if (forcedUpdateInstallTimer) clearTimeout(forcedUpdateInstallTimer);
  atlasOverlay?.dispose();
  notificationPopup?.dispose();
});
