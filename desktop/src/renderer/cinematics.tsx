import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { desktopProduct } from "../shared/product";
import type { DesktopLockReason } from "../shared/contracts";

type StopSound = () => void;

function seededUnit(index: number, salt: number): number {
  const value = Math.sin((index + 1) * (12.9898 + salt * 19.193)) * 43_758.5453;
  return value - Math.floor(value);
}

const STAR_SEEDS = Array.from({ length: 68 }, (_, index) => {
  const bright = index % 13 === 0 || index % 19 === 0;
  const size = bright ? 1.45 + seededUnit(index, 3) * 1.05 : .45 + seededUnit(index, 4) * 1.05;
  const opacity = bright ? .52 + seededUnit(index, 5) * .22 : .12 + seededUnit(index, 6) * .32;
  return {
    x: 2 + seededUnit(index, 1) * 96,
    y: 3 + seededUnit(index, 2) * 92,
    size,
    opacity,
    peak: Math.min(.96, opacity + .2 + seededUnit(index, 7) * .18),
    delay: -seededUnit(index, 8) * 8,
    duration: 4.4 + seededUnit(index, 9) * 5.8,
    shiftX: -3 + seededUnit(index, 10) * 6,
    shiftY: -2 + seededUnit(index, 11) * 4,
    bright,
    temperature: index % 9 === 0 ? "warm" : index % 4 === 0 ? "cool" : "neutral",
  };
});

type MeteorSeed = {
  right: number;
  top: number;
  length: number;
  thickness: number;
  angle: number;
  delay: number;
  duration: number;
  travelX: number;
  travelY: number;
  opacity: number;
  depth: "near" | "far";
};

const LAUNCH_METEORS: readonly MeteorSeed[] = [
  { right: 6, top: 14, length: 235, thickness: 5.2, angle: -31, delay: .52, duration: 1.72, travelX: -72, travelY: 52, opacity: .92, depth: "near" },
  { right: -5, top: 46, length: 132, thickness: 3.4, angle: -28, delay: 2.48, duration: 1.28, travelX: -48, travelY: 33, opacity: .63, depth: "far" },
  { right: 22, top: 26, length: 86, thickness: 2.4, angle: -35, delay: 4.58, duration: 1.08, travelX: -36, travelY: 29, opacity: .48, depth: "far" },
  { right: 2, top: 8, length: 58, thickness: 1.8, angle: -25, delay: 3.72, duration: .92, travelX: -24, travelY: 13, opacity: .34, depth: "far" },
];

const LOCK_METEORS: readonly MeteorSeed[] = [
  { right: 5, top: 17, length: 118, thickness: 3.2, angle: -30, delay: 2.8, duration: 13.5, travelX: -48, travelY: 34, opacity: .58, depth: "far" },
  { right: 28, top: 62, length: 72, thickness: 2.2, angle: -34, delay: 8.6, duration: 16.8, travelX: -34, travelY: 27, opacity: .38, depth: "far" },
  { right: -2, top: 39, length: 154, thickness: 4.1, angle: -27, delay: 15.4, duration: 22.5, travelX: -56, travelY: 35, opacity: .66, depth: "near" },
];

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
      {STAR_SEEDS.map((star, index) => (
        <i
          key={index}
          style={{
            left: `${star.x}%`,
            top: `${star.y}%`,
            width: `${star.size}px`,
            height: `${star.size}px`,
            "--star-delay": `${star.delay}s`,
            "--star-duration": `${star.duration}s`,
            "--star-opacity": star.opacity,
            "--star-peak": star.peak,
            "--star-shift-x": `${star.shiftX}px`,
            "--star-shift-y": `${star.shiftY}px`,
          } as CSSProperties}
          className={`${star.bright ? "bright" : ""} ${star.temperature}`}
        />
      ))}
    </div>
  );
}

function MeteorShower({ mode }: { mode: "launch" | "lock" }) {
  const seeds = mode === "launch" ? LAUNCH_METEORS : LOCK_METEORS;
  return (
    <div className={`meteor-shower ${mode === "launch" ? "cinema-meteors" : "lock-meteors"}`} aria-hidden="true">
      {seeds.map((meteor, index) => (
        <i
          className={meteor.depth}
          key={`${mode}-${index}`}
          style={{
            right: `${meteor.right}%`,
            top: `${meteor.top}%`,
            width: `${meteor.length}px`,
            height: `${meteor.thickness}px`,
            "--meteor-angle": `${meteor.angle}deg`,
            "--meteor-delay": `${meteor.delay}s`,
            "--meteor-duration": `${meteor.duration}s`,
            "--meteor-mid-x": `${meteor.travelX * .56}vw`,
            "--meteor-mid-y": `${meteor.travelY * .56}vh`,
            "--meteor-travel-x": `${meteor.travelX}vw`,
            "--meteor-travel-y": `${meteor.travelY}vh`,
            "--meteor-opacity": meteor.opacity,
          } as CSSProperties}
        />
      ))}
    </div>
  );
}

