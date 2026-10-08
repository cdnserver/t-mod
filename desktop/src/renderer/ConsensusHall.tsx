import { useEffect, useRef, useState } from "react";
import type { ConsensusLiveSnapshot } from "../shared/contracts";
import { consensusHallMayEnter, consensusHallPhase } from "../shared/consensus-flow";
import senate from "./assets/blackbird/consensus.png";
import "./consensus-arrival.css";

const previewSnapshot = (sessionKey: string, plenaryNumber: number, stage: string): ConsensusLiveSnapshot => ({
  sessionKey, plenaryNumber, stage, stageLabel: stage === "registration" ? "Регистрация" : "Представление законопроекта",
  confirmed: true, ballotAvailable: true, confirmedCount: 8, invitedCount: 12,
  quorumReady: true, currentBillTitle: stage === "registration" ? null : "Проект о развитии Товарищества",
});

export function ConsensusHall({ sessionKey, plenaryNumber, serviceReady, preview = false, hold = false, onEnter, onLeave, onRetry }: {
  sessionKey: string; plenaryNumber: number; serviceReady: boolean; preview?: boolean; hold?: boolean;
  onEnter: () => void; onLeave: () => void | Promise<void>; onRetry: () => void;
}) {
  const [snapshot, setSnapshot] = useState<ConsensusLiveSnapshot | null>(preview ? previewSnapshot(sessionKey, plenaryNumber, "registration") : null);
  const [connected, setConnected] = useState(preview);
  const [entering, setEntering] = useState(false);
  const [failedAdmissionKey, setFailedAdmissionKey] = useState("");
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState(false);
  const leaveInFlight = useRef(false);
  const leave = async () => {
    if (leaveInFlight.current) return;
    leaveInFlight.current = true;
    setLeaving(true); setLeaveError(false); setEntering(false);
    try { await onLeave(); }
    catch { setLeaveError(true); }
    finally { leaveInFlight.current = false; setLeaving(false); }
  };
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;

  useEffect(() => {
    if (preview) return;
    let live = true;
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || !live) return;
      inFlight = true;
      setRefreshing(true);
      try {
        const next = await window.tmodDesktop?.consensusLiveState() ?? null;
        if (!live) return;
        setConnected(Boolean(next));
        if (next) setSnapshot(next);
      } catch { if (live) setConnected(false); }
      finally { inFlight = false; if (live) setRefreshing(false); }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 4_500);
    const reconnect = () => void refresh();
    window.addEventListener("focus", reconnect);
    window.addEventListener("online", reconnect);
    return () => { live = false; window.clearInterval(timer); window.removeEventListener("focus", reconnect); window.removeEventListener("online", reconnect); };
  }, [preview, sessionKey, refreshGeneration]);

  const phase = consensusHallPhase(snapshot, sessionKey, connected);
  const mayEnter = consensusHallMayEnter(snapshot, sessionKey, connected, serviceReady) && !hold && !leaving;
  const admissionKey = `${sessionKey}:${snapshot?.stage || ""}:${snapshot?.currentBillTitle || ""}`;
  useEffect(() => {
    if (!mayEnter) setEntering(false);
    else if (failedAdmissionKey !== admissionKey) setEntering(true);
  }, [mayEnter, failedAdmissionKey, admissionKey]);
  useEffect(() => {
    if (!entering || !mayEnter) return;
    let live = true;
    const timer = window.setTimeout(() => {
      if (preview) { onEnterRef.current(); return; }
      // The session can close or the account can change during the entrance
      // animation. A fresh server projection is the final admission gate.
      void Promise.resolve(window.tmodDesktop?.consensusLiveState() ?? null).then(next => {
        if (!live) return;
        setConnected(Boolean(next));
        if (next) setSnapshot(next);
        if (consensusHallMayEnter(next, sessionKey, Boolean(next), serviceReady)) onEnterRef.current();
        else { setFailedAdmissionKey(admissionKey); setEntering(false); }
      }).catch(() => { if (live) { setFailedAdmissionKey(admissionKey); setConnected(false); setEntering(false); } });
    }, 1_800);
    return () => { live = false; window.clearTimeout(timer); };
  }, [admissionKey, entering, mayEnter, preview, serviceReady, sessionKey]);

  const stageCopy = phase === "syncing" ? snapshot ? "Восстанавливаем связь с залом" : "Сверяем ваше место с Консенсусом"
    : phase === "finished" ? "Заседание завершено"
    : phase === "changed" ? "Началось другое заседание"
    : phase === "ready" ? "Ваше место открыто"
    : snapshot?.stage === "registration" ? "Зал собирается" : "Ожидаем доступ к бюллетеню";
  const description = phase === "syncing" ? "Проверяем текущее заседание и ваш допуск. Переход начнётся только после подтверждения связи с Консенсусом."
    : phase === "ready" ? "Ведущий открыл рассмотрение. Переходим к вашему персональному месту и материалам проекта."
    : phase === "finished" || phase === "changed" ? "Вернитесь в Сенат, чтобы увидеть актуальное состояние Консенсуса."
    : !snapshot?.confirmed ? "Ваше участие не подтверждено. Вернитесь в Сенат, чтобы проверить регистрацию."
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
      <div className="bb-ch-kicker"><i/>{entering ? "ОТКРЫВАЕМ ВАШЕ МЕСТО" : phase === "syncing" ? "ПРОВЕРЯЕМ СОЕДИНЕНИЕ" : snapshot?.confirmed ? "ЛИЧНЫЙ ДОПУСК ПОДТВЕРЖДЁН" : "ЗАЛ КОНСЕНСУСА"}<i/></div>
      <h1>{entering ? "Время решения." : stageCopy}</h1>
      <p>{entering ? snapshot?.currentBillTitle || "Зал готов. Материалы заседания появляются на вашем месте." : description}</p>
      {leaveError && <p role="alert">Не удалось вернуться в Сенат. Ваше участие сохранено — попробуйте ещё раз.</p>}
      {!entering && <div className="bb-ch-status" aria-live="polite">
        <div><small>УЧАСТНИКИ</small><strong>{invited ? `${confirmed} / ${invited}` : "—"}</strong><span>{snapshot?.quorumReady ? "Кворум сформирован" : "Идёт подтверждение"}</span></div>
        <div><small>ЭТАП</small><strong>{!connected ? "Синхронизация" : snapshot?.sessionKey === sessionKey ? snapshot.stageLabel : "Ожидание"}</strong><span>{connected ? "Состояние обновляется автоматически" : "Восстанавливаем связь с залом"}</span></div>
      </div>}
    </main>
    <footer className="bb-ch-bottom">
      <button type="button" className="bb-ch-quiet" disabled={leaving} onClick={() => void leave()}>{leaving ? "Возвращаемся…" : "Вернуться в Сенат"} <span>↗</span></button>
      <span className="bb-ch-live"><i/>{connected ? "ЗАЛ НА СВЯЗИ" : "ПЕРЕПОДКЛЮЧЕНИЕ"}</span>
      {preview && phase === "waiting" ? <button type="button" className="bb-ch-primary" onClick={() => setSnapshot(previewSnapshot(sessionKey, plenaryNumber, "presentation"))}>Показать начало заседания <span>→</span></button> :
        <button type="button" className="bb-ch-primary" disabled={leaving || entering || (!connected ? refreshing : phase !== "ready")} onClick={() => {
          if (!connected) { setFailedAdmissionKey(""); setRefreshGeneration(value => value + 1); }
          else if (serviceReady) setEntering(true);
          else onRetry();
        }}>{entering ? "Переходим…" : !connected ? refreshing ? "Подключаемся…" : "Повторить соединение" : phase === "ready" ? serviceReady ? "Открыть своё место" : "Повторить соединение" : "Ожидаем заседание"}<span>→</span></button>}
    </footer>
  </section>;
}
