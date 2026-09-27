import { useEffect, useRef, useState } from "react";
import { AccountAvatar } from "./AccountAvatar";
import type { DesktopBootstrap } from "../shared/contracts";

export function AccountMenu({ viewer, name, open, onOpenChange, onSettings, onLock, onLogout }: {
  viewer: DesktopBootstrap["viewer"]; name: string; open: boolean;
  onOpenChange: (value: boolean) => void; onSettings: () => void; onLock: () => void;
  onLogout: () => Promise<void>;
}) {
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    setError("");
    root.current?.querySelector<HTMLButtonElement>(".bb-profile-menu > button")?.focus();
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) onOpenChange(false); };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") { onOpenChange(false); trigger.current?.focus(); }
    };
    window.addEventListener("pointerdown", outside, true);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("pointerdown", outside, true); window.removeEventListener("keydown", key); };
  }, [open, onOpenChange]);
  return <div className="bb-profile-anchor" ref={root} onBlur={event => {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) onOpenChange(false);
  }}>
    <button ref={trigger} className="bb-profile-trigger" aria-label={`Профиль: ${name}`} aria-expanded={open} aria-controls="bb-account-menu" onClick={() => onOpenChange(!open)}><AccountAvatar name={name} url={viewer.avatar_url}/></button>
    {open && <section id="bb-account-menu" className="bb-profile-menu" aria-label="Меню аккаунта">
      <header><strong>{name}</strong><small>{viewer.display_name}</small><small>{viewer.administrator ? "Администратор" : viewer.guild_member ? "Товарищество" : "Единый аккаунт"}</small></header>
      <button onClick={() => { onOpenChange(false); onSettings(); }}>Мой аккаунт и настройки</button>
      <button onClick={() => { onOpenChange(false); onLock(); }}>Заблокировать приложение</button>
      <button className="danger" disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try { await onLogout(); onOpenChange(false); }
        catch { setError("Не удалось выйти. Попробуйте ещё раз."); }
        finally { setBusy(false); }
      }}>{busy ? "Выходим…" : "Выйти из аккаунта"}</button>
      {error && <p role="alert">{error}</p>}
    </section>}
  </div>;
}
