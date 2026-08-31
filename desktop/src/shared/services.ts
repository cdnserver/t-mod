import type { DesktopService, ServiceId } from "./contracts";

export interface ServiceDefinition {
  id: ServiceId;
  title: string;
  eyebrow: string;
  description: string;
  accent: string;
  url?: string;
  shortcut?: string;
}

export const services: ServiceDefinition[] = [
  {
    id: "home",
    title: "Центр",
    eyebrow: "T-Mod Desktop",
    description: "Ваш день, внимание и все пространства экосистемы.",
    accent: "#91a8ff",
    shortcut: "⌘ 1",
  },
  {
    id: "reactor",
    title: "Мой Reactor",
    eyebrow: "Личный контур",
    description: "Мандат, казна, законопроекты и личные события.",
    accent: "#aa8cff",
    url: "https://tvr.lat/reactor",
    shortcut: "⌘ 2",
  },
  {
    id: "consensus",
    title: "Consensus",
    eyebrow: "Светлый круг",
    description: "Заседания, трансляция, голосование и протоколы.",
    accent: "#67b7ff",
    url: "https://consensus.tvr.lat/",
    shortcut: "⌘ 3",
  },
  {
    id: "atlas",
    title: "Atlas",
    eyebrow: "Интеллектуальный контур",
    description: "Правовой помощник, источники и специализированные агенты.",
    accent: "#52dfc1",
    url: "https://atlas.tvr.lat/",
    shortcut: "⌘ 4",
  },
  {
    id: "sgl",
    title: "SGL",
    eyebrow: "Правовое бюро",
    description: "Обращения, дела, договоры и архив практики.",
    accent: "#e9b65e",
    url: "https://sgl.tvr.lat/sgl",
  },
  {
    id: "ovr",
    title: "ОВР",
    eyebrow: "Закрытый контур",
    description: "Проверки кандидатов и защищённые досье.",
    accent: "#ee7a83",
    url: "https://ovr.tvr.lat/ovr",
  },
  {
    id: "games",
    title: "Games",
    eyebrow: "Игровой зал",
    description: "Шахматы, нарды и приватные матчи по приглашению.",
    accent: "#d58af0",
    url: "https://tvr.lat/games",
  },
  {
    id: "tasks",
    title: "Задачи",
    eyebrow: "Общий ритм",
    description: "Открытый список работ по решениям Товарищества.",
    accent: "#6ed29b",
    url: "https://consensus.tvr.lat/tasks",
  },
  {
    id: "admin",
    title: "Ядерный Reactor",
    eyebrow: "Операционный центр",
    description: "Контроль, аудит, доступы и здоровье всей системы.",
    accent: "#ff8b63",
    url: "https://reactor.tvr.lat/admin",
  },
];

export const serviceById = Object.fromEntries(
  services.map((service) => [service.id, service]),
) as Record<ServiceId, ServiceDefinition>;

// Navigation is an explicit product boundary.  Do not broaden this to
// `*.tvr.lat`: authenticated cookies are shared across the ecosystem and an
// arbitrary subdomain must never become an embedded T-Mod surface.
export const TRUSTED_TMOD_HOSTS = new Set([
  "tvr.lat",
  "reactor.tvr.lat",
  "consensus.tvr.lat",
  "atlas.tvr.lat",
  "sgl.tvr.lat",
  "ovr.tvr.lat",
  "phx.tvr.lat",
  "log.global.tvr.lat",
  "zigmund.tvr.lat",
]);

export function isServiceId(value: unknown): value is ServiceId {
  return typeof value === "string" && services.some((service) => service.id === value);
}

export function mergeServiceAccess(remote: DesktopService[] = []): ServiceDefinition[] {
  const access = new Map(remote.map((service) => [service.id, service]));
  return services.map((service) => {
    const state = service.id === "home" ? undefined : access.get(service.id);
    return {
      ...service,
      url: state?.url || service.url,
      ...(state && !state.enabled ? { description: state.reason || service.description } : {}),
    };
  });
}

export function isTrustedTModUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      url.username === "" &&
      url.password === "" &&
      url.port === "" &&
      TRUSTED_TMOD_HOSTS.has(url.hostname.toLowerCase())
    );
  } catch {
    return false;
  }
}

export function isTModAuthenticationUrl(value: string): boolean {
  if (!isTrustedTModUrl(value)) return false;
  try {
    const path = new URL(value).pathname.replace(/\/+$/, "") || "/";
    return path === "/login" || path === "/auth/login" || path === "/logout";
  } catch {
    return false;
  }
}

export function resolveNotificationServiceId(route: string | null): ServiceId | null {
  if (!route) return null;
  const firstSegment = route
    .trim()
    .replace(/^https:\/\/[^/]+/i, "")
    .split(/[/?#]/)
    .filter(Boolean)[0];
  if (firstSegment === "admin") return "admin";
  if (firstSegment === "reactor") return "reactor";
  if (firstSegment === "host") return "consensus";
  return isServiceId(firstSegment) ? firstSegment : null;
}
