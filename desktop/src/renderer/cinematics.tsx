import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import type { DesktopLockReason } from "../shared/contracts";

type StopSound = () => void;

const STAR_SEEDS = [
  [7, 16, 1, .2], [13, 64, 2, 1.4], [18, 31, 1, 2.1], [24, 79, 1, .7],
  [30, 11, 2, 1.8], [35, 53, 1, .4], [41, 24, 1, 2.8], [46, 70, 2, 1.1],
  [52, 12, 1, .9], [57, 43, 1, 2.3], [62, 82, 1, 1.5], [68, 27, 2, .1],
  [73, 61, 1, 2.6], [79, 9, 1, 1.2], [84, 46, 2, 2], [91, 73, 1, .6],
  [95, 22, 1, 1.7], [4, 88, 1, 2.4], [21, 46, 1, 3], [39, 91, 1, 1.3],
  [66, 7, 1, 2.7], [76, 88, 1, .8], [88, 35, 1, 2.2], [97, 55, 1, 1],
] as const;

const LOCK_PHRASES = [
  "Поработаем?",
  "Что сегодня на уме?",
  "Продолжим с того места?",
  "Куда направимся дальше?",
  "Всё готово к работе.",
  "Начнём с главного?",
] as const;

const CLOCK_GLYPHS: Record<string, readonly string[]> = {
  "0": ["11111", "10001", "10001", "10001", "10001", "10001", "11111"],
  "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
  "2": ["11111", "00001", "00001", "11111", "10000", "10000", "11111"],
  "3": ["11111", "00001", "00001", "01111", "00001", "00001", "11111"],
  "4": ["10001", "10001", "10001", "11111", "00001", "00001", "00001"],
  "5": ["11111", "10000", "10000", "11111", "00001", "00001", "11111"],
  "6": ["11111", "10000", "10000", "11111", "10001", "10001", "11111"],
  "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
  "8": ["11111", "10001", "10001", "11111", "10001", "10001", "11111"],
  "9": ["11111", "10001", "10001", "11111", "00001", "00001", "11111"],
  ":": ["0", "1", "1", "0", "1", "1", "0"],
};

function greetingFor(name: string, now = new Date()): string {
  const hour = now.getHours();
  const greeting = hour < 5
    ? "Доброй ночи"
    : hour < 12
      ? "Доброе утро"
      : hour < 18
        ? "Добрый день"
        : "Добрый вечер";
  const cleanName = name.trim();
  return cleanName && cleanName !== "T-Mod" ? `${greeting}, ${cleanName}` : greeting;
}

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
      data[index] = (Math.random() * 2 - 1) * Math.pow(1 - index / length, decay);
    }
  }
  return buffer;
}

function cinematicBus(context: AudioContext, peak: number) {
  const input = context.createGain();
  const dry = context.createGain();
  const wet = context.createGain();
  const reverb = context.createConvolver();
  const compressor = context.createDynamicsCompressor();
  input.gain.value = peak;
  dry.gain.value = .8;
  wet.gain.value = .38;
  reverb.buffer = impulse(context, 4.6, 2.65);
  compressor.threshold.value = -18;
  compressor.knee.value = 18;
  compressor.ratio.value = 4;
  compressor.attack.value = .014;
  compressor.release.value = .48;
  input.connect(dry).connect(compressor);
  input.connect(reverb).connect(wet).connect(compressor);
  compressor.connect(context.destination);
  return input;
}

function tone(
  context: AudioContext,
  destination: AudioNode,
  frequency: number,
  offset: number,
  duration: number,
  level: number,
  type: OscillatorType = "sine",
  pan = 0,
) {
  const starts = context.currentTime + offset;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  const stereo = context.createStereoPanner();
  oscillator.type = type;
  oscillator.frequency.setValueAtTime(frequency, starts);
  stereo.pan.value = pan;
  gain.gain.setValueAtTime(.0001, starts);
  gain.gain.exponentialRampToValueAtTime(level, starts + Math.min(.32, duration * .18));
  gain.gain.exponentialRampToValueAtTime(.0001, starts + duration);
  oscillator.connect(gain).connect(stereo).connect(destination);
  oscillator.start(starts);
  oscillator.stop(starts + duration + .08);
}

