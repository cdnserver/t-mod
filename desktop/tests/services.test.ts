import { describe, expect, it } from "vitest";
import {
  isServiceId,
  isTrustedTModUrl,
  mergeServiceAccess,
  resolveNotificationServiceId,
  services,
} from "../src/shared/services";

describe("desktop service boundary", () => {
  it("accepts only declared service identifiers", () => {
    expect(isServiceId("atlas")).toBe(true);
    expect(isServiceId("unknown-service")).toBe(false);
    expect(isServiceId({ id: "atlas" })).toBe(false);
  });

  it("allows secure T-Mod hosts and rejects lookalikes", () => {
    expect(isTrustedTModUrl("https://tvr.lat/reactor")).toBe(true);
    expect(isTrustedTModUrl("https://consensus.tvr.lat/")).toBe(true);
    expect(isTrustedTModUrl("http://tvr.lat/reactor")).toBe(false);
    expect(isTrustedTModUrl("https://tvr.lat.attacker.example/")).toBe(false);
    expect(isTrustedTModUrl("javascript:alert(1)")).toBe(false);
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
