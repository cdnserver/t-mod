import { describe, expect, it } from "vitest";
import { MediaComposers, MediaReadGate, isPublishedMediaPost, mergeMediaPosts } from "../src/shared/media-flow";

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
