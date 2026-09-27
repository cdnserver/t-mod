import { useEffect, useState } from "react";
import { atlasUsageMeter } from "../shared/atlas-usage";

export function AtlasUsage({ authenticated, onOpen }: { authenticated: boolean; onOpen: () => void }) {
  const [data, setData] = useState<Record<string, unknown>>();
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
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
  }, [authenticated]);
  if (!authenticated) return null;
  const meter = data ? atlasUsageMeter(data) : undefined;
  const description = meter ? `${meter.prepaid ? "Баланс пополнений" : "Токены текущего периода"}: ${meter.remaining.toLocaleString("ru-RU")} из ${meter.capacity.toLocaleString("ru-RU")} AT · осталось ${meter.percent}%${unavailable ? " · обновление временно недоступно" : ""}` : unavailable ? "Баланс временно недоступен" : "Загружаем баланс Atlas";
  return <button className={`bb-atlas-usage ${unavailable ? "stale" : ""}`} title={description} aria-label={`Atlas · ${description}`} onClick={onOpen}>
    <svg viewBox="0 0 32 32" aria-hidden="true"><circle className="bb-usage-track" cx="16" cy="16" r="13"/><circle className="bb-usage-ring" cx="16" cy="16" r="13" pathLength="100" strokeDasharray={`${meter?.percent ?? 0} 100`}/><circle cx="16" cy="16" r="6"/><path d="M10 16h12M16 10c-4 3-4 9 0 12 4-3 4-9 0-12M23 5v6m-3-3h6"/></svg>
    <span>{meter ? `${meter.percent}%` : "—"}</span>
  </button>;
}
