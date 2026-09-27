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

// A valid JSON projection wins, not simply the first HTTP 200 (proxy login/HTML).
// Explicit access denials remain authoritative if all healthy checks fail.
export async function selectBootstrapCandidate(requests: Promise<BootstrapCandidate>[]): Promise<BootstrapCandidate> {
  const healthy = Promise.any(requests.map(async request => {
    const candidate = await request;
    if (!candidate.data) throw new Error("bootstrap_denied");
    return candidate;
  }));
  // Requests already have a hard deadline. Do not discard a valid session just
  // because one contour rejects its cookie before the other has responded.
  try { return await healthy.catch(() => Promise.any(requests)); }
  catch (error) {
    if (error instanceof AggregateError && error.errors.some(item => item instanceof BootstrapProtocolError)) {
      throw new BootstrapProtocolError("desktop_protocol_invalid");
    }
    throw error;
  }
}
