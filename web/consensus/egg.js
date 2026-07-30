"use strict";

const root = document.documentElement;
const screen = document.querySelector(".egg-screen");
const entry = document.querySelector("#egg-entry");
const enterButton = document.querySelector("#egg-enter");
const entryStatus = document.querySelector("#entry-status");
const audio = document.querySelector("#egg-audio");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let context;
let analyser;
let spectrum;
let baseline = 0.08;
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
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.7;
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
    total += spectrum[index] / 255;
  }
  return total / Math.max(1, last - first + 1);
}

function markBeat() {
  screen.classList.remove("on-beat");
  void screen.offsetWidth;
  screen.classList.add("on-beat");
}

function render(now) {
  if (now - lastFrameAt < 32) {
    requestAnimationFrame(render);
    return;
  }
  lastFrameAt = now;

  const measuredBass = bandEnergy(38, 190);
  const measuredMid = bandEnergy(190, 2200);
  const measuredHigh = bandEnergy(2200, 9000);
  const fallback = started ? 0.17 + Math.max(0, Math.sin(now / 235)) * 0.12 : 0;
  const bass = measuredBass ?? fallback;
  const mid = measuredMid ?? fallback * 0.72;
  const high = measuredHigh ?? fallback * 0.46;

  baseline = baseline * 0.94 + bass * 0.06;
  const transient = Math.max(0, bass - baseline);
  const target = Math.min(1, bass * 0.76 + mid * 0.16 + transient * 5.1);
  energy = Math.max(target, energy * 0.83);

  const beat = started && transient > 0.078 && now - lastBeatAt > 145;
  if (beat) {
    lastBeatAt = now;
    markBeat();
  }

  const motionEnergy = reducedMotion.matches ? Math.min(energy, 0.16) : energy;
  root.style.setProperty("--energy", motionEnergy.toFixed(3));
  root.style.setProperty("--bass", bass.toFixed(3));
  root.style.setProperty("--mid", mid.toFixed(3));
  root.style.setProperty("--high", high.toFixed(3));
  root.style.setProperty("--flow", ((now / 1000) % 10).toFixed(3));
  screen.classList.toggle("high-energy", started && energy > 0.56);
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