function CosmicSignatures({ mode }: { mode: "launch" | "lock" }) {
  return (
    <svg className={`cosmic-signatures ${mode}`} viewBox="0 0 1200 700" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <g className="constellation constellation-tmod">
        <path d="M118 126 157 106 194 128 194 172 157 193 118 171 118 126 157 148 194 128M157 148V193"/>
        <circle cx="118" cy="126" r="2.2"/><circle cx="157" cy="106" r="2.6"/><circle cx="194" cy="128" r="2"/><circle cx="194" cy="172" r="1.8"/><circle cx="157" cy="193" r="2.4"/><circle cx="118" cy="171" r="1.7"/><circle cx="157" cy="148" r="3"/>
      </g>
      <g className="constellation constellation-atlas">
        <circle className="orbit" cx="1005" cy="142" r="52"/><path d="M953 142h104M1005 90c-31 28-31 76 0 104M1005 90c31 28 31 76 0 104M968 107c23 17 52 17 74 0M968 177c23-17 52-17 74 0"/>
        <circle cx="1005" cy="90" r="2.2"/><circle cx="1057" cy="142" r="2.7"/><circle cx="968" cy="177" r="1.9"/><circle cx="953" cy="142" r="1.8"/>
      </g>
      <g className="constellation constellation-sgl">
        <path d="M794 502v-76M758 441h72M772 441l-24 38h48l-24-38M816 441l-24 38h48l-24-38M778 506h32"/>
        <circle cx="794" cy="426" r="2.5"/><circle cx="758" cy="441" r="1.8"/><circle cx="830" cy="441" r="1.8"/><circle cx="748" cy="479" r="2.1"/><circle cx="840" cy="479" r="2.1"/><circle cx="794" cy="506" r="2.8"/>
      </g>
      <g className="constellation constellation-light-circle">
        <path d="M286 548 315 523 348 537 361 571 338 600 301 596 280 568 315 565 348 537M315 565 338 600"/>
        <circle cx="286" cy="548" r="1.6"/><circle cx="315" cy="523" r="2.3"/><circle cx="348" cy="537" r="1.7"/><circle cx="361" cy="571" r="2.4"/><circle cx="338" cy="600" r="1.8"/><circle cx="301" cy="596" r="1.5"/><circle cx="280" cy="568" r="2"/><circle cx="315" cy="565" r="2.8"/>
      </g>
    </svg>
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
    <section className={`cinema-launch edition-${desktopProduct.edition} ${reduced ? "reduced" : ""}`} aria-label={`${desktopProduct.name} запускается`} aria-live="polite">
      <div className="cinema-space" aria-hidden="true"><i/><b/><em/></div>
      <Starfield/>
      <CosmicSignatures mode="launch"/>
      <MeteorShower mode="launch"/>
      <div className="cinema-focus" aria-hidden="true"><i/><b/><em/></div>
      <div className="cinema-horizon" aria-hidden="true"><i/><b/></div>
      <div className="cinema-story">
        <div className="cinema-collaboration">
          <span className="cinema-tmod"><strong>{desktopProduct.name}</strong><small>{desktopProduct.privateEdition ? "private owner edition" : "by cdnserver"}</small></span>
          <i>{desktopProduct.privateEdition ? "·" : "×"}</i>
          <span className="cinema-tvrs"><strong>{desktopProduct.privateEdition ? "ТЕХНОЛОГИИ ТОВАРИЩЕСТВА" : "ТОВАРИЩЕСТВО"}</strong><small>{desktopProduct.privateEdition ? "Персональный контур" : "Светлый круг"}</small></span>
        </div>
        <div className="cinema-greeting">
          <small>ВАШЕ ПРОСТРАНСТВО ГОТОВО</small>
          <h1>{greetingFor(name)}.</h1>
          <p>Добро пожаловать в {desktopProduct.name}.</p>
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
    <section className={`cosmic-lock edition-${desktopProduct.edition} ${reduced ? "reduced" : ""} ${unlocking ? "unlocking" : ""}`} role="dialog" aria-modal="true" aria-label={`${desktopProduct.name} заблокирован`} data-reason={reason}>
      <div className="lock-space" aria-hidden="true"><i/><b/><em/></div>
      <Starfield/>
      <CosmicSignatures mode="lock"/>
      <MeteorShower mode="lock"/>
      <header className="lock-topbar">
        <div className="lock-collab"><strong>{desktopProduct.name}</strong><small>{desktopProduct.privateEdition ? "private" : "by cdnserver"}</small><i>×</i><span>{desktopProduct.organization}</span></div>
        <button type="button" className="lock-minimize" aria-label="Свернуть приложение" title="Свернуть приложение" onClick={onMinimize}><i/></button>
      </header>
      <div className="lock-celestial" aria-hidden="true">
        <i className="planet-cloud primary"/>
        <b className="planet-cloud secondary"/>
        <em className="planet-glow"/>
        <span className="planet-aurora"/>
        <small className="planet-lights"/>
        <strong className="planet-terminator"/>
        <u className="planet-detail"/>
      </div>
      <main className="lock-center">
        <div className="lock-message">
          <small>{unlocking ? "СЕАНС ВОССТАНОВЛЕН" : `${desktopProduct.name.toUpperCase()} РЯДОМ`}</small>
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
