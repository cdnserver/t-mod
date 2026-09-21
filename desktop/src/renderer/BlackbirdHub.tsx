import { useEffect, useMemo, useState, type CSSProperties } from "react";
import type { DesktopNotification, ServiceId } from "../shared/contracts";
import type { AtlasOverlayConfig } from "../shared/atlas-overlay";
import { resolveNotificationServiceId, serviceById } from "../shared/services";
import masterMark from "./assets/blackbird/master.png";
import atlasMark from "./assets/blackbird/atlas.png";
import consensusMark from "./assets/blackbird/consensus.png";
import reactorMark from "./assets/blackbird/reactor.png";
import sglMark from "./assets/blackbird/sgl.png";
import ovrMark from "./assets/blackbird/ovr.png";
import tasksMark from "./assets/blackbird/tasks.png";
import adminMark from "./assets/blackbird/admin.png";
import gamesMark from "./assets/blackbird/games.png";

type AccessMap = Map<string, { enabled: boolean; reason: string | null }>;

interface BlackbirdHubProps {
  name: string;
  tier: string;
  online: boolean;
  notifications: DesktopNotification[];
  access: AccessMap;
  overlayConfig: AtlasOverlayConfig;
  overlayAllowed: boolean;
  onOpen: (id: ServiceId) => Promise<void>;
  onOverlaySettings: () => void;
  onOpenNotifications: () => void;
}

const senateServices = [
  ["reactor", reactorMark],
  ["consensus", consensusMark],
  ["sgl", sglMark],
  ["ovr", ovrMark],
  ["tasks", tasksMark],
  ["admin", adminMark],
  ["games", gamesMark],
] as const satisfies ReadonlyArray<readonly [Exclude<ServiceId, "home" | "atlas">, string]>;

function formatClock(date: Date): string {
  return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function formatDate(date: Date): string {
  return new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long" }).format(date);
}

function greeting(date: Date): string {
  const hour = date.getHours();
  if (hour < 5) return "Доброй ночи";
  if (hour < 12) return "Доброе утро";
  if (hour < 18) return "Добрый день";
  return "Добрый вечер";
}

function notificationTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "сейчас" : formatClock(parsed);
}

