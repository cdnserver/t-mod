import { useState } from "react";
import { createRoot } from "react-dom/client";
import type { TModDesktopApi } from "../shared/contracts";
import { BlackbirdMediaNetwork } from "./BlackbirdMediaNetwork";
import { BlackbirdCommunicate } from "./BlackbirdCommunicate";
import { installInteractionSurface } from "../shared/interaction-surface";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";

// A separate, development-only renderer: no fake API data, real local routes.
if (!import.meta.env.DEV || location.hostname !== "127.0.0.1") throw new Error("development_lab_only");
installInteractionSurface();
const token = new URLSearchParams(location.search).get("token") || "";
if (!/^[a-f0-9]{48}$/.test(token)) throw new Error("lab_token_required");
let actor = "100";
const endpoint = "http://127.0.0.1:5182/api/blackbird/media";
const chatEndpoint = "http://127.0.0.1:5182/api/blackbird/communicate";
const chat = new URLSearchParams(location.search).get("view") === "communicate";
const lostAcknowledgements = new Set<string>();
const headers = () => ({ "X-Blackbird-Lab": token, "X-Lab-Actor": actor });
async function json(response: Response): Promise<Record<string, unknown>> {
  const body = await response.json() as Record<string, unknown>;
  if (!response.ok) throw new Error(String(body.error || `HTTP ${response.status}`));
  return body;
}
async function csrf(requestHeaders: Record<string, string>, url = endpoint): Promise<string> {
  const state = await json(await fetch(url, { headers: requestHeaders }));
  return String((state.viewer as { csrf_token: string }).csrf_token);
}
const bridge = {
  async accountRequest(action: string, data?: Record<string, string>) {
    if (!["media", "media-update", "communicate", "communicate-update"].includes(action)) throw new Error("lab_action_invalid");
    const url = action.startsWith("communicate") ? chatEndpoint : endpoint;
    const h = headers();
    if (!action.endsWith("-update")) return json(await fetch(url, { headers: h }));
    const result = await json(await fetch(url, { method: "POST", headers: { ...h, "Content-Type": "application/json", "X-CSRF-Token": await csrf(h, url) }, body: JSON.stringify(data) }));
    if (action === "media-update" && data?.action === "post") {
      // Inject a late/lost ACK only AFTER the real backend committed the post.
      const params = new URLSearchParams(location.search);
      const delay = Math.min(3000, Math.max(0, Number(params.get("publish-delay")) || 0));
      if (delay) await new Promise(resolve => window.setTimeout(resolve, delay));
      if (params.get("lose-publish-ack") === "1" && !lostAcknowledgements.has(data.client_nonce)) {
        lostAcknowledgements.add(data.client_nonce);
        throw new Error("local_test_lost_acknowledgement");
      }
    }
    return result;
  },
  async mediaUpload(kind: "avatar" | "cover", bytes: Uint8Array) {
    const h = headers();
    return json(await fetch(`${endpoint}/assets/${kind}`, { method: "POST", headers: { ...h, "Content-Type": "application/octet-stream", "X-CSRF-Token": await csrf(h) }, body: bytes as BodyInit }));
  },
  async mediaRemove(kind: "avatar" | "cover") {
    const h = headers();
    const result = await json(await fetch(`${endpoint}/assets/${kind}`, { method: "DELETE", headers: { ...h, "X-CSRF-Token": await csrf(h) } }));
    return result.removed === true;
  },
  async mediaAsset(userId: string, kind: "avatar" | "cover") {
    const response = await fetch(`${endpoint}/assets/${userId}/${kind}`, { headers: headers() });
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return { bytes: new Uint8Array(await response.arrayBuffer()), mimeType: response.headers.get("Content-Type") || "image/webp", revision: (response.headers.get("ETag") || "").replaceAll('"', "") };
  },
  openSharedLink: async () => false, // Native navigation is tested in the app, not impersonated here.
};
// This lab renders Media/Communicate directly, never the native application shell.
window.tmodDesktop = bridge as unknown as TModDesktopApi;
if (chat) window.blackbirdCommunicate = {
  context: async () => ({ viewerId: actor, authenticated: true, sharedUrl: "" }),
  request: bridge.accountRequest,
  async upload(input) {
    const h = headers();
    return json(await fetch(`${chatEndpoint}/attachments`, { method: "POST", headers: {
      ...h, "Content-Type": "application/octet-stream", "X-CSRF-Token": await csrf(h, chatEndpoint),
      "X-Blackbird-Partner": input.partnerId, "X-Blackbird-Filename": encodeURIComponent(input.filename),
      "X-Blackbird-Caption": encodeURIComponent(input.caption), "X-Blackbird-Nonce": input.clientNonce || "",
    }, body: input.bytes as BodyInit }));
  },
  async attachment(id) {
    const response = await fetch(`${chatEndpoint}/attachments/${id}`, { headers: headers() });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return { bytes: new Uint8Array(await response.arrayBuffer()), mimeType: response.headers.get("Content-Type") || "application/octet-stream", filename: decodeURIComponent(response.headers.get("X-Blackbird-Filename") || "file") };
  },
  async linkPreview(url) {
    const result = await json(await fetch(`${chatEndpoint}/preview?url=${encodeURIComponent(url)}`, { headers: headers() }));
    return result.result as { title: string; detail: string; resource: string; status: string } | null;
  },
  close: async () => {}, minimize: async () => {}, openLink: async () => false,
  copyLink: async url => { await navigator.clipboard.writeText(url); return true; },
  onShare: () => () => {},
};

function Lab() {
  const [user, setUser] = useState("100");
  const change = (id: string) => { actor = id; setUser(id); };
  return <><div style={{ height: 42, boxSizing: "border-box", display: "flex", alignItems: "center", gap: 12, padding: "0 18px", color: "#e2cfad", background: "#332c20", font: "12px 'IBM Plex Sans', sans-serif" }}>
    <strong>ЛОКАЛЬНАЯ ПРОВЕРКА · ВРЕМЕННАЯ БАЗА</strong>
    <button disabled={user === "100"} onClick={() => change("100")}>Ирина · аккаунт 100</button>
    <button disabled={user === "200"} onClick={() => change("200")}>Алексей · аккаунт 200</button>
  </div><div style={{ height: "calc(100vh - 42px)" }}>{chat ? <BlackbirdCommunicate key={user} servers={[]} onBack={() => {}}/> : <BlackbirdMediaNetwork key={user} name={user === "100" ? "Ирина Тестовая" : "Алексей Тестовый"} onBack={() => {}}/>}</div></>;
}

document.body.style.margin = "0";
createRoot(document.getElementById("root")!).render(<Lab/>);
