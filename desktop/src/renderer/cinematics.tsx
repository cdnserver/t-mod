import { useEffect, useState } from "react";
import type { DesktopLockReason } from "../shared/contracts";

type StopSound = () => void;

function audioContext(): AudioContext | null {
  const Context = globalThis.AudioContext;
  return Context ? new Context() : null;
}

function impulse(context: AudioContext, seconds: number, decay: number): AudioBuffer {
  const length = Math.floor(context.sampleRate * seconds);
  const buffer = context.createBuffer(2, length, context.sampleRate);
  for (let channel = 0; channel < 2; channel += 1) {
    const data = buffer.getChannelData(channel);
    for (let index = 0; index < length; index += 1) {
      const envelope = Math.pow(1 - index / length, decay);
      data[index] = (Math.random() * 2 - 1) * envelope;
    }
  }
  return buffer;
}

function cinematicBus(context: AudioContext, peak: number) {
  const master = context.createGain();
  const dry = context.createGain();
  const wet = context.createGain();
  const convolver = context.createConvolver();
  const compressor = context.createDynamicsCompressor();
  master.gain.value = peak;
  dry.gain.value = .72;
  wet.gain.value = .34;
  convolver.buffer = impulse(context, 3.8, 2.4);
  compressor.threshold.value = -20;
  compressor.knee.value = 20;
  compressor.ratio.value = 4.5;
  compressor.attack.value = .012;
  compressor.release.value = .42;
  master.connect(dry).connect(compressor);
  master.connect(convolver).connect(wet).connect(compressor);
  compressor.connect(context.destination);
  return master;
}

function scheduleTone(
  context: AudioContext,
  destination: AudioNode,
  frequency: number,
  offset: number,
  duration: number,
  level: number,
  type: OscillatorType = "sine",
  pan = 0,
  detune = 0,
) {
  const starts = context.currentTime + offset;
  const oscillator = context.createOscillator();
  const envelope = context.createGain();
  const stereo = context.createStereoPanner();
  oscillator.type = type;
  oscillator.frequency.setValueAtTime(frequency, starts);
  oscillator.detune.setValueAtTime(detune, starts);
  stereo.pan.setValueAtTime(pan, starts);
  envelope.gain.setValueAtTime(.0001, starts);
  envelope.gain.exponentialRampToValueAtTime(level, starts + Math.min(.45, duration * .22));
  envelope.gain.exponentialRampToValueAtTime(.0001, starts + duration);
  oscillator.connect(envelope).connect(stereo).connect(destination);
  oscillator.start(starts);
  oscillator.stop(starts + duration + .06);
}

function scheduleSweep(
  context: AudioContext,
  destination: AudioNode,
  offset: number,
  duration: number,
  from: number,
  to: number,
  level: number,
) {
  const starts = context.currentTime + offset;
  const length = Math.floor(context.sampleRate * duration);
  const buffer = context.createBuffer(2, length, context.sampleRate);
  for (let channel = 0; channel < 2; channel += 1) {
    const data = buffer.getChannelData(channel);
    for (let index = 0; index < length; index += 1) {
      const position = index / length;
      data[index] = (Math.random() * 2 - 1) * Math.sin(Math.PI * position);
    }
  }
  const source = context.createBufferSource();
  const filter = context.createBiquadFilter();
  const gain = context.createGain();
  const pan = context.createStereoPanner();
  source.buffer = buffer;
  filter.type = "bandpass";
  filter.Q.value = .68;
  filter.frequency.setValueAtTime(from, starts);
  filter.frequency.exponentialRampToValueAtTime(to, starts + duration);
  gain.gain.setValueAtTime(.0001, starts);
  gain.gain.exponentialRampToValueAtTime(level, starts + duration * .34);
  gain.gain.exponentialRampToValueAtTime(.0001, starts + duration);
  pan.pan.setValueAtTime(-.72, starts);
  pan.pan.linearRampToValueAtTime(.72, starts + duration);
  source.connect(filter).connect(gain).connect(pan).connect(destination);
  source.start(starts);
  source.stop(starts + duration);
}

