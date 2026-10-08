import { describe, expect, it, vi } from "vitest";
import { attachmentSizeLabel, CHAT_IMAGE_TYPES, createAttachmentQueue } from "../src/shared/communicate-attachments";

describe("Communicate visible image requests", () => {
  it("shows useful file sizes, including tiny pasted images", () => {
    expect(attachmentSizeLabel(68)).toBe("68 Б");
    expect(attachmentSizeLabel(1536)).toBe("1,5 КБ");
    expect(attachmentSizeLabel(1024 * 1024)).toBe("1 МБ");
    expect(attachmentSizeLabel(Number.NaN)).toBe("Размер неизвестен");
  });
  it("bounds downloads and does not retain file contents after completion", async () => {
    const pending: Array<(value: string) => void> = [];
    const fetch = vi.fn(() => new Promise<string>(resolve => pending.push(resolve)));
    const load = createAttachmentQueue(fetch);
    const a = load("a"), b = load("b"), c = load("c");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    pending[0]("A");
    await expect(a.promise).resolves.toBe("A");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    pending[1]("B"); pending[2]("C");
    await expect(b.promise).resolves.toBe("B");
    await expect(c.promise).resolves.toBe("C");
    const again = load("a");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    pending[3]("fresh");
    await expect(again.promise).resolves.toBe("fresh");
  });
  it("skips cancelled hidden images before starting their request", async () => {
    let release!: (value: string) => void;
    const fetch = vi.fn((id: string) => id === "first" ? new Promise<string>(resolve => { release = resolve; }) : Promise.resolve(id));
    const load = createAttachmentQueue(fetch, 1);
    const first = load("first"), hidden = load("hidden"), next = load("next");
    hidden.cancel();
    await expect(hidden.promise).resolves.toBeNull();
    release("first");
    await first.promise;
    await expect(next.promise).resolves.toBe("next");
    expect(fetch.mock.calls.map(call => call[0])).toEqual(["first", "next"]);
  });
  it("discards late content after navigation and releases slots after errors", async () => {
    let release!: (value: string) => void;
    const load = createAttachmentQueue((id: string) => id === "slow"
      ? new Promise<string>(resolve => { release = resolve; }) : Promise.reject(new Error("offline")), 1);
    const slow = load("slow"), failed = load("failed");
    await Promise.resolve();
    slow.cancel();
    await expect(slow.promise).resolves.toBeNull();
    release("private image");
    await expect(failed.promise).resolves.toBeNull();
  });
  it("never embeds SVG, HTML or arbitrary declared image formats", () => {
    expect(CHAT_IMAGE_TYPES.has("image/png")).toBe(true);
    expect(CHAT_IMAGE_TYPES.has("image/svg+xml")).toBe(false);
    expect(CHAT_IMAGE_TYPES.has("text/html")).toBe(false);
  });
});
