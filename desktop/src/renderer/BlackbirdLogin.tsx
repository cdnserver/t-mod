import type { FormEvent } from "react";
import masterMark from "./assets/blackbird/master.png";
import { BlackbirdWordmark } from "./BlackbirdWordmark";

interface BlackbirdLoginProps {
  login: string;
  pin: string;
  busy: boolean;
  online: boolean;
  bridgeAvailable: boolean;
  error?: string;
  onLoginChange: (value: string) => void;
  onPinChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onRetry: () => void;
}

export function BlackbirdLogin({ login, pin, busy, online, bridgeAvailable, error, onLoginChange, onPinChange, onSubmit, onRetry }: BlackbirdLoginProps) {
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
          <label><span>PIN-код</span><input value={pin} onChange={(event) => onPinChange(event.target.value.replace(/\D/g, "").slice(0,8))} autoComplete="current-password" inputMode="numeric" type="password" minLength={8} maxLength={8} placeholder="8 цифр" disabled={busy}/><i/></label>
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
