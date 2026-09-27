import { useEffect, useRef } from "react";
import type { DesktopNotification, ServiceId } from "../shared/contracts";
import type { BlackbirdWorkspace } from "../shared/workspaces";
import { canEnterWorkspace, workspaceForService } from "../shared/workspaces";
import { resolveNotificationServiceId } from "../shared/services";
import atlas from "./assets/blackbird/atlas.png";
import senate from "./assets/blackbird/consensus.png";
import reactor from "./assets/blackbird/reactor.png";
import tasks from "./assets/blackbird/tasks.png";
import sgl from "./assets/blackbird/sgl.png";
import ovr from "./assets/blackbird/ovr.png";
import games from "./assets/blackbird/games.png";
import admin from "./assets/blackbird/admin.png";

type Access = Map<string, { enabled:boolean; reason:string|null }>;
const marks = { atlas, consensus:senate, reactor, tasks, sgl, ovr, games, admin };
const details = {
  reactor:["Личный реактор","Мандат, профиль и ваши инициативы."],
  consensus:["Консенсус","Заседания, голосование и общие решения."],
  tasks:["Общие задачи","От решения — к исполнению."],
  sgl:["Правовое бюро","Дела, договоры и юридическая практика."],
  ovr:["Внешняя разведка","Проверки, расследования и досье."],
  games:["Игровой зал","Матчи и встречи вне заседаний."],
  admin:["Ядерный реактор","Доступы, аудит и управление системой."],
  atlas:["Atlas AI","Вопросы, разбор ситуаций и источники."],
} as const;
const groups: {title:string; ids:Exclude<ServiceId,"home">[]}[] = [
  {title:"Участие",ids:["reactor","consensus","tasks"]},
  {title:"Институты",ids:["sgl","ovr"]},
  {title:"Системы",ids:["games","admin"]},
];

export function WorkspaceNavigation({ space, active, access, collapsed, online, inert, onHome, onOverview, onSwitch, onOpen, onOverlay, onToggle }: {
  space:BlackbirdWorkspace; active:ServiceId; access:Access; collapsed:boolean; online:boolean;
  inert?:boolean;
  onHome:()=>void; onOverview:()=>void; onSwitch:()=>void; onOpen:(id:ServiceId)=>Promise<void>; onOverlay:()=>void; onToggle:()=>void;
}) {
  const menuItem = (id:Exclude<ServiceId,"home">) => {
    const granted = access.get(id)?.enabled === true;
    return <button key={id} className={`bbw-nav-item ${active === id ? "active" : ""}`} disabled={!granted} aria-current={active === id ? "page" : undefined} title={granted ? details[id][0] : access.get(id)?.reason || "Доступ не выдан"} onClick={() => void onOpen(id)}><img src={marks[id]} alt=""/><span>{details[id][0]}</span>{!granted && <i aria-label="Нет доступа">·</i>}</button>;
  };
  const other = space === "atlas" ? "senate" : "atlas";
  return <aside inert={inert} className={`sidebar bbw-sidebar space-${space}`} aria-label={`Навигация: ${space === "atlas" ? "Atlas" : "Сенат"}`}>
    <div className="bbw-nav-top"><button title="Главная Blackbird" aria-label="Главная Blackbird" onClick={onHome}>↖<span>Blackbird</span></button><button aria-label={collapsed ? "Развернуть меню" : "Свернуть меню"} title={collapsed ? "Развернуть меню" : "Свернуть меню"} onClick={onToggle}>☰</button></div>
    <div className="bbw-nav-identity"><img src={space === "atlas" ? atlas : senate} alt=""/><div><small>РАБОЧЕЕ ПРОСТРАНСТВО</small><strong>{space === "atlas" ? "Atlas" : "Сенат"}</strong></div></div>
    <nav className="bbw-nav" aria-label="Разделы пространства"><button className={`bbw-nav-item ${active === "home" ? "active" : ""}`} aria-current={active === "home" ? "page" : undefined} title="Обзор пространства" onClick={onOverview}><b aria-hidden="true">◈</b><span>Обзор</span></button>
      {space === "atlas" ? <><small>ИНТЕЛЛЕКТ</small>{menuItem("atlas")}<button className="bbw-nav-item" title="Настройки Atlas Overlay" disabled={!access.get("atlas")?.enabled} onClick={onOverlay}><b aria-hidden="true">▱</b><span>Игровой оверлей</span></button></> : groups.map(group => <div key={group.title}><small>{group.title.toLocaleUpperCase()}</small>{group.ids.map(menuItem)}</div>)}
    </nav>
    <footer><span className={`bbw-online ${online ? "online" : ""}`}><i/><span>{online ? "На связи" : "Восстанавливаем связь"}</span></span><button className="bbw-switch" disabled={!canEnterWorkspace(other,access)} title={`Перейти в ${other === "atlas" ? "Atlas" : "Сенат"}`} onClick={onSwitch}><img src={other === "atlas" ? atlas : senate} alt=""/><span>{other === "atlas" ? "Перейти в Atlas" : "Перейти в Сенат"}</span><b>↗</b></button></footer>
  </aside>;
}

