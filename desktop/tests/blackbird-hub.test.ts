import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BlackbirdHub } from "../src/renderer/BlackbirdHub";
import { BlackbirdLogin } from "../src/renderer/BlackbirdLogin";
import { BlackbirdMediaNetwork } from "../src/renderer/BlackbirdMediaNetwork";
import { DEFAULT_ATLAS_OVERLAY_CONFIG } from "../src/shared/atlas-overlay";
import type { DesktopNotification } from "../src/shared/contracts";

const base = { name: "Роберт", tier: "Полный контур", online: true, notifications: [] as DesktopNotification[], access: new Map(), overlayConfig: DEFAULT_ATLAS_OVERLAY_CONFIG, overlayAllowed: false, onOpen: async () => {}, onOverlaySettings: () => {}, onOpenNotifications: () => {}, onOpenMediaNetwork: () => {} };
describe("Blackbird workspace", () => {
  it("does not invent account activity when no events exist", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdHub, base));
    expect(html).toContain("Пока всё спокойно");
    expect(html).toContain("Доступ не выдан");
    expect(html).toContain("disabled");
    expect(html).not.toContain("bb3-rail");
    expect(html).not.toContain("bb-wordmark");
    expect(html).not.toContain('alt="Blackbird"');
    expect(html).toContain("Медиасеть");
  });
  it("counts only unread events, not all historical messages", () => {
    const item: DesktopNotification = { id: 1, title: "Событие", body: "Материал", route: null, severity: "info", kind: "test", created_at: "2026-09-27T10:00:00Z", read_at: null };
    const html = renderToStaticMarkup(createElement(BlackbirdHub, { ...base, notifications: [item, { ...item, id: 2, read_at: item.created_at }] }));
    expect(html).toContain('Новые события <b>1</b>');
  });
  it("escapes notification and user text", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdHub, { ...base, name: "<script>alert(1)</script>" }));
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
});
describe("login connection display", () => {
  it("shows progress instead of an outage during a submitted login", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdLogin, { login: "", pin: "", busy: true, online: false, bridgeAvailable: true, onLoginChange: () => {}, onPinChange: () => {}, onSubmit: () => {}, onRetry: () => {} }));
    expect(html).toContain("Устанавливаем защищённую сессию");
    expect(html).toContain('aria-busy="true"');
    expect(html).not.toContain("Восстанавливаем соединение");
  });
});
describe("media network", () => {
  it("loads privately and does not expose account data before the authenticated response", () => {
    const html = renderToStaticMarkup(createElement(BlackbirdMediaNetwork, { name: "<Роберт>", avatarUrl: null, onBack: () => {} }));
    expect(html).toContain("Медиасеть");
    expect(html).toContain("ПРИВАТНЫЙ ПРОФИЛЬ");
    expect(html).toContain("Открываем Медиасеть");
    expect(html).toContain("Люди");
    expect(html).toContain("Откаты");
    expect(html).not.toContain("<Роберт>");
    expect(html).not.toContain("Опубликовать");
  });
});
