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
  visible: boolean;
  minimized: boolean;
  workArea?: AtlasOverlayRect;
}

export interface AtlasOverlayActiveGameWindow {
  title: string;
  processName: string;
  processId: number;
  workArea: AtlasOverlayRect;
}

const GAME_WINDOW_TITLE_PATTERN = /(?:grand theft auto(?:\s*v)?|gta\s*5|gta5|rage\s*(?:multiplayer|mp)|ragemp|majestic)/i;
const GAME_PROCESS_PATTERN = /^(?:gta5(?:_enhanced)?|playgtav|ragemp(?:_v)?|majestic(?:rp)?)$/i;
const MAX_DESKTOP_COORDINATE = 100_000;

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
    workArea: probe.workArea,
  };
}
