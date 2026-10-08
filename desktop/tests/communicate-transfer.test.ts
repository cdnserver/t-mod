import { describe, expect, it, vi } from "vitest";
import { downloadCommunicateAttachment, uploadCommunicateAttachment } from "../src/main/communicate-transfer";
import { ProtectedAccessGate, type ProtectedAccessState } from "../src/shared/protected-access";

const id = "a".repeat(32);
const snapshot = () => Response.json({ viewer: { id: "100", csrf_token: "csrf" } });
const input = () => ({ partnerId: "200", filename: "Снимок.png", caption: "Посмотри", bytes: new Uint8Array([1, 2, 3]), clientNonce: "b".repeat(32) });
function context() {
  const state: ProtectedAccessState = { revision: 1, accountId: "100", locked: false, banned: false };
  const access = new ProtectedAccessGate(() => state);
  const fetch = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
  const deps = { access, fetch, viewer: () => state.accountId ? { id: Number(state.accountId), id_exact: state.accountId } : undefined,
    headers: () => ({ "X-TMod-Desktop-Edition": "blackbird" }), onDenied: vi.fn(), onMismatch: vi.fn() };
  return { state, access, fetch, deps };
}

describe("native Communicate binary pipeline", () => {
  it("sends owned binary bytes, Unicode metadata, CSRF and the stable retry nonce", async () => {
    const { fetch, deps } = context(), file = input();
    let finish!: (response: Response) => void;
    fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(Response.json({ ok: true, result: { id: 42 } }));
    const pending = uploadCommunicateAttachment(deps, file);
    file.bytes[0] = 99; finish(snapshot());
    expect(await pending).toMatchObject({ ok: true });
    expect(fetch.mock.calls[1][0]).toBe("https://reactor.tvr.lat/api/blackbird/communicate/attachments");
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: "POST", headers: {
      "X-Blackbird-Partner": "200", "X-CSRF-Token": "csrf", "X-Blackbird-Filename": encodeURIComponent(file.filename),
      "X-Blackbird-Caption": encodeURIComponent(file.caption), "X-Blackbird-Nonce": file.clientNonce,
    } });
    expect([...fetch.mock.calls[1][1].body as Uint8Array]).toEqual([1, 2, 3]);
  });
  it("never sends a file after a delayed context finishes on a locked client", async () => {
    const { state, access, fetch, deps } = context();
    fetch.mockImplementationOnce(async () => { state.locked = true; access.revoke(); return snapshot(); });
    await expect(uploadCommunicateAttachment(deps, input())).rejects.toThrow("session_changed");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each(["upload", "download"])("refuses %s on a shared cookie that belongs to another account", async operation => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(Response.json({ viewer: { id: "200", csrf_token: "other" } }));
    await expect(operation === "upload" ? uploadCommunicateAttachment(deps, input()) : downloadCommunicateAttachment(deps, id)).rejects.toThrow("session_changed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(deps.onMismatch).toHaveBeenCalledOnce();
  });
  it("does not return the upload acknowledgement after the account changes during decoding", async () => {
    const { state, access, fetch, deps } = context();
    const response = Response.json({ ok: true });
    vi.spyOn(response, "json").mockImplementation(async () => {
      state.accountId = "200"; access.revoke(); return { ok: true };
    });
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(response);
    await expect(uploadCommunicateAttachment(deps, input())).rejects.toThrow("session_changed");
  });
  it("handles proxy HTML and invalid success JSON without losing the retryable error", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response("<h1>Timeout</h1>", { status: 504 }));
    await expect(uploadCommunicateAttachment(deps, input())).rejects.toThrow("communicate_upload_failed");
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(Response.json([]));
    await expect(uploadCommunicateAttachment(deps, input())).rejects.toThrow("communicate_response_invalid");
  });
  it("preserves useful server errors and signals revoked authorization", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(Response.json({ error: "globally_banned" }, { status: 423 }));
    await expect(uploadCommunicateAttachment(deps, input())).rejects.toThrow("globally_banned");
    expect(deps.onDenied).toHaveBeenCalledWith(423);
  });
  it("validates input before doing any network work", async () => {
    const { fetch, deps } = context();
    for (const value of [null, [], { ...input(), partnerId: "0" }, { ...input(), partnerId: 200 },
      { ...input(), filename: {} }, { ...input(), bytes: new Uint8Array() }, { ...input(), clientNonce: "bad" }]) {
      await expect(uploadCommunicateAttachment(deps, value)).rejects.toThrow();
    }
    await expect(downloadCommunicateAttachment(deps, "../../private")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it("downloads only after verifying the account and normalizes metadata", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3]), {
      headers: { "Content-Type": "IMAGE/PNG; charset=binary", "X-Blackbird-Filename": encodeURIComponent("../Снимок.png") },
    }));
    expect(await downloadCommunicateAttachment(deps, id)).toEqual({ bytes: new Uint8Array([1, 2, 3]), mimeType: "image/png", filename: ".._Снимок.png" });
  });
  it("returns a readable unavailable error for missing files", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response(null, { status: 404 }));
    await expect(downloadCommunicateAttachment(deps, id)).rejects.toThrow("communicate_attachment_unavailable");
  });
  it("aborts an already streaming download when the app locks", async () => {
    const { state, access, fetch, deps } = context();
    const cancel = vi.fn();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response(new ReadableStream({ cancel })));
    const pending = downloadCommunicateAttachment(deps, id);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    const rejected = expect(pending).rejects.toThrow();
    state.locked = true; access.revoke(); await rejected;
    expect(cancel).toHaveBeenCalledOnce();
  });
});
