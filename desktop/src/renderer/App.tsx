import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from "react";
import type {
  BootstrapResult,
  DesktopLoginCredentials,
  DesktopLoginResult,
  DesktopNotification,
  DesktopState,
  DesktopUpdateState,
  ServiceId,
} from "../shared/contracts";
import {
  resolveNotificationServiceId,
  serviceById,
  services,
} from "../shared/services";

function Icon({ name }: { name: ServiceId | "search" | "bell" | "refresh" | "back" | "forward" | "command" | "lock" | "download" | "logout" | "shield" }) {
  const paths: Record<string, ReactNode> = {
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
    download: <><path d="M12 3v12M7.5 10.5L12 15l4.5-4.5"/><path d="M5 20h14"/></>,
    logout: <><path d="M10 4H5v16h5M14 8l4 4-4 4M8 12h10"/></>,
    shield: <><path d="M12 2.5l8 3.2v5.6c0 5.1-3.3 8.4-8 10.2-4.7-1.8-8-5.1-8-10.2V5.7z"/><path d="M9 12l2 2 4-4"/></>,
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
  const bridgeAvailable = Boolean(browserApi());
  const [bootstrap, setBootstrap] = useState<BootstrapResult>({
    authenticated: false,
    online: bridgeAvailable,
    error: bridgeAvailable ? "loading" : "desktop_bridge_unavailable",
  });
  const [bootstrapLoading, setBootstrapLoading] = useState(bridgeAvailable);
  const [desktopState, setDesktopState] = useState<DesktopState>({
    activeService: "home",
    loading: false,
    canGoBack: false,
    canGoForward: false,
  });
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [updateState, setUpdateState] = useState<DesktopUpdateState>({
    phase: "development",
    currentVersion: "—",
  });
  const [dismissedUpdate, setDismissedUpdate] = useState<string>();
  const searchRef = useRef<HTMLInputElement>(null);

  const loadBootstrap = useCallback(async () => {
    const api = browserApi();
    if (!api) {
      setBootstrapLoading(false);
      setBootstrap({
        authenticated: false,
        online: false,
        error: "desktop_bridge_unavailable",
      });
      return;
    }
    setBootstrapLoading(true);
    try {
      setBootstrap(await api.bootstrap());
    } finally {
      setBootstrapLoading(false);
    }
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
    const api = browserApi();
    if (!api) return;
    const unsubscribeUpdate = api.onUpdate(setUpdateState);
    return unsubscribeUpdate;
  }, []);

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
    if (serviceId !== "home" && (!bootstrap.authenticated || !remote?.enabled)) return;
    setPaletteOpen(false);
    setNotificationsOpen(false);
    const api = browserApi();
    if (api) setDesktopState(await api.navigate(serviceId));
    else setDesktopState((current) => ({ ...current, activeService: serviceId }));
  };

  const notifications = bootstrap.data?.notifications.items || [];
  const unread = bootstrap.data?.notifications.unread || 0;
  const userName = bootstrap.data?.viewer.name || "T-Mod";
  const style = { "--active-accent": activeDefinition?.accent || "#8ea4ff" } as CSSProperties;
  const updateBusy = ["checking", "available", "downloading"].includes(updateState.phase);
  const updateLabel = updateState.phase === "ready"
    ? `Установить ${updateState.version ? `v${updateState.version}` : "обновление"}`
    : updateState.phase === "downloading" || updateState.phase === "available"
      ? `Загрузка ${updateState.percent || 0}%`
      : updateState.phase === "checking"
        ? "Проверяем версию"
        : updateState.phase === "error"
          ? "Скачать обновление"
        : `v${updateState.currentVersion}`;

  const runUpdateAction = () => {
    const api = browserApi();
    if (!api || updateBusy) return;
    if (updateState.phase === "ready") void api.installUpdate();
    else if (updateState.phase === "error") void api.openReleasePage();
    else void api.checkForUpdates().then(setUpdateState);
  };

  const login = async (credentials: DesktopLoginCredentials): Promise<DesktopLoginResult> => {
    const api = browserApi();
    if (!api) return { ok: false, error: "login_failed" };
    const result = await api.login(credentials);
    if (result.ok) await loadBootstrap();
    return result;
  };

  const logout = async () => {
    const api = browserApi();
    if (!api) return;
    await api.logout();
    setBootstrap({ authenticated: false, online: true, error: "login_required" });
    setDesktopState((current) => ({ ...current, activeService: "home", error: undefined }));
  };

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
            const locked = service.id !== "home" && (
              !bootstrap.authenticated || !remote || !remote.enabled
            );
            const active = desktopState.activeService === service.id;
            return (
              <button
                key={service.id}
                className={`nav-item ${active ? "active" : ""} ${locked ? "locked" : ""}`}
                style={{ "--service-accent": service.accent } as CSSProperties}
                onClick={() => void selectService(service.id)}
                title={locked ? remote?.reason || "Войдите в T-Mod Account" : service.description}
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
          <div className="identity-row">
          <button className="identity" onClick={() => bootstrap.authenticated ? void selectService("reactor") : void selectService("home")}>
            <span className="avatar">{userName.slice(0, 1).toUpperCase()}</span>
            <span><strong>{bootstrap.authenticated ? userName : "Войти в T-Mod"}</strong><small>{bootstrap.data?.viewer.account_tier === "administrator" ? "Администратор" : bootstrap.data?.viewer.guild_member ? "Товарищество" : "Единый аккаунт"}</small></span>
            <span className="identity-arrow">›</span>
          </button>
          {bootstrap.authenticated && <button className="logout-button" onClick={() => void logout()} title="Выйти из аккаунта"><Icon name="logout"/></button>}
          </div>
        </div>
      </aside>

      <header className="topbar">
        <div className="browser-tools">
          <button disabled={!desktopState.canGoBack} onClick={() => void browserApi()?.goBack()}><Icon name="back"/></button>
          <button disabled={!desktopState.canGoForward} onClick={() => void browserApi()?.goForward()}><Icon name="forward"/></button>
          <div className="surface-title"><span style={{ background: activeDefinition.accent }}/><strong>{activeDefinition.title}</strong><small>{activeDefinition.eyebrow}</small></div>
        </div>
        <div className="top-actions">
          {browserApi() && (
            <button
              className={`update-pill ${updateState.phase}`}
              onClick={runUpdateAction}
              disabled={updateBusy}
              title={updateState.message || "Проверить обновления T-Mod"}
            >
              <Icon name={updateState.phase === "ready" ? "download" : "refresh"}/>
              <span>{updateLabel}</span>
              {(updateState.phase === "downloading" || updateState.phase === "available") && (
                <i style={{ width: `${updateState.percent || 0}%` }}/>
              )}
            </button>
          )}
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
            loading={bootstrapLoading}
            bridgeAvailable={bridgeAvailable}
            notifications={notifications}
            access={access}
            onOpen={selectService}
            onLogin={login}
          />
        ) : desktopState.error ? (
          <section className="service-error-stage">
            <span><Icon name="refresh"/></span>
            <p className="kicker">СЕРВИС НЕДОСТУПЕН</p>
            <h1>{activeDefinition.title} не открылся</h1>
            <p>Соединение могло прерваться или доступ изменился. Ваши данные не потеряны.</p>
            <div><button className="primary" onClick={() => void selectService(desktopState.activeService)}>Повторить <b>↻</b></button><button onClick={() => void selectService("home")}>Вернуться в Центр</button></div>
          </section>
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
          authenticated={bootstrap.authenticated}
          inputRef={searchRef}
          onClose={() => setPaletteOpen(false)}
          onOpen={selectService}
        />
      )}
      {updateState.phase === "ready" && dismissedUpdate !== updateState.version && (
        <aside className="update-toast" role="status">
          <span className="update-toast-icon"><Icon name="download"/></span>
          <div><small>T-Mod готов к обновлению</small><strong>Версия {updateState.version}</strong><p>Перезапуск займёт несколько секунд.</p></div>
          <button className="update-later" onClick={() => setDismissedUpdate(updateState.version)}>Позже</button>
          <button className="update-install" onClick={() => void browserApi()?.installUpdate()}>Перезапустить</button>
        </aside>
      )}
    </div>
  );
}

