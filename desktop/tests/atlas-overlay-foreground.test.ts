import { describe, expect, it } from "vitest";
import {
  parseAtlasOverlayForegroundProbe,
  resolveAtlasOverlayForegroundGame,
} from "../src/main/atlas-overlay-foreground";

describe("Atlas Overlay foreground guard", () => {
  it("accepts a visible GTA/RAGE foreground process and preserves its work area", () => {
    const probe = parseAtlasOverlayForegroundProbe(JSON.stringify({
      available: true,
      title: "Majestic RP — Grand Theft Auto V",
      processName: "RAGEMP_v.exe",
      processId: 4242,
      visible: true,
      minimized: false,
      workArea: { x: 1920, y: 0, width: 2560, height: 1400 },
    }));

    expect(resolveAtlasOverlayForegroundGame(probe)).toEqual({
      title: "Majestic RP — Grand Theft Auto V",
      processName: "RAGEMP_v",
      processId: 4242,
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
});
