import { useEffect, useState } from "react";

const explanations: Record<string, string> = {
  current_credential_invalid: "Текущий PIN или пароль не подошёл.", password_length: "Пароль должен содержать от 12 до 128 символов.",
  web_pin_invalid: "PIN должен состоять из 8 цифр.", verification_invalid: "Код не подошёл, уже использован или истёк. Запросите новый.",
  challenge_rate_limited: "Слишком много запросов. Подождите минуту.", factor_already_enabled: "Сначала отключите текущий способ подтверждения.",
  telegram_not_linked: "Сначала привяжите Telegram к аккаунту.", delivery_unavailable: "Не удалось доставить код. Проверьте личные сообщения и попробуйте позже.",
  security_key_unavailable: "Администратор ещё не настроил защищённое хранение ключей 2FA.", session_expired: "Сессия истекла. Войдите в аккаунт ещё раз.",
};
function errorText(error: unknown) {
  const value = error instanceof Error ? error.message : "";
  return Object.entries(explanations).find(([key]) => value.includes(key))?.[1] || "Не удалось связаться с аккаунтом. Повторите попытку — изменения не подтверждены.";
}
function api(action: "security" | "billing" | "update", data?: Record<string, string>) {
  const call = window.tmodDesktop?.accountRequest;
  if (!call) return Promise.reject(new Error("account_unavailable"));
  return call(action, data);
}
const methods: Record<string, string> = { totp: "Приложение-аутентификатор", discord: "Код в Discord", telegram: "Код в Telegram" };

