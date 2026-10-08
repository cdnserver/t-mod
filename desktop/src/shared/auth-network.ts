import type { BootstrapResult, DesktopBootstrap, DesktopLoginResult } from "./contracts";

export function loginPayloadError(payload: unknown): DesktopLoginResult["error"] {
  if (!payload || typeof payload !== "object") return "server_response_invalid";
  const value = payload as { ok?: unknown; error?: unknown };
  if (value.ok === true) return undefined;
  const error = String(value.error || "");
  if (["invalid", "locked", "reset_required", "character_required", "atlas_access", "private_access_required", "banned", "two_factor_required", "login_failed"].includes(error)) return error as DesktopLoginResult["error"];
  if (error.endsWith("_private_access_required")) return "private_access_required";
  if (["administrator", "membership"].includes(error)) return "login_failed";
  return "server_response_invalid";
}

export class BootstrapProtocolError extends Error {}
export interface BootstrapCandidate { response: Response; data?: DesktopBootstrap }

export function isFreshLoginProjection(result: BootstrapResult | undefined): result is BootstrapResult & { data: DesktopBootstrap } {
  return result?.authenticated === true && result.online === true && isBootstrapPayload(result.data);
}

export function isBootstrapPayload(value: unknown): value is DesktopBootstrap {
  if (!value || typeof value !== "object") return false;
  const data = value as DesktopBootstrap;
  return data.protocol_version === 1 && typeof data.generated_at === "string"
    // Existing v1 emits Discord snowflakes as JSON numbers (above 2^53).
    // Do not reject every real account while validating this wire contract.
    && Number.isInteger(data.viewer?.id) && data.viewer.id > 0
    && typeof data.viewer.name === "string" && typeof data.viewer.display_name === "string"
    && typeof data.viewer.administrator === "boolean" && typeof data.viewer.guild_member === "boolean"
    && Array.isArray(data.viewer.sections) && data.viewer.sections.every(section => typeof section === "string")
    && Array.isArray(data.services) && data.services.every(service => service && typeof service.id === "string"
      && typeof service.url === "string" && typeof service.title === "string" && typeof service.enabled === "boolean")
    && Array.isArray(data.notifications?.items) && Number.isSafeInteger(data.notifications.unread)
    && data.notifications.unread >= 0 && data.notifications.items.every(item => item && Number.isSafeInteger(item.id)
      && typeof item.title === "string" && typeof item.body === "string" && typeof item.created_at === "string");
}

export async function parseBootstrapResponse(response: Response): Promise<BootstrapCandidate> {
  if ([408, 425, 429].includes(response.status) || response.status >= 500) throw new Error(`bootstrap_http_${response.status}`);
  if (!response.ok) return { response };
  let data: unknown;
  try { data = await response.json(); } catch { throw new BootstrapProtocolError("desktop_protocol_invalid"); }
  if (!isBootstrapPayload(data)) throw new BootstrapProtocolError("desktop_protocol_invalid");
  return { response, data };
}

// A ban from either first-party endpoint must win over a stale healthy mirror.
// Both requests have a short deadline in the main process, so this does not
// keep a denied session open while a second contour is still responding.
export async function selectBootstrapCandidate(requests: Promise<BootstrapCandidate>[]): Promise<BootstrapCandidate> {
  if (!requests.length) throw new Error("bootstrap_unavailable");
  return new Promise((resolve, reject) => {
    const settled: PromiseSettledResult<BootstrapCandidate>[] = new Array(requests.length);
    let remaining = requests.length;
    let finished = false;
    const complete = (index: number, result: PromiseSettledResult<BootstrapCandidate>) => {
      settled[index] = result; remaining--;
      if (finished) return;
      // A ban is already authoritative: a stalled mirror must not postpone it.
      if (result.status === "fulfilled" && result.value.response.status === 423) {
        finished = true; resolve(result.value); return;
      }
      if (remaining) return;
      finished = true;
      const candidates = settled.flatMap(item => item.status === "fulfilled" ? [item.value] : []);
      const healthy = candidates.find(candidate => candidate.data);
      if (healthy) { resolve(healthy); return; }
      if (candidates.length) { resolve(candidates[0]); return; }
      if (settled.some(item => item.status === "rejected" && item.reason instanceof BootstrapProtocolError))
        reject(new BootstrapProtocolError("desktop_protocol_invalid"));
      else reject(new Error("bootstrap_unavailable"));
    };
    requests.forEach((request, index) => void request.then(
      value => complete(index, { status: "fulfilled", value }),
      reason => complete(index, { status: "rejected", reason }),
    ));
  });
}
