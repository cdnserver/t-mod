import { useEffect, useRef, useState, type ReactNode } from "react";
import type { DesktopBootstrap, DesktopShellPreferences, DesktopUpdateState } from "../shared/contracts";
import { desktopProduct } from "../shared/product";
import { AccountAvatar } from "./AccountAvatar";
import { AccountSecurity, AccountBilling } from "./AccountSettings";
import "./blackbird-settings.css";

const sections = [
  ["account", "Мой аккаунт", "Ваше пространство"],
  ["security", "Безопасность", "Привязки, пароль и второй фактор"],
  ["billing", "Биллинг", "Atlas Token и платежи"],
  ["appearance", "Внешний вид", "Интерфейс и масштаб"],
  ["lock", "Экран блокировки", "Пауза и звуки"],
  ["notifications", "Уведомления", "События и доставка"],
  ["atlas", "Atlas Overlay", "Голос, управление, оформление"],
  ["updates", "Обновления", "Версии и каналы"],
  ["connection", "Соединение", "Состояние приложения"],
] as const;
export type SettingsSection = typeof sections[number][0];

export function BlackbirdSettings(props: {
  preferences: DesktopShellPreferences; viewer?: DesktopBootstrap["viewer"]; name: string;
  online: boolean; lastSuccessfulAt?: string; updateState: DesktopUpdateState;
  initialSection?: SettingsSection; atlas: ReactNode; defaults: DesktopShellPreferences;
  onChange: (preferences: DesktopShellPreferences) => void; onClose: () => void;
  onReconnect: () => Promise<void>; onLock: () => void; onSetup?: () => void;
  onLogout: () => Promise<void>; onUpdate: () => void; onPreviewNotification: () => void;
  onPreviewIntro?: () => void;
}) {
  const { preferences: p, onChange, viewer } = props;
  const [section, setSection] = useState<SettingsSection>(props.initialSection || "account");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);
  const navigation = useRef<HTMLElement>(null);
  useEffect(() => { navigation.current?.querySelector<HTMLButtonElement>("button[aria-current]")?.focus(); }, []);
  const change = <K extends keyof DesktopShellPreferences>(key: K, value: DesktopShellPreferences[K]) => onChange({ ...p, [key]: value });
  const action = async (run: () => Promise<void>, success: string) => {
    if (busy) return;
    setBusy(true); setMessage("");
    try { await run(); setMessage(success); } catch { setMessage("Не удалось завершить действие. Попробуйте ещё раз."); }
    finally { setBusy(false); }
  };
  const toggle = (key: "compactMode" | "reduceMotion" | "solidSurfaces" | "sidebarCollapsed" | "lockSound" | "notificationSound", label: string, hint: string) =>
    <label className="bbs-toggle"><span><strong>{label}</strong><small>{hint}</small></span><input type="checkbox" checked={p[key]} onChange={event => change(key, event.target.checked)}/><i aria-hidden="true"/></label>;
  const options = <T extends string | number>(key: keyof DesktopShellPreferences, values: readonly (readonly [T, string])[], label: string) =>
    <div className="bbs-options" role="group" aria-label={label}>{values.map(([value, title]) => <button key={value} aria-pressed={p[key] === value} onClick={() => onChange({ ...p, [key]: value })}>{title}</button>)}</div>;
  const title = sections.find(([id]) => id === section)!;
  return <section className="bb-settings-page" aria-label="Настройки приложения">
    <nav className="bbs-nav" ref={navigation} aria-label="Разделы настроек">
      <div className="bbs-nav-heading"><span>ВАШЕ ПРОСТРАНСТВО</span><strong>Настройки</strong></div>
      {sections.map(([id, label, hint]) => <button key={id} aria-current={section === id ? "page" : undefined} onClick={() => { setSection(id); setMessage(""); setConfirmReset(false); }}><strong>{label}</strong><small>{hint}</small></button>)}
      <div className="bbs-nav-bottom"><span>{desktopProduct.name}</span><small>{props.updateState.currentVersion} · {p.updateChannel.toUpperCase()}</small><button onClick={props.onClose}>← Вернуться в приложение</button></div>
    </nav>
    <div className="bbs-main" key={section}>
      <div className="bbs-inner"><header className="bbs-heading"><small>НАСТРОЙКИ / {title[1].toLocaleUpperCase()}</small><h1>{title[1]}</h1><p>{title[2]}. {section === "security" ? "Защита единого аккаунта. Изменения действуют после подтверждения." : section === "billing" ? "Серверный баланс и история ваших платежей." : "Изменения сохраняются автоматически на этом устройстве."}</p></header>
      {section === "account" && <>
        <div className="bbs-account"><AccountAvatar url={viewer?.avatar_url} name={props.name}/><div><h2>{props.name}</h2><p>{viewer?.display_name || "Вход ещё не выполнен"}</p><span>{viewer?.administrator ? "Администратор" : viewer?.guild_member ? "Участник Товарищества" : "Единый аккаунт"}</span></div></div>
        <section className="bbs-group"><h2>Личное обращение</h2><p>Как к вам обращаться на заставке, в хабе и на экране блокировки.</p><label className="bbs-input"><span>Как вас называть</span><input value={p.preferredName} maxLength={24} autoComplete="off" placeholder="Ваше имя" onChange={e => change("preferredName", e.target.value)}/><small>{p.preferredName.length}/24 · оставьте пустым, чтобы использовать имя аккаунта</small></label></section>
        <section className="bbs-group"><h2>Аккаунт и устройство</h2><p>Одна сессия для доступных вам сервисов. Выход не удаляет аккаунт и его данные.</p><div className="bbs-buttons">{props.onSetup && <button onClick={props.onSetup}>Повторить первый запуск</button>}{viewer && <button className="danger" disabled={busy} onClick={() => void action(props.onLogout, "")}>Выйти из аккаунта</button>}</div></section>
      </>}
      {section === "security" && <AccountSecurity/>}
      {section === "billing" && <AccountBilling/>}
      {section === "appearance" && <>
        <section className="bbs-group"><h2>Панель управления</h2><p>Компактная полоса сверху или вертикальная панель справа. Окна сервисов подстраиваются под выбранное расположение.</p>{options("controlBar", [["horizontal","Сверху"],["vertical","Справа"]], "Расположение панели управления")}</section>
        <section className="bbs-group"><h2>Вступительная сцена</h2><p>Оригинальный логотип, три характера появления. Продолжительность и переход к Луне остаются одинаковыми.</p><div className="bbs-ident-options" role="group" aria-label="Проявление логотипа">{([
          ["letters","Поэтапно","Надпись последовательно собирается из света."],
          ["veil","Из темноты","Цельный логотип мягко выходит из тёмной сцены."],
          ["light","Световой проход","Скользящий луч постепенно раскрывает надпись."],
        ] as const).map(([value,label,hint]) => <button key={value} aria-pressed={p.introStyle === value} onClick={() => change("introStyle",value)}><i className={`bbs-ident-sample ${value}`} aria-hidden="true"/><strong>{label}</strong><small>{hint}</small></button>)}</div>{props.onPreviewIntro && <button onClick={props.onPreviewIntro}>Посмотреть вступление ↗</button>}</section>
        <section className="bbs-group"><h2>Поведение интерфейса</h2>{toggle("compactMode", "Компактный режим", "Уменьшить отступы и разместить больше информации.")}{toggle("reduceMotion", "Спокойные анимации", "Свести движение и переходы к минимуму.")}{toggle("solidSurfaces", "Плотные поверхности", "Снизить прозрачность и повысить контраст.")}{toggle("sidebarCollapsed", "Скрывать боковую навигацию", "Оставить больше места для открытого сервиса.")}</section><section className="bbs-group"><h2>Масштаб сервисов</h2><p>Крупнее текст и элементы во всех открытых контурах.</p>{options("serviceZoom", [[0.9,"90%"],[1,"100%"],[1.1,"110%"]], "Масштаб сервисов")}</section></>}
      {section === "lock" && <><section className="bbs-group"><h2>Автоматическая пауза</h2><p>Блокировать оболочку после отсутствия активности. Этот экран не заменяет блокировку Windows.</p>{options("idleLockMinutes", [[0,"Выключено"],[5,"5 минут"],[10,"10 минут"],[15,"15 минут"],[30,"30 минут"]], "Время автоблокировки")}{toggle("lockSound", "Звуки блокировки", "Сигнал при блокировке и возвращении в приложение.")}</section><section className="bbs-group"><h2>Сделать паузу</h2><p>Чтобы продолжить, нажмите клавишу на клавиатуре.</p><button onClick={props.onLock}>Заблокировать сейчас</button></section></>}
      {section === "notifications" && <><section className="bbs-group"><h2>Доставка событий</h2><p>В адаптивном режиме карточки появляются внутри активного приложения, а когда оно свёрнуто — в Windows. Одно событие не дублируется.</p>{options("notificationDelivery", [["both","Адаптивно"],["in-app","Карточки Blackbird"],["system","Windows"],["off","Выключено"]], "Доставка уведомлений")}{toggle("notificationSound", "Звук новых событий", "Короткий сигнал при получении уведомления.")}</section><section className="bbs-group"><h2>Предпросмотр</h2><p>Проверить внешний вид собственной карточки уведомления.</p><button onClick={props.onPreviewNotification}>Показать тестовую карточку</button></section></>}
      {section === "atlas" && <div className="bbs-atlas">{props.atlas}</div>}
      {section === "updates" && <><section className="bbs-group"><h2>Канал обновлений</h2>{desktopProduct.privateEdition ? <p>Закрытые сборки Blackbird. Публичные Beta и Dev не устанавливаются поверх этого клиента.</p> : <>{options("updateChannel", [["beta","Beta"],["dev","Dev"]], "Канал обновлений")}<p>Beta — проверенные выпуски. Dev — экспериментальные возможности и более частые обновления.</p></>}</section><section className="bbs-group"><h2>Установленная версия · {props.updateState.currentVersion}</h2><p>{props.updateState.message || "Проверить наличие нового выпуска."}</p>{props.updateState.phase === "downloading" && <progress max={100} value={props.updateState.percent || 0} aria-label="Загрузка обновления"/>}<button disabled={["checking","downloading"].includes(props.updateState.phase)} onClick={props.onUpdate}>{props.updateState.phase === "ready" ? "Установить и перезапустить" : props.updateState.phase === "available" ? "Скачать обновление" : "Проверить обновления"}</button></section></>}
      {section === "connection" && <><section className="bbs-group"><div className={`bbs-connection ${props.online ? "online" : ""}`}><i/><h2>{props.online ? "Соединение установлено" : "Восстанавливаем соединение"}</h2></div><p>{props.lastSuccessfulAt ? `Последняя синхронизация: ${new Date(props.lastSuccessfulAt).toLocaleString("ru-RU")}` : "Ожидаем первую синхронизацию."}</p><button disabled={busy} onClick={() => void action(props.onReconnect, "Проверка завершена. Состояние соединения обновлено.")}>{busy ? "Проверяем…" : "Повторить подключение"}</button></section><section className="bbs-group"><h2>Сброс настроек</h2><p>Восстановить параметры этого устройства. Аккаунт, персонажи и данные сервисов останутся на месте.</p><button onClick={() => { if (!confirmReset) { setConfirmReset(true); return; } onChange({ ...props.defaults }); setConfirmReset(false); setMessage("Настройки восстановлены."); }}>{confirmReset ? "Подтвердить сброс" : "Вернуть настройки по умолчанию"}</button>{confirmReset && <button className="bbs-cancel" onClick={() => setConfirmReset(false)}>Отмена</button>}</section></>}
      {message && <p className="bbs-message" role="status">{message}</p>}
      </div>
    </div>
    <button className="bbs-close" onClick={props.onClose} aria-label="Закрыть настройки"><span>×</span><small>ESC</small></button>
  </section>;
}