function warmBloom(
  context: AudioContext,
  destination: AudioNode,
  offset: number,
  duration = 2.4,
  level = .055,
) {
  const starts = context.currentTime + offset;
  const length = Math.floor(context.sampleRate * duration);
  const buffer = context.createBuffer(1, length, context.sampleRate);
  const data = buffer.getChannelData(0);
  for (let index = 0; index < length; index += 1) {
    const position = index / length;
    data[index] = (Math.random() * 2 - 1) * Math.sin(Math.PI * position);
  }
  const source = context.createBufferSource();
  const gain = context.createGain();
  const filter = context.createBiquadFilter();
  const stereo = context.createStereoPanner();
  source.buffer = buffer;
  filter.type = "lowpass";
  filter.Q.value = .35;
  filter.frequency.setValueAtTime(180, starts);
  filter.frequency.exponentialRampToValueAtTime(760, starts + duration * .42);
  filter.frequency.exponentialRampToValueAtTime(220, starts + duration);
  stereo.pan.setValueAtTime(-.28, starts);
  stereo.pan.linearRampToValueAtTime(.28, starts + duration);
  gain.gain.setValueAtTime(.0001, starts);
  gain.gain.exponentialRampToValueAtTime(level, starts + duration * .3);
  gain.gain.exponentialRampToValueAtTime(.0001, starts + duration);
  source.connect(filter).connect(gain).connect(stereo).connect(destination);
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
  const bus = cinematicBus(context, .24);
  const now = context.currentTime;
  const shape = context.createGain();
  shape.gain.setValueAtTime(.0001, now);
  shape.gain.exponentialRampToValueAtTime(1, now + .42);
  shape.gain.setValueAtTime(1, now + 6.4);
  shape.gain.exponentialRampToValueAtTime(.0001, now + 7.55);
  shape.connect(bus);

  tone(context, shape, 41.2, 0, 7.2, .38);
  tone(context, shape, 82.41, .15, 6.8, .19);
  tone(context, shape, 123.47, .72, 5.8, .08, "triangle", -.35);
  warmBloom(context, shape, .42, 3.15, .065);
  tone(context, shape, 164.81, 1.62, 4.2, .085, "sine", -.45);
  tone(context, shape, 220, 1.88, 3.9, .068, "sine", .45);
  tone(context, shape, 293.66, 3.48, 2.9, .06, "sine", -.2);
  tone(context, shape, 369.99, 3.72, 2.6, .045, "sine", .22);
  tone(context, shape, 440, 5.18, 1.85, .028, "sine");
  return closeLater(context, 8_100);
}

export function playVaultSound(kind: "lock" | "unlock", enabled: boolean): StopSound {
  const context = enabled ? audioContext() : null;
  if (!context) return () => undefined;
  const bus = cinematicBus(context, kind === "lock" ? .22 : .25);
  const now = context.currentTime;
  const shape = context.createGain();
  shape.gain.setValueAtTime(.0001, now);
  shape.gain.exponentialRampToValueAtTime(1, now + .06);
  shape.gain.exponentialRampToValueAtTime(.0001, now + 2.35);
  shape.connect(bus);
  const notes = kind === "lock" ? [246.94, 164.81, 82.41] : [164.81, 246.94, 329.63, 493.88];
  notes.forEach((frequency, index) => {
    tone(context, shape, frequency, index * .14, 1.7, .2 / Math.sqrt(index + 1), index > 1 ? "triangle" : "sine", (index - 1.5) * .22);
  });
  warmBloom(context, shape, .04, kind === "lock" ? 1.7 : 1.35, kind === "lock" ? .03 : .038);
  return closeLater(context, 2_750);
}

function Starfield() {
  return (
    <div className="cosmic-stars" aria-hidden="true">
      {STAR_SEEDS.map(([x, y, size, delay], index) => (
        <i
          key={`${x}-${y}`}
          style={{ left: `${x}%`, top: `${y}%`, width: size, height: size, "--star-delay": `${delay}s` } as CSSProperties}
          className={index % 5 === 0 ? "bright" : ""}
        />
      ))}
    </div>
  );
}

