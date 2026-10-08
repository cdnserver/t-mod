import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { BlackbirdIdle } from "../src/renderer/BlackbirdIdle";

describe("Blackbird observatory", () => {
  it("has its own scene, stable time and minimize action without old vault copy", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-27T22:30:00"));
    const html = renderToStaticMarkup(createElement(BlackbirdIdle, { name:"Роберт", reduced:false, unlocking:false, onMinimize:()=>{} }));
    expect(html).toContain("Добрый вечер"); expect(html).toContain("Роберт"); expect(html).toContain("Свернуть приложение");
    expect(html).toContain("22:30"); expect(html).not.toContain("Вручную"); expect(html).not.toContain("vault-screen");
    vi.useRealTimers();
  });
  it("native timer locks independently of focus and shell rendering remains unthrottled", () => {
    const main = readFileSync(new URL("../src/main/index.ts", import.meta.url), "utf8");
    const monitor = main.slice(main.indexOf("function startIdleLockMonitor"), main.indexOf("function clearServiceRetry"));
    expect(monitor).toContain("powerMonitor.getSystemIdleTime()"); expect(monitor).not.toContain("isFocused");
    expect(main).toContain("backgroundThrottling: false"); expect(main).toContain('mainWindow.on("restore", emitState)');
    expect(main).toContain("locked: desktopLocked");
  });
  it("shows access verification and a recoverable error on the pause scene", () => {
    const verifying = renderToStaticMarkup(createElement(BlackbirdIdle, { name: "Роберт", reduced: false, unlocking: false, checking: true, onMinimize: () => {} }));
    const unavailable = renderToStaticMarkup(createElement(BlackbirdIdle, { name: "Роберт", reduced: false, unlocking: false, error: true, onMinimize: () => {} }));
    expect(verifying).toContain("Проверяем доступ");
    expect(unavailable).toContain("Нет связи или доступ закрыт");
  });
  it("unloads protected pages before logout and waits before restoring a service", () => {
    const main = readFileSync(new URL("../src/main/index.ts", import.meta.url), "utf8");
    const logout = main.slice(main.indexOf("async function logout()"), main.indexOf("async function accountRequest("));
    const unlock = main.slice(main.indexOf("function unlockDesktop()"), main.indexOf("function startIdleLockMonitor()"));
    const navigation = main.slice(main.indexOf("async function navigate("), main.indexOf("function shareableLink("));
    expect(logout.indexOf("clearSensitiveServiceView()")).toBeLessThan(logout.indexOf("await desktopSession().fetch(LOGOUT_URL"));
    expect(unlock).toContain("if (serviceClearInFlight) await serviceClearInFlight");
    expect(unlock).toContain('return "login_required"');
    expect(navigation).toContain("if (serviceClearInFlight) await serviceClearInFlight");
  });
});
