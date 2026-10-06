import type { ServiceId } from "./contracts";
import { isTrustedTModUrl } from "./services";

export interface ServiceDeepLink {
  serviceId: Exclude<ServiceId, "home">;
  url: string;
}

const SERVICE_HOSTS: Record<string, Exclude<ServiceId, "home"> > = {
  "home.tvr.lat": "reactor",
  "senate.tvr.lat": "consensus",
  "reactor.tvr.lat": "admin",
  "consensus.tvr.lat": "consensus",
  "atlas.tvr.lat": "atlas",
  "dash.tvr.lat": "atlas",
  "sgl.tvr.lat": "sgl",
  "ovr.tvr.lat": "ovr",
  "phx.tvr.lat": "reactor",
  "log.global.tvr.lat": "admin",
  "zigmund.tvr.lat": "atlas",
  "ap.finance.tvr.lat": "admin",
};

export function serviceForDeepLink(value: string): ServiceDeepLink | null {
  if (value.length > 4096 || !isTrustedTModUrl(value)) return null;
  const url = new URL(value);
  const serviceId = SERVICE_HOSTS[url.hostname];
  if (!serviceId) return null;
  if (url.hostname === "home.tvr.lat" &&
    (url.pathname === "/games" || url.pathname.startsWith("/games/"))) {
    return { serviceId: "games", url: url.toString() };
  }
  if (url.hostname === "consensus.tvr.lat" &&
    (url.pathname === "/tasks" || url.pathname.startsWith("/tasks/"))) {
    return { serviceId: "tasks", url: url.toString() };
  }
  return { serviceId, url: url.toString() };
}

export function parseServiceDeepLink(value: string, protocol: "tmod" | "blackbird"): ServiceDeepLink | null {
  try {
    const link = new URL(value);
    if (link.protocol !== `${protocol}:` || link.hostname !== "open" ||
      (link.pathname !== "" && link.pathname !== "/") || link.port ||
      link.username || link.password || link.hash) return null;
    const target = link.searchParams.get("url");
    if (!target || link.searchParams.getAll("url").length !== 1) return null;
    return serviceForDeepLink(target);
  } catch {
    return null;
  }
}
