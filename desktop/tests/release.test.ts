import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..");
const workspaceRoot = resolve(root, "..");

describe("desktop release contract", () => {
  it("never ships demonstration identities", () => {
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    expect(renderer).not.toContain("mockBootstrap");
    expect(renderer).not.toContain("S. Goodman | 263345 | Иван");
    expect(renderer).not.toContain("721577061143019555");
  });

  it("uses the production Electron bridge and native account flow", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain("new BrowserWindow");
    expect(main).not.toContain("new BaseWindow");
    expect(main).toContain("/auth/login?client=desktop");
    expect(preload).toContain('contextBridge.exposeInMainWorld("tmodDesktop"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:login"');
  });

  it("publishes installers and updater metadata from the public release channel", () => {
    const manifest = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
    expect(manifest.version).toMatch(/^\d+\.\d+\.\d+(?:-dev\.\d+)?$/);
    expect(manifest.build.publish).toEqual([
      expect.objectContaining({
        provider: "github",
        owner: "cdnserver",
        repo: "t-mod-releases",
      }),
    ]);
    expect(manifest.build.win.target).toBe("nsis");
    expect(manifest.build.mac.target).toContain("zip");
  });

  it("keeps shell controls above every remote service contour", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain('ipcMain.handle("desktop:shell-overlay"');
    expect(main).toContain('mainWindow?.webContents.send("desktop:command-palette")');
    expect(main).toContain("syncServiceVisibility");
    expect(preload).toContain('ipcRenderer.invoke("desktop:shell-overlay"');
    expect(preload).toContain('ipcRenderer.on("desktop:command-palette"');
  });

  it("keeps beta sessions resilient and exposes shell preferences", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain("SERVICE_RETRY_DELAYS");
    expect(main).toContain("TModDesktop/${app.getVersion()}");
    expect(main).toContain('ipcMain.handle("desktop:preferences"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:preferences"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:copy-current-link"');
  });

  it("locks the complete service surface and supports Beta or Dev updates", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    const styles = readFileSync(resolve(root, "src/renderer/styles.css"), "utf8");
    expect(main).toContain("powerMonitor.getSystemIdleTime()");
    expect(main).toContain("!desktopLocked");
    expect(main).toContain('ipcMain.handle("desktop:lock"');
    expect(main).toContain('shellPreferences.updateChannel === "dev"');
    expect(main).toContain('autoUpdater.channel = shellPreferences.updateChannel === "dev" ? "dev" : "latest"');
    expect(preload).toContain('ipcRenderer.on("desktop:lock-requested"');
    expect(renderer).toContain("VaultScreen");
    expect(renderer).toContain("event.repeat || unlocking");
    expect(renderer).not.toContain("onPointerDown={onUnlock}");
    expect(renderer).toContain("onMinimize={() => void browserApi()?.minimize()}");
    expect(renderer).toContain('preferredName: ""');
    expect(renderer).toContain("preferences.preferredName.trim()");
    expect(renderer).toContain("Как вас называть");
    expect(renderer).toContain("idleLockMinutes: 10");
    expect(renderer).toContain('updateChannel: "beta"');
    const cinematics = readFileSync(resolve(root, "src/renderer/cinematics.css"), "utf8");
    const cinematicRenderer = readFileSync(resolve(root, "src/renderer/cinematics.tsx"), "utf8");
    expect(cinematics).toContain(".cosmic-lock");
    expect(cinematics).toContain(".lock-minimize");
    expect(cinematics).toContain(".cosmic-clock");
    expect(cinematics).toContain("font-variant-numeric:tabular-nums");
    expect(cinematicRenderer).toContain('String(now.getMinutes()).padStart(2, "0")');
    expect(cinematics).toContain(".lock-celestial");
    expect(styles).toContain(".preferred-name-setting");
    expect(styles).toContain(".update-channel-setting");
  });

  it("shows the branded launch sequence and allows its local audio signature", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    expect(main).toContain('appendSwitch("autoplay-policy", "no-user-gesture-required")');
    expect(main).toContain("screen.getPrimaryDisplay()");
    expect(main).toContain("workArea.width * .96");
    expect(main).toContain("workArea.height * .94");
    const cinematics = readFileSync(resolve(root, "src/renderer/cinematics.css"), "utf8");
    expect(renderer).toContain("CinematicLaunch");
    expect(renderer).toContain("playIgnitionSound");
    expect(renderer).toContain("launchVisible && <CinematicLaunch");
    expect(cinematics).toContain(".cinema-meteors");
    expect(cinematics).toContain("@keyframes meteor-flight");
    expect(cinematics).toContain("@keyframes star-twinkle");
    expect(cinematics).toContain(".cosmic-signatures");
    expect(cinematics).toContain(".cinema-focus");
    expect(cinematics).toContain(".planet-terminator");
    expect(cinematics).toContain("@keyframes cinema-stage-out");
    expect(cinematics).not.toContain("feTurbulence");
    const cinematicRenderer = readFileSync(resolve(root, "src/renderer/cinematics.tsx"), "utf8");
    expect(cinematicRenderer).toContain("function MeteorShower(");
    expect(cinematicRenderer).toContain("function CosmicSignatures(");
    expect(cinematicRenderer).toContain("Array.from({ length: 68 }");
    expect(cinematicRenderer).toContain("function warmBloom(");
    expect(cinematicRenderer).not.toContain("function starFall(");
  });

  it("keeps Atlas Overlay lifecycle failures isolated from the desktop shell", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const controller = readFileSync(resolve(root, "src/main/atlas-overlay-controller.ts"), "utf8");
    expect(main).toContain("applyAtlasOverlayBootstrapSafely");
    expect(main).toContain("failed without blocking Desktop");
    expect(main).toContain("Atlas overlay initialization failed without blocking Desktop");
    expect(main).toContain("atlasOverlay = undefined;");
    expect(controller).toContain('this.csrfToken = "";');
    expect(controller).toContain("this.activeThreadId = undefined;");
    expect(controller).toContain("this.clearHideTimer();");
  });

  it("ships cursor-free Atlas calibration and background animation protection", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const helper = readFileSync(resolve(root, "resources/atlas-overlay-hotkey.ps1"), "utf8");
    const controller = readFileSync(resolve(root, "src/main/atlas-overlay-controller.ts"), "utf8");
    expect(main).toContain('appendSwitch("disable-renderer-backgrounding")');
    expect(main).toContain('appendSwitch("disable-background-timer-throttling")');
    expect(helper).toContain('[Console]::Out.WriteLine("edit")');
    expect(helper).toContain('"move:left" = 0x25');
    expect(helper).toContain('"width:up" = 0xDD');
    expect(controller).toContain('line === "edit-done"');
    expect(controller).toContain("reportSpeech(active: boolean)");
  });

  it("shows Atlas initialization once per app launch and avoids expensive overlay blur", () => {
    const controller = readFileSync(resolve(root, "src/main/atlas-overlay-controller.ts"), "utf8");
    const styles = readFileSync(resolve(root, "src/renderer/overlay/atlas-overlay.css"), "utf8");
    expect(controller).toContain("private initializationPresented = false");
    expect(controller).toContain("!this.initializationPresented");
    expect(controller).not.toContain("initializedGameProcessId");
    expect(controller).toContain("speechSynthesisPending");
    expect(styles).not.toContain("backdrop-filter:");
    expect(styles).toContain("contain: layout paint style");
  });

  it("permits only in-memory Atlas voice audio inside the hardened overlay", () => {
    const overlay = readFileSync(resolve(root, "src/renderer/overlay.html"), "utf8");
    expect(overlay).toContain("media-src blob:");
    expect(overlay).toContain("connect-src 'none'");
    expect(overlay).not.toContain("media-src *");
  });

  it("ships one complete vector identity for every desktop contour", () => {
    const services = [
      "home",
      "reactor",
      "consensus",
      "atlas",
      "sgl",
      "ovr",
      "games",
      "tasks",
      "admin",
    ];
    const sizes = [24, 32, 64, 128, 256];

    for (const service of services) {
      expect(existsSync(resolve(workspaceRoot, `brand/services/${service}.svg`))).toBe(true);
      expect(existsSync(resolve(workspaceRoot, `brand/services/mono/${service}.svg`))).toBe(true);
      for (const size of sizes) {
        expect(existsSync(resolve(workspaceRoot, `brand/exports/color/${size}/${service}.png`))).toBe(true);
        expect(existsSync(resolve(workspaceRoot, `brand/exports/mono/${size}/${service}.png`))).toBe(true);
      }
    }

    for (const variant of ["color", "mono-light", "mono-dark"]) {
      for (const size of sizes) {
        expect(existsSync(resolve(workspaceRoot, `brand/exports/tmod-${variant}/${size}/tmod.png`))).toBe(true);
      }
    }

    const masterMark = readFileSync(resolve(workspaceRoot, "brand/tmod/mark.svg"), "utf8");
    const desktopMark = readFileSync(resolve(root, "resources/icon.svg"), "utf8");
    expect(desktopMark).toBe(masterMark);
  });

  it("keeps Atlas cartographic and SGL institutional", () => {
    const atlas = readFileSync(resolve(workspaceRoot, "brand/services/atlas.svg"), "utf8");
    const sgl = readFileSync(resolve(workspaceRoot, "brand/services/sgl.svg"), "utf8");
    expect(atlas).toContain('<circle cx="58" cy="70" r="34"');
    expect(atlas).toContain('m98 17 3.5 8.5');
    expect(sgl).toContain('m24 49 40-24 40 24H24Z');
    expect(sgl).toContain('M36 55v43');
  });

  it("uses a dedicated transparent multi-resolution Windows icon", () => {
    const manifest = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
    const iconPath = resolve(root, "resources/icon.ico");
    expect(manifest.build.win.icon).toBe("resources/icon.ico");
    expect(existsSync(iconPath)).toBe(true);
    expect([...readFileSync(iconPath).subarray(0, 4)]).toEqual([0, 0, 1, 0]);
  });

  it("publishes installers without consuming Actions artifact storage", () => {
    const workflow = readFileSync(resolve(workspaceRoot, ".github/workflows/desktop-release.yml"), "utf8");
    expect(workflow).toContain("Prepare public release");
    expect(workflow).toContain("gh release upload");
    expect(workflow).not.toContain("actions/upload-artifact");
    expect(workflow).not.toContain("actions/download-artifact");
  });
});
