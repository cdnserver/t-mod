export interface SharedLinkCard { url: string; contour: string; title: string; detail: string; tone: string }
const LINK_PATTERN = /https?:\/\/[^\s<>"']+/i;
const CONTOURS: Record<string, [string, string, string]> = {
  "home.tvr.lat": ["СЕНАТ · ЛИЧНЫЙ КОНТУР", "Личный реактор", "senate"],
  "reactor.tvr.lat": ["СЕНАТ · УПРАВЛЕНИЕ", "Ядерный реактор", "senate"],
  "consensus.tvr.lat": ["СЕНАТ · ЗАСЕДАНИЯ", "Консенсус", "senate"],
  "phx.tvr.lat": ["СЕНАТ · PHOENIX", "Портал Phoenix", "senate"],
  "sgl.tvr.lat": ["СЕНАТ · ПРАВО", "Бюро SGL", "senate"],
  "ovr.tvr.lat": ["СЕНАТ · ОВР", "Портал ОВР", "senate"],
  "atlas.tvr.lat": ["ATLAS · ИНТЕЛЛЕКТ", "Атлас AI", "atlas"],
  "dash.tvr.lat": ["ATLAS · АККАУНТ", "Атлас AI", "atlas"],
  "tvr.lat": ["ТЕХНОЛОГИИ ТОВАРИЩЕСТВА", "Товарищество", "tmod"],
};

export function describeSharedLink(raw: string): SharedLinkCard | undefined {
  try {
    const url = new URL(raw.replace(/[),.!?]+$/, ""));
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return;
    const host = url.hostname.toLowerCase();
    const service = CONTOURS[host] || (host.endsWith(".tvr.lat") ? ["ТЕХНОЛОГИИ ТОВАРИЩЕСТВА", host.split(".")[0].toUpperCase(), "tmod"] : ["ВНЕШНЯЯ ССЫЛКА", host, "external"]);
    const path = decodeURIComponent(url.pathname).replace(/\/+/g, " / ").trim();
    return { url: url.href, contour: service[0], title: service[1], tone: service[2], detail: path === "/" ? host : path.slice(0, 90) };
  } catch { return; }
}

export function sharedLinkInMessage(body: string): { card?: SharedLinkCard; text: string } {
  const match = body.match(LINK_PATTERN);
  const card = match ? describeSharedLink(match[0]) : undefined;
  return { card, text: card && match ? body.replace(match[0], "").trim() : body };
}