function Home({
  name,
  bootstrap,
  loading,
  bridgeAvailable,
  notifications,
  access,
  onOpen,
  onLogin,
}: {
  name: string;
  bootstrap: BootstrapResult;
  loading: boolean;
  bridgeAvailable: boolean;
  notifications: DesktopNotification[];
  access: Map<string, { enabled: boolean; reason: string | null }>;
  onOpen: (id: ServiceId) => Promise<void>;
  onLogin: (credentials: DesktopLoginCredentials) => Promise<DesktopLoginResult>;
}) {
  const [loginValue, setLoginValue] = useState("");
  const [pin, setPin] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState<DesktopLoginResult["error"]>();

  if (loading) {
    return (
      <section className="session-stage" aria-live="polite">
        <div className="session-orbit"><span>T</span><i/><i/></div>
        <p className="kicker">T-MOD ACCOUNT</p>
        <h1>Восстанавливаем<br/>защищённую сессию</h1>
        <p>Проверяем аккаунт, доступные пространства и актуальную версию клиента.</p>
        <div className="session-progress"><i/></div>
      </section>
    );
  }

  if (!bootstrap.authenticated) {
    const messages: Record<NonNullable<DesktopLoginResult["error"]>, string> = {
      invalid: "Логин или PIN не подошли. Проверьте данные и повторите вход.",
      locked: "Слишком много попыток. Подождите несколько минут и попробуйте снова.",
      reset_required: "PIN заблокирован. Напишите T-Mod команду /reset в Discord.",
      character_required: "Сначала добавьте персонажа через /account в Discord.",
      atlas_access: "Для этой учётной записи ещё не выдан доступ к Atlas.",
      banned: "Доступ к экосистеме T-Mod заблокирован.",
      network_unavailable: "Нет связи с T-Mod. Проверьте интернет и повторите вход.",
      login_failed: "Сессию не удалось подтвердить. Повторите вход.",
      invalid_input: "Логин — от 3 символов, PIN — ровно 8 цифр.",
    };
    const submit = async (event: FormEvent) => {
      event.preventDefault();
      if (loginBusy || !bridgeAvailable) return;
      setLoginBusy(true);
      setLoginError(undefined);
      try {
        const result = await onLogin({ login: loginValue, pin });
        if (!result.ok) setLoginError(result.error || "login_failed");
      } finally {
        setLoginBusy(false);
      }
    };
    return (
      <section className="login-stage">
        <div className="login-visual">
          <div className="login-sigil"><span>T</span><i/><i/><i/></div>
          <p className="kicker">ЕДИНЫЙ КОНТУР</p>
          <h1>Один вход.<br/>Вся экосистема.</h1>
          <p>Ваши права, сервисы и сессия синхронизируются через защищённый T-Mod Account.</p>
          <div className="login-assurances"><span><Icon name="shield"/><b>HttpOnly-сессия</b></span><span><i/>Все домены tvr.lat</span></div>
        </div>
        <form className="desktop-login-form" onSubmit={(event) => void submit(event)}>
          <header><p>T·ID</p><h2>Войти в T-Mod</h2><span>Данные задаются через <b>/account</b> в личных сообщениях боту.</span></header>
          <label><span>Логин</span><input value={loginValue} onChange={(event) => setLoginValue(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={3} maxLength={32} placeholder="ваш.логин" disabled={loginBusy}/></label>
          <label><span>PIN · 8 цифр</span><input value={pin} onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 8))} autoComplete="current-password" inputMode="numeric" type="password" minLength={8} maxLength={8} placeholder="••••••••" disabled={loginBusy}/></label>
          {loginError && <output className="desktop-login-error">{messages[loginError]}</output>}
          {!bridgeAvailable && <output className="desktop-login-error">Компонент приложения не загрузился. Переустановите T-Mod из последнего релиза.</output>}
          <button className="primary" type="submit" disabled={loginBusy || !bridgeAvailable}>{loginBusy ? "Проверяем аккаунт…" : "Войти в T-Mod"}<span>→</span></button>
          <footer><i className={bootstrap.online ? "online" : ""}/><span>{bootstrap.online ? "Сервер T-Mod доступен" : "Нет соединения с сервером"}</span></footer>
        </form>
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
        <article className={`reactor-card ${access.get("reactor")?.enabled ? "" : "locked"}`} onClick={() => access.get("reactor")?.enabled && void onOpen("reactor")}>
          <div className="reactor-visual"><i/><i/><i/><span>T</span></div>
          <div className="reactor-copy"><p>Личный Reactor</p><h2>{access.get("reactor")?.enabled ? <>Ваш мандат<br/>в активном состоянии</> : <>Доступ откроется<br/>после вступления</>}</h2><span>{access.get("reactor")?.enabled ? <>Открыть пространство <b>→</b></> : access.get("reactor")?.reason}</span></div>
          <div className="reactor-noise"/>
        </article>
        <article className="attention-card">
          <div className="section-heading"><span><Icon name="bell"/></span><div><p>Центр внимания</p><h2>{notifications.length ? `${notifications.length} важных события` : "Всё спокойно"}</h2></div></div>
          <div className="mini-events">
            {notifications.slice(0, 3).map((item) => (
              <button key={item.id} onClick={() => { const target = resolveNotificationServiceId(item.route); if (target) void onOpen(target); }}>
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
            const locked = !remote || !remote.enabled;
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
  return <><button className="scrim clear" onClick={onClose} aria-label="Закрыть"/><aside className="notification-drawer"><header><div><p className="kicker">Поток T-Mod</p><h2>Уведомления</h2></div><span>{unread} новых</span></header><div className="notification-list">{items.map((item) => <button key={item.id} onClick={() => { const target = resolveNotificationServiceId(item.route); if (target) void onOpen(target); }}><i className={item.severity}/><span><strong>{item.title}</strong><p>{item.body}</p><small>{formatTime(item.created_at)}</small></span></button>)}{!items.length && <div className="drawer-empty"><Icon name="bell"/><p>В центре уведомлений тихо.</p></div>}</div></aside></>;
}

function CommandPalette({ query, setQuery, items, access, authenticated, inputRef, onClose, onOpen }: { query: string; setQuery: (query: string) => void; items: typeof services; access: Map<string, { enabled: boolean; reason: string | null }>; authenticated: boolean; inputRef: RefObject<HTMLInputElement | null>; onClose: () => void; onOpen: (id: ServiceId) => Promise<void> }) {
  return <div className="palette-layer"><button className="scrim" onClick={onClose} aria-label="Закрыть"/><section className="palette"><div className="palette-input"><Icon name="search"/><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Открыть сервис, задачу или инструмент…"/><kbd>ESC</kbd></div><div className="palette-results"><p>Пространства T-Mod</p>{items.map((service) => { const remote = access.get(service.id); const locked = service.id !== "home" && (!authenticated || !remote || !remote.enabled); return <button key={service.id} disabled={locked} onClick={() => void onOpen(service.id)} style={{ "--service-accent": service.accent } as CSSProperties}><span><Icon name={locked ? "lock" : service.id}/></span><div><strong>{service.title}</strong><small>{locked ? remote?.reason || "Войдите в T-Mod Account" : service.description}</small></div><kbd>↵</kbd></button>; })}</div><footer><span><kbd>Esc</kbd> закрыть</span><span><kbd>Enter</kbd> открыть</span><span>Команды выполняются только внутри T-Mod</span></footer></section></div>;
}
