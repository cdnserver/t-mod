import { describe, expect, it } from "vitest";
import { BootstrapProtocolError, isBootstrapPayload, isFreshLoginProjection, parseBootstrapResponse, selectBootstrapCandidate } from "../src/shared/auth-network";

const payload = { protocol_version: 1, generated_at: "2026-09-27", viewer: { id: 1, name: "User", display_name: "User", administrator: true, guild_member: true, sections: [] }, services: [], notifications: { unread: 0, items: [] } };
const valid = () => parseBootstrapResponse(Response.json(payload));
describe("authenticated bootstrap mirror selection", () => {
  it("rejects HTML from an HTTP 200 proxy", async () => {
    await expect(parseBootstrapResponse(new Response("<html>Proxy</html>"))).rejects.toBeInstanceOf(BootstrapProtocolError);
  });
  it("uses valid JSON even when a faster mirror returns HTML", async () => {
    const result = await selectBootstrapCandidate([parseBootstrapResponse(new Response("<html>")), valid()]);
    expect(result.data?.viewer.id).toBe(1);
  });
  it("waits for a slow healthy mirror instead of dropping a valid session", async () => {
    const delayed = new Promise<Awaited<ReturnType<typeof valid>>>(resolve => setTimeout(() => void valid().then(resolve), 30));
    expect((await selectBootstrapCandidate([parseBootstrapResponse(new Response(null, { status: 401 })), delayed])).data).toEqual(payload);
  });
  it("preserves authoritative denial when all healthy checks fail", async () => {
    const result = await selectBootstrapCandidate([parseBootstrapResponse(new Response(null, { status: 403 })), parseBootstrapResponse(new Response(null, { status: 504 }))]);
    expect(result.response.status).toBe(403);
  });
  it("distinguishes a broken protocol from a network outage", async () => {
    await expect(selectBootstrapCandidate([parseBootstrapResponse(Response.json({ protocol_version: 1 })), Promise.reject(new Error("timeout"))])).rejects.toBeInstanceOf(BootstrapProtocolError);
  });
  it("validates identity and notification structure before projecting access", () => {
    expect(isBootstrapPayload(payload)).toBe(true);
    expect(isBootstrapPayload({ ...payload, viewer: { ...payload.viewer, id: 902235631952998410 } })).toBe(true);
    expect(isBootstrapPayload({ ...payload, viewer: { ...payload.viewer, id: "1" } })).toBe(false);
    expect(isBootstrapPayload({ ...payload, notifications: { items: null, unread: 0 } })).toBe(false);
  });
  it("never uses offline cached identity to confirm a new login", async () => {
    const data = (await valid()).data!;
    expect(isFreshLoginProjection({ authenticated: true, online: false, data })).toBe(false);
    expect(isFreshLoginProjection({ authenticated: true, online: true })).toBe(false);
    expect(isFreshLoginProjection({ authenticated: true, online: true, data })).toBe(true);
  });
});