function StarClock({ hour, minute, date }: { hour: string; minute: string; date: string }) {
  return (
    <div className="cosmic-clock" aria-label={`Сейчас ${hour}:${minute}`}>
      <div className="star-clock-face" aria-hidden="true">
        {`${hour}:${minute}`.split("").map((character, glyphIndex) => {
          const glyph = CLOCK_GLYPHS[character];
          const points = glyph.flatMap((row) => row.split(""));
          return (
            <span
              className={`star-clock-glyph ${character === ":" ? "colon" : ""}`}
              style={{ "--clock-columns": glyph[0].length } as CSSProperties}
              key={`${character}-${glyphIndex}`}
            >
              {points.map((point, pointIndex) => (
                <i
                  className={point === "1" ? "lit" : ""}
                  style={{ "--clock-delay": `${((glyphIndex * 7 + pointIndex) % 13) * -.17}s` } as CSSProperties}
                  key={pointIndex}
                />
              ))}
            </span>
          );
        })}
      </div>
      <small>{date}</small>
    </div>
  );
}

export function CinematicLaunch({ name, reduced }: { name: string; reduced: boolean }) {
  return (
    <section className={`cinema-launch ${reduced ? "reduced" : ""}`} aria-label="T-Mod запускается" aria-live="polite">
      <div className="cinema-space" aria-hidden="true"><i/><b/><em/></div>
      <Starfield/>
      <div className="cinema-meteors" aria-hidden="true"><i/><i/><i/></div>
      <div className="cinema-horizon" aria-hidden="true"><i/><b/></div>
      <div className="cinema-story">
        <div className="cinema-collaboration">
          <span className="cinema-tmod"><strong>T‑MOD</strong><small>by cdnserver</small></span>
          <i>×</i>
          <span className="cinema-tvrs"><strong>ТОВАРИЩЕСТВО</strong><small>Светлый круг</small></span>
        </div>
        <div className="cinema-greeting">
          <small>ВАШЕ ПРОСТРАНСТВО ГОТОВО</small>
          <h1>{greetingFor(name)}.</h1>
          <p>Добро пожаловать в T‑Mod.</p>
        </div>
      </div>
      <div className="cinema-finale" aria-hidden="true"><i/><b/></div>
    </section>
  );
}

export function VaultScreen({
  name,
  reason,
  reduced,
  unlocking,
  onMinimize,
}: {
  name: string;
  reason: DesktopLockReason;
  reduced: boolean;
  unlocking: boolean;
  onMinimize: () => void;
}) {
  const [now, setNow] = useState(() => new Date());
  const [phrase] = useState(() => LOCK_PHRASES[Math.floor(Math.random() * LOCK_PHRASES.length)]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const hour = String(now.getHours()).padStart(2, "0");
  const minute = String(now.getMinutes()).padStart(2, "0");
  const date = new Intl.DateTimeFormat("ru", { weekday: "long", day: "numeric", month: "long" }).format(now);
  return (
    <section className={`cosmic-lock ${reduced ? "reduced" : ""} ${unlocking ? "unlocking" : ""}`} role="dialog" aria-modal="true" aria-label="T-Mod заблокирован" data-reason={reason}>
      <div className="lock-space" aria-hidden="true"><i/><b/><em/></div>
      <Starfield/>
      <div className="lock-meteors" aria-hidden="true"><i/><i/></div>
      <header className="lock-topbar">
        <div className="lock-collab"><strong>T‑MOD</strong><small>by cdnserver</small><i>×</i><span>Товарищество</span></div>
        <button type="button" className="lock-minimize" aria-label="Свернуть приложение" title="Свернуть приложение" onClick={onMinimize}><i/></button>
      </header>
      <div className="lock-celestial" aria-hidden="true"><i/><b/><em/></div>
      <main className="lock-center">
        <div className="lock-message">
          <small>{unlocking ? "СЕАНС ВОССТАНОВЛЕН" : "T‑MOD РЯДОМ"}</small>
          <h1>{unlocking ? "С возвращением." : `${greetingFor(name, now)}.`}</h1>
          <p>{unlocking ? "Открываем рабочее пространство…" : phrase}</p>
        </div>
        <div className="lock-resume"><i/><span>{unlocking ? "ГОТОВО" : "Нажмите любую клавишу, чтобы продолжить"}</span></div>
      </main>
      <footer className="lock-footer">
        <span><i/> ВАШ СЕАНС СОХРАНЁН</span>
        <StarClock hour={hour} minute={minute} date={date}/>
      </footer>
      <div className="lock-finale" aria-hidden="true"><i/><b/></div>
    </section>
  );
}
