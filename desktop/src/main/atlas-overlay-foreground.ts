/**
 * The foreground probe is deliberately kept free of Electron APIs.  It makes
 * the safety decision testable and, more importantly, keeps an untrusted or
 * partially parsed native response from making the overlay visible.
 */
export interface AtlasOverlayRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AtlasOverlayForegroundProbe {
  available: boolean;
  title: string;
  processName: string;
  processId?: number;
  windowHandle?: string;
  visible: boolean;
  minimized: boolean;
  workArea?: AtlasOverlayRect;
}

export interface AtlasOverlayActiveGameWindow {
  title: string;
  processName: string;
  processId: number;
  /** Exact DesktopCapturer/Win32 window source used to keep Atlas above GTA. */
  mediaSourceId?: string;
  /** True when Win32 confirmed that GTA is the actual foreground window. */
  foregroundVerified: boolean;
  workArea: AtlasOverlayRect;
}

const GAME_WINDOW_TITLE_PATTERN = /(?:grand theft auto(?:\s*v)?|gta\s*5|gta5|rage\s*(?:multiplayer|mp)|ragemp|majestic)/i;
// RAGE MP, GTA V Enhanced and BattlEye use different executable names across
// launcher generations. Keep the boundary explicit while accepting the real
// variants seen on current Majestic installations.
const GAME_PROCESS_PATTERN = /^(?:gta5(?:[_-][a-z0-9-]+)*|playgtav(?:[_-][a-z0-9-]+)*|ragemp(?:[_-][a-z0-9-]+)*|majestic(?:rp|launcher)?(?:[_-][a-z0-9-]+)*)$/i;
const MAX_DESKTOP_COORDINATE = 100_000;
const MAX_WINDOW_HANDLE_LENGTH = 24;

function boundedString(value: unknown, maximum = 260): string {
  return typeof value === "string" ? value.trim().slice(0, maximum) : "";
}

function finiteCoordinate(value: unknown): number | undefined {
  const number = Number(value);
  if (!Number.isFinite(number) || Math.abs(number) > MAX_DESKTOP_COORDINATE) return undefined;
  return Math.round(number);
}

function finiteProcessId(value: unknown): number | undefined {
  const processId = Number(value);
  return Number.isInteger(processId) && processId > 0 && processId <= 0x7fffffff
    ? processId
    : undefined;
}

function windowHandle(value: unknown): string | undefined {
  const clean = String(value ?? "").trim();
  if (!/^\d+$/.test(clean) || clean.length > MAX_WINDOW_HANDLE_LENGTH || clean === "0") return undefined;
  return clean;
}

function parseRect(value: unknown): AtlasOverlayRect | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const source = value as Record<string, unknown>;
  const x = finiteCoordinate(source.x);
  const y = finiteCoordinate(source.y);
  const width = finiteCoordinate(source.width);
  const height = finiteCoordinate(source.height);
  if (
    x === undefined || y === undefined || width === undefined || height === undefined ||
    width < 200 || height < 120
  ) return undefined;
  return { x, y, width, height };
}

function executableStem(value: string): string {
  return value.replace(/^.*[\\/]/, "").replace(/\.exe$/i, "").trim();
}

/** Parses one JSON line emitted by the Windows foreground helper. */
export function parseAtlasOverlayForegroundProbe(line: string): AtlasOverlayForegroundProbe | undefined {
  try {
    const parsed = JSON.parse(line) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    const source = parsed as Record<string, unknown>;
    if (source.available !== true) {
      return {
        available: false,
        title: "",
        processName: "",
        visible: false,
        minimized: true,
      };
    }
    return {
      available: true,
      title: boundedString(source.title),
      processName: boundedString(source.processName ?? source.process, 120),
      processId: finiteProcessId(source.processId ?? source.pid),
      windowHandle: windowHandle(source.windowHandle ?? source.hwnd),
      visible: source.visible !== false,
      minimized: source.minimized === true,
      workArea: parseRect(source.workArea),
    };
  } catch {
    return undefined;
  }
}

