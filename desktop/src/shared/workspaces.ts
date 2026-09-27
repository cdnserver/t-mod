import type { ServiceId } from "./contracts";
export type BlackbirdWorkspace = "atlas" | "senate";
export const SENATE_SERVICES: ServiceId[] = ["reactor", "consensus", "tasks", "sgl", "ovr", "games", "admin"];
export function workspaceForService(id: ServiceId): BlackbirdWorkspace | null {
  return id === "home" ? null : id === "atlas" ? "atlas" : "senate";
}
export function canEnterWorkspace(space: BlackbirdWorkspace, access: Map<string, { enabled: boolean }>): boolean {
  return (space === "atlas" ? ["atlas"] : SENATE_SERVICES).some(id => access.get(id)?.enabled === true);
}
