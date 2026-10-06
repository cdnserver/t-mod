import { useEffect, useRef, useState } from "react";
import type { ConsensusRegistrationNotice } from "../shared/contracts";
import senate from "./assets/blackbird/consensus.png";
import "./consensus-call.css";

export function ConsensusCall({ notice, onConfirm, onLater }: { notice: ConsensusRegistrationNotice; onConfirm: () => Promise<boolean>; onLater: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    confirmRef.current?.focus({ preventScroll: true });
    const key = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onLater(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [busy, onLater]);
  const confirm = async () => {
    if (busy) return;
    setBusy(true); setError(false);
    try { if (!(await onConfirm())) setError(true); }
    catch { setError(true); }
    finally { setBusy(false); }
  };
  return <div className="bb-consensus-call-backdrop"><section className="bb-consensus-call" role="dialog" aria-modal="true" aria-label="Регистрация на Консенсус">
    <div className="bb-cc-lines" aria-hidden="true"><i/><i/><i/></div>
    <header><span><img src={senate} alt=""/> BLACKBIRD <i/> СЕНАТ</span><button type="button" onClick={onLater} aria-label="Закрыть приглашение">×</button></header>
    <div className="bb-cc-number" aria-hidden="true">{String(notice.plenaryNumber || 0).padStart(2, "0")}</div>
    <div className="bb-cc-body"><small><i/> РЕГИСТРАЦИЯ ОТКРЫТА</small><h1>Ваше место<br/>на Консенсусе.</h1>
    <p>Подтвердите участие в пленарном заседании № {notice.plenaryNumber || "—"}. После подтверждения Blackbird откроет зал ожидания и сам проведёт вас к голосованию.</p></div>
    {error && <p className="bb-consensus-error" role="alert">Не удалось подтвердить участие. Проверьте связь и попробуйте снова.</p>}
    <footer><button type="button" className="secondary" disabled={busy} onClick={onLater}>Позже</button><button ref={confirmRef} type="button" className="primary" disabled={busy} onClick={() => void confirm()}>{busy ? "Подтверждаем…" : "Подтвердить участие"}<span>→</span></button></footer>
  </section></div>;
}
