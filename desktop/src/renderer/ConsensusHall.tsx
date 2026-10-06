import { useEffect, useRef, useState } from "react";
import type { ConsensusLiveSnapshot } from "../shared/contracts";
import { consensusHallPhase } from "../shared/consensus-flow";
import senate from "./assets/blackbird/consensus.png";
import "./consensus-arrival.css";

const previewSnapshot = (sessionKey: string, plenaryNumber: number, stage: string): ConsensusLiveSnapshot => ({
  sessionKey, plenaryNumber, stage, stageLabel: stage === "registration" ? "Регистрация" : "Представление законопроекта",
  confirmed: true, ballotAvailable: true, confirmedCount: 8, invitedCount: 12,
  quorumReady: true, currentBillTitle: stage === "registration" ? null : "Проект о развитии Товарищества",
});

export function ConsensusHall({ sessionKey, plenaryNumber, serviceReady, preview = false, hold = false, onEnter, onLeave, onRetry }: {
  sessionKey: string; plenaryNumber: number; serviceReady: boolean; preview?: boolean; hold?: boolean;
  onEnter: () => void; onLeave: () => void; onRetry: () => void;
}) {
  const [snapshot, setSnapshot] = useState<ConsensusLiveSnapshot | null>(preview ? previewSnapshot(sessionKey, plenaryNumber, "registration") : null);
  const [connected, setConnected] = useState(preview);
  const [entering, setEntering] = useState(false);
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;

  useEffect(() => {
    if (preview) return;
    let live = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const next = await window.tmodDesktop?.consensusLiveState() ?? null;
        if (!live) return;
        setConnected(Boolean(next));
        if (next) setSnapshot(next);
      } catch { if (live) setConnected(false); }
      finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4_500);
    return () => { live = false; window.clearInterval(timer); };
  }, [preview, sessionKey]);

  const phase = consensusHallPhase(snapshot, sessionKey);
  useEffect(() => {
    if (phase === "ready" && serviceReady && !hold) setEntering(true);
  }, [phase, serviceReady, hold]);
  useEffect(() => {
    if (!entering) return;
    const timer = window.setTimeout(() => onEnterRef.current(), 1_800);
    return () => window.clearTimeout(timer);
  }, [entering]);

  const stageCopy = phase === "syncing" ? "Сверяем ваше место с Консенсусом"
    : phase === "finished" ? "Заседание завершено"
    : phase === "changed" ? "Началось другое заседание"
    : phase === "ready" ? "Ваше место открыто"
    : snapshot?.stage === "registration" ? "Зал собирается" : "Ожидаем доступ к бюллетеню";
  const description = phase === "ready" ? "Ведущий открыл рассмотрение. Переходим к вашему персональному месту и материалам проекта."
    : phase === "finished" || phase === "changed" ? "Вернитесь в Сенат, чтобы увидеть актуальное состояние Консенсуса."
    : "Вы подтвердили участие. Оставайтесь здесь: когда начнётся рассмотрение, Blackbird сам откроет ваше место.";
  const number = snapshot?.sessionKey === sessionKey && snapshot.plenaryNumber ? snapshot.plenaryNumber : plenaryNumber;
  const confirmed = snapshot?.sessionKey === sessionKey ? snapshot.confirmedCount : 0;
  const invited = snapshot?.sessionKey === sessionKey ? snapshot.invitedCount : 0;

  return <section className={`bb-consensus-hall ${entering ? "entering" : ""}`} role="dialog" aria-modal="true" aria-label="Зал ожидания Консенсуса">
    <div className="bb-ch-sky" aria-hidden="true"><i/><i/><i/><i/><i/></div>
    <svg className="bb-ch-architecture" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <defs><linearGradient id="bb-ch-arc" x1="0" y1="0" x2="1" y2="1"><stop stopColor="#9c8a68" stopOpacity=".06"/><stop offset=".5" stopColor="#d5c49d" stopOpacity=".42"/><stop offset="1" stopColor="#9c8a68" stopOpacity=".04"/></linearGradient></defs>
      <path className="bb-ch-roof" d="M-130 574 Q720 -320 1570 574"/>
      <path className="bb-ch-roof inner" d="M64 570 Q720 -120 1376 570"/>
      <path className="bb-ch-floor" d="M-160 824 Q720 288 1600 824"/>
      <path className="bb-ch-floor inner" d="M92 852 Q720 452 1348 852"/>
      <path className="bb-ch-floor core" d="M335 890 Q720 605 1105 890"/>
      <path className="bb-ch-axis" d="M720 78V900"/>
      {Array.from({ length: 21 }, (_, index) => {
        const x = 190 + index * 53;
        const y = 698 - 135 * Math.sin(index / 20 * Math.PI);
        return <circle className="bb-ch-seat" key={index} cx={x} cy={y} r={index % 5 === 0 ? 3.3 : 2.2} style={{ animationDelay: `${index * 90}ms` }}/>;
      })}
    </svg>
    <div className="bb-ch-threshold" aria-hidden="true"><span/><span/></div>
    <header className="bb-ch-top"><span><img src={senate} alt=""/> BLACKBIRD <i/> КОНСЕНСУС</span><strong>ПЛЕНАРНОЕ ЗАСЕДАНИЕ / {String(number).padStart(2, "0")}</strong></header>
    <main className="bb-ch-main">
      <div className="bb-ch-emblem" aria-hidden="true"><img src={senate} alt=""/><span/></div>
      <div className="bb-ch-kicker"><i/>{entering ? "ОТКРЫВАЕМ ВАШЕ МЕСТО" : "ЛИЧНЫЙ ДОПУСК ПОДТВЕРЖДЁН"}<i/></div>
      <h1>{entering ? "Время решения." : stageCopy}</h1>
      <p>{entering ? snapshot?.currentBillTitle || "Зал готов. Материалы заседания появляются на вашем месте." : description}</p>
      {!entering && <div className="bb-ch-status" aria-live="polite">
        <div><small>УЧАСТНИКИ</small><strong>{invited ? `${confirmed} / ${invited}` : "—"}</strong><span>{snapshot?.quorumReady ? "Кворум сформирован" : "Идёт подтверждение"}</span></div>
        <div><small>ЭТАП</small><strong>{snapshot?.sessionKey === sessionKey ? snapshot.stageLabel : "Ожидание"}</strong><span>{connected ? "Состояние обновляется автоматически" : "Восстанавливаем связь с залом"}</span></div>
      </div>}
    </main>
    <footer className="bb-ch-bottom"><button type="button" className="bb-ch-quiet" onClick={onLeave}>Вернуться в Сенат <span>↗</span></button><span className="bb-ch-live"><i/>{connected ? "ЗАЛ НА СВЯЗИ" : "ПЕРЕПОДКЛЮЧЕНИЕ"}</span>{preview && phase === "waiting" ? <button type="button" className="bb-ch-primary" onClick={() => setSnapshot(previewSnapshot(sessionKey, plenaryNumber, "presentation"))}>Показать начало заседания <span>→</span></button> : <button type="button" className="bb-ch-primary" disabled={phase !== "ready" || entering} onClick={() => serviceReady ? setEntering(true) : onRetry()}>{entering ? "Переходим…" : phase === "ready" ? serviceReady ? "Открыть своё место" : "Повторить соединение" : "Ожидаем заседание"}<span>→</span></button>}</footer>
  </section>;
}