function closeLater(context: AudioContext, milliseconds: number): StopSound {
  void context.resume().catch(() => undefined);
  const timer = window.setTimeout(() => {
    if (context.state !== "closed") void context.close();
  }, milliseconds);
  return () => {
    window.clearTimeout(timer);
    if (context.state !== "closed") void context.close();
  };
}

export function playIgnitionSound(): StopSound {
  const context = audioContext();
  if (!context) return () => undefined;
  const bus = cinematicBus(context, .17);
  const now = context.currentTime;
  const masterShape = context.createGain();
  masterShape.gain.setValueAtTime(.0001, now);
  masterShape.gain.exponentialRampToValueAtTime(1, now + .52);
  masterShape.gain.setValueAtTime(1, now + 6.4);
  masterShape.gain.exponentialRampToValueAtTime(.0001, now + 7.72);
  masterShape.connect(bus);

  // Foundation: a wide D-major suspension that resolves only when the shell appears.
  scheduleTone(context, masterShape, 36.71, 0, 7.35, .42, "sine");
  scheduleTone(context, masterShape, 73.42, .12, 6.7, .2, "sine", -.2);
  scheduleTone(context, masterShape, 110, .44, 5.9, .1, "triangle", .22, -5);
  scheduleTone(context, masterShape, 146.83, 1.15, 5.3, .075, "sine", -.48);
  scheduleTone(context, masterShape, 185, 1.86, 4.65, .064, "sine", .45);
  scheduleTone(context, masterShape, 220, 2.42, 4.1, .055, "sine", -.18);

  // Glass harmonics and a small final identity chime.
  [293.66, 440, 587.33].forEach((frequency, index) => {
    scheduleTone(context, masterShape, frequency, 2.25 + index * .24, 3.45, .038 / (index + 1), "sine", index - 1, index * 3);
  });
  scheduleTone(context, masterShape, 587.33, 5.22, 1.82, .07, "sine", -.35);
  scheduleTone(context, masterShape, 739.99, 5.34, 1.72, .055, "sine", .35);
  scheduleTone(context, masterShape, 880, 5.5, 1.52, .036, "sine");

  scheduleSweep(context, masterShape, .18, 2.9, 105, 2_800, .12);
  scheduleSweep(context, masterShape, 4.78, 1.75, 240, 5_400, .095);

  const impact = context.createOscillator();
  const impactGain = context.createGain();
  impact.type = "sine";
  impact.frequency.setValueAtTime(78, now + 1.02);
  impact.frequency.exponentialRampToValueAtTime(34, now + 2.28);
  impactGain.gain.setValueAtTime(.0001, now + 1.02);
  impactGain.gain.exponentialRampToValueAtTime(.34, now + 1.08);
  impactGain.gain.exponentialRampToValueAtTime(.0001, now + 2.36);
  impact.connect(impactGain).connect(masterShape);
  impact.start(now + 1.02);
  impact.stop(now + 2.42);
  return closeLater(context, 8_200);
}

export function playVaultSound(kind: "lock" | "unlock", enabled: boolean): StopSound {
  const context = enabled ? audioContext() : null;
  if (!context) return () => undefined;
  const bus = cinematicBus(context, kind === "lock" ? .12 : .14);
  const now = context.currentTime;
  const shape = context.createGain();
  shape.gain.setValueAtTime(.0001, now);
  shape.gain.exponentialRampToValueAtTime(1, now + .07);
  shape.gain.exponentialRampToValueAtTime(.0001, now + (kind === "lock" ? 2.5 : 2.12));
  shape.connect(bus);

  const notes = kind === "lock"
    ? [220, 146.83, 73.42, 36.71]
    : [146.83, 220, 293.66, 440];
  notes.forEach((frequency, index) => {
    scheduleTone(context, shape, frequency, index * (kind === "lock" ? .15 : .11), 1.62, .16 / Math.sqrt(index + 1), index < 2 ? "sine" : "triangle", (index - 1.5) * .28);
  });
  scheduleSweep(context, shape, .05, kind === "lock" ? 1.55 : 1.18, kind === "lock" ? 2_400 : 180, kind === "lock" ? 130 : 4_600, .055);
  return closeLater(context, 2_850);
}

