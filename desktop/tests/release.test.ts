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
    expect(manifest.version).toBe("0.3.5-dev.4");
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
    expect(renderer).toContain("idleLockMinutes: 10");
    expect(renderer).toContain('updateChannel: "beta"');
    const cinematics = readFileSync(resolve(root, "src/renderer/cinematics.css"), "utf8");
    expect(cinematics).toContain(".vault-stage");
    expect(styles).toContain(".update-channel-setting");
  });

  it("shows the branded launch sequence and allows its local audio signature", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    const styles = readFileSync(resolve(root, "src/renderer/styles.css"), "utf8");
    expect(main).toContain('appendSwitch("autoplay-policy", "no-user-gesture-required")');
    const cinematics = readFileSync(resolve(root, "src/renderer/cinematics.css"), "utf8");
    expect(renderer).toContain("CinematicLaunch");
    expect(renderer).toContain("playIgnitionSound");
    expect(renderer).toContain("launchVisible && <CinematicLaunch");
    expect(cinematics).toContain(".ignition-monolith");
    expect(cinematics).toContain("@keyframes ignition-monolith-in");
    expect(cinematics).toContain("@keyframes ignition-exit");
    expect(cinematics).not.toContain("launch-orbit");
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