/**
 * Returns a placement target only for a real, visible foreground GTA/RAGE/
 * Majestic process.  Any unknown, unavailable, minimized or malformed state
 * intentionally resolves to hidden.
 */
export function resolveAtlasOverlayForegroundGame(
  probe: AtlasOverlayForegroundProbe | undefined,
): AtlasOverlayActiveGameWindow | undefined {
  if (!probe?.available || !probe.visible || probe.minimized || !probe.workArea || !probe.processId) return undefined;
  const processName = executableStem(probe.processName);
  if (!GAME_PROCESS_PATTERN.test(processName)) return undefined;
  if (!GAME_WINDOW_TITLE_PATTERN.test(`${probe.title} ${processName}`)) return undefined;
  return {
    title: probe.title,
    processName,
    processId: probe.processId,
    ...(probe.windowHandle ? { mediaSourceId: `window:${probe.windowHandle}:0` } : {}),
    foregroundVerified: true,
    workArea: probe.workArea,
  };
}

/** Keeps the fixed renderer viewport entirely inside the selected display. */
export function resolveAtlasOverlayWindowBounds(
  area: AtlasOverlayRect,
  positionX: number,
  positionY: number,
  preferredWidth = 760,
  preferredHeight = 620,
): AtlasOverlayRect {
  const margin = Math.min(18, Math.max(8, Math.floor(Math.min(area.width, area.height) * .025)));
  const width = Math.max(1, Math.min(Math.round(preferredWidth), area.width - margin * 2));
  const height = Math.max(1, Math.min(Math.round(preferredHeight), area.height - margin * 2));
  const availableWidth = Math.max(0, area.width - width - margin * 2);
  const availableHeight = Math.max(0, area.height - height - margin * 2);
  const normalizedX = Math.max(0, Math.min(1, Number.isFinite(positionX) ? positionX : 1));
  const normalizedY = Math.max(0, Math.min(1, Number.isFinite(positionY) ? positionY : .5));
  return {
    x: area.x + margin + Math.round(availableWidth * normalizedX),
    y: area.y + margin + Math.round(availableHeight * normalizedY),
    width,
    height,
  };
}

function intersectionArea(first: AtlasOverlayRect, second: AtlasOverlayRect): number {
  const width = Math.max(
    0,
    Math.min(first.x + first.width, second.x + second.width) - Math.max(first.x, second.x),
  );
  const height = Math.max(
    0,
    Math.min(first.y + first.height, second.y + second.height) - Math.max(first.y, second.y),
  );
  return width * height;
}

/**
 * Converts the monitor rectangle returned by Win32/PowerShell into Electron's
 * device-independent coordinate system. This prevents the overlay from being
 * positioned off-screen on 125–200% DPI and mixed-scale multi-monitor PCs.
 */
export function resolveAtlasOverlayDisplayArea(
  nativeArea: AtlasOverlayRect,
  electronAreas: readonly AtlasOverlayRect[],
): AtlasOverlayRect {
  if (!electronAreas.length) return { ...nativeArea };
  let best = electronAreas[0];
  let bestScore = Number.NEGATIVE_INFINITY;
  const nativeCenterX = nativeArea.x + nativeArea.width / 2;
  const nativeCenterY = nativeArea.y + nativeArea.height / 2;
  for (const candidate of electronAreas) {
    const overlap = intersectionArea(nativeArea, candidate);
    const candidateCenterX = candidate.x + candidate.width / 2;
    const candidateCenterY = candidate.y + candidate.height / 2;
    const distance = Math.hypot(
      nativeCenterX - candidateCenterX,
      nativeCenterY - candidateCenterY,
    );
    const containsOrigin = (
      nativeArea.x >= candidate.x &&
      nativeArea.x < candidate.x + candidate.width &&
      nativeArea.y >= candidate.y &&
      nativeArea.y < candidate.y + candidate.height
    );
    const score = overlap * 10 + (containsOrigin ? 1_000_000_000 : 0) - distance;
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  return { ...best };
}
