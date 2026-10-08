import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { conversationSnippet, describeSharedLink, sharedLinkInMessage, sharedLinksInMessage } from "../src/shared/communicate-links";

describe("Communicate link cards", () => {
  it("summarizes shared pages in the conversation list without showing internal routes", () => {
    expect(conversationSnippet("https://consensus.tvr.lat/bills/42")).toBe("↗ Инициатива №42");
    expect(conversationSnippet("Посмотри это https://consensus.tvr.lat/bills/42")).toBe("Посмотри это");
    expect(conversationSnippet("https://consensus.tvr.lat/bills/42 https://ovr.tvr.lat/cases/8")).toBe("↗ Инициатива №42 · ещё 1");
    expect(conversationSnippet("")).toBe("Начните разговор");
  });
  it("allows only local blob image previews for downloaded chat attachments", () => {
    const html = readFileSync(new URL("../src/renderer/communicate.html", import.meta.url), "utf8");
    expect(html).toContain("img-src 'self' data: blob:");
    expect(html).toContain("connect-src 'none'");
  });
  it("shows the correct contour for Reactor and Atlas links", () => {
    expect(describeSharedLink("https://reactor.tvr.lat/admin/case/42")?.title).toBe("Дело №42");
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
    expect(result.card?.title).toBe("Дело №42");
  });
  it("makes separate cards for multiple links and keeps trailing punctuation", () => {
    const result = sharedLinksInMessage("Вот дело https://reactor.tvr.lat/case/42, и источник https://atlas.tvr.lat/source/1.");
    expect(result.cards.map(card => card.title)).toEqual(["Дело №42", "Атлас AI"]);
    expect(result.text).toBe("Вот дело, и источник.");
  });
  it("describes the resource rather than exposing a raw route as the preview", () => {
    const card = describeSharedLink("https://consensus.tvr.lat/bills/183");
    expect(card?.title).toBe("Инициатива №183");
    expect(card?.detail).toContain("Текст инициативы");
    expect(card?.detail).not.toContain("/bills");
    expect(describeSharedLink("https://ovr.tvr.lat/ovr?case_id=9")?.title).toBe("Дело №9");
  });
  it("does not pretend external paths are verified internal resources", () => {
    const card = describeSharedLink("https://unknown.example/cases/42");
    expect(card?.title).toBe("unknown.example");
    expect(card?.resource).toBeUndefined();
    expect(card?.detail).toContain("Внешний сайт");
  });
});