export function AccountSecurity() {
  const [snapshot, setSnapshot] = useState<Record<string, unknown>>();
  const [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const [current, setCurrent] = useState(""), [value, setValue] = useState(""), [repeat, setRepeat] = useState("");
  const [kind, setKind] = useState("password"), [code, setCode] = useState(""), [challenge, setChallenge] = useState("");
  const [enrollment, setEnrollment] = useState<Record<string, unknown>>(), [recovery, setRecovery] = useState<string[]>([]);
  const refresh = () => api("security").then(setSnapshot).catch(e => setError(errorText(e)));
  useEffect(() => { let live = true; void api("security").then(data => { if (live) setSnapshot(data); }).catch(e => { if (live) setError(errorText(e)); }); return () => { live = false; }; }, []);
  const run = async (action: string, data: Record<string, string> = {}) => {
    if (busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await api("update", { action, current, code, challenge, ...data });
      if (action === "enroll") { setEnrollment(result); setChallenge(String(result.challenge)); setCode(""); if (result.delivery_failed) setError("Код не доставлен. Способ ещё не включён; проверьте личные сообщения и повторите подключение позже."); }
      else if (action === "challenge") { setChallenge(String(result.challenge)); setCode(""); setMessage(result.delivery_failed ? "Доставка недоступна. Используйте сохранённый резервный код." : "Подтвердите действие кодом или резервным кодом."); }
      else {
        setEnrollment(undefined); setChallenge(""); setCode(""); setCurrent(""); setValue(""); setRepeat("");
        if (Array.isArray(result.recovery_codes)) setRecovery(result.recovery_codes as string[]);
        setMessage(action === "credential" ? "Способ входа изменён. Прежние сессии отозваны." : action === "disable" ? "Двухэтапный вход отключён." : "Двухэтапный вход подключён.");
      }
      await refresh();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const links = snapshot?.links as { discord?: string; telegram?: { username?: string } | null } | undefined;
  const enabled = String(snapshot?.mfa_method || "");
  const input = (title: string, val: string, change: (value: string) => void, name: string, max = 128) => <label className="bbs-input"><span>{title}</span><input name={name} type="password" autoComplete={name === "current_password" ? "current-password" : name === "verification_code" ? "one-time-code" : "new-password"} maxLength={max} value={val} onChange={e => change(e.target.value)} disabled={busy}/></label>;
  return <div className="bbs-security">
    {error && <p className="bbs-message" role="alert">{error}</p>}
    {!snapshot ? <section className="bbs-group"><h2>Проверяем безопасность аккаунта</h2><p>Нужна действующая серверная сессия. Предпросмотр не меняет учётные данные.</p><button onClick={() => void refresh()}>Повторить загрузку</button></section> : <>
      <section className="bbs-group"><small>ЕДИНЫЙ АККАУНТ</small><h2>Ваши привязки</h2><div className="bbs-security-links"><article><strong>Discord</strong><span>{links?.discord || "Не подключён"}</span></article><article><strong>Telegram</strong><span>{links?.telegram ? links.telegram.username ? `@${links.telegram.username}` : "Подключён" : "Не подключён"}</span></article></div><p>Привязка Telegram выполняется через команду /telegram в Товариществе. Неподключённый канал нельзя использовать для подтверждения входа.</p></section>
      <section className="bbs-group"><h2>Подтверждение изменений</h2><p>Текущий способ входа: {snapshot.credential_kind === "password" ? "пароль" : "PIN из 8 цифр"}. Чувствительные изменения требуют подтверждения.</p>{input("Текущий PIN или пароль", current, setCurrent, "current_password")}{enabled && <><button disabled={busy || !current} onClick={() => void run("challenge")}>Запросить подтверждение · {methods[enabled]}</button>{challenge && input("Код подтверждения или резервный код", code, setCode, "verification_code", 32)}</>}</section>
      <section className="bbs-group"><h2>PIN или пароль</h2><div className="bbs-options"><button aria-pressed={kind === "password"} onClick={() => { setKind("password"); setValue(""); setRepeat(""); }}>Пароль</button><button aria-pressed={kind === "pin"} onClick={() => { setKind("pin"); setValue(""); setRepeat(""); }}>PIN-код</button></div><p>{kind === "password" ? "От 12 до 128 символов. Используйте уникальный пароль." : "Ровно 8 цифр. Пароль обычно даёт больше вариантов защиты."}</p>{input("Новый способ входа", value, setValue, "new_password")}{input("Повторите", repeat, setRepeat, "confirm_password")}<button disabled={busy || !current || !value || value !== repeat || Boolean(enabled && (!challenge || !code))} onClick={() => void run("credential", { kind, value })}>Изменить способ входа</button></section>
      <section className="bbs-group"><small>ВТОРОЙ ФАКТОР</small><h2>{enabled ? methods[enabled] : "Защитите вход дополнительным кодом"}</h2><p>Google Authenticator и совместимые приложения генерируют код без сети. Telegram и Discord доставляют временный код в личные сообщения.</p>{!snapshot.mfa_available && <p className="bbs-message">2FA ещё не настроена на сервере. Она не будет включена, пока защищённое хранение ключей недоступно.</p>}{!enabled && !enrollment && <div className="bbs-factor-options">{Object.entries(methods).map(([method, label]) => <button key={method} disabled={busy || !current || !snapshot.mfa_available || (method === "telegram" && !links?.telegram)} onClick={() => void run("enroll", { method })}><strong>{label}</strong><small>{method === "totp" ? "Рекомендуется · работает без сети" : "Личные сообщения · 5 минут"}</small></button>)}</div>}{enrollment && <div className="bbs-enrollment"><h3>Подтвердите подключение</h3>{Boolean(enrollment.secret) && <><p>В аутентификаторе выберите «Ввести ключ настройки», имя Blackbird, тип — по времени. Сохраните ключ и введите полученный код.</p><code>{String(enrollment.secret)}</code></>}{!enrollment.secret && <p>Код отправлен в личные сообщения. До подтверждения новый способ не активен.</p>}{input("Код из аутентификатора или сообщения", code, setCode, "verification_code", 6)}<button disabled={busy || code.length !== 6 || !current} onClick={() => void run("confirm")}>Подтвердить подключение</button><button onClick={() => { setEnrollment(undefined); setCode(""); setChallenge(""); }}>Отмена</button></div>}{enabled && <><p>Осталось резервных кодов: {String(snapshot.recovery_remaining || 0)}. Храните их отдельно от устройства.</p><button className="danger" disabled={busy || !current || !challenge || !code} onClick={() => { if (window.confirm("Отключить второй фактор? Вход останется защищён только PIN или паролем.")) void run("disable"); }}>Отключить двухэтапный вход</button></>}</section>
    </>}
    {!!recovery.length && <section className="bbs-group"><h2>Сохраните резервные коды</h2><p>Показываем только сейчас. Каждый действует один раз. Не публикуйте и не отправляйте другим людям.</p><div className="bbs-recovery">{recovery.map(item => <code key={item}>{item}</code>)}</div><button onClick={() => { if (window.confirm("Вы сохранили резервные коды в безопасном месте?")) setRecovery([]); }}>Коды сохранены</button></section>}
    {message && <p className="bbs-message" role="status">{message}</p>}
  </div>;
}

export function AccountBilling() {
  const [data, setData] = useState<Record<string, unknown>>(), [error, setError] = useState("");
  useEffect(() => { let live = true; void api("billing").then(v => { if (live) setData(v); }).catch(e => { if (live) setError(errorText(e)); }); return () => { live = false; }; }, []);
  const account = data?.account as Record<string, unknown> | undefined;
  const plan = data?.plan as Record<string, unknown> | undefined;
  const balance = data?.balance as Record<string, unknown> | undefined;
  const orders = (data?.orders || []) as Array<Record<string, unknown>>;
  return <>
    <section className="bbs-group"><small>ATLAS TOKEN</small><h2>Платёжная информация</h2>
      <p>Баланс и история вашего аккаунта. Blackbird не сохраняет данные банковской карты и не принимает платежи внутри приложения.</p>
      {error && <p role="alert" className="bbs-message">{error}</p>}
      {data ? <div className="bbs-billing-stats">
        <article><small>Доступно токенов</small><strong>{Number(balance?.total ?? data.balance_tokens ?? 0).toLocaleString("ru-RU")}</strong></article>
        <article><small>Тариф</small><strong>{String(plan?.name || account?.plan_code || "Free")}</strong><small>{Number(plan?.monthly_price_rub || 0).toLocaleString("ru-RU")} ₽ / месяц · стоимость тарифа</small></article>
        <article><small>Токены текущего периода</small><strong>{Number(data.monthly_balance_tokens || 0).toLocaleString("ru-RU")}</strong><small>{account?.period_ends_at ? `Период до ${new Date(String(account.period_ends_at)).toLocaleDateString("ru-RU")}` : ""}</small></article>
        <article><small>Отдельные пополнения</small><strong>{Number(data.payg_balance_tokens || 0).toLocaleString("ru-RU")}</strong><small>Оставшийся баланс пополнений</small></article>
      </div> : !error && <p role="status">Загружаем данные аккаунта…</p>}
      <button onClick={() => void window.tmodDesktop?.openBilling?.().catch(e => setError(errorText(e)))}>Открыть платёжный кабинет в браузере ↗</button>
    </section>
    <section className="bbs-group"><h2>История платежей</h2>{!orders.length ? <p>{data ? "Платежей пока нет." : "Ожидаем данные сервера."}</p> : <div className="bbs-payment-history">{orders.map(order => <article key={String(order.id)}><span>Заказ №{String(order.id)}<small>{order.created_at ? new Date(String(order.created_at)).toLocaleString("ru-RU") : ""}</small></span><strong>{(Number(order.amount_kopecks || 0) / 100).toLocaleString("ru-RU", { style: "currency", currency: "RUB" })}</strong><span>{({ paid: "Оплачен", pending: "Ожидает оплаты", cancelled: "Отменён", refunded: "Возвращён" } as Record<string, string>)[String(order.status)] || String(order.status)}</span></article>)}</div>}</section>
  </>;
}
