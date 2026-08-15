const byId = (id) => document.getElementById(id);
let audio = null;

function formatDate(value) {
  const parsed = new Date(value || "");
  return Number.isNaN(parsed.getTime()) ? "—" : new Intl.DateTimeFormat("ru-RU", { dateStyle: "long", timeStyle: "short" }).format(parsed);
}

async function loadBan() {
  try {
    const response = await fetch("/api/banned", { credentials: "same-origin", cache: "no-store" });
    const data = await response.json();
    if (!data.active) { location.replace("/"); return; }
    byId("ban-user").textContent = data.user_id || "—";
    byId("ban-reason").textContent = data.reason || "Причина не указана";
    byId("ban-reference").textContent = data.reference || "—";
    byId("ban-issued").textContent = formatDate(data.issued_at);
  } catch {
    byId("ban-reason").textContent = "Решение активно. Подробности временно недоступны.";
  }
}

function createDrone() {
  const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AudioContext) return null;
  const context = new AudioContext();
  const master = context.createGain();
  master.gain.value = 0.055;
  master.connect(context.destination);
  const filter = context.createBiquadFilter();
  filter.type = "lowpass"; filter.frequency.value = 210; filter.Q.value = 1.2; filter.connect(master);
  const oscillators = [43.65, 65.41, 87.31].map((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = index === 0 ? "sine" : "triangle";
    oscillator.frequency.value = frequency;
    gain.gain.value = index === 0 ? .55 : .12;
    oscillator.connect(gain); gain.connect(filter); oscillator.start();
    return oscillator;
  });
  const lfo = context.createOscillator(); const lfoGain = context.createGain();
  lfo.frequency.value = .08; lfoGain.gain.value = 45; lfo.connect(lfoGain); lfoGain.connect(filter.frequency); lfo.start();
  return { context, oscillators, lfo, master };
}

byId("ban-sound").addEventListener("click", async () => {
  if (!audio) audio = createDrone();
  if (!audio) return;
  const enabled = byId("ban-sound").getAttribute("aria-pressed") !== "true";
  if (enabled) await audio.context.resume(); else await audio.context.suspend();
  byId("ban-sound").setAttribute("aria-pressed", String(enabled));
  byId("ban-sound").querySelector("b").textContent = enabled ? "Атмосфера включена" : "Включить атмосферу";
});

void loadBan();
