import { useEffect, useMemo, useState } from "react";
import type { DesktopNotification, ServiceId } from "../shared/contracts";
import type { AtlasOverlayConfig } from "../shared/atlas-overlay";
import atlasMark from "./assets/blackbird/atlas.png";
import consensusMark from "./assets/blackbird/consensus.png";

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

const senatePriority: ServiceId[] = ["reactor", "consensus", "tasks", "sgl", "ovr", "admin", "games"];

function clock(date: Date): string {
  return new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(date);
}

export function BlackbirdHub({ name, tier, online, notifications, access, overlayConfig, overlayAllowed, onOpen, onOverlaySettings, onOpenNotifications }: BlackbirdHubProps) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 20_000);
    return () => window.clearInterval(timer);
  }, []);

  const senateEntry = useMemo(() => senatePriority.find((id) => access.get(id)?.enabled), [access]);
  const atlasReady = access.get("atlas")?.enabled === true;
  const greeting = name?.trim() || "друг";
  const unread = notifications.filter(item => !item.read_at).length;
  const recent = [...notifications].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)).slice(0, 2);

  return (
    <section className="blackbird-hub bb3-hub" aria-label="Главная Blackbird">
      <div className="bb3-workspace">
        <header className="bb3-top">
          <span className="bb3-location">Главная</span>
          <span className="bb3-status"><time>{clock(now)}</time><i className={online ? "online" : ""}/>{online ? "На связи" : "Восстанавливаем связь"}</span>
        </header>

        <main className="bb3-content">
          <section className="bb3-hero">
            <div className="bb3-hero-copy">
              <p>{tier} <i/> ВАШЕ ПРОСТРАНСТВО</p>
              <h1><span>Добро пожаловать в Blackbird,</span><strong>{greeting}.</strong></h1>
              <p className="bb3-hero-detail">Интеллект и сообщество. Одно пространство для того, что важно.</p>
            </div>
          </section>

          <section className="bb3-apps" aria-label="Рабочие пространства">
            <header><h2>Выберите своё направление</h2></header>
            <div className="bb3-app-grid">
              <button type="button" className="bb3-app atlas" disabled={!atlasReady} title={!atlasReady ? access.get("atlas")?.reason || "Доступ не выдан" : undefined} onClick={() => atlasReady && void onOpen("atlas")}>
                <span className="bb3-app-icon"><img src={atlasMark} alt=""/></span>
                <span className="bb3-app-copy"><small>ИНТЕЛЛЕКТУАЛЬНАЯ СИСТЕМА</small><strong>Atlas</strong><span>Ответы, источники и ваш помощник в игре.</span><em>{atlasReady ? "Открыть интеллект" : "Доступ не выдан"}</em></span>
                <span className="bb3-app-arrow" aria-hidden="true">↗</span>
              </button>
              <button type="button" className="bb3-app senate" disabled={!senateEntry} onClick={() => senateEntry && void onOpen(senateEntry)}>
                <span className="bb3-app-icon"><img src={consensusMark} alt=""/></span>
                <span className="bb3-app-copy"><small>МАНДАТ · РЕШЕНИЯ · СИСТЕМЫ</small><strong>Сенат</strong><span>Личный реактор, инициативы и совместные решения.</span><em>{senateEntry ? "Открыть контур" : "Доступ не выдан"}</em></span>
                <span className="bb3-app-arrow" aria-hidden="true">↗</span>
              </button>
            </div>
          </section>
          <section className="bb3-recent" aria-label="Последние события"><header><h2>В вашем пространстве</h2><button onClick={onOpenNotifications}>Все события <span>↗</span></button></header>
            {recent.length ? <div>{recent.map(item => <button key={item.id} onClick={onOpenNotifications}><i className={item.read_at ? "" : "unread"}/><span><strong>{item.title}</strong><small>{item.body}</small></span><time>{new Date(item.created_at).toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}</time></button>)}</div> : <p>Пока всё спокойно. Новые события появятся здесь.</p>}
          </section>
        </main>

        <footer className="bb3-footer">
          <span>Ваше рабочее пространство</span>
          <div>
            <button type="button" onClick={onOpenNotifications}>Новые события <b>{unread}</b></button>
            {overlayAllowed && <button type="button" onClick={onOverlaySettings}>{overlayConfig.enabled ? "Оверлей Atlas включён" : "Настроить оверлей"}</button>}
          </div>
        </footer>
      </div>
    </section>
  );
}
