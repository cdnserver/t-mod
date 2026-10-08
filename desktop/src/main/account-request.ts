import type { AccountIdentity } from "../shared/account-identity";
import { accountSnapshotMatches, csrfTokenFromAccountSnapshot } from "../shared/account-response";
import type { ProtectedAccessGate } from "../shared/protected-access";

interface Dependencies {
  access: ProtectedAccessGate;
  viewer(): AccountIdentity | undefined;
  fetch(url: string, init: RequestInit): Promise<Response>;
  headers(): Record<string, string>;
  onDenied(status: number): void;
  onMismatch(): void;
}

const actions = new Set(["security", "billing", "update", "characters", "characters-update", "communicate", "communicate-update", "media", "media-update"]);
async function jsonObject(response: Response): Promise<Record<string, unknown>> {
  let value: unknown;
  try { value = await response.json(); } catch { throw new Error("account_response_invalid"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("account_response_invalid");
  return value as Record<string, unknown>;
}

/** Native account bridge, deliberately independent of Electron for race tests. */
export async function requestAccount(deps: Dependencies, action: string, data?: Record<string, string>): Promise<Record<string, unknown>> {
  if (!actions.has(action)) throw new Error("invalid_request");
  const lease = deps.access.capture();
  const blackbird = /^(characters|communicate|media)(-update)?$/.test(action);
  const host = blackbird ? "https://reactor.tvr.lat" : "https://tvr.lat";
  const endpoint = blackbird ? `/api/blackbird/${action.split("-")[0]}` : `/api/account/${action === "billing" ? "billing" : "security"}`;
  const target = host + endpoint;
  const snapshot = await deps.fetch(target, {
    credentials: "include", headers: deps.headers(), signal: AbortSignal.any([lease.signal, AbortSignal.timeout(12_000)]),
  });
  lease.assertCurrent();
  deps.onDenied(snapshot.status);
  if (!snapshot.ok) throw new Error(snapshot.status === 401 ? "session_expired" : "account_unavailable");
  const result = await jsonObject(snapshot);
  lease.assertCurrent();
  const expected = deps.viewer();
  if (!expected || !accountSnapshotMatches(result, expected)) {
    deps.access.revoke(); deps.onMismatch(); throw new Error("session_changed");
  }
  if (action !== "update" && !action.endsWith("-update")) return result;
  const csrfToken = csrfTokenFromAccountSnapshot(result);
  if (!data || typeof data !== "object" || Array.isArray(data) ||
      JSON.stringify(data).length > (action === "media-update" ? 8192 : 4096) || !csrfToken) throw new Error("invalid_request");
  lease.assertCurrent();
  const response = await deps.fetch(target, {
    method: "POST", credentials: "include", headers: { ...deps.headers(), "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(data), signal: AbortSignal.any([lease.signal, AbortSignal.timeout(20_000)]),
  });
  lease.assertCurrent();
  deps.onDenied(response.status);
  if (!response.ok) {
    let error = "account_unavailable";
    try { const body = await jsonObject(response); if (typeof body.error === "string") error = body.error; } catch { /* Proxy errors need not be JSON. */ }
    lease.assertCurrent();
    throw new Error(error);
  }
  const body = await jsonObject(response);
  lease.assertCurrent();
  return body;
}