export function BlackbirdHub({
  name,
  tier,
  online,
  notifications,
  access,
  overlayConfig,
  overlayAllowed,
  onOpen,
  onOverlaySettings,
  onOpenNotifications,
}: BlackbirdHubProps) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 20_000);
    return () => window.clearInterval(timer);
  }, []);

  const senateAvailable = useMemo(
    () => senateServices.filter(([id]) => access.get(id)?.enabled).length,
    [access],
  );
  const senateEntry = access.get("reactor")?.enabled
    ? "reactor"
    : access.get("consensus")?.enabled
      ? "consensus"
      : senateServices.find(([id]) => access.get(id)?.enabled)?.[0];
  const atlasReady = access.get("atlas")?.enabled === true;
  const recent = notifications.slice(0, 3);

  return (
    <div className="blackbird-hub">
      <div className="bbh-grid" aria-hidden="true"/>
      <header className="bbh-header">
        <div className="bbh-intro">
          <span className="bbh-overline"><i/> BLACKBIRD · COMMAND HUB</span>
          <h1>{greeting(now)}, <strong>{name}</strong></h1>
          <p>Выберите контур. Остальное Blackbird синхронизирует вокруг вашей работы.</p>
        </div>
        <div className="bbh-clock">
          <strong>{formatClock(now)}</strong>
          <span>{formatDate(now)}</span>
          <small><i className={online ? "online" : ""}/>{online ? "Система синхронизирована" : "Автономный режим"}</small>
        </div>
      </header>

      <main className="bbh-portals">
        <article className="bbh-portal senate" style={{ "--portal-accent": "#e7edf2" } as CSSProperties}>
          <div className="bbh-portal-light" aria-hidden="true"/>
          <img className="bbh-portal-mark" src={consensusMark} alt="" aria-hidden="true"/>
          <div className="bbh-portal-index"><span>01</span><i/></div>
          <div className="bbh-portal-copy">
            <small>УПРАВЛЕНИЕ · МАНДАТ · РЕШЕНИЯ</small>
            <h2>Сенат</h2>
            <p>Единый рабочий контур Товарищества: мандат, консенсус, правовая работа, задачи и закрытые системы.</p>
          </div>
          <div className="bbh-service-strip" aria-label="Сервисы Сената">
            {senateServices.map(([id, mark]) => {
              const enabled = access.get(id)?.enabled === true;
              return (
                <button key={id} className={enabled ? "available" : "locked"} disabled={!enabled} onClick={() => void onOpen(id)} title={enabled ? serviceById[id].description : access.get(id)?.reason || "Нет доступа"}>
                  <img src={mark} alt=""/><span>{serviceById[id].title}</span>{!enabled && <b>·</b>}
                </button>
              );
            })}
          </div>
          <button className="bbh-enter" disabled={!senateEntry} onClick={() => senateEntry && void onOpen(senateEntry)}>
            <span><small>{senateAvailable} СЕРВИСОВ ДОСТУПНО</small><b>Войти в контур Сената</b></span><i>↗</i>
          </button>
        </article>

        <article className="bbh-portal atlas" style={{ "--portal-accent": "#9fe7d2" } as CSSProperties}>
          <div className="bbh-portal-light" aria-hidden="true"/>
          <img className="bbh-portal-mark" src={atlasMark} alt="" aria-hidden="true"/>
          <div className="bbh-portal-index"><span>02</span><i/></div>
          <div className="bbh-portal-copy">
            <small>ИНТЕЛЛЕКТ · ИСТОЧНИКИ · ПОЛЕ</small>
            <h2>Atlas</h2>
            <p>Интеллектуальная система для анализа, права и работы в игре. Контекст следует за вами между экраном и оверлеем.</p>
          </div>
          <div className="bbh-atlas-status">
            <span><i className={atlasReady ? "online" : ""}/><b>{atlasReady ? "Интеллект доступен" : "Ожидает допуска"}</b></span>
            <span><i className={overlayAllowed && overlayConfig.enabled ? "online" : ""}/><b>{overlayAllowed && overlayConfig.enabled ? "Overlay готов" : "Overlay выключен"}</b></span>
            {overlayAllowed && <button onClick={onOverlaySettings}>Настроить поле <b>→</b></button>}
          </div>
          <button className="bbh-enter" disabled={!atlasReady} onClick={() => atlasReady && void onOpen("atlas")}>
            <span><small>ATLAS · GENERAL AGENT</small><b>Открыть Atlas</b></span><i>↗</i>
          </button>
        </article>
      </main>

      <section className="bbh-lower">
        <article className="bbh-pulse">
          <header><span><img src={masterMark} alt=""/>Системный пульс</span><small>{tier}</small></header>
          <div><span><i className={online ? "online" : ""}/><b>Ядро</b><small>{online ? "на связи" : "восстановление"}</small></span><span><i className={senateAvailable ? "online" : ""}/><b>Сенат</b><small>{senateAvailable}/{senateServices.length}</small></span><span><i className={atlasReady ? "online" : ""}/><b>Atlas</b><small>{atlasReady ? "готов" : "закрыт"}</small></span><span><i className={overlayConfig.enabled ? "online" : ""}/><b>Overlay</b><small>{overlayConfig.enabled ? "активен" : "ожидание"}</small></span></div>
        </article>
        <article className="bbh-events">
          <header><span>Последние события</span><button onClick={onOpenNotifications}>Все уведомления <b>→</b></button></header>
          <div>
            {recent.map((item) => (
              <button key={item.id} onClick={() => { const target = resolveNotificationServiceId(item.route); if (target) void onOpen(target); }}>
                <i className={item.severity}/><span><b>{item.title}</b><small>{item.body}</small></span><time>{notificationTime(item.created_at)}</time>
              </button>
            ))}
            {!recent.length && <p>Новых событий нет. Blackbird продолжает наблюдение.</p>}
          </div>
        </article>
      </section>
    </div>
  );
}
