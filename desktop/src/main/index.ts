import {
  app,
  BaseWindow,
  ipcMain,
  nativeTheme,
  session,
  shell,
  WebContentsView,
} from "electron";
import { fileURLToPath } from "node:url";
import path from "node:path";
import log from "electron-log/main";
import electronUpdater from "electron-updater";
import {
  isServiceId,
  isTrustedTModUrl,
  serviceById,
} from "../shared/services";
import type {
  BootstrapResult,
  DesktopState,
  DesktopUpdateState,
  ServiceId,
} from "../shared/contracts";

const { autoUpdater } = electronUpdater;

// electron-vite injects its own `__dirname` shim into the ESM bundle.
// A distinct name avoids a duplicate top-level declaration in packaged builds.
const bundleDirectory = path.dirname(fileURLToPath(import.meta.url));
const SHELL_HEADER_HEIGHT = 70;
const SHELL_SIDEBAR_WIDTH = 286;
const DESKTOP_PARTITION = "persist:tmod-desktop-v1";
const BOOTSTRAP_URL = "https://tvr.lat/api/desktop/v1/bootstrap";
const LOGIN_URL = "https://tvr.lat/login?next=/reactor";
const UPDATE_INTERVAL_MS = 6 * 60 * 60 * 1_000;

let mainWindow: BaseWindow | null = null;
let shellView: WebContentsView | null = null;
let serviceView: WebContentsView | null = null;
let activeService: ServiceId = "home";
let serviceLoading = false;
let lastServiceError: string | undefined;
let updateTimer: ReturnType<typeof setInterval> | undefined;
let updateState: DesktopUpdateState = {
  phase: app.isPackaged ? "idle" : "development",
  currentVersion: app.getVersion(),
};

app.enableSandbox();
nativeTheme.themeSource = "dark";

function desktopSession() {
  return session.fromPartition(DESKTOP_PARTITION, { cache: true });
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
  if (shellView && !shellView.webContents.isDestroyed()) {
    shellView.webContents.send("desktop:state", state());
  }
}

function setUpdateState(next: Partial<DesktopUpdateState>): DesktopUpdateState {
  updateState = {
    ...updateState,
    ...next,
    currentVersion: app.getVersion(),
  };
  if (shellView && !shellView.webContents.isDestroyed()) {
    shellView.webContents.send("desktop:update", updateState);
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
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowPrerelease = false;

  autoUpdater.on("checking-for-update", () => {
    setUpdateState({ phase: "checking", message: undefined, percent: undefined });
  });
  autoUpdater.on("update-available", (info) => {
    setUpdateState({ phase: "available", version: info.version, percent: 0 });
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
      checkedAt: new Date().toISOString(),
    });
  });
  autoUpdater.on("update-not-available", (info) => {
    setUpdateState({
      phase: "current",
      version: info.version,
      percent: undefined,
      checkedAt: new Date().toISOString(),
    });
  });
  autoUpdater.on("error", (error) => {
    setUpdateState({
      phase: "error",
      message: error.message || "Не удалось получить обновление.",
      checkedAt: new Date().toISOString(),
    });
  });

  setTimeout(() => void checkForUpdates(), 5_000);
  updateTimer = setInterval(() => void checkForUpdates(), UPDATE_INTERVAL_MS);
}

function positionViews(): void {
  if (!mainWindow || !shellView || !serviceView) return;
  const [width, height] = mainWindow.getContentSize();
  shellView.setBounds({ x: 0, y: 0, width, height });
  serviceView.setBounds({
    x: SHELL_SIDEBAR_WIDTH,
    y: SHELL_HEADER_HEIGHT,
    width: Math.max(1, width - SHELL_SIDEBAR_WIDTH),
    height: Math.max(1, height - SHELL_HEADER_HEIGHT),
  });
}

function secureContents(view: WebContentsView, options: { local: boolean }): void {
  const { local } = options;
  const contents = view.webContents;
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
  activeService = serviceId;
  lastServiceError = undefined;

  if (serviceId === "home") {
    serviceLoading = false;
    serviceView.setVisible(false);
    emitState();
    return state();
  }

  const target = serviceById[serviceId]?.url;
  if (!target || !isTrustedTModUrl(target)) {
    lastServiceError = "service_route_invalid";
    emitState();
    return state();
  }

  serviceView.setVisible(true);
  serviceLoading = true;
  emitState();
  const current = serviceView.webContents.getURL();
  if (current !== target) {
    try {
      await serviceView.webContents.loadURL(target);
    } catch (error) {
      lastServiceError = error instanceof Error ? error.message : String(error);
      serviceLoading = false;
      emitState();
    }
  } else {
    serviceLoading = false;
    emitState();
  }
  return state();
}

