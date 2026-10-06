import { describe, expect, it } from "vitest";
import {
  isServiceId,
  isTModAuthenticationUrl,
  isTrustedTModUrl,
  mergeServiceAccess,
  resolveNotificationServiceId,
  services,
  TRUSTED_TMOD_HOSTS,
} from "../src/shared/services";

describe("desktop service boundary", () => {
  it("accepts only declared service identifiers", () => {
    expect(isServiceId("atlas")).toBe(true);
    expect(isServiceId("unknown-service")).toBe(false);
    expect(isServiceId({ id: "atlas" })).toBe(false);
  });

  it("allows secure T-Mod hosts and rejects lookalikes", () => {
    expect(isTrustedTModUrl("https://home.tvr.lat/")).toBe(true);
    expect(isTrustedTModUrl("https://consensus.tvr.lat/")).toBe(true);
    expect(isTrustedTModUrl("http://home.tvr.lat/")).toBe(false);
    expect(isTrustedTModUrl("https://tvr.lat.attacker.example/")).toBe(false);
    expect(isTrustedTModUrl("https://unknown.tvr.lat/")).toBe(false);
    expect(isTrustedTModUrl("https://tvr.lat:8443/reactor")).toBe(false);
    expect(isTrustedTModUrl("https://user:password@tvr.lat/reactor")).toBe(false);
    expect(isTrustedTModUrl("javascript:alert(1)")).toBe(false);
    expect(TRUSTED_TMOD_HOSTS.has("atlas.tvr.lat")).toBe(true);
    expect(TRUSTED_TMOD_HOSTS.has("dash.tvr.lat")).toBe(true);
    expect(TRUSTED_TMOD_HOSTS.has("senate.tvr.lat")).toBe(true);
    expect(TRUSTED_TMOD_HOSTS.has("ap.finance.tvr.lat")).toBe(true);
  });

  it("recognizes only canonical T-Mod authentication routes", () => {
    expect(isTModAuthenticationUrl("https://tvr.lat/login?next=/reactor")).toBe(true);
    expect(isTModAuthenticationUrl("https://reactor.tvr.lat/auth/login?client=desktop")).toBe(true);
    expect(isTModAuthenticationUrl("https://dash.tvr.lat/logout")).toBe(true);
    expect(isTModAuthenticationUrl("https://dash.tvr.lat/atlas")).toBe(false);
    expect(isTModAuthenticationUrl("https://tvr.lat.attacker.example/login")).toBe(false);
  });

  it("keeps the local catalog stable while applying server access text", () => {
    const merged = mergeServiceAccess([
      {
        id: "ovr",
        title: "ОВР",
        url: "https://ovr.tvr.lat/ovr",
        enabled: false,
        reason: "Нужен ручной доступ.",
      },
    ]);
    expect(merged).toHaveLength(services.length);
    expect(merged.find((item) => item.id === "ovr")?.description).toBe("Нужен ручной доступ.");
  });

  it("routes notifications only to declared desktop services", () => {
    expect(resolveNotificationServiceId("/reactor/bills/74")).toBe("reactor");
    expect(resolveNotificationServiceId("https://consensus.tvr.lat/host")).toBe("consensus");
    expect(resolveNotificationServiceId("admin")).toBe("admin");
    expect(resolveNotificationServiceId("javascript:alert(1)")).toBeNull();
    expect(resolveNotificationServiceId(null)).toBeNull();
  });
});
