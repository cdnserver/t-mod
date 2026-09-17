import type { AtlasOverlayBootstrapProjection } from "./atlas-overlay";

export const serviceIds = [
  "home",
  "reactor",
  "consensus",
  "atlas",
  "sgl",
  "ovr",
  "games",
  "tasks",
  "admin",
] as const;

export type ServiceId = (typeof serviceIds)[number];

export interface DesktopService {
  id: Exclude<ServiceId, "home">;
  title: string;
  url: string;
  enabled: boolean;
  reason: string | null;
}

export interface DesktopNotification {
  id: number;
  severity: "info" | "success" | "warning" | "critical";
  kind: string;
  title: string;
  body: string;
  route: string | null;
  read_at: string | null;
  created_at: string;
}

export interface DesktopBootstrap {
  protocol_version: 1;
  generated_at: string;
  client?: {
    edition: "tmod" | "blackbird" | "lumen";
    private: boolean;
    title: string;
  };
  client_update?: {
    required: boolean;
    minimum_version: string | null;
    latest_version: string | null;
    current_version: string | null;
    release_url: string;
    message: string;
  };
  viewer: {
    id: number;
    name: string;
    display_name: string;
    account_tier: "zero" | "member" | "administrator";
    guild_member: boolean;
    administrator: boolean;
    sections: string[];
  };
  services: DesktopService[];
  notifications: {
    items: DesktopNotification[];
    unread: number;
  };
  device?: {
    trusted: boolean;
    installation_ref?: string;
    account_count?: number;
    hardware_bound?: boolean;
  };
  atlas_overlay?: AtlasOverlayBootstrapProjection;
}

export interface BootstrapResult {
  authenticated: boolean;
  online: boolean;
  data?: DesktopBootstrap;
  error?: string;
  ban?: {
    reason: string;
    reference: string;
  };
  lastSuccessfulAt?: string;
}

export interface DesktopShellPreferences {
  preferredName: string;
  sidebarCollapsed: boolean;
  compactMode: boolean;
  reduceMotion: boolean;
  solidSurfaces: boolean;
  serviceZoom: number;
  idleLockMinutes: number;
  lockSound: boolean;
  updateChannel: "beta" | "dev" | "private";
}

export type DesktopLockReason = "idle" | "manual";

export interface DesktopLoginCredentials {
  login: string;
  pin: string;
}

export interface DesktopLoginResult {
  ok: boolean;
  /** Fresh server projection returned by the successful login transaction. */
  bootstrap?: BootstrapResult;
  error?:
    | "invalid"
    | "locked"
    | "reset_required"
    | "character_required"
    | "atlas_access"
    | "private_access_required"
    | "banned"
    | "network_unavailable"
    | "login_failed"
    | "invalid_input";
}

export interface DesktopState {
  activeService: ServiceId;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  url?: string;
  error?: string;
}

export type DesktopUpdatePhase =
  | "idle"
  | "checking"
  | "available"
  | "downloading"
  | "ready"
  | "current"
  | "error"
  | "development";

export interface DesktopUpdateState {
  phase: DesktopUpdatePhase;
  currentVersion: string;
  channel: "beta" | "dev" | "private";
  version?: string;
  percent?: number;
  message?: string;
  checkedAt?: string;
  required?: boolean;
  minimumVersion?: string;
  releaseUrl?: string;
}

export interface TModDesktopApi {
  bootstrap(): Promise<BootstrapResult>;
  login(credentials: DesktopLoginCredentials): Promise<DesktopLoginResult>;
  logout(): Promise<boolean>;
  navigate(serviceId: ServiceId): Promise<DesktopState>;
  reload(): Promise<void>;
  goBack(): Promise<void>;
  goForward(): Promise<void>;
  setShellOverlayOpen(open: boolean): Promise<void>;
  applyPreferences(preferences: DesktopShellPreferences): Promise<DesktopShellPreferences>;
  copyCurrentLink(): Promise<boolean>;
  openCurrentLink(): Promise<boolean>;
  openLogin(): Promise<DesktopState>;
  lock(): Promise<boolean>;
  unlock(): Promise<boolean>;
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  close(): Promise<void>;
  checkForUpdates(): Promise<DesktopUpdateState>;
  installUpdate(): Promise<boolean>;
  openReleasePage(): Promise<boolean>;
  onState(listener: (state: DesktopState) => void): () => void;
  onAuthChanged(listener: () => void): () => void;
  onCommandPalette(listener: () => void): () => void;
  onAtlasOverlaySettings(listener: () => void): () => void;
  onLockRequested(listener: (reason: DesktopLockReason) => void): () => void;
  onUpdate(listener: (state: DesktopUpdateState) => void): () => void;
}

declare global {
  interface Window {
    tmodDesktop?: TModDesktopApi;
  }
}