export function WorkspaceHome({ space, name, access, notifications, overlayEnabled, onOpen, onOverlay, onNotifications }: {
  space:BlackbirdWorkspace; name:string; access:Access; notifications:DesktopNotification[]; overlayEnabled:boolean;
  onOpen:(id:ServiceId)=>Promise<void>; onOverlay:()=>void; onNotifications:()=>void;
}) {
  const events = [...notifications].filter(item => {
    const service = resolveNotificationServiceId(item.route);
    return service && workspaceForService(service) === space;
  }).sort((a,b) => Date.parse(b.created_at) - Date.parse(a.created_at)).slice(0,3);
  const card = (id:Exclude<ServiceId,"home">, feature = false) => {
    const granted = access.get(id)?.enabled === true;
    return <button key={id} className={`bbw-service ${feature ? "feature" : ""}`} disabled={!granted} title={!granted ? access.get(id)?.reason || "Доступ не выдан" : undefined} onClick={() => void onOpen(id)}><img src={marks[id]} alt=""/><span><strong>{details[id][0]}</strong><small>{details[id][1]}</small></span><em>{granted ? "↗" : "Нет доступа"}</em></button>;
  };
  return <section className={`bbw-home space-${space}`} aria-label={`Обзор: ${space === "atlas" ? "Atlas" : "Сенат"}`}>
    <div className="bbw-home-inner"><header className="bbw-location"><span>{space === "atlas" ? "ATLAS / INTELLIGENCE" : "СЕНАТ / ТОВАРИЩЕСТВО"}</span><span>Обзор пространства</span></header>
    {space === "atlas" ? <>
      <div className="bbw-atlas-hero"><div className="bbw-hero-art" aria-hidden="true"><i/><img src={atlas} alt=""/></div><small>ВАШ ИНТЕЛЛЕКТУАЛЬНЫЙ КОНТУР</small><h1>Меньше шума.<br/><span>Больше ясности.</span></h1><p>{name}, здесь начинается работа с Atlas.<br/>Ответы, правовой разбор и помощь прямо в игре.</p><button className="bbw-primary" disabled={!access.get("atlas")?.enabled} onClick={() => void onOpen("atlas")}>Открыть Atlas AI <b>↗</b></button></div>
      <div className="bbw-atlas-tools"><button disabled={!access.get("atlas")?.enabled} onClick={onOverlay}><span>01 / В ИГРЕ</span><h2>Всегда рядом.</h2><p>Голос, оформление и управление оверлеем.</p><footer><i className={overlayEnabled ? "online" : ""}/>{overlayEnabled ? "Оверлей включён" : "Оверлей выключен"}<b>Настроить ↗</b></footer></button><button onClick={onNotifications}><span>02 / ВАШЕ ВНИМАНИЕ</span><h2>Ничего лишнего.</h2><p>Уведомления и история событий вашего аккаунта.</p><footer>Центр уведомлений<b>Открыть ↗</b></footer></button></div>
    </> : <>
      <div className="bbw-senate-hero"><small>УЧАСТВОВАТЬ. ОБСУЖДАТЬ. РЕШАТЬ.</small><h1>Сенат.<br/><span>Общее дело.</span></h1><p>{name}, ваше пространство участия в Товариществе.<br/>От личной инициативы до общего решения.</p><img src={senate} alt="" aria-hidden="true"/></div>
      <section className="bbw-services" aria-label="Участие в Сенате">{card("reactor",true)}{card("consensus",true)}{card("tasks")}</section>
      <section className="bbw-institutions"><header><h2>Институты и системы</h2><p>Возможности открываются в соответствии с вашим доступом.</p></header><div>{["sgl","ovr","games","admin"].map(id => card(id as Exclude<ServiceId,"home">))}</div></section>
    </>}
    <section className="bbw-events"><header><h2>События пространства</h2><button onClick={onNotifications}>Все уведомления ↗</button></header>{events.length ? events.map(item => <button key={item.id} onClick={onNotifications}><i className={!item.read_at ? "unread" : ""}/><span><strong>{item.title}</strong><small>{item.body}</small></span><time>{new Date(item.created_at).toLocaleDateString("ru-RU",{day:"numeric",month:"short"})}</time></button>) : <p>Пока всё спокойно. Новые события появятся здесь.</p>}</section>
    <footer className="bbw-home-footer"><span>ТЕХНОЛОГИИ ТОВАРИЩЕСТВА</span><span>{space === "atlas" ? "INTELLIGENCE, WITH CONTEXT." : "ОДНО СООБЩЕСТВО. ОБЩИЙ РИТМ."}</span></footer></div>
  </section>;
}

