import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import type {
  BootstrapResult,
  DesktopBootstrap,
  DesktopNotification,
  DesktopState,
  ServiceId,
} from "../shared/contracts";
import { serviceById, services } from "../shared/services";

const mockBootstrap: DesktopBootstrap = {
  protocol_version: 1,
  generated_at: new Date().toISOString(),
  viewer: {
    id: 721577061143019555,
    name: "Иван",
    display_name: "S. Goodman | 263345 | Иван",
    account_tier: "administrator",
    guild_member: true,
    administrator: true,
    sections: ["minecraft", "ovr"],
  },
  services: services
    .filter((service) => service.id !== "home")
    .map((service) => ({
      id: service.id as Exclude<ServiceId, "home">,
      title: service.title,
      url: service.url || "",
      enabled: true,
      reason: null,
    })),
  notifications: {
    unread: 3,
    items: [
      {
        id: 1,
        severity: "warning",
        kind: "consensus",
        title: "Заседание требует внимания",
        body: "Подготовка к пленарному консенсусу открыта.",
        route: "consensus",
        read_at: null,
        created_at: new Date().toISOString(),
      },
      {
        id: 2,
        severity: "success",
        kind: "atlas",
        title: "Atlas обновил библиотеку",
        body: "Новые материалы готовы к поиску.",
        route: "atlas",
        read_at: null,
        created_at: new Date(Date.now() - 36e5).toISOString(),
      },
    ],
  },
};

