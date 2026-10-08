import { describe, expect, it, vi } from "vitest";
import { requestAccount } from "../src/main/account-request";
import { ProtectedAccessGate, type ProtectedAccessState } from "../src/shared/protected-access";

const snapshot = () => Response.json({ viewer: { id: "100", csrf_token: "csrf" } });
function context() {
  const state: ProtectedAccessState = { revision: 1, accountId: "100", locked: false, banned: false };
  const access = new ProtectedAccessGate(() => state);
  const fetch = vi.fn<(url: string, init: RequestInit) => Promise<Response>>();
  const deps = { access, fetch, viewer: () => state.accountId ? { id: Number(state.accountId), id_exact: state.accountId } : undefined,
    headers: () => ({ "X-TMod-Desktop-Edition": "blackbird" }), onDenied: vi.fn(), onMismatch: vi.fn() };
  return { state, access, fetch, deps };
}

describe("native account request pipeline", () => {
  it("sends the scoped mutation only after verifying the CSRF snapshot owner", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(Response.json({ ok: true, result: { id: 42 } }));
    const data = { action: "post", body: "Новая публикация" };
    expect(await requestAccount(deps, "media-update", data)).toEqual({ ok: true, result: { id: 42 } });
    expect(fetch.mock.calls[0][0]).toBe("https://reactor.tvr.lat/api/blackbird/media");
    expect(fetch.mock.calls[1][1]).toMatchObject({ method: "POST", body: JSON.stringify(data), headers: { "X-CSRF-Token": "csrf" } });
  });
  it("never posts if a delayed context response arrives after pause", async () => {
    const { state, access, fetch, deps } = context();
    let finish!: (response: Response) => void;
    fetch.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const pending = requestAccount(deps, "communicate-update", { action: "send", body: "Привет" });
    state.locked = true; access.revoke(); finish(snapshot());
    await expect(pending).rejects.toThrow("session_changed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
  });
  it("checks again after decoding the snapshot, not just after network headers", async () => {
    const { state, access, fetch, deps } = context();
    const response = snapshot();
    vi.spyOn(response, "json").mockImplementation(async () => {
      state.locked = true; access.revoke();
      return { viewer: { id: "100", csrf_token: "csrf" } };
    });
    fetch.mockResolvedValueOnce(response);
    await expect(requestAccount(deps, "media-update", { action: "delete", post_id: "42" })).rejects.toThrow("session_changed");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("does not return a late mutation response after lock and unlock", async () => {
    const { state, access, fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockImplementationOnce(async () => {
      state.locked = true; access.revoke(); state.locked = false;
      return Response.json({ ok: true, result: { private: "old-context" } });
    });
    await expect(requestAccount(deps, "media-update", { action: "post", body: "Привет" })).rejects.toThrow("session_changed");
  });
  it("never applies an earlier screen's draft to a different cookie owner", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(Response.json({ viewer: { id: "200", csrf_token: "other-csrf" } }));
    await expect(requestAccount(deps, "characters-update", { action: "delete", character_id: "8" })).rejects.toThrow("session_changed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(deps.onMismatch).toHaveBeenCalledOnce();
  });
  it("supports the existing security and billing snapshots without requiring a viewer field", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(Response.json({ links: { discord: "100" }, csrf_token: "security-csrf" }))
      .mockResolvedValueOnce(Response.json({ ok: true }))
      .mockResolvedValueOnce(Response.json({ account: { user_id: 100 }, tokens: 500 }));
    expect(await requestAccount(deps, "update", { action: "challenge" })).toEqual({ ok: true });
    expect(fetch.mock.calls[1][0]).toBe("https://tvr.lat/api/account/security");
    expect(await requestAccount(deps, "billing")).toEqual({ account: { user_id: 100 }, tokens: 500 });
  });
  it("notices HTML authorization denials before attempting to parse their bodies", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(new Response("Sign in", { status: 401 }));
    await expect(requestAccount(deps, "media")).rejects.toThrow("session_expired");
    expect(deps.onDenied).toHaveBeenCalledWith(401);
  });
  it("reports a plain proxy failure and never returns invalid JSON as account data", async () => {
    const { fetch, deps } = context();
    fetch.mockResolvedValueOnce(snapshot()).mockResolvedValueOnce(new Response("Gateway timeout", { status: 504 }));
    await expect(requestAccount(deps, "media-update", { action: "post", body: "Привет" })).rejects.toThrow("account_unavailable");
    fetch.mockResolvedValueOnce(new Response("<html>Proxy</html>"));
    await expect(requestAccount(deps, "media")).rejects.toThrow("account_response_invalid");
  });
  it("rejects unknown actions and cannot turn the bridge into an arbitrary URL fetcher", async () => {
    const { fetch, deps } = context();
    await expect(requestAccount(deps, "https://example.com/private")).rejects.toThrow("invalid_request");
    expect(fetch).not.toHaveBeenCalled();
  });
});
