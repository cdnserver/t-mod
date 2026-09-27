import { useEffect, useState } from "react";
import type { DesktopLoginCredentials, DesktopLoginResult, DesktopShellPreferences } from "../shared/contracts";
import technologies from "./assets/blackbird/technologies-signature.png";
import "./blackbird-setup.css";

const SETUP_STEPS = [
  ["Единый аккаунт", "Ваш ключ ко всей системе"],
  ["Личные настройки", "Имя, движение и спокойствие"],
  ["События и внимание", "Вы выбираете, как быть на связи"],
  ["Всё готово", "Ваше новое пространство"],
] as const;

export function BlackbirdSetup({ authenticated, name, preferences, onLogin, onComplete, onLater }: {
  authenticated: boolean; name: string; preferences: DesktopShellPreferences;
  onLogin(credentials: DesktopLoginCredentials): Promise<DesktopLoginResult>;
  onComplete(preferences: DesktopShellPreferences): void; onLater(): void;
}) {
  const [step, setStep] = useState(authenticated ? 1 : 0);
  const [draft, setDraft] = useState({ ...preferences, preferredName: preferences.preferredName || (authenticated ? name : "") });
  const [credentials, setCredentials] = useState({ login: "", pin: "", code: "", challenge: "" });
  const [factor, setFactor] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  useEffect(() => { if (!authenticated && step > 0) setStep(0); else if (authenticated && step === 0) setStep(1); }, [authenticated, step]);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError("");
    try {
      const result = await onLogin(credentials);
      if (result.ok) { setCredentials({ login: "", pin: "", code: "", challenge: "" }); setFactor(""); }
      if (!result.ok) {
        if (result.challenge) { setFactor(result.method || "totp"); setCredentials(current => ({ ...current, challenge: result.challenge! })); }
        const messages: Partial<Record<NonNullable<DesktopLoginResult["error"]>, string>> = {
          private_access_required: "Для Blackbird требуется личный допуск.",
          network_unavailable: "Сервер не ответил после повторных попыток. Попробуйте позже — менять PIN не нужно.",
          server_response_invalid: "Ответ сервера повреждён. Попробуйте позже — ваши данные входа не изменились.",
          login_in_progress: "Вход уже выполняется. Подождите завершения проверки.",
          locked: "Слишком много попыток входа. Подождите несколько минут.",
          banned: "Доступ к системе заблокирован.",
          login_failed: "Не удалось подтвердить сессию. Попробуйте снова.",
          two_factor_required: "Подтвердите вход временным или резервным кодом.",
        };
        setError(result.deliveryFailed ? "Доставка кода недоступна. Используйте сохранённый резервный код." : messages[result.error || "login_failed"] || "Не удалось войти. Проверьте логин и PIN-код.");
      }
    } catch { setError("Связь прервалась. Ваши настройки сохранены в мастере — попробуйте снова."); }
    finally { setBusy(false); }
  };
  return <section className={`bb-setup${draft.reduceMotion ? " reduced" : ""}`} aria-label="Первоначальная настройка Blackbird">
    <aside className="bb-setup-aside">
      <div className="bb-setup-brand"><img className="bb-setup-technologies" src={technologies} alt="Технологии Товарищества"/></div>
      <div className="bb-setup-intro"><h1>Ваш Blackbird.<br/><span>Ваш ритм.</span></h1><p>Одно пространство.<br/>Настроенное на вас.</p></div>
      <ol>{SETUP_STEPS.map(([label, description], index) => <li aria-current={index === step ? "step" : undefined} className={index === step ? "active" : index < step ? "done" : ""} key={label}><b>{index < step ? "✓" : `0${index + 1}`}</b><div><strong>{label}</strong><small>{description}</small></div><i/></li>)}</ol>
      <div className="bb-setup-aside-foot"><span>ЛИЧНОЕ ПРОСТРАНСТВО</span><b>BLACKBIRD CLIENT</b></div>
    </aside>
    <main className="bb-setup-main"><header><span className="bb-setup-header-label"><i/> НАСТРОЙКА ПРОСТРАНСТВА</span><button onClick={onLater}>Продолжить позже <span>↗</span></button></header>
      <div className="bb-setup-progress" aria-label={`Шаг ${step + 1} из 4`}><div>{SETUP_STEPS.map(([label], index) => <i key={label} className={index <= step ? "filled" : ""}/>)}</div><span>0{step + 1}<b> / 04</b></span></div>
      <div className="bb-setup-page" key={step}>
        {step === 0 && <><small>01 / АККАУНТ</small><h2>Начнём с вас.</h2><p>Подключите единый аккаунт Технологий Товарищества. Существующий аккаунт T-Mod подходит — новый создавать не нужно.</p>
          <div className="bb-setup-tabs"><button aria-pressed={!creating} className={!creating ? "active" : ""} onClick={() => setCreating(false)}>У меня есть аккаунт</button><button aria-pressed={creating} className={creating ? "active" : ""} onClick={() => setCreating(true)}>Создать аккаунт</button></div>
          {creating && <div className="bb-setup-note"><strong>Подтвердите свою личность</strong><p>Откройте сервер Товарищества, вызовите <b>/account</b> и создайте веб-доступ. Затем вернитесь сюда и введите полученные логин и PIN. Доступ к закрытому Blackbird выдаётся отдельно.</p><button onClick={() => { void window.tmodDesktop?.openAccountCreation().catch(() => setError("Не удалось открыть страницу создания аккаунта.")); }}>Открыть создание аккаунта ↗</button></div>}
          <form onSubmit={submit}><label>Логин<input required autoComplete="username" value={credentials.login} minLength={3} maxLength={32} onChange={event => { setFactor(""); setCredentials(current => ({ ...current, login: event.target.value, challenge: "", code: "" })); }}/></label><label>PIN или пароль<input required type="password" autoComplete="current-password" maxLength={128} value={credentials.pin} onChange={event => { setFactor(""); setCredentials(current => ({ ...current, pin: event.target.value, challenge: "", code: "" })); }}/></label>{factor && <label>Код {factor === "totp" ? "аутентификатора" : factor === "telegram" ? "Telegram" : "Discord"} или резервный код<input name="verification_code" type="password" autoComplete="one-time-code" required maxLength={32} value={credentials.code} onChange={event => setCredentials(current => ({ ...current, code: event.target.value }))}/></label>}{error && <p role="alert" className="bb-setup-error">{error}</p>}<button className="bb-setup-primary" disabled={busy}>{busy ? "Подтверждаем аккаунт…" : "Подключить аккаунт →"}</button></form>
        </>}
        {step === 1 && <><small>02 / ЛИЧНЫЕ НАСТРОЙКИ</small><h2>Как к вам обращаться?</h2><p>Это имя появится в приветствии и на экране блокировки. Оно не меняет имя вашего аккаунта.</p><label>Ваше имя<input maxLength={24} value={draft.preferredName} placeholder="Например, Роберт" onChange={event => setDraft(current => ({ ...current, preferredName: event.target.value }))}/></label>
          <label>Блокировка при бездействии<select value={draft.idleLockMinutes} onChange={event => setDraft(current => ({ ...current, idleLockMinutes: Number(event.target.value) }))}><option value={0}>Только вручную</option><option value={5}>Через 5 минут</option><option value={10}>Через 10 минут</option><option value={15}>Через 15 минут</option><option value={30}>Через 30 минут</option></select></label>
          <label className="bb-setup-check"><input type="checkbox" checked={draft.reduceMotion} onChange={event => setDraft(current => ({ ...current, reduceMotion: event.target.checked }))}/><span>Уменьшить движение<small>Спокойные переходы и статичная Луна.</small></span></label>
        </>}
        {step === 2 && <><small>03 / УВЕДОМЛЕНИЯ</small><h2>Только нужное внимание.</h2><p>События остаются в центре уведомлений независимо от выбранного способа показа.</p><div className="bb-setup-delivery">{([
          ["both", "Адаптивно", "В Blackbird — фирменная карточка. В фоне — уведомление Windows. Без дублей."],
          ["in-app", "Карточки Blackbird", "Отдельное небольшое окно поверх рабочего стола. Не перехватывает фокус."],
          ["system", "Уведомления Windows", "Стандартные уведомления и системный центр Windows."],
          ["off", "Без всплывающих окон", "Только история в центре уведомлений."],
        ] as const).map(([value, title, body], index) => <button key={value} aria-pressed={draft.notificationDelivery === value} className={draft.notificationDelivery === value ? "selected" : ""} onClick={() => setDraft(current => ({ ...current, notificationDelivery: value }))}><em>0{index + 1}</em><b>{title}</b><span>{body}</span><i aria-hidden="true"/></button>)}</div>
          <label className="bb-setup-check"><input type="checkbox" checked={draft.notificationSound} onChange={event => setDraft(current => ({ ...current, notificationSound: event.target.checked }))}/><span>Звук новых событий</span></label>
          <label className="bb-setup-check"><input type="checkbox" checked={draft.lockSound} onChange={event => setDraft(current => ({ ...current, lockSound: event.target.checked }))}/><span>Звук блокировки и разблокировки</span></label>
        </>}
        {step === 3 && <><small>04 / ГОТОВО</small><div className="bb-setup-ready-mark" aria-hidden="true">✓</div><h2>{draft.preferredName.trim() || name}, ваше пространство готово.</h2><p>Аккаунт подключён. Выбранные настройки будут запомнены на этом компьютере. Повторно открыть мастер можно из настроек.</p><dl className="bb-setup-summary"><div><dt>Ваше имя</dt><dd>{draft.preferredName.trim() || name}</dd></div><div><dt>Блокировка</dt><dd>{draft.idleLockMinutes ? `Через ${draft.idleLockMinutes} мин.` : "Вручную"}</dd></div><div><dt>Уведомления</dt><dd>{{ both: "Адаптивно", "in-app": "Blackbird", system: "Windows", off: "Только история" }[draft.notificationDelivery]}</dd></div></dl><div className="bb-setup-note"><strong>Atlas и Сенат</strong><p>Возможности появятся в соответствии с доступом вашего аккаунта. Оверлей настраивается отдельно: микрофон не включается автоматически.</p></div></>}
      </div>
      {step > 0 && <footer><button onClick={() => setStep(current => Math.max(1, current - 1))} disabled={step === 1}>Назад</button><button className="bb-setup-primary" disabled={!authenticated || (step === 1 && !draft.preferredName.trim())} onClick={() => step === 3 ? onComplete(draft) : setStep(current => current + 1)}>{step === 3 ? "Открыть Blackbird →" : "Продолжить →"}</button></footer>}
    </main>
  </section>;
}
