import { describe, expect, it, vi } from "vitest";
import { createDocumentPreviewLoader, readableDocumentPreview } from "../src/shared/communicate-preview";
import { supportsDocumentPreview } from "../src/shared/communicate-links";

describe("Communicate document previews", () => {
  it("renders only readable metadata and tolerates missing optional fields", () => {
    expect(readableDocumentPreview({ title: "  Законопроект\nPhoenix  ", detail: "Общий\tтранспорт", status: "Принято" })).toEqual({
      title: "Законопроект Phoenix", detail: "Общий транспорт", resource: "", status: "Принято",
    });
    for (const value of [null, [], "document", { title: "" }, { title: "\u0000\u0007" }, { title: "Дело", detail: {} }, { title: "Дело", status: 1 }]) {
      expect(readableDocumentPreview(value)).toBeNull();
    }
    expect(readableDocumentPreview({ title: "a".repeat(200), detail: "b".repeat(500) })?.detail).toHaveLength(240);
  });
  it("only requests supported internal references", () => {
    expect(supportsDocumentPreview("https://ovr.tvr.lat/ovr?case_id=42")).toBe(true);
    expect(supportsDocumentPreview("https://consensus.tvr.lat/legal?bill_number=42")).toBe(true);
    for (const url of ["https://ovr.tvr.lat.evil.example/?case_id=42", "http://ovr.tvr.lat/?case_id=42",
      "https://user:password@ovr.tvr.lat/?case_id=42", "https://ovr.tvr.lat:8443/?case_id=42",
      "https://ovr.tvr.lat/?case_id=0", "https://reactor.tvr.lat/case/42"]) {
      expect(supportsDocumentPreview(url)).toBe(false);
    }
  });

  it("deduplicates requests and bounds concurrency", async () => {
    const pending: Array<(result: string) => void> = [];
    const fetcher = vi.fn(() => new Promise<string>(resolve => pending.push(resolve)));
    const load = createDocumentPreviewLoader(fetcher);
    const first = load("first");
    expect(load("first")).toBe(first);
    const second = load("second");
    const third = load("third");
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    pending[0]("A");
    await expect(first).resolves.toBe("A");
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
    pending[1]("B"); pending[2]("C");
    await expect(second).resolves.toBe("B");
    await expect(third).resolves.toBe("C");
    await expect(load("first")).resolves.toBe("A");
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("falls back quietly and releases a queue slot after failure", async () => {
    const load = createDocumentPreviewLoader(async url => {
      if (url === "failed") throw new Error("offline");
      return url;
    }, 1);
    await expect(load("failed")).resolves.toBeNull();
    await expect(load("next")).resolves.toBe("next");
  });

  it("does not cache document metadata indefinitely", async () => {
    let now = 1000;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => now);
    try {
      const fetcher = vi.fn(async () => "document");
      const load = createDocumentPreviewLoader(fetcher, 2, 100);
      await load("page");
      await Promise.resolve();
      await Promise.resolve();
      now += 101;
      await load("page");
      expect(fetcher).toHaveBeenCalledTimes(2);
    } finally { clock.mockRestore(); }
  });
});