export function WorkspaceIntro({ space, reduced, onComplete }: { space:BlackbirdWorkspace; reduced:boolean; onComplete:()=>void }) {
  const skip = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement|null;
    skip.current?.focus({preventScroll:true});
    const key = (event:KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onComplete(); }
      if (event.key === "Tab") { event.preventDefault(); skip.current?.focus(); }
    };
    window.addEventListener("keydown",key,true);
    return () => { window.removeEventListener("keydown",key,true); if (previous?.isConnected) previous.focus({preventScroll:true}); };
  },[onComplete]);
  useEffect(() => {
    const timer = window.setTimeout(onComplete,reduced ? 850 : 3400);
    return () => window.clearTimeout(timer);
  },[space,reduced,onComplete]);
  return <section className={`bbw-intro space-${space} ${reduced ? "reduced" : ""}`} role="dialog" aria-modal="true" aria-label={`Переход в ${space === "atlas" ? "Atlas" : "Сенат"}`}>
    <div className="bbw-intro-scene" aria-hidden="true">{space === "atlas" ? <><i className="bbw-orbit one"/><i className="bbw-orbit two"/><i className="bbw-orbit three"/><b className="bbw-star"/></> : <><i className="bbw-column one"/><i className="bbw-column two"/><i className="bbw-column three"/><b className="bbw-plinth"/></>}</div>
    <div className="bbw-intro-title"><img src={space === "atlas" ? atlas : senate} alt=""/><small>BLACKBIRD / {space === "atlas" ? "INTELLIGENCE" : "COMMUNITY"}</small><h1>{space === "atlas" ? "Atlas" : "Сенат"}</h1><p>{space === "atlas" ? "Ясность начинается здесь." : "В пространстве общих решений."}</p><i/></div><button ref={skip} className="bbw-intro-skip" onClick={onComplete}>Пропустить ↗</button>
  </section>;
}