async function bootstrap(): Promise<BootstrapResult> {
  try {
    const response = await desktopSession().fetch(BOOTSTRAP_URL, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      return { authenticated: false, online: true, error: "login_required" };
    }
    if (!response.ok) {
      return {
        authenticated: false,
        online: true,
        error: `bootstrap_http_${response.status}`,
      };
    }
    return {
      authenticated: true,
      online: true,
      data: await response.json(),
    } as BootstrapResult;
  } catch (error) {
    return {
      authenticated: false,
      online: false,
      error: error instanceof Error ? error.message : "network_unavailable",
    };
  }
}

function registerIpc(): void {
  const trusted = (event: Electron.IpcMainInvokeEvent): boolean =>
    Boolean(shellView && event.sender.id === shellView.webContents.id);

  ipcMain.handle("desktop:bootstrap", (event) =>
    trusted(event)
      ? bootstrap()
      : ({ authenticated: false, online: false, error: "untrusted_sender" } satisfies BootstrapResult),
  );
  ipcMain.handle("desktop:navigate", (event, serviceId: unknown) => {
    if (!trusted(event) || !isServiceId(serviceId)) return state();
    return navigate(serviceId);
  });
  ipcMain.handle("desktop:open-login", async (event) => {
    if (!trusted(event) || !serviceView) return state();
    activeService = "reactor";
    serviceView.setVisible(true);
    serviceLoading = true;
    emitState();
    await serviceView.webContents.loadURL(LOGIN_URL);
    return state();
  });
  ipcMain.handle("desktop:reload", (event) => {
    if (trusted(event)) serviceView?.webContents.reload();
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
}

async function createWindow(): Promise<void> {
  mainWindow = new BaseWindow({
    width: 1480,
    height: 940,
    minWidth: 1080,
    minHeight: 700,
    show: false,
    frame: false,
    backgroundColor: "#07090f",
    title: "T-Mod",
  });

  shellView = new WebContentsView({
    webPreferences: {
      preload: path.join(bundleDirectory, "../preload/index.mjs"),
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

  secureContents(shellView, { local: true });
  secureContents(serviceView, { local: false });
  const networkSession = desktopSession();
  networkSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  networkSession.setPermissionCheckHandler(() => false);

  mainWindow.contentView.addChildView(shellView);
  mainWindow.contentView.addChildView(serviceView);
  serviceView.setVisible(false);
  positionViews();

  serviceView.webContents.on("did-start-loading", () => {
    serviceLoading = true;
    lastServiceError = undefined;
    emitState();
  });
  serviceView.webContents.on("did-stop-loading", () => {
    serviceLoading = false;
    emitState();
    if (isTrustedTModUrl(serviceView?.webContents.getURL() || "")) {
      shellView?.webContents.send("desktop:auth-changed");
    }
  });
  serviceView.webContents.on("did-fail-load", (_event, code, description) => {
    if (code === -3) return;
    serviceLoading = false;
    lastServiceError = description || `load_error_${code}`;
    emitState();
  });
  serviceView.webContents.on("did-navigate", emitState);
  serviceView.webContents.on("did-navigate-in-page", emitState);

  mainWindow.on("resize", positionViews);
  mainWindow.on("maximize", positionViews);
  mainWindow.on("unmaximize", positionViews);
  mainWindow.on("closed", () => {
    shellView?.webContents.close();
    serviceView?.webContents.close();
    shellView = null;
    serviceView = null;
    mainWindow = null;
  });

  const rendererUrl = process.env.ELECTRON_RENDERER_URL;
  if (rendererUrl) {
    await shellView.webContents.loadURL(rendererUrl);
  } else {
    await shellView.webContents.loadFile(path.join(bundleDirectory, "../renderer/index.html"));
  }
  mainWindow.show();
}

app.whenReady().then(async () => {
  app.setAppUserModelId("lat.tvr.tmod.desktop");
  app.setAsDefaultProtocolClient("tmod");
  registerIpc();
  configureAutoUpdater();
  await createWindow();
  setUpdateState({});

  app.on("activate", () => {
    if (BaseWindow.getAllWindows().length === 0) void createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (updateTimer) clearInterval(updateTimer);
});