export function CinematicLaunch({ reduced }: { reduced: boolean }) {
  return (
    <section className={`ignition-stage ${reduced ? "reduced" : ""}`} aria-label="T-Mod запускается" aria-live="polite">
      <div className="ignition-void" aria-hidden="true"><i/><i/><i/><i/></div>
      <div className="ignition-veil" aria-hidden="true"><i/><b/></div>
      <div className="ignition-frame" aria-hidden="true"><i/><i/><i/><i/></div>
      <div className="ignition-signal" aria-hidden="true"><i/><b/><em/></div>
      <div className="ignition-monolith" aria-hidden="true">
        <div className="ignition-glass"><i/><b/><em/></div>
        <div className="ignition-mark"><i/><b/><span/></div>
        <div className="ignition-reflection"/>
      </div>
      <div className="ignition-wordmark">
        <span>TM / SYSTEM</span>
        <h1>T‑MOD</h1>
        <p>Ваша экосистема готова.</p>
        <small><i/> SECURE CORE · DEV CHANNEL</small>
      </div>
      <div className="ignition-release" aria-hidden="true"><i/><b/></div>
    </section>
  );
}

export function VaultScreen({
  name,
  reason,
  reduced,
  unlocking,
}: {
  name: string;
  reason: DesktopLockReason;
  reduced: boolean;
  unlocking: boolean;
}) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const time = new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(now);
  const seconds = new Intl.DateTimeFormat("ru", { second: "2-digit" }).format(now);
  const date = new Intl.DateTimeFormat("ru", { weekday: "long", day: "numeric", month: "long" }).format(now);
  return (
    <section className={`vault-stage ${reduced ? "reduced" : ""} ${unlocking ? "unlocking" : ""}`} role="dialog" aria-modal="true" aria-label="T-Mod заблокирован">
      <div className="vault-sky" aria-hidden="true"><i/><i/><b/></div>
      <div className="vault-landscape" aria-hidden="true"><i/><b/><em/></div>
      <div className="vault-film" aria-hidden="true"/>
      <header className="vault-header">
        <div className="vault-brand"><i/><span><strong>T‑MOD</strong><small>DESKTOP SYSTEM</small></span></div>
        <span className="vault-state"><i/> {reason === "idle" ? "СЕАНС ПРИОСТАНОВЛЕН" : "ЗАБЛОКИРОВАНО ВРУЧНУЮ"}</span>
      </header>
      <main className="vault-content">
        <div className="vault-clock" aria-label={`Сейчас ${time}`}><strong>{time}</strong><sup>{seconds}</sup><span>{date}</span></div>
        <div className="vault-divider" aria-hidden="true"><i/></div>
        <div className="vault-welcome">
          <small>{unlocking ? "ИДЕНТИЧНОСТЬ ПОДТВЕРЖДЕНА" : "ЗАЩИЩЁННЫЙ СЕАНС"}</small>
          <h1>{unlocking ? "С возвращением." : name}</h1>
          <p>{unlocking ? "Рабочее пространство уже открывается." : "Нажмите любую клавишу, чтобы продолжить работу."}</p>
          <div className="vault-key"><kbd>{unlocking ? "✓" : "ANY KEY"}</kbd><span>{unlocking ? "ДОСТУП ВОССТАНОВЛЕН" : "ТОЛЬКО КЛАВИАТУРА"}</span></div>
        </div>
      </main>
      <footer className="vault-footer"><span><i/> Локальный контур защищён</span><small>T‑Mod · {time}</small></footer>
      <div className="vault-release" aria-hidden="true"><i/><b/></div>
    </section>
  );
}
