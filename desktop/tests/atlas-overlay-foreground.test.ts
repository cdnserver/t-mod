import { describe, expect, it } from "vitest";
import {
  parseAtlasOverlayForegroundProbe,
  resolveAtlasOverlayDisplayArea,
  resolveAtlasOverlayForegroundGame,
  resolveAtlasOverlayWindowBounds,
} from "../src/main/atlas-overlay-foreground";

describe("Atlas Overlay foreground guard", () => {
  it("accepts a visible GTA/RAGE foreground process and preserves its work area", () => {
    const probe = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Majestic RP — Grand Theft Auto V",
      processName: "RAGEMP_v.exe",
      processId: 4242,
      windowHandle: "998877",
      visible: true,
      minimized: false,
      workArea: { x: 1920, y: 0, width: 2560, height: 1400 },
    }));

    expect(resolveAtlasOverlayForegroundGame(probe)).toEqual({
      title: "Majestic RP — Grand Theft Auto V",
      processName: "RAGEMP_v",
      processId: 4242,
      mediaSourceId: "window:998877:0",
      foregroundVerified: true,
      workArea: { x: 1920, y: 0, width: 2560, height: 1400 },
    });
  });

  it("fails closed for another app, a minimized window, or malformed monitor data", () => {
    const browserTitle = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Grand Theft Auto V guide - browser tab",
      processName: "chrome.exe",
      processId: 101,
      visible: true,
      minimized: false,
      workArea: { x: 0, y: 0, width: 1920, height: 1080 },
    }));
    const minimizedGame = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Grand Theft Auto V",
      processName: "GTA5.exe",
      processId: 102,
      visible: true,
      minimized: true,
      workArea: { x: 0, y: 0, width: 1920, height: 1080 },
    }));
    const malformedArea = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Grand Theft Auto V",
      processName: "GTA5.exe",
      processId: 103,
      visible: true,
      minimized: false,
      workArea: { x: 0, y: 0, width: 10, height: 10 },
    }));

    expect(resolveAtlasOverlayForegroundGame(browserTitle)).toBeUndefined();
    expect(resolveAtlasOverlayForegroundGame(minimizedGame)).toBeUndefined();
    expect(resolveAtlasOverlayForegroundGame(malformedArea)).toBeUndefined();
    expect(resolveAtlasOverlayForegroundGame(parseAtlasOverlayForegroundProbe('{"available":false}'))).toBeUndefined();
    expect(parseAtlasOverlayForegroundProbe("not json")).toBeUndefined();
  });

  it("requires a real foreground process identifier before exposing a game area", () => {
    const missingProcess = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Grand Theft Auto V",
      processName: "GTA5.exe",
      visible: true,
      minimized: false,
      workArea: { x: 0, y: 0, width: 1920, height: 1080 },
    }));
    expect(resolveAtlasOverlayForegroundGame(missingProcess)).toBeUndefined();
  });

  it("accepts current Enhanced, BattlEye and Majestic launcher process variants", () => {
    for (const processName of [
      "GTA5_Enhanced_BE.exe",
      "GTA5_BE.exe",
      "RAGEMP_launcher.exe",
      "MajesticLauncher.exe",
      "GTA5_Enhanced_BE_3095.exe",
      "RAGEMP_client_release.exe",
    ]) {
      const probe = parseAtlasOverlayForegroundProbe(JSON.stringify({
        available: true,
        title: "Majestic RP",
        processName,
        processId: 4040,
        visible: true,
        minimized: false,
        workArea: { x: 0, y: 0, width: 2560, height: 1440 },
      }));
      expect(resolveAtlasOverlayForegroundGame(probe), processName).toBeDefined();
    }
  });

  it("rejects malformed native window handles instead of creating a z-order target", () => {
    const probe = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Grand Theft Auto V",
      processName: "GTA5.exe",
      processId: 202,
      windowHandle: "12:34",
      visible: true,
      minimized: false,
      workArea: { x: 0, y: 0, width: 1920, height: 1080 },
    }));
    expect(resolveAtlasOverlayForegroundGame(probe)?.mediaSourceId).toBeUndefined();
  });

  it("maps a native monitor rectangle onto Electron DPI coordinates", () => {
    expect(resolveAtlasOverlayDisplayArea(
      { x: 0, y: 0, width: 2560, height: 1440 },
      [
        { x: 0, y: 0, width: 1707, height: 920 },
        { x: 1707, y: 0, width: 1920, height: 1040 },
      ],
    )).toEqual({ x: 0, y: 0, width: 1707, height: 920 });

    expect(resolveAtlasOverlayDisplayArea(
      { x: 2560, y: 0, width: 1920, height: 1080 },
      [
        { x: 0, y: 0, width: 2560, height: 1400 },
        { x: 2560, y: 0, width: 1920, height: 1040 },
      ],
    )).toEqual({ x: 2560, y: 0, width: 1920, height: 1040 });
  });

  it("keeps the overlay inside compact and offset display work areas", () => {
    expect(resolveAtlasOverlayWindowBounds(
      { x: 1920, y: -120, width: 640, height: 360 },
      1,
      1,
    )).toEqual({ x: 1929, y: -111, width: 622, height: 342 });
    expect(resolveAtlasOverlayWindowBounds(
      { x: -1280, y: 0, width: 1280, height: 720 },
      0,
      .5,
    )).toEqual({ x: -1262, y: 50, width: 760, height: 620 });
  });
});
