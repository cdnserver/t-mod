const root = document.documentElement;
const screen = document.querySelector(".egg-screen");
const audio = document.querySelector("#egg-audio");
const soundGate = document.querySelector(".sound-gate");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let context;
let analyser;
let spectrum;
let source;
let baseline = 0.08;
let energy = 0;
let lastBeatAt = 0;
let lastFrameAt = 0;

function revealSoundGate() {
  soundGate.hidden = false;
  screen.dataset.sound = "blocked";
}

function hideSoundGate() {
  soundGate.hidden = true;
  screen.dataset.sound = "playing";
}

function buildAudioGraph() {
  if (context) return;
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return;

  context = new AudioContext();
  analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.72;
  spectrum = new Uint8Array(analyser.frequencyBinCount);
  source = context.createMediaElementSource(audio);
  source.connect(analyser);
  analyser.connect(context.destination);
}

async function startSound() {
  try {
    buildAudioGraph();
    if (context?.state === "suspended") await context.resume();
    audio.volume = 0.88;
    await audio.play();
    hideSoundGate();
  } catch {
    revealSoundGate();
  }
}

function bassEnergy() {
  if (!analyser || context?.state !== "running") return null;
  analyser.getByteFrequencyData(spectrum);

  const binWidth = context.sampleRate / analyser.fftSize;
  const first = Math.max(1, Math.floor(42 / binWidth));
  const last = Math.min(spectrum.length - 1, Math.ceil(190 / binWidth));
  let total = 0;
  for (let index = first; index <= last; index += 1) {
    total += spectrum[index] / 255;
  }
  return total / Math.max(1, last - first + 1);
}

function render(now) {
  if (now - lastFrameAt < 32) {
    requestAnimationFrame(render);
    return;
  }
  lastFrameAt = now;

  const measured = bassEnergy();
  const fallback = 0.17 + Math.max(0, Math.sin(now / 235)) * 0.12;
  const bass = measured ?? fallback;

  baseline = baseline * 0.94 + bass * 0.06;
  const transient = Math.max(0, bass - baseline);
  const target = Math.min(1, bass * 0.78 + transient * 4.8);
  energy = Math.max(target, energy * 0.84);

  const beat = transient > 0.085 && now - lastBeatAt > 155;
  if (beat) {
    lastBeatAt = now;
    screen.classList.remove("on-beat");
    void screen.offsetWidth;
    screen.classList.add("on-beat");
  }

  const motionEnergy = reducedMotion.matches ? Math.min(energy, 0.18) : energy;
  root.style.setProperty("--energy", motionEnergy.toFixed(3));
  root.style.setProperty("--bass", bass.toFixed(3));
  root.style.setProperty("--flow", ((now / 1000) % 10).toFixed(3));
  requestAnimationFrame(render);
}

soundGate.addEventListener("click", startSound);
audio.addEventListener("playing", hideSoundGate);
audio.addEventListener("error", revealSoundGate);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && audio.paused) void startSound();
});

void startSound();
requestAnimationFrame(render);
