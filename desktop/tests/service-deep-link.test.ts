import { describe, expect, it } from "vitest";
import { parseServiceDeepLink, serviceForDeepLink } from "../src/shared/service-deep-link";

describe("desktop service deep links", () => {
  it("maps known service hosts and preserves the destination", () => {
    expect(serviceForDeepLink("https://senate.tvr.lat/bills/42?tab=history"))
      .toEqual({ serviceId: "consensus", url: "https://senate.tvr.lat/bills/42?tab=history" });
    expect(serviceForDeepLink("https://home.tvr.lat/games/chess"))
      .toEqual({ serviceId: "games", url: "https://home.tvr.lat/games/chess" });
    expect(serviceForDeepLink("https://ap.finance.tvr.lat/" )?.serviceId).toBe("admin");
  });

  it("accepts the matching installed-app protocol only", () => {
    const destination = "https://sgl.tvr.lat/sgl/cases/17?view=full";
    const blackbird = `blackbird://open?url=${encodeURIComponent(destination)}`;
    expect(parseServiceDeepLink(blackbird, "blackbird"))
      .toEqual({ serviceId: "sgl", url: destination });
    expect(parseServiceDeepLink(blackbird, "tmod")).toBeNull();
  });

  it("rejects attacker URLs, ambiguity and unsupported schemes", () => {
    for (const value of [
      "blackbird://open?url=https%3A%2F%2Fsgl.tvr.lat.attacker.example%2F",
      "blackbird://open?url=http%3A%2F%2Fsgl.tvr.lat%2F",
      "blackbird://open?url=https%3A%2F%2Fuser%40sgl.tvr.lat%2F",
      "blackbird://open?url=https%3A%2F%2Fsgl.tvr.lat%3A8443%2F",
      "blackbird://open?url=https%3A%2F%2Funknown.tvr.lat%2F",
      "blackbird://evil?url=https%3A%2F%2Fsgl.tvr.lat%2F",
      "blackbird://open?url=https%3A%2F%2Fsgl.tvr.lat%2F&url=https%3A%2F%2Fovr.tvr.lat%2F",
      "blackbird://open?url=https%3A%2F%2Fsgl.tvr.lat%2F#fragment",
    ]) {
      expect(parseServiceDeepLink(value, "blackbird")).toBeNull();
    }
  });
});
