import { useEffect, useRef, useState } from "react";
import { atlasUsageMeter } from "../shared/atlas-usage";

export function AtlasUsage({ authenticated, onOpen, previewOpen = false }: { authenticated: boolean; onOpen: () => void; previewOpen?: boolean }) {
  const [data, setData] = useState<Record<string, unknown> | undefined>(previewOpen ? { monthly_balance_tokens: 8420, monthly_capacity_tokens: 10000 } : undefined);
  const [unavailable, setUnavailable] = useState(false);
  const [open, setOpen] = useState(previewOpen);
  const anchor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!anchor.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    window.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); window.removeEventListener("keydown", escape); };
  }, [open]);
  useEffect(() => {
    if (previewOpen) return;
    setData(undefined);
    if (!authenticated) return;
    let live = true, pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await window.tmodDesktop?.accountRequest?.("billing");
        if (live && result) { setData(result); setUnavailable(false); }
      } catch { if (live) setUnavailable(true); }
      finally { pending = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60000);
    window.addEventListener("focus", refresh);
    return () => { live = false; window.clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [authenticated, previewOpen]);
  if (!authenticated) return null;
  const meter = data ? atlasUsageMeter(data) : undefined;
  const description = meter ? `${meter.prepaid ? "Баланс пополнений" : "Токены текущего периода"}: ${meter.remaining.toLocaleString("ru-RU")} из ${meter.capacity.toLocaleString("ru-RU")} AT · осталось ${meter.percent}%${unavailable ? " · обновление временно недоступно" : ""}` : unavailable ? "Баланс временно недоступен" : "Загружаем баланс Atlas";
  return <div className="bb-atlas-usage-anchor" ref={anchor}><button className={`bb-atlas-usage ${unavailable ? "stale" : ""}`} title={description} aria-label={`Atlas · ${description}`} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(current => !current)}>
    <svg viewBox="0 0 32 32" aria-hidden="true"><circle className="bb-usage-track" cx="16" cy="16" r="13"/><circle className="bb-usage-ring" cx="16" cy="16" r="13" pathLength="100" strokeDasharray={`${meter?.percent ?? 0} 100`}/><circle cx="16" cy="16" r="6"/><path d="M10 16h12M16 10c-4 3-4 9 0 12 4-3 4-9 0-12M23 5v6m-3-3h6"/></svg>
    <span>{meter ? `${meter.percent}%` : "—"}</span>
  </button>{open && <section className="bb-atlas-usage-popover" role="dialog" aria-label="Баланс Atlas">
    <header><span>ATLAS / ВАШ РЕСУРС</span><button aria-label="Закрыть" onClick={() => setOpen(false)}>×</button></header>
    <strong>{meter ? meter.remaining.toLocaleString("ru-RU") : "—"}<small> AT</small></strong>
    <p>{meter ? meter.prepaid ? "Доступно из пополнений" : "Доступно в текущем периоде" : unavailable ? "Баланс временно недоступен" : "Получаем состояние счёта…"}</p>
    <div className="bb-atlas-usage-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={meter?.percent ?? 0}><i style={{width:`${meter?.percent ?? 0}%`}}/></div>
    <footer><span>{meter ? `${meter.percent}% осталось` : "Обновляется"}</span><button onClick={() => { setOpen(false); onOpen(); }}>Счёт и история ↗</button></footer>
  </section>}</div>;
}
