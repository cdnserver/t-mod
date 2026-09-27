import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BlackbirdSetup } from "../src/renderer/BlackbirdSetup";
import type { DesktopShellPreferences } from "../src/shared/contracts";
const preferences: DesktopShellPreferences = {
  introStyle: "letters", controlBar: "horizontal",
  preferredName: "", sidebarCollapsed: false, compactMode: false, reduceMotion: false, solidSurfaces: false,
  serviceZoom: 1, idleLockMinutes: 10, lockSound: true, notificationDelivery: "both", notificationSound: true, updateChannel: "private",
};
describe("Blackbird first-run identity gate", () => {
  const base = { name: "Роберт", preferences, onLogin: async () => ({ ok: false } as const), onComplete: () => {}, onLater: () => {} };
  it("asks unauthenticated users to connect their real account", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdSetup, { ...base, authenticated: false }));
    expect(html).toContain("Подключить аккаунт");
    expect(html).toContain('type="password"');
    expect(html).not.toContain("Открыть Blackbird →");
    expect(html).toContain('alt="Технологии Товарищества"');
    expect(html).not.toContain('class="bb-wordmark"');
    expect(html).not.toContain("bb-setup-emblem");
    expect(html).not.toContain("ПЕРВЫЙ ЗАПУСК · WINDOWS");
    expect(html).toContain('aria-current="step"');
  });
  it("does not ask an already authenticated user for credentials again", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdSetup, { ...base, authenticated: true }));
    expect(html).toContain("Как к вам обращаться?");
    expect(html).not.toContain('type="password"');
    expect(html).toContain('value="Роберт"');
  });
});