function Icon({ name }: { name: ServiceId | "search" | "bell" | "refresh" | "back" | "forward" | "command" | "lock" }) {
  const paths: Record<string, React.ReactNode> = {
    home: <><circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="2"/><path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3"/></>,
    reactor: <><path d="M4 8.5h16M4 15.5h16"/><path d="M8.5 4v16M15.5 4v16"/><circle cx="12" cy="12" r="3"/></>,
    consensus: <><path d="M4 20h16M6 17V9M10 17V9M14 17V9M18 17V9M3 7l9-4 9 4z"/></>,
    atlas: <><circle cx="12" cy="12" r="8.5"/><path d="M8 16l2-6 6-2-2 6z"/><circle cx="12" cy="12" r="1"/></>,
    sgl: <><path d="M6 3h9l3 3v15H6z"/><path d="M15 3v4h4M9 11h6M9 15h6"/></>,
    ovr: <><path d="M12 2.5l8 3.2v5.6c0 5.1-3.3 8.4-8 10.2-4.7-1.8-8-5.1-8-10.2V5.7z"/><path d="M8.5 12l2.2 2.2 4.8-5"/></>,
    games: <><path d="M8 8h8a5 5 0 014.8 6.4l-1 3.2a2.6 2.6 0 01-4.4 1l-1.2-1.4H9.8l-1.2 1.4a2.6 2.6 0 01-4.4-1l-1-3.2A5 5 0 018 8z"/><path d="M8 11v4M6 13h4M16.5 12.5h.01M18.5 14.5h.01"/></>,
    tasks: <><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 9l1.5 1.5L12 8M8 15l1.5 1.5L12 14M14 9h3M14 15h3"/></>,
    admin: <><path d="M12 2l2.2 5.4L20 8l-4.3 3.7L17 18l-5-3.2L7 18l1.3-6.3L4 8l5.8-.6z"/><circle cx="12" cy="11" r="2.2"/></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/></>,
    bell: <><path d="M18 9a6 6 0 00-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8A7 7 0 0118 6l2 6M17.9 16A7 7 0 016 18l-2-6"/></>,
    back: <path d="M15 18l-6-6 6-6"/>,
    forward: <path d="M9 18l6-6-6-6"/>,
    command: <><path d="M9 7V5.5A2.5 2.5 0 106.5 8H18M15 17v1.5a2.5 2.5 0 102.5-2.5H6"/></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 018 0v3"/></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

function greeting(name: string): string {
  const hour = new Date().getHours();
  if (hour < 5) return `Доброй ночи, ${name}`;
  if (hour < 12) return `Доброе утро, ${name}`;
  if (hour < 18) return `Добрый день, ${name}`;
  return `Добрый вечер, ${name}`;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "сейчас"
    : new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function browserApi() {
  return window.tmodDesktop;
}

export function App() {
  const [bootstrap, setBootstrap] = useState<BootstrapResult>({
    authenticated: !browserApi(),
    online: true,
    data: browserApi() ? undefined : mockBootstrap,
  });
  const [desktopState, setDesktopState] = useState<DesktopState>({
    activeService: "home",
    loading: false,
    canGoBack: false,
    canGoForward: false,
  });
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const loadBootstrap = useCallback(async () => {
    const api = browserApi();
    if (!api) return;
    setBootstrap((current) => ({ ...current, online: true }));
    setBootstrap(await api.bootstrap());
  }, []);

  useEffect(() => {
    void loadBootstrap();
    const api = browserApi();
    if (!api) return;
    const unsubscribeState = api.onState(setDesktopState);
    const unsubscribeAuth = api.onAuthChanged(loadBootstrap);
    const refresh = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadBootstrap();
    }, 45_000);
    const onVisible = () => {
      if (document.visibilityState === "visible") void loadBootstrap();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      unsubscribeState();
      unsubscribeAuth();
      window.clearInterval(refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [loadBootstrap]);

  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setNotificationsOpen(false);
      }
      if ((event.metaKey || event.ctrlKey) && /^[1-4]$/.test(event.key)) {
        const target = services[Number(event.key) - 1];
        if (target) void selectService(target.id);
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  });

  useEffect(() => {
    if (paletteOpen) window.setTimeout(() => searchRef.current?.focus(), 40);
  }, [paletteOpen]);

  const access = useMemo(
    () => new Map(bootstrap.data?.services.map((service) => [service.id, service]) || []),
    [bootstrap.data],
  );
  const visibleServices = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return services.filter((service) =>
      !normalized || `${service.title} ${service.eyebrow} ${service.description}`.toLowerCase().includes(normalized),
    );
  }, [query]);
  const activeDefinition = serviceById[desktopState.activeService];

  const selectService = async (serviceId: ServiceId) => {
    const remote = serviceId === "home" ? undefined : access.get(serviceId);
    if (remote && !remote.enabled) return;
    setPaletteOpen(false);
    setNotificationsOpen(false);
    const api = browserApi();
    if (api) setDesktopState(await api.navigate(serviceId));
    else setDesktopState((current) => ({ ...current, activeService: serviceId }));
  };

  const notifications = bootstrap.data?.notifications.items || [];
  const unread = bootstrap.data?.notifications.unread || 0;
  const userName = bootstrap.data?.viewer.name || "гость";
  const style = { "--active-accent": activeDefinition?.accent || "#8ea4ff" } as CSSProperties;

  return (
    <div className="desktop" style={style}>
      <div className="aurora" aria-hidden="true"><i/><i/><i/></div>
      <aside className="sidebar">
        <button className="brand" onClick={() => void selectService("home")} aria-label="T-Mod — домой">
          <span className="brand-mark"><span>T</span></span>
          <span><strong>T-Mod</strong><small>desktop system</small></span>
        </button>

        <button className="quick-search" onClick={() => setPaletteOpen(true)}>
          <Icon name="search"/><span>Найти или открыть</span><kbd>⌘ K</kbd>
        </button>

        <nav className="nav-list" aria-label="Сервисы T-Mod">
          {services.map((service) => {
            const remote = service.id === "home" ? undefined : access.get(service.id);
            const locked = Boolean(remote && !remote.enabled);
            const active = desktopState.activeService === service.id;
            return (
              <button
                key={service.id}
                className={`nav-item ${active ? "active" : ""} ${locked ? "locked" : ""}`}
                style={{ "--service-accent": service.accent } as CSSProperties}
                onClick={() => void selectService(service.id)}
                title={locked ? remote?.reason || "Нет доступа" : service.description}
              >
                <span className="nav-icon"><Icon name={locked ? "lock" : service.id}/></span>
                <span className="nav-copy"><strong>{service.title}</strong><small>{service.eyebrow}</small></span>
                {service.shortcut && <kbd>{service.shortcut.replace("⌘ ", "")}</kbd>}
                {active && <span className="active-line"/>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className={`connection ${bootstrap.online ? "online" : "offline"}`}>
            <span className="connection-dot"/><span>{bootstrap.online ? "Контур на связи" : "Нет соединения"}</span>
          </div>
          <button className="identity" onClick={() => !bootstrap.authenticated && void browserApi()?.openLogin()}>
            <span className="avatar">{userName.slice(0, 1).toUpperCase()}</span>
            <span><strong>{bootstrap.authenticated ? userName : "Войти в T-Mod"}</strong><small>{bootstrap.data?.viewer.account_tier === "administrator" ? "Администратор" : bootstrap.data?.viewer.guild_member ? "Товарищество" : "Единый аккаунт"}</small></span>
            <span className="identity-arrow">›</span>
          </button>
        </div>
      </aside>

      <header className="topbar">
        <div className="browser-tools">
          <button disabled={!desktopState.canGoBack} onClick={() => void browserApi()?.goBack()}><Icon name="back"/></button>
          <button disabled={!desktopState.canGoForward} onClick={() => void browserApi()?.goForward()}><Icon name="forward"/></button>
          <div className="surface-title"><span style={{ background: activeDefinition.accent }}/><strong>{activeDefinition.title}</strong><small>{activeDefinition.eyebrow}</small></div>
        </div>
        <div className="top-actions">
          {desktopState.activeService !== "home" && <button className="circle-action" onClick={() => void browserApi()?.reload()} title="Обновить"><Icon name="refresh"/></button>}
          <button className={`circle-action ${unread ? "has-unread" : ""}`} onClick={() => setNotificationsOpen((open) => !open)} title="Уведомления"><Icon name="bell"/>{unread > 0 && <b>{Math.min(unread, 99)}</b>}</button>
          <div className="window-actions"><button onClick={() => void browserApi()?.minimize()}>—</button><button onClick={() => void browserApi()?.toggleMaximize()}>□</button><button className="close" onClick={() => void browserApi()?.close()}>×</button></div>
        </div>
        {desktopState.loading && <div className="load-line"/>}
      </header>

      <main className={`content ${desktopState.activeService !== "home" ? "service-open" : ""}`}>
        {desktopState.activeService === "home" ? (
          <Home
            name={userName}
            bootstrap={bootstrap}
            notifications={notifications}
            access={access}
            onOpen={selectService}
            onLogin={() => void browserApi()?.openLogin()}
          />
        ) : (
          <div className="service-underlay"><div className="service-orbit"/><p>Открываем {activeDefinition.title}</p></div>
        )}
      </main>

      {notificationsOpen && (
        <Notifications items={notifications} unread={unread} onClose={() => setNotificationsOpen(false)} onOpen={selectService}/>
      )}
      {paletteOpen && (
        <CommandPalette
          query={query}
          setQuery={setQuery}
          items={visibleServices}
          access={access}
          inputRef={searchRef}
          onClose={() => setPaletteOpen(false)}
          onOpen={selectService}
        />
      )}
    </div>
  );
}

function Home({
  name,
  bootstrap,
  notifications,
  access,
  onOpen,
  onLogin,
}: {
  name: string;
  bootstrap: BootstrapResult;
  notifications: DesktopNotification[];
  access: Map<string, { enabled: boolean; reason: string | null }>;
  onOpen: (id: ServiceId) => Promise<void>;
  onLogin: () => void;
}) {
  if (!bootstrap.authenticated) {
    return (
      <section className="login-stage">
        <div className="login-sigil"><span>T</span><i/><i/><i/></div>
        <p className="kicker">ЕДИНЫЙ КОНТУР</p>
        <h1>Вся экосистема<br/>в одном движении.</h1>
        <p>Один аккаунт для Atlas, Consensus, SGL, Reactor и следующих систем T‑Mod.</p>
        <button className="primary" onClick={onLogin}>Войти в T-Mod <span>→</span></button>
        {!bootstrap.online && <small className="offline-note">Сеть недоступна. Хаб продолжит проверять соединение.</small>}
      </section>
    );
  }

  const tier = bootstrap.data?.viewer.administrator ? "Полный контур" : bootstrap.data?.viewer.guild_member ? "Контур Товарищества" : "Базовый контур";
  return (
    <div className="home-scroll">
      <section className="welcome">
        <div>
          <p className="kicker">{tier}</p>
          <h1>{greeting(name)}</h1>
          <p>Здесь собраны пространства, решения и события, которым сегодня нужно ваше внимание.</p>
        </div>
        <div className="time-block"><strong>{new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(new Date())}</strong><span>время Товарищества</span></div>
      </section>

      <section className="overview-grid">
        <article className="reactor-card" onClick={() => void onOpen("reactor")}>
          <div className="reactor-visual"><i/><i/><i/><span>T</span></div>
          <div className="reactor-copy"><p>Личный Reactor</p><h2>Ваш мандат<br/>в активном состоянии</h2><span>Открыть пространство <b>→</b></span></div>
          <div className="reactor-noise"/>
        </article>
        <article className="attention-card">
          <div className="section-heading"><span><Icon name="bell"/></span><div><p>Центр внимания</p><h2>{notifications.length ? `${notifications.length} важных события` : "Всё спокойно"}</h2></div></div>
          <div className="mini-events">
            {notifications.slice(0, 3).map((item) => (
              <button key={item.id} onClick={() => item.route && void onOpen((item.route.split("/")[0] || "home") as ServiceId)}>
                <i className={item.severity}/><span><strong>{item.title}</strong><small>{item.body}</small></span><time>{formatTime(item.created_at)}</time>
              </button>
            ))}
            {!notifications.length && <div className="empty-events">Новых событий нет. Можно сосредоточиться на текущей работе.</div>}
          </div>
        </article>
      </section>

      <section className="spaces-section">
        <div className="section-title"><div><p className="kicker">Пространства</p><h2>Продолжить работу</h2></div><button onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}><Icon name="command"/> Командная строка</button></div>
        <div className="space-grid">
          {services.filter((service) => !["home", "reactor"].includes(service.id)).map((service) => {
            const remote = access.get(service.id);
            const locked = Boolean(remote && !remote.enabled);
            return (
              <button key={service.id} className={`space-card ${locked ? "locked" : ""}`} style={{ "--service-accent": service.accent } as CSSProperties} onClick={() => !locked && void onOpen(service.id)}>
                <span className="space-icon"><Icon name={locked ? "lock" : service.id}/></span>
                <span className="space-meta"><small>{service.eyebrow}</small><strong>{service.title}</strong><p>{locked ? remote?.reason : service.description}</p></span>
                <span className="space-arrow">↗</span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function Notifications({ items, unread, onClose, onOpen }: { items: DesktopNotification[]; unread: number; onClose: () => void; onOpen: (id: ServiceId) => Promise<void> }) {
  return <><button className="scrim clear" onClick={onClose} aria-label="Закрыть"/><aside className="notification-drawer"><header><div><p className="kicker">Поток T-Mod</p><h2>Уведомления</h2></div><span>{unread} новых</span></header><div className="notification-list">{items.map((item) => <button key={item.id} onClick={() => item.route && void onOpen((item.route.split("/")[0] || "home") as ServiceId)}><i className={item.severity}/><span><strong>{item.title}</strong><p>{item.body}</p><small>{formatTime(item.created_at)}</small></span></button>)}{!items.length && <div className="drawer-empty"><Icon name="bell"/><p>В центре уведомлений тихо.</p></div>}</div></aside></>;
}

function CommandPalette({ query, setQuery, items, access, inputRef, onClose, onOpen }: { query: string; setQuery: (query: string) => void; items: typeof services; access: Map<string, { enabled: boolean; reason: string | null }>; inputRef: React.RefObject<HTMLInputElement | null>; onClose: () => void; onOpen: (id: ServiceId) => Promise<void> }) {
  return <div className="palette-layer"><button className="scrim" onClick={onClose} aria-label="Закрыть"/><section className="palette"><div className="palette-input"><Icon name="search"/><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Открыть сервис, задачу или инструмент…"/><kbd>ESC</kbd></div><div className="palette-results"><p>Пространства T-Mod</p>{items.map((service) => { const remote = access.get(service.id); const locked = Boolean(remote && !remote.enabled); return <button key={service.id} disabled={locked} onClick={() => void onOpen(service.id)} style={{ "--service-accent": service.accent } as CSSProperties}><span><Icon name={locked ? "lock" : service.id}/></span><div><strong>{service.title}</strong><small>{locked ? remote?.reason : service.description}</small></div><kbd>↵</kbd></button>; })}</div><footer><span><kbd>↑↓</kbd> навигация</span><span><kbd>Enter</kbd> открыть</span><span>Локальная командная строка — команды не покидают T-Mod</span></footer></section></div>;
}
