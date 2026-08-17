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
    accent: "#8ea4ff",
    shortcut: "⌘ 1",
  },
  {
    id: "reactor",
    title: "Мой Reactor",
    eyebrow: "Личный контур",
    description: "Мандат, казна, законопроекты и личные события.",
    accent: "#a99cff",
    url: "https://tvr.lat/reactor",
    shortcut: "⌘ 2",
  },
  {
    id: "consensus",
    title: "Consensus",
    eyebrow: "Светлый круг",
    description: "Заседания, трансляция, голосование и протоколы.",
    accent: "#79b8ff",
    url: "https://consensus.tvr.lat/",
    shortcut: "⌘ 3",
  },
  {
    id: "atlas",
    title: "Atlas",
    eyebrow: "Интеллектуальный контур",
    description: "Правовой помощник, источники и специализированные агенты.",
    accent: "#57dbc2",
    url: "https://atlas.tvr.lat/",
    shortcut: "⌘ 4",
  },
  {
    id: "sgl",
    title: "SGL",
    eyebrow: "Правовое бюро",
    description: "Обращения, дела, договоры и архив практики.",
    accent: "#e6bd72",
    url: "https://sgl.tvr.lat/sgl",
  },
  {
    id: "ovr",
    title: "ОВР",
    eyebrow: "Закрытый контур",
    description: "Проверки кандидатов и защищённые досье.",
    accent: "#e58284",
    url: "https://ovr.tvr.lat/ovr",
  },
  {
    id: "games",
    title: "Games",
    eyebrow: "Игровой зал",
    description: "Шахматы, нарды и приватные матчи по приглашению.",
    accent: "#d590ed",
    url: "https://tvr.lat/games",
  },
  {
    id: "tasks",
    title: "Задачи",
    eyebrow: "Общий ритм",
    description: "Открытый список работ по решениям Товарищества.",
    accent: "#7ed6a5",
    url: "https://consensus.tvr.lat/tasks",
  },
  {
    id: "admin",
    title: "Ядерный Reactor",
    eyebrow: "Операционный центр",
    description: "Контроль, аудит, доступы и здоровье всей системы.",
    accent: "#ff9777",
    url: "https://reactor.tvr.lat/admin",
  },
];

export const serviceById = Object.fromEntries(
  services.map((service) => [service.id, service]),
) as Record<ServiceId, ServiceDefinition>;

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
      (url.hostname === "tvr.lat" || url.hostname.endsWith(".tvr.lat"))
    );
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
