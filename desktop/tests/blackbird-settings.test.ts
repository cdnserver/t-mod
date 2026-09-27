import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { AccountAvatar, trustedDiscordAvatar } from "../src/renderer/AccountAvatar";
import { AccountMenu } from "../src/renderer/AccountMenu";
import { BlackbirdSettings, type SettingsSection } from "../src/renderer/BlackbirdSettings";
import type { DesktopShellPreferences, DesktopUpdateState } from "../src/shared/contracts";

vi.mock("../src/shared/product", () => ({ desktopProduct: { name:"BLACKBIRD", privateEdition:true } }));

const preferences: DesktopShellPreferences = { preferredName:"Роберт", sidebarCollapsed:false, compactMode:false, reduceMotion:false, solidSurfaces:false, serviceZoom:1, idleLockMinutes:10, lockSound:true, notificationDelivery:"both", notificationSound:true, updateChannel:"private" };
const viewer = { id:42, name:"Роберт", display_name:"R. Smith | 123 | Роберт", account_tier:"administrator" as const, guild_member:true, administrator:true, sections:[] };
const props = { preferences, defaults:preferences, viewer, name:"Роберт", online:true, updateState:{ phase:"idle", currentVersion:"1.0" } as DesktopUpdateState,
  atlas:createElement("div", {}, "Существующие настройки Atlas"), onChange:()=>{}, onClose:()=>{}, onReconnect:async()=>{}, onLock:()=>{}, onLogout:async()=>{}, onUpdate:()=>{}, onPreviewNotification:()=>{} };

describe("Blackbird full-page settings", () => {
  it.each<SettingsSection>(["account","appearance","lock","notifications","atlas","updates","connection"])("renders section %s with shared navigation", initialSection => {
    const html = renderToStaticMarkup(createElement(BlackbirdSettings, { ...props, initialSection }));
    expect(html).toContain('class="bb-settings-page"');
    expect(html).toContain('aria-current="page"');
    expect(html).toContain("Закрыть настройки");
    expect(html).not.toContain("settings-drawer");
  });
  it("reuses the full Atlas controls instead of replacing them with placeholders", () => {
    expect(renderToStaticMarkup(createElement(BlackbirdSettings, { ...props, initialSection:"atlas" }))).toContain("Существующие настройки Atlas");
  });
  it("shows actual account identity and logout actions", () => {
    const html = renderToStaticMarkup(createElement(AccountMenu, { viewer, name:"Роберт", open:true, onOpenChange:()=>{}, onSettings:()=>{}, onLock:()=>{}, onLogout:async()=>{} }));
    expect(html).toContain("R. Smith | 123 | Роберт");
    expect(html).toContain("Выйти из аккаунта");
    expect(html).toContain('aria-expanded="true"');
  });
});
describe("Discord avatars", () => {
  it("accepts Discord profile, guild and default avatars", () => {
    for (const path of ["avatars/42/hash.png", "guilds/77/users/42/avatars/hash.webp", "embed/avatars/0.png"]) expect(trustedDiscordAvatar(`https://cdn.discordapp.com/${path}`)).toBeTruthy();
  });
  it("rejects third-party trackers, attachment URLs and unsafe schemes", () => {
    for (const url of ["https://evil.test/avatars/42/h.png", "https://cdn.discordapp.com.evil.test/avatars/42/h.png", "https://cdn.discordapp.com/attachments/42/image.png", "http://cdn.discordapp.com/avatars/42/h.png", "https://user:pass@cdn.discordapp.com/avatars/42/h.png", "javascript:alert(1)"]) expect(trustedDiscordAvatar(url)).toBeUndefined();
  });
  it("provides a local fallback without inventing a Discord image", () => {
    const html = renderToStaticMarkup(createElement(AccountAvatar, { name:"Роберт", url:"https://evil.test/track.png" }));
    expect(html).toContain("Р"); expect(html).not.toContain("<img");
  });
});
