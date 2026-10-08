export interface SharedLinkCard { url: string; contour: string; title: string; detail: string; tone: string; resource?: string; previewable?: boolean }
const LINK_PATTERN = /https?:\/\/[^\s<>"']+/gi;
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

const SERVICE_DESCRIPTIONS: Record<string, string> = {
  "home.tvr.lat": "Личное пространство: события, задачи и участие в работе Товарищества.",
  "reactor.tvr.lat": "Рабочее пространство Сената: дела, задачи и решения в одном месте.",
  "consensus.tvr.lat": "Заседания Сената, повестка, обсуждение инициатив и голосование.",
  "phx.tvr.lat": "Вступление в Сенат Phoenix и статус рассмотрения заявки.",
  "sgl.tvr.lat": "Правовые материалы и работа с обращениями в Бюро SGL.",
  "ovr.tvr.lat": "Расследования ОВР, материалы и ход рассмотрения дел.",
  "atlas.tvr.lat": "Atlas: работа с правилами, законодательством и игровыми ситуациями.",
  "dash.tvr.lat": "Рабочее пространство Atlas: вопросы, источники и история диалогов.",
  "tvr.lat": "Сервисы и приложения Технологий Товарищества.",
};

function describeResource(url: URL): { title?: string; resource?: string; detail?: string } {
  if (!CONTOURS[url.hostname]) return {};
  const path = decodeURIComponent(url.pathname);
  const caseId = path.match(/\/(?:case|cases)\/(\d+)(?:\/|$)/)?.[1] || url.searchParams.get("case_id");
  if (caseId && /^\d{1,18}$/.test(caseId) && ["reactor.tvr.lat", "ovr.tvr.lat", "sgl.tvr.lat"].includes(url.hostname)) {
    return { title: `Дело №${caseId}`, resource: "Материалы дела", detail: "Карточка дела и связанные с ним материалы. Доступ проверяется при открытии." };
  }
  const billId = path.match(/\/(?:bill|bills|initiative|initiatives)\/(\d+)(?:\/|$)/)?.[1] || url.searchParams.get("bill_id");
  if (billId && /^\d{1,18}$/.test(billId) && ["consensus.tvr.lat", "reactor.tvr.lat"].includes(url.hostname)) {
    return { title: `Инициатива №${billId}`, resource: "Инициатива Сената", detail: "Текст инициативы и её рассмотрение на Консенсусе. Откройте, чтобы ознакомиться с деталями." };
  }
  if (["atlas.tvr.lat", "dash.tvr.lat"].includes(url.hostname) && /\/sources?\//.test(path)) {
    return { resource: "Источник Atlas", detail: "Ссылка на материал из базы знаний Atlas. Откройте источник, чтобы прочитать оригинал." };
  }
  if (path.includes("billing") && ["atlas.tvr.lat", "dash.tvr.lat"].includes(url.hostname)) {
    return { title: "Подписка и Atlas Tokens", resource: "Аккаунт Atlas", detail: "Баланс токенов, подписка и история использования ресурсов Atlas." };
  }
  return {};
}

export function supportsDocumentPreview(raw: string): boolean {
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return false;
    const host = url.hostname;
    const caseHost = host === "ovr.tvr.lat" || (host === "reactor.tvr.lat" && url.pathname.startsWith("/ovr"));
    const caseId = url.pathname.match(/\/(?:case|cases)\/(\d+)(?:\/|$)/)?.[1] || url.searchParams.get("case_id");
    if (caseHost && caseId && /^[0-9]{1,18}$/.test(caseId) && Number(caseId) > 0) return true;
    const billHost = ["consensus.tvr.lat", "reactor.tvr.lat", "home.tvr.lat"].includes(host);
    const billId = url.pathname.match(/\/(?:bill|bills|initiative|initiatives)\/(\d+)(?:\/|$)/)?.[1]
      || url.searchParams.get("bill_id") || url.searchParams.get("bill_number");
    return Boolean(billHost && billId && /^[0-9]{1,18}$/.test(billId) && Number(billId) > 0);
  } catch { return false; }
}

export function describeSharedLink(raw: string): SharedLinkCard | undefined {
  try {
    const url = new URL(raw.replace(/[),.!?]+$/, ""));
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return;
    const host = url.hostname.toLowerCase();
    const service = CONTOURS[host] || (host.endsWith(".tvr.lat") ? ["ТЕХНОЛОГИИ ТОВАРИЩЕСТВА", host.split(".")[0].toUpperCase(), "tmod"] : ["ВНЕШНЯЯ ССЫЛКА", host, "external"]);
    const resource = describeResource(url);
    return {
      url: url.href, contour: service[1], title: resource.title || service[1], tone: service[2], resource: resource.resource,
      previewable: supportsDocumentPreview(url.href),
      detail: resource.detail || SERVICE_DESCRIPTIONS[host] || (service[2] === "external"
        ? `Внешний сайт ${host}. Содержание страницы доступно по ссылке.`
        : "Страница сервиса Технологий Товарищества. Откроется в Blackbird."),
    };
  } catch { return; }
}

export function sharedLinkInMessage(body: string): { card?: SharedLinkCard; text: string } {
  const result = sharedLinksInMessage(body);
  return { card: result.cards[0], text: result.text };
}

/** A conversation row should read like a message, not a raw internal route. */
export function conversationSnippet(body: string): string {
  const { text, cards } = sharedLinksInMessage(body);
  const caption = text.replace(/\s+/g, " ").trim();
  if (caption) return caption;
  return cards.length ? `↗ ${cards[0].title}${cards.length > 1 ? ` · ещё ${cards.length - 1}` : ""}` : "Начните разговор";
}

export function sharedLinksInMessage(body: string): { cards: SharedLinkCard[]; text: string } {
  const cards: SharedLinkCard[] = [];
  let text = "";
  let offset = 0;
  for (const match of body.matchAll(LINK_PATTERN)) {
    const token = match[0];
    const card = describeSharedLink(token);
    if (!card || cards.length >= 6) continue;
    const start = match.index;
    text += body.slice(offset, start);
    // Keep sentence punctuation after the card instead of swallowing it.
    text += token.slice(token.replace(/[),.!?]+$/, "").length);
    offset = start + token.length;
    cards.push(card);
  }
  text += body.slice(offset);
  return { cards, text: text.replace(/\s+([,.!?;)])/g, "$1").trim() };
}
