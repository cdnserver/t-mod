"use strict";

const root = document.documentElement;
const screen = document.querySelector(".egg-screen");
const entry = document.querySelector("#egg-entry");
const enterButton = document.querySelector("#egg-enter");
const entryStatus = document.querySelector("#entry-status");
const audio = document.querySelector("#egg-audio");
const equalizerBars = [...document.querySelectorAll(".equalizer i")];
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const equalizerRanges = [
  [42, 105],
  [105, 220],
  [220, 520],
  [520, 1600],
  [1600, 7200],
];

let context;
let analyser;
let spectrum;
let bassEnvelope = 0;
let bassPunch = 0;
let previousBass = 0;
let analysisReady = false;
let energy = 0;
let lastBeatAt = 0;
let lastFrameAt = 0;
let started = false;
let starting = false;

function setEntryState(state, message) {
  entry.dataset.state = state;
  entryStatus.textContent = message;
}

function buildAudioGraph() {
  if (context) return;
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return;

  try {
    context = new AudioContext();
    analyser = context.createAnalyser();
    analyser.fftSize = 2048;
    analyser.minDecibels = -90;
    analyser.maxDecibels = -15;
    analyser.smoothingTimeConstant = 0.42;
    spectrum = new Uint8Array(analyser.frequencyBinCount);
    const source = context.createMediaElementSource(audio);
    source.connect(analyser);
    analyser.connect(context.destination);
  } catch {
    analyser = undefined;
    spectrum = undefined;
  }
}

function revealExperience() {
  started = true;
  document.body.classList.add("experience-started");
  screen.removeAttribute("inert");
  screen.setAttribute("aria-hidden", "false");
  setEntryState("playing", "Трансляция запущена");
  window.setTimeout(() => {
    entry.hidden = true;
  }, 850);
}

function returnToEntry(message) {
  started = false;
  document.body.classList.remove("experience-started");
  screen.setAttribute("inert", "");
  screen.setAttribute("aria-hidden", "true");
  entry.hidden = false;
  enterButton.disabled = false;
  setEntryState("error", message);
}

async function startExperience() {
  if (starting || started) return;
  starting = true;
  enterButton.disabled = true;
  setEntryState("loading", "Запускаем звук и готовим сцену…");

  try {
    buildAudioGraph();
    audio.volume = 0.92;

    // Both calls happen inside the same trusted click. This is important for
    // Chromium-based browsers, including Yandex Browser.
    const resumePromise =
      context?.state === "suspended" ? context.resume() : Promise.resolve();
    const playPromise = Promise.resolve(audio.play());
    await Promise.all([resumePromise, playPromise]);
    revealExperience();
  } catch {
    enterButton.disabled = false;
    setEntryState(
      "error",
      "Браузер не запустил звук. Нажмите ещё раз — сцена откроется только вместе с музыкой.",
    );
  } finally {
    starting = false;
  }
}

function bandEnergy(fromHz, toHz) {
  if (!analyser || !spectrum || context?.state !== "running") return null;
  const binWidth = context.sampleRate / analyser.fftSize;
  const first = Math.max(1, Math.floor(fromHz / binWidth));
  const last = Math.min(spectrum.length - 1, Math.ceil(toHz / binWidth));
  let total = 0;
  for (let index = first; index <= last; index += 1) {
    const value = spectrum[index] / 255;
    total += value * value;
  }
  return Math.sqrt(total / Math.max(1, last - first + 1));
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function markBeat(strength) {
  root.style.setProperty("--beat-strength", strength.toFixed(3));
  screen.classList.remove("on-beat");
  void screen.offsetWidth;
  screen.classList.add("on-beat");
}

function updateEqualizer(now, punch) {
  equalizerBars.forEach((bar, index) => {
    const range = equalizerRanges[index];
    const measured = bandEnergy(range[0], range[1]);
    const fallback =
      0.24
      + Math.max(0, Math.sin(now / (105 + index * 19) + index * 1.7)) * 0.48;
    const level = measured === null
      ? fallback
      : clamp01((measured - 0.18) / 0.7 + punch * (0.24 - index * 0.025));
    bar.style.setProperty(
      "--bar-level",
      (reducedMotion.matches ? Math.min(level, 0.3) : level).toFixed(3),
    );
  });
}

function render(now) {
  if (now - lastFrameAt < 32) {
    requestAnimationFrame(render);
    return;
  }
  lastFrameAt = now;

  analyser?.getByteFrequencyData(spectrum);
  const measuredBass = bandEnergy(36, 180);
  const measuredMid = bandEnergy(190, 2200);
  const measuredHigh = bandEnergy(2200, 9000);
  const fallback = started ? 0.17 + Math.max(0, Math.sin(now / 235)) * 0.12 : 0;
  const bass = measuredBass ?? fallback;
  const mid = measuredMid ?? fallback * 0.72;
  const high = measuredHigh ?? fallback * 0.46;

  if (!analysisReady && measuredBass !== null) {
    bassEnvelope = bass;
    previousBass = bass;
    analysisReady = true;
  }
  bassEnvelope = bassEnvelope * 0.9 + bass * 0.1;
  const transient = Math.max(
    0,
    bass - bassEnvelope,
    (bass - previousBass) * 0.72,
  );
  const punchTarget = clamp01(transient * 14);
  bassPunch =
    punchTarget > bassPunch
      ? bassPunch * 0.18 + punchTarget * 0.82
      : bassPunch * 0.72;
  const bassLevel = clamp01((bass - 0.42) / 0.5);
  const target = clamp01(
    bassLevel * 0.16
      + bassPunch * 0.84
      + mid * 0.08
      + high * 0.025,
  );
  energy =
    target > energy
      ? energy * 0.24 + target * 0.76
      : energy * 0.7 + target * 0.3;

  const beat =
    started
    && punchTarget > 0.5
    && bassLevel > 0.18
    && now - lastBeatAt > 180;
  if (beat) {
    lastBeatAt = now;
    markBeat(punchTarget);
  }
  previousBass = bass;

  const motionEnergy = reducedMotion.matches ? Math.min(energy, 0.16) : energy;
  const motionPunch = reducedMotion.matches ? Math.min(bassPunch, 0.1) : bassPunch;
  root.style.setProperty("--energy", motionEnergy.toFixed(3));
  root.style.setProperty("--bass", bass.toFixed(3));
  root.style.setProperty("--bass-level", bassLevel.toFixed(3));
  root.style.setProperty("--bass-punch", motionPunch.toFixed(3));
  root.style.setProperty("--mid", mid.toFixed(3));
  root.style.setProperty("--high", high.toFixed(3));
  root.style.setProperty("--flow", ((now / 1000) % 10).toFixed(3));
  updateEqualizer(now, bassPunch);
  screen.classList.toggle("high-energy", started && (energy > 0.56 || bassPunch > 0.68));
  requestAnimationFrame(render);
}

enterButton.addEventListener("click", startExperience);
audio.addEventListener("error", () => {
  returnToEntry(
    "Не удалось загрузить аудио. Проверьте соединение и попробуйте ещё раз.",
  );
});
audio.addEventListener("stalled", () => {
  if (!started && !starting) {
    setEntryState("loading", "Аудио загружается…");
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && started && audio.paused) {
    void audio.play().catch(() => {});
  }
});

audio.load();
requestAnimationFrame(render);
