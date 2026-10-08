import { requestAccount } from "./account-request";
import { readBoundedBytes } from "./bounded-response";
import { csrfTokenFromAccountSnapshot } from "../shared/account-response";
import { CHAT_FILE_LIMIT } from "../shared/communicate-attachments";

type Dependencies = Parameters<typeof requestAccount>[0];
const endpoint = "https://reactor.tvr.lat/api/blackbird/communicate/attachments";

async function uploadResult(response: Response): Promise<Record<string, unknown>> {
  let value: unknown;
  try { value = await response.json(); } catch { /* A proxy failure can be HTML. */ }
  const payload = value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
  if (!response.ok) throw new Error(typeof payload?.error === "string" ? payload.error : "communicate_upload_failed");
  if (!payload) throw new Error("communicate_response_invalid");
  return payload;
}

/** Both binary directions use the same account-owner check as text messages. */
export async function uploadCommunicateAttachment(deps: Dependencies, input: unknown): Promise<Record<string, unknown>> {
  const lease = deps.access.capture();
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("communicate_attachment_invalid");
  const value = input as Record<string, unknown>;
  const { partnerId, filename, caption } = value;
  const clientNonce = value.clientNonce ?? "";
  if (typeof partnerId !== "string" || !/^[1-9]\d{0,21}$/.test(partnerId) ||
      typeof filename !== "string" || !filename || filename.length > 120 ||
      typeof caption !== "string" || caption.length > 1000 ||
      !(value.bytes instanceof Uint8Array || value.bytes instanceof ArrayBuffer)) throw new Error("communicate_attachment_invalid");
  if (typeof clientNonce !== "string" || (clientNonce && !/^[a-f0-9]{32}$/.test(clientNonce))) throw new Error("communicate_nonce_invalid");
  const size = value.bytes.byteLength;
  if (!size || size > CHAT_FILE_LIMIT) throw new Error("communicate_attachment_size_invalid");
  // Own the bytes before awaiting the context; callers cannot modify a pending send.
  const bytes = value.bytes instanceof ArrayBuffer ? Buffer.from(new Uint8Array(value.bytes)) : Buffer.from(value.bytes);
  const context = await requestAccount(deps, "communicate");
  lease.assertCurrent();
  const csrf = csrfTokenFromAccountSnapshot(context);
  if (!csrf) throw new Error("communicate_response_invalid");
  const response = await deps.fetch(endpoint, {
    method: "POST", credentials: "include", signal: AbortSignal.any([lease.signal, AbortSignal.timeout(30_000)]),
    headers: { ...deps.headers(), "Content-Type": "application/octet-stream", "X-CSRF-Token": csrf,
      "X-Blackbird-Partner": partnerId, "X-Blackbird-Filename": encodeURIComponent(filename),
      "X-Blackbird-Caption": encodeURIComponent(caption), ...(clientNonce ? { "X-Blackbird-Nonce": clientNonce } : {}) },
    body: bytes,
  });
  lease.assertCurrent(); deps.onDenied(response.status);
  const result = await uploadResult(response);
  lease.assertCurrent();
  return result;
}

export async function downloadCommunicateAttachment(deps: Dependencies, id: unknown): Promise<{ bytes: Uint8Array; mimeType: string; filename: string }> {
  const lease = deps.access.capture();
  if (typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id)) throw new Error("communicate_attachment_invalid");
  // Shared cookies can change without the old chat window knowing yet.
  await requestAccount(deps, "communicate");
  lease.assertCurrent();
  const response = await deps.fetch(`${endpoint}/${id}`, {
    credentials: "include", headers: { ...deps.headers(), Accept: "application/octet-stream" },
    signal: AbortSignal.any([lease.signal, AbortSignal.timeout(20_000)]),
  });
  lease.assertCurrent(); deps.onDenied(response.status);
  if (!response.ok) throw new Error(response.status === 404 ? "communicate_attachment_unavailable" : "communicate_unavailable");
  const bytes = await readBoundedBytes(response, CHAT_FILE_LIMIT, lease.signal);
  lease.assertCurrent();
  if (!bytes.length) throw new Error("communicate_attachment_unavailable");
  let filename = "attachment";
  try { filename = decodeURIComponent(response.headers.get("X-Blackbird-Filename") || filename); } catch { /* Keep fallback. */ }
  filename = filename.replace(/[/\\:\u0000-\u001f\u007f]/g, "_").slice(0, 120) || "attachment";
  return { bytes, mimeType: (response.headers.get("Content-Type") || "application/octet-stream").split(";")[0].trim().toLowerCase(), filename };
}
