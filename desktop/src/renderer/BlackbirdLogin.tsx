import type { FormEvent } from "react";
import masterMark from "./assets/blackbird/master.png";
import { BlackbirdWordmark } from "./BlackbirdWordmark";

interface BlackbirdLoginProps {
  login: string;
  pin: string;
  code?: string;
  factor?: string;
  onCodeChange?: (value: string) => void;
  busy: boolean;
  online: boolean;
  bridgeAvailable: boolean;
  error?: string;
  onLoginChange: (value: string) => void;
  onPinChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onRetry: () => void;
}

export function BlackbirdLogin({ login, pin, code, factor, onCodeChange, busy, online, bridgeAvailable, error, onLoginChange, onPinChange, onSubmit, onRetry }: BlackbirdLoginProps) {
  return (
    <section className="blackbird-login">
      <div className="bbl-lines" aria-hidden="true"><i/><i/><i/><i/></div>
      <header className="bbl-head"><span><BlackbirdWordmark/><i/>CLIENT</span><small>DEVELOPER MODE</small></header>
      <div className="bbl-symbol" aria-hidden="true"><img src={masterMark} alt=""/><i/></div>
      <main className="bbl-panel">
        <div className="bbl-copy">
          <small>ЛИЧНЫЙ ДОСТУП</small>
          <h1>Подтвердите<br/>личность.</h1>
          <p>Единый защищённый допуск к Atlas и системам Сената.</p>
        </div>
        <form onSubmit={onSubmit}>
          <label><span>Логин</span><input value={login} onChange={(event) => onLoginChange(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} minLength={3} maxLength={32} placeholder="Имя аккаунта" disabled={busy}/><i/></label>
          <label><span>PIN или пароль</span><input value={pin} onChange={(event) => onPinChange(event.target.value.slice(0,128))} autoComplete="current-password" type="password" minLength={1} maxLength={128} placeholder="Ваш способ входа" disabled={busy}/><i/></label>
          {factor && <label><span>{factor === "totp" ? "Код аутентификатора" : `Код из ${factor === "telegram" ? "Telegram" : "Discord"}`} · или резервный код</span><input autoFocus name="verification_code" autoComplete="one-time-code" type="password" maxLength={32} value={code || ""} onChange={event => onCodeChange?.(event.target.value)} disabled={busy}/><i/></label>}
          {error && <output>{error}</output>}
          {!bridgeAvailable && <output>Компонент авторизации не загружен. Откройте Blackbird Client.</output>}
          <button type="submit" disabled={busy || !bridgeAvailable} aria-busy={busy}><span>{busy ? "Подключаем аккаунт…" : "Войти в Blackbird"}</span><b>{busy ? "···" : "→"}</b></button>
          <footer className={online ? "online" : ""} aria-live="polite"><i/><span>{busy ? "Устанавливаем защищённую сессию…" : online ? "Соединение установлено" : "Восстанавливаем соединение"}</span>{!online && !busy && <button type="button" onClick={onRetry}>Повторить</button>}</footer>
        </form>
      </main>
      <footer className="bbl-foot"><span>Технологии Товарищества</span></footer>
    </section>
  );
}
