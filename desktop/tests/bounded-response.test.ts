import { describe, expect, it, vi } from "vitest";
import { readBoundedBytes } from "../src/main/bounded-response";

describe("bounded native response bodies", () => {
  it("reads binary chunks in order up to the exact limit", async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array([1, 2])); controller.enqueue(new Uint8Array([3])); controller.close();
    } });
    expect(await readBoundedBytes(new Response(stream), 3)).toEqual(new Uint8Array([1, 2, 3]));
  });
  it("rejects a declared oversized body before reading it", async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ cancel });
    await expect(readBoundedBytes(new Response(stream, { headers: { "Content-Length": "100" } }), 3)).rejects.toThrow("response_size_exceeded");
    expect(cancel).toHaveBeenCalledOnce();
  });
  it.each([null, "1"])("cancels oversized chunked or misdeclared responses (%j)", length => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array([1, 2])); controller.enqueue(new Uint8Array([3, 4]));
    }, cancel });
    return expect(readBoundedBytes(new Response(stream, { headers: length ? { "Content-Length": length } : undefined }), 3)).rejects.toThrow("response_size_exceeded")
      .then(() => expect(cancel).toHaveBeenCalledOnce());
  });
  it("cancels a pending body immediately on account lock", async () => {
    const cancel = vi.fn();
    const controller = new AbortController();
    const stream = new ReadableStream<Uint8Array>({ cancel });
    const pending = readBoundedBytes(new Response(stream), 3, controller.signal);
    const rejected = expect(pending).rejects.toThrow("session_changed");
    controller.abort(new Error("session_changed"));
    await rejected;
    expect(cancel).toHaveBeenCalledOnce();
  });
  it("rejects truncated transfers instead of returning partial private data", async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array([1])); controller.error(new Error("connection_lost"));
    } });
    await expect(readBoundedBytes(new Response(stream), 3)).rejects.toThrow("connection_lost");
    expect(stream.locked).toBe(false);
  });
  it("supports an empty response and refuses invalid limits", async () => {
    expect(await readBoundedBytes(new Response(null), 3)).toEqual(new Uint8Array());
    await expect(readBoundedBytes(new Response(null), 0)).rejects.toThrow("response_limit_invalid");
  });
});
