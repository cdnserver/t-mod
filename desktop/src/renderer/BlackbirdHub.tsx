import { useEffect, useMemo, useState } from "react";
import type { DesktopNotification, ServiceId } from "../shared/contracts";
import type { AtlasOverlayConfig } from "../shared/atlas-overlay";
import masterMark from "./assets/blackbird/master.png";
import masterMarkHd from "./assets/blackbird/master-hd.png";
import atlasMark from "./assets/blackbird/atlas.png";
import consensusMark from "./assets/blackbird/consensus.png";
import { BlackbirdWordmark } from "./BlackbirdWordmark";

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

  return (
    <section className="blackbird-hub bb3-hub" aria-label="Главная Blackbird">
      <aside className="bb3-rail" aria-label="Быстрый переход">
        <img className="bb3-rail-mark" src={masterMark} alt="" aria-hidden="true"/>
        <div className="bb3-rail-links">
          <span className="bb3-rail-home" aria-label="Главная" aria-current="page"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m3.5 10 8.5-7 8.5 7v10H3.5V10Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><path d="M9 20v-7h6v7" stroke="currentColor" strokeWidth="1.5"/></svg></span>
          <button type="button" aria-label="Открыть Atlas" title="Atlas" disabled={!atlasReady} onClick={() => atlasReady && void onOpen("atlas")}><img src={atlasMark} alt=""/></button>
          <button type="button" aria-label="Открыть Сенат" title="Сенат" disabled={!senateEntry} onClick={() => senateEntry && void onOpen(senateEntry)}><img src={consensusMark} alt=""/></button>
        </div>
        <button type="button" className="bb3-rail-notifications" aria-label="Открыть уведомления" title="Уведомления" onClick={onOpenNotifications}><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 9h18c0-1-3-2-3-9Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"/><path d="M10 20h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></svg>{notifications.length > 0 && <i/>}</button>
      </aside>

      <div className="bb3-workspace">
        <header className="bb3-top">
          <span className="bb3-product"><BlackbirdWordmark/><i/>CLIENT</span>
          <span className="bb3-status"><i className={online ? "online" : ""}/>{online ? "На связи" : "Восстанавливаем связь"}</span>
        </header>

        <main className="bb3-content">
          <section className="bb3-hero">
            <div className="bb3-hero-copy">
              <p>{tier} <i/> {clock(now)}</p>
              <h1><span>Добро пожаловать в Blackbird,</span><strong>{greeting}.</strong></h1>
              <p className="bb3-hero-detail">Ваше рабочее пространство готово. Выберите направление, чтобы продолжить.</p>
            </div>
            <div className="bb3-hero-art" aria-hidden="true"><i/><img src={masterMarkHd} alt=""/></div>
          </section>

          <section className="bb3-apps" aria-label="Рабочие пространства">
            <header><div><small>РАБОЧИЕ ПРОСТРАНСТВА</small><h2>Куда направимся?</h2></div><span>01 — 02</span></header>
            <div className="bb3-app-grid">
              <button type="button" className="bb3-app atlas" disabled={!atlasReady} onClick={() => atlasReady && void onOpen("atlas")}>
                <span className="bb3-app-icon"><img src={atlasMark} alt=""/></span>
                <span className="bb3-app-copy"><small>ИНТЕЛЛЕКТУАЛЬНАЯ СИСТЕМА</small><strong>Atlas</strong><em>{atlasReady ? "Открыть интеллект" : "Доступ не выдан"}</em></span>
                <span className="bb3-app-arrow" aria-hidden="true">↗</span>
              </button>
              <button type="button" className="bb3-app senate" disabled={!senateEntry} onClick={() => senateEntry && void onOpen(senateEntry)}>
                <span className="bb3-app-icon"><img src={consensusMark} alt=""/></span>
                <span className="bb3-app-copy"><small>МАНДАТ · РЕШЕНИЯ · СИСТЕМЫ</small><strong>Сенат</strong><em>{senateEntry ? "Открыть контур" : "Доступ не выдан"}</em></span>
                <span className="bb3-app-arrow" aria-hidden="true">↗</span>
              </button>
            </div>
          </section>
        </main>

        <footer className="bb3-footer">
          <span>Технологии Товарищества <i/> Blackbird Client</span>
          <div>
            <button type="button" onClick={onOpenNotifications}>Уведомления <b>{notifications.length}</b></button>
            {overlayAllowed && <button type="button" onClick={onOverlaySettings}>{overlayConfig.enabled ? "Оверлей Atlas включён" : "Настроить оверлей"}</button>}
          </div>
        </footer>
      </div>
    </section>
  );
}
