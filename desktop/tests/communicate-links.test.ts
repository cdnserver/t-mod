import { describe, expect, it } from "vitest";
import { describeSharedLink, sharedLinkInMessage } from "../src/shared/communicate-links";

describe("Communicate link cards", () => {
  it("shows the correct contour for Reactor and Atlas links", () => {
    expect(describeSharedLink("https://reactor.tvr.lat/admin/case/42")?.title).toBe("Ядерный реактор");
    expect(describeSharedLink("https://home.tvr.lat/reactor")?.title).toBe("Личный реактор");
    expect(describeSharedLink("https://dash.tvr.lat/atlas")?.tone).toBe("atlas");
  });
  it("does not misbrand external lookalikes and does not accept embedded credentials", () => {
    expect(describeSharedLink("https://reactor.tvr.lat.evil.example/")?.tone).toBe("external");
    expect(describeSharedLink("https://user:pass@reactor.tvr.lat/")).toBeUndefined();
  });
  it("turns a link into a card while preserving the human message", () => {
    const result = sharedLinkInMessage("Посмотри дело https://reactor.tvr.lat/admin/case/42");
    expect(result.text).toBe("Посмотри дело");
    expect(result.card?.title).toBe("Ядерный реактор");
  });
});
