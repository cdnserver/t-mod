import { describe, expect, it, vi } from "vitest";
import { MediaAssetCache, MediaComposers, MediaReadGate, isPublishedMediaPost, mergeMediaPosts } from "../src/shared/media-flow";

describe("Media asset memory budget", () => {
  it("shares simultaneous requests and evicts the least recently used image", async () => {
    const cache = new MediaAssetCache<number>(2);
    const loader = vi.fn(async () => 1);
    const first = cache.load("a", loader);
    expect(cache.load("a", loader)).toBe(first);
    await first; expect(loader).toHaveBeenCalledOnce();
    await cache.load("b", async () => 2);
    expect(cache.load("a", loader)).toBe(first);
    await cache.load("c", async () => 3);
    const reload = vi.fn(async () => 4);
    expect(await cache.load("b", reload)).toBe(4);
    expect(reload).toHaveBeenCalledOnce();
  });
  it("retries failures without retaining rejected image bytes", async () => {
    const cache = new MediaAssetCache<number>();
    await expect(cache.load("a", async () => { throw Error("offline"); })).rejects.toThrow("offline");
    expect(await cache.load("a", async () => 2)).toBe(2);
  });
  it("a late failure cannot evict a replacement with the same key", async () => {
    const cache = new MediaAssetCache<number>(1);
    let reject!: (reason: Error) => void;
    const old = cache.load("a", () => new Promise<number>((_, fail) => { reject = fail; }));
    await cache.load("b", async () => 2);
    const replacement = cache.load("a", async () => 3);
    reject(Error("late")); await expect(old).rejects.toThrow("late");
    expect(cache.load("a", async () => 4)).toBe(replacement);
    expect(await replacement).toBe(3);
  });
  it("keeps accounts isolated and rejects invalid budgets", async () => {
    expect(() => new MediaAssetCache(0)).toThrow();
    const alice = new MediaAssetCache<number>(), bob = new MediaAssetCache<number>();
    await alice.load("photo", async () => 1);
    expect(await bob.load("photo", async () => 2)).toBe(2);
  });
});

describe("Media Network request coordination", () => {
  it("ignores an old profile or feed response after navigation and unmount", () => {
    const gate = new MediaReadGate();
    const first = gate.begin();
    const second = gate.begin();
    expect(gate.accepts(first)).toBe(false);
    expect(gate.accepts(second)).toBe(true);
    gate.invalidate();
    expect(gate.accepts(second)).toBe(false);
  });
  it("reuses a nonce on retry but not for edited or delivered content", () => {
    let count = 0;
    const drafts = new MediaComposers(() => String(++count));
    drafts.update("post", { body: "  Hello  " });
    expect(drafts.capture("post").nonce).toBe("1");
    drafts.update("post", { body: "Hello" });
    expect(drafts.capture("post").nonce).toBe("1");
    drafts.update("rollback", { body: "Hello", source: "https://example.com" });
    const rollback = drafts.capture("rollback");
    expect(rollback.nonce).toBe("2");
    drafts.acknowledge(rollback);
    drafts.update("rollback", { body: "Hello", source: "https://example.com" });
    expect(drafts.capture("rollback").nonce).toBe("3");
    expect(drafts.capture("post").nonce).toBe("1");
  });
  it("does not share text or sources between posts and rollbacks", () => {
    const drafts = new MediaComposers(() => "nonce");
    drafts.update("post", { body: "A thought" });
    expect(drafts.read("rollback")).toMatchObject({ body: "", source: "" });
    drafts.update("rollback", { body: "Video context", source: "https://example.com/video" });
    expect(drafts.read("post")).toMatchObject({ body: "A thought", source: "" });
  });
  it("only clears the unchanged captured revision after delivery", () => {
    const drafts = new MediaComposers(() => "nonce");
    drafts.update("post", { body: "First" });
    const first = drafts.capture("post");
    drafts.update("post", { body: "Next" });
    drafts.update("post", { body: "First" });
    drafts.acknowledge(first);
    expect(drafts.read("post").body).toBe("First");
    const second = drafts.capture("post");
    drafts.acknowledge(second);
    expect(drafts.read("post").body).toBe("");
  });
  it("cannot clear the other composer's matching text on a late acknowledgement", () => {
    const drafts = new MediaComposers(() => "nonce");
    drafts.update("post", { body: "Same words" });
    const sending = drafts.capture("post");
    drafts.update("rollback", { body: "Same words", source: "https://example.com/video" });
    drafts.acknowledge(sending);
    expect(drafts.read("post").body).toBe("");
    expect(drafts.read("rollback").body).toBe("Same words");
  });
  it("does not accept malformed or mismatched publication acknowledgements", () => {
    const drafts = new MediaComposers(() => "nonce");
    drafts.update("rollback", { body: "Video", source: "https://example.com/video" });
    const sending = drafts.capture("rollback");
    const valid = { id: 1, author_id: "100", kind: "rollback", body: "Video", source_url: "https://example.com/video", created_at: "now", display_name: "Test" };
    expect(isPublishedMediaPost(valid, "100", sending)).toBe(true);
    for (const patch of [{ author_id: "200" }, { id: 0 }, { id: Infinity }, { kind: "post" }, { body: "Other" }, { source_url: "" }]) {
      expect(isPublishedMediaPost({ ...valid, ...patch }, "100", sending)).toBe(false);
    }
    expect(isPublishedMediaPost(null, "100", sending)).toBe(false);
  });
  it("deduplicates overlapping pages and acknowledgements in newest-first order", () => {
    expect(mergeMediaPosts([{ id: 5, body: "old" }, { id: 3, body: "3" }], [{ id: 5, body: "updated" }, { id: 4, body: "4" }]))
      .toEqual([{ id: 5, body: "updated" }, { id: 4, body: "4" }, { id: 3, body: "3" }]);
  });
});
