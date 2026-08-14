"use strict";

const byId = (id) => document.getElementById(id);
const login = byId("login");
const dashboard = byId("dashboard");
const loginForm = byId("login-form");
const loginError = byId("login-error");
const tokenInput = byId("token");
const connectionDot = byId("connection-dot");
const connectionText = byId("connection-text");
const BLOCK_STATES = {
  yes: ["за", "yes"],
  no: ["против", "no"],
  abstain: ["воздержался", "abstain"],
  inactive: ["неактивен", "inactive"],
  hidden: ["скрыто", "hidden"],
  pending: ["ожидание", "pending"],
};
const RESULT_LABELS = {
  accepted: "принят",
  rejected: "отклонён",
  vetoed: "вето",
  oral: "устное решение",
};
const CATEGORY_LABELS = {
  ordinary: "Обычное",
  significant: "Значимое",
  supreme: "Верховное",
};
const VOTE_LABELS = {
  yes: "За",
  no: "Против",
  abstain: "Воздержаться",
};

const initialQuery = new URLSearchParams(window.location.search);
let token = sessionStorage.getItem("t-consensus-token") || "";
let selectedMode = initialQuery.get("mode")
  || sessionStorage.getItem("t-consensus-mode")
  || "";
let selectedExperience = initialQuery.get("view")
  || sessionStorage.getItem("t-consensus-experience")
  || "ballot";
let requestedBillId = Number(initialQuery.get("bill") || 0);
let state = null;
let pollTimer = null;
let clockTimer = null;
let fetching = false;
let commanding = false;
let stateSignature = "";
let messageTimer = null;
let observerTab = sessionStorage.getItem("t-consensus-observer-tab") || "participants";
let dialogBill = null;
let dialogResult = null;
let libraryItems = [];
let libraryFilter = "all";
let libraryLoading = false;
let observerFeedSignature = "";
let participantSignature = "";
let controlsSignature = "";
let visualOutcomeKey = "";
let verdictAnimationTimer = null;
let billDialogReturnFocus = null;
let libraryDialogReturnFocus = null;
let pendingBallotVote = null;
let ballotNoticeTimer = null;
let renderedBallotVote = "";
let ballotAudioContext = null;
let ballotSoundReady = false;
let ballotSoundEnabled = localStorage.getItem("t-consensus-sound") !== "off";
let observedSoundBill = "";
let observedSoundStage = "";
let ambientGain = null;
let ambientNodes = [];
let ambientLowpass = null;
let ambientNoiseFilter = null;
let ambientEnabled = localStorage.getItem("t-consensus-ambient") === "on";
let ambientVolume = Math.max(0, Math.min(1, Number(localStorage.getItem("t-consensus-volume") || 28) / 100));
let weatherLoadedAt = 0;
let weatherLoading = false;
let senatorCanvasReady = false;
let senatorCanvasDrawing = false;
let senatorCanvasLast = null;
let senatorBillStartedAt = Date.now();
let senatorBillKey = "";
let previousScheduleSeconds = null;
let countdownSoundProfile = "";

function text(id, value) {
  byId(id).textContent = String(value ?? "—");
}

function setConnection(mode, label) {
  connectionDot.className = `connection-dot ${mode}`;
  connectionText.textContent = label;
}

function renderSoundToggle() {
  const button = byId("ballot-sound-toggle");
  button.classList.toggle("muted", !ballotSoundEnabled);
  button.setAttribute("aria-pressed", String(ballotSoundEnabled));
  button.title = ballotSoundEnabled
    ? ballotSoundReady ? "Звуки заседания включены" : "Нажмите, чтобы проверить звук"
    : "Звуки заседания выключены";
  button.querySelector("span").textContent = ballotSoundEnabled ? "♪" : "×";
  button.querySelector("b").textContent = ballotSoundEnabled ? "Звук" : "Тихо";
  const broadcastButton = byId("broadcast-sound-toggle");
  if (broadcastButton) {
    broadcastButton.classList.toggle("active", ambientEnabled);
    broadcastButton.setAttribute("aria-pressed", String(ambientEnabled));
    broadcastButton.title = ambientEnabled
      ? "Выключить тёмный эмбиент"
      : "Включить тёмный эмбиент";
    text("broadcast-sound-state", ambientEnabled ? "тёмный эмбиент" : "без звука");
  }
}

function ensureAudioContext() {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext) return null;
  if (!ballotAudioContext) ballotAudioContext = new AudioContext();
  if (ballotAudioContext.state === "suspended") void ballotAudioContext.resume();
  ballotSoundReady = true;
  renderSoundToggle();
  return ballotAudioContext;
}

function ensureBallotAudio() {
  if (!ballotSoundEnabled) return null;
  return ensureAudioContext();
}

function playConsensusCue(kind) {
  const context = ensureBallotAudio();
  if (!context || !ballotSoundEnabled) return;
  const notes = kind === "vote-open"
    ? [174.61, 261.63, 349.23]
    : [146.83, 220, 293.66];
  const start = context.currentTime + 0.025;
  const cueBus = context.createGain();
  const cueFilter = context.createBiquadFilter();
  cueFilter.type = "lowpass";
  cueFilter.frequency.value = 1150;
  cueFilter.Q.value = 0.45;
  cueBus.gain.setValueAtTime(0.0001, start);
  cueBus.gain.exponentialRampToValueAtTime(0.42, start + 0.08);
  cueBus.gain.exponentialRampToValueAtTime(0.0001, start + 1.65);
  cueBus.connect(cueFilter).connect(context.destination);
  notes.forEach((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const noteStart = start + index * 0.13;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, noteStart);
    gain.gain.setValueAtTime(0.0001, noteStart);
    gain.gain.exponentialRampToValueAtTime(0.026 - index * 0.004, noteStart + 0.075);
    gain.gain.exponentialRampToValueAtTime(0.0001, noteStart + 1.2);
    oscillator.connect(gain).connect(cueBus);
    oscillator.start(noteStart);
    oscillator.stop(noteStart + 1.3);
  });
}

function setAmbientVolume(value) {
  ambientVolume = Math.max(0, Math.min(1, Number(value) || 0));
  localStorage.setItem("t-consensus-volume", String(Math.round(ambientVolume * 100)));
  if (ambientGain && ballotAudioContext) {
    ambientGain.gain.setTargetAtTime(
      ambientEnabled ? 0.012 + ambientVolume * 0.105 : 0.0001,
      ballotAudioContext.currentTime,
      ambientEnabled ? 1.8 : 0.7,
    );
  }
}

function startConsensusAmbience() {
  const context = ensureAudioContext();
  if (!context) return;
  ambientEnabled = true;
  localStorage.setItem("t-consensus-ambient", "on");
  if (!ambientGain) {
    ambientGain = context.createGain();
    ambientGain.gain.setValueAtTime(0.0001, context.currentTime);
    ambientLowpass = context.createBiquadFilter();
    ambientLowpass.type = "lowpass";
    ambientLowpass.frequency.value = 720;
    ambientLowpass.Q.value = 0.2;
    const compressor = context.createDynamicsCompressor();
    compressor.threshold.value = -30;
    compressor.knee.value = 24;
    compressor.ratio.value = 2.4;
    compressor.attack.value = 0.18;
    compressor.release.value = 1.2;
    ambientGain.connect(ambientLowpass).connect(compressor).connect(context.destination);

    const dryBus = context.createGain();
    const wetBus = context.createGain();
    const reverb = context.createConvolver();
    dryBus.gain.value = 0.7;
    wetBus.gain.value = 0.3;
    dryBus.connect(ambientGain);
    reverb.connect(wetBus).connect(ambientGain);
    const impulse = context.createBuffer(2, Math.floor(context.sampleRate * 6.8), context.sampleRate);
    for (let channel = 0; channel < impulse.numberOfChannels; channel += 1) {
      const samples = impulse.getChannelData(channel);
      let smoothed = 0;
      for (let index = 0; index < samples.length; index += 1) {
        const decay = Math.pow(1 - index / samples.length, 2.7);
        smoothed = smoothed * 0.84 + (Math.random() * 2 - 1) * 0.16;
        samples[index] = smoothed * decay * 0.3;
      }
    }
    reverb.buffer = impulse;

    const padFilter = context.createBiquadFilter();
    padFilter.type = "lowpass";
    padFilter.frequency.value = 510;
    padFilter.Q.value = 0.16;
    padFilter.connect(dryBus);
    padFilter.connect(reverb);
    [
      [36.71, "sine", 0.2, -5],
      [73.42, "triangle", 0.11, 4],
      [110, "sine", 0.062, -3],
      [146.83, "triangle", 0.035, 7],
      [174.61, "sine", 0.018, -6],
    ].forEach(([frequency, waveform, level, detune], index) => {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const lfo = context.createOscillator();
      const lfoGain = context.createGain();
      const amplitudeLfo = context.createOscillator();
      const amplitudeDepth = context.createGain();
      oscillator.type = waveform;
      oscillator.frequency.value = frequency;
      oscillator.detune.value = detune;
      gain.gain.value = level;
      lfo.type = "sine";
      lfo.frequency.value = [0.012, 0.017, 0.009, 0.014, 0.011][index];
      lfoGain.gain.value = [3.2, 4.1, 2.8, 5.2, 3.7][index];
      amplitudeLfo.type = "sine";
      amplitudeLfo.frequency.value = [0.026, 0.019, 0.031, 0.023, 0.017][index];
      amplitudeDepth.gain.value = level * 0.22;
      lfo.connect(lfoGain).connect(oscillator.detune);
      amplitudeLfo.connect(amplitudeDepth).connect(gain.gain);
      oscillator.connect(gain);
      gain.connect(padFilter);
      oscillator.start();
      lfo.start();
      amplitudeLfo.start();
      ambientNodes.push(oscillator, gain, lfo, lfoGain, amplitudeLfo, amplitudeDepth);
    });

    const noiseBuffer = context.createBuffer(1, context.sampleRate * 12, context.sampleRate);
    const noise = noiseBuffer.getChannelData(0);
    let pink = 0;
    for (let index = 0; index < noise.length; index += 1) {
      const white = Math.random() * 2 - 1;
      pink = pink * 0.992 + white * 0.008;
      noise[index] = pink * 0.62 + white * 0.025;
    }
    const noiseSource = context.createBufferSource();
    ambientNoiseFilter = context.createBiquadFilter();
    const noiseGain = context.createGain();
    noiseSource.buffer = noiseBuffer;
    noiseSource.loop = true;
    ambientNoiseFilter.type = "lowpass";
    ambientNoiseFilter.frequency.value = 760;
    ambientNoiseFilter.Q.value = 0.18;
    noiseGain.gain.value = 0.035;
    noiseSource.connect(ambientNoiseFilter).connect(noiseGain);
    noiseGain.connect(dryBus);
    noiseGain.connect(reverb);
    noiseSource.start();
    ambientNodes.push(noiseSource, ambientNoiseFilter, noiseGain, padFilter, dryBus, wetBus, reverb, ambientLowpass, compressor);
  }
  setAmbientVolume(ambientVolume);
  renderSoundToggle();
}

function stopConsensusAmbience() {
  ambientEnabled = false;
  localStorage.setItem("t-consensus-ambient", "off");
  setAmbientVolume(ambientVolume);
  renderSoundToggle();
}

function maybePlayConsensusCue(session, bill) {
  const billKey = session && bill
    ? `${session.key || "session"}:${bill.id || bill.bill_number || "bill"}`
    : "";
  const stage = String(session?.stage || "");
  if (observedSoundBill && billKey && billKey !== observedSoundBill) {
    playConsensusCue("new-bill");
  } else if (
    observedSoundStage
    && observedSoundStage !== "voting"
    && stage === "voting"
  ) {
    playConsensusCue("vote-open");
  }
  observedSoundBill = billKey;
  observedSoundStage = stage;
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function stableSignature(value) {
  try {
    return JSON.stringify(value);
  } catch (error) {
    return String(value);
  }
}

function payloadSignature(payload) {
  if (!payload || typeof payload !== "object") return "";
  const {
    updated_at: _updatedAt,
    cache_state: _cacheState,
    ...stablePayload
  } = payload;
  return stableSignature(stablePayload);
}

function setDataLoading(visible) {
  byId("data-loading").hidden = !visible;
  if (visible) dashboard.setAttribute("inert", "");
  else dashboard.removeAttribute("inert");
}

function requestTimeoutSignal(timeoutMs) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(timeoutMs);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), timeoutMs);
  return controller.signal;
}

function schedulePoll(delay = null) {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  const nextDelay = delay ?? (state?.active ? 2500 : 8000);
  pollTimer = setTimeout(async () => {
    if (!document.hidden && !dashboard.hidden) await fetchState();
    schedulePoll();
  }, nextDelay);
}

function formatNumber(value) {
  return String(Number(value || 0)).padStart(3, "0");
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric.toLocaleString("ru-RU", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })}%`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function categoryLabel(value) {
  return CATEGORY_LABELS[String(value || "ordinary")] || "Обычное";
}

function visualStage(session) {
  const stage = String(session?.stage || "idle");
  return [
    "registration",
    "presentation",
    "voting",
    "finalizing",
    "discussion_type",
    "discussion",
    "paused",
    "after_result",
  ].includes(stage)
    ? stage.replace("_", "-")
    : "idle";
}

function applyVisualState(data) {
  const session = data.session;
  const bill = session?.current_bill || null;
  const result = currentResult(session, bill);
  const active = Boolean(data.active && session);
  const stage = visualStage(session);
  const rawOutcome = String(result?.status || "");
  const outcome = ["accepted", "rejected", "vetoed", "oral"].includes(rawOutcome)
    ? rawOutcome
    : active
      ? "pending"
      : "idle";

  document.body.dataset.stage = stage;
  document.body.dataset.outcome = outcome;
  document.body.dataset.mode = data.mode === "simulation" ? "simulation" : "live";

  const nextOutcomeKey = result
    ? `${data.mode || "live"}:${result.bill_id || bill?.id || bill?.bill_number || "bill"}:${outcome}:${result.overall_percent ?? ""}`
    : "";
  if (nextOutcomeKey && nextOutcomeKey !== visualOutcomeKey) {
    document.body.classList.remove("verdict-transition");
    void document.body.offsetWidth;
    document.body.classList.add("verdict-transition");
    clearTimeout(verdictAnimationTimer);
    verdictAnimationTimer = setTimeout(() => {
      document.body.classList.remove("verdict-transition");
    }, 1800);
    text(
      "result-announcer",
      `${RESULT_LABELS[outcome] || "Результат зафиксирован"}. Общий консенсус ${formatPercent(result.overall_percent)}.`,
    );
  }
  visualOutcomeKey = nextOutcomeKey;
}

function formatTimer(deadline) {
  if (!deadline) return "—";
  const seconds = Math.max(
    0,
    Math.floor((new Date(deadline).getTime() - Date.now()) / 1000),
  );
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function formatScheduleCountdown(deadline) {
  if (!deadline) return "—";
  const seconds = Math.max(0, Math.floor((new Date(deadline).getTime() - Date.now()) / 1000));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days} дн. ${hours} ч.`;
  if (hours > 0) return `${hours} ч. ${minutes} мин.`;
  if (minutes > 0) return `${minutes} мин.`;
  return "начинается";
}

function renderSchedule(data) {
  const schedule = data?.schedule || null;
  const card = byId("observer-schedule");
  const visible = Boolean(schedule && !data?.active && selectedMode !== "simulation");
  document.body.classList.toggle("schedule-visible", visible);
  card.hidden = !visible;
  if (!visible) return;
  text("observer-schedule-title", schedule.title || "Пленарный консенсус");
  text(
    "observer-schedule-description",
    schedule.description || "Повестка и состав будут подтверждены председателем перед началом.",
  );
  const date = new Date(schedule.scheduled_for);
  text(
    "observer-schedule-date",
    Number.isNaN(date.getTime())
      ? "Время уточняется"
      : date.toLocaleString("ru-RU", {
          weekday: "long",
          day: "2-digit",
          month: "long",
          hour: "2-digit",
          minute: "2-digit",
        }),
  );
  text("observer-schedule-countdown", formatScheduleCountdown(schedule.scheduled_for));
  const adjustment = byId("observer-schedule-adjustment");
  const shiftMinutes = Number(schedule.time_shift_minutes || 0);
  adjustment.hidden = !shiftMinutes;
  adjustment.textContent = shiftMinutes
    ? `Время перенесено на ${Math.abs(shiftMinutes)} мин. ${shiftMinutes > 0 ? "позже" : "раньше"} · отсчёт обновлён`
    : "";
  const link = byId("observer-schedule-link");
  link.hidden = !schedule.event_url;
  if (schedule.event_url) link.href = schedule.event_url;
}

function exactCountdown(deadline) {
  if (!deadline) return "00:00:00";
  const seconds = Math.max(0, Math.floor((new Date(deadline).getTime() - Date.now()) / 1000));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600) + days * 24;
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function formatAddedTime(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (!value) return "";
  const minutes = Math.floor(value / 60);
  const rest = Math.floor(value % 60);
  return `+${minutes}:${String(rest).padStart(2, "0")}`;
}

function countdownWindowFor(seconds) {
  if (seconds > 86400) return "distant";
  if (seconds > 21600) return "horizon";
  if (seconds > 3600) return "approaching";
  if (seconds > 900) return "soon";
  if (seconds > 300) return "imminent";
  if (seconds > 60) return "final-five";
  if (seconds > 30) return "final-minute";
  return "final-30";
}

function playCountdownCue(profile) {
  if (!ambientEnabled || !ballotAudioContext || ballotAudioContext.state !== "running") return;
  const frequencies = {
    soon: [82.41],
    imminent: [82.41, 110],
    "final-five": [98, 130.81],
    "final-minute": [110, 146.83],
    "final-30": [130.81, 174.61],
  }[profile];
  if (!frequencies) return;
  const context = ballotAudioContext;
  const start = context.currentTime + .02;
  frequencies.forEach((frequency, index) => {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = frequency;
    gain.gain.setValueAtTime(.0001, start + index * .16);
    gain.gain.exponentialRampToValueAtTime(.018, start + .12 + index * .16);
    gain.gain.exponentialRampToValueAtTime(.0001, start + 1.5 + index * .16);
    oscillator.connect(gain).connect(context.destination);
    oscillator.start(start + index * .16);
    oscillator.stop(start + 1.7 + index * .16);
  });
}

function applyCountdownProfile(profile) {
  const clean = profile || "normal";
  document.body.dataset.countdownWindow = clean;
  const frequencies = {
    distant: [560, 620], horizon: [610, 670], approaching: [680, 730],
    soon: [760, 810], imminent: [840, 900], "final-five": [920, 980],
    "final-minute": [1010, 1060], "final-30": [1100, 1160], normal: [720, 760],
  }[clean] || [720, 760];
  if (ballotAudioContext && ambientLowpass && ambientNoiseFilter) {
    ambientLowpass.frequency.setTargetAtTime(frequencies[0], ballotAudioContext.currentTime, 2.4);
    ambientNoiseFilter.frequency.setTargetAtTime(frequencies[1], ballotAudioContext.currentTime, 2.8);
  }
  if (countdownSoundProfile && countdownSoundProfile !== clean) playCountdownCue(clean);
  countdownSoundProfile = clean;
}

function weatherCopy(code) {
  const numeric = Number(code);
  if (numeric === 0) return ["Ясно", "◯"];
  if ([1, 2].includes(numeric)) return ["Переменная облачность", "◒"];
  if (numeric === 3) return ["Облачно", "●"];
  if ([45, 48].includes(numeric)) return ["Туман", "≋"];
  if ([51, 53, 55, 56, 57].includes(numeric)) return ["Морось", "⋮"];
  if ([61, 63, 65, 66, 67, 80, 81, 82].includes(numeric)) return ["Дождь", "╱"];
  if ([71, 73, 75, 77, 85, 86].includes(numeric)) return ["Снег", "✦"];
  if ([95, 96, 99].includes(numeric)) return ["Гроза", "ϟ"];
  return ["Погода обновляется", "◌"];
}

async function refreshBroadcastWeather() {
  if (weatherLoading || Date.now() - weatherLoadedAt < 10 * 60 * 1000) return;
  weatherLoading = true;
  try {
    const response = await fetch(
      "https://api.open-meteo.com/v1/forecast?latitude=56.9496&longitude=24.1052&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m&timezone=Europe%2FRiga",
      { cache: "no-store", signal: requestTimeoutSignal(5000) },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const current = payload.current || {};
    const [copy, icon] = weatherCopy(current.weather_code);
    text("broadcast-weather-temp", Number.isFinite(Number(current.temperature_2m)) ? `${Math.round(Number(current.temperature_2m))}°` : "—");
    text("broadcast-weather-copy", copy);
    text("broadcast-weather-icon", icon);
    weatherLoadedAt = Date.now();
  } catch {
    text("broadcast-weather-copy", "данные временно недоступны");
  } finally {
    weatherLoading = false;
  }
}

function renderBroadcastResults(results = []) {
  const container = byId("broadcast-results");
  const pageSize = window.innerWidth <= 800 ? 2 : 5;
  const pageCount = Math.max(1, Math.ceil(results.length / pageSize));
  const page = Math.floor(Date.now() / 6500) % pageCount;
  const pageKey = `${results.length}:${pageSize}:${page}`;
  if (container.dataset.pageKey === pageKey) {
    return { page: page + 1, pageCount };
  }
  container.dataset.pageKey = pageKey;
  clearNode(container);
  results.slice(page * pageSize, (page + 1) * pageSize).forEach((result) => {
    const item = document.createElement("div");
    item.className = `broadcast-result-pill ${result.status || ""}`;
    const title = document.createElement("strong");
    const meta = document.createElement("span");
    title.textContent = `№${formatNumber(result.bill_number)} · ${result.title || "Законопроект"}`;
    meta.textContent = `${RESULT_LABELS[result.status] || result.status || "решение"} · ${formatPercent(result.overall_percent)}`;
    item.append(title, meta);
    container.append(item);
  });
  return { page: page + 1, pageCount };
}

function agendaMetrics(data, session, currentBill) {
  const seen = new Set();
  const keyFor = (item) => {
    const id = Number(item?.bill_id || item?.id || 0);
    if (id > 0) return `id:${id}`;
    const number = Number(item?.bill_number || 0);
    if (number > 0) return `number:${number}`;
    return `title:${String(item?.title || "").trim().toLowerCase()}`;
  };
  (session?.results || []).forEach((item) => seen.add(keyFor(item)));
  let position = seen.size;
  if (currentBill) {
    const currentKey = keyFor(currentBill);
    if (!seen.has(currentKey)) position += 1;
    seen.add(currentKey);
  }
  (data.queue || []).forEach((item) => seen.add(keyFor(item)));
  return { position, total: seen.size };
}

function renderBroadcast(data) {
  const phase = String(data.broadcast_phase || (data.active ? "live" : data.schedule ? "scheduled" : "idle"));
  const session = data.session;
  const schedule = data.schedule;
  const completed = data.last_session;
  const bill = session?.current_bill || null;
  document.body.dataset.broadcastPhase = phase;
  const stateKicker = {
    idle: "КОНСЕНСУС · ТОВАРИЩЕСТВО",
    scheduled: "ЗАСЕДАНИЕ · ЗАПЛАНИРОВАНО",
    preparing: "ЗАСЕДАНИЕ · ПОДГОТОВКА",
    live: "ПРЯМОЙ ЭФИР · ЗАСЕДАНИЕ",
    completed: "ПРОТОКОЛ · ЗАВЕРШЕНО",
  }[phase] || "КОНСЕНСУС · ТОВАРИЩЕСТВО";
  const stateMeta = {
    idle: "единая трансляция",
    scheduled: "до открытия светлого круга",
    preparing: "проверка кворума",
    live: "решения в реальном времени",
    completed: "решения зафиксированы",
  }[phase] || "единая трансляция";
  text("broadcast-state-kicker", stateKicker);
  text("broadcast-state-meta", stateMeta);
  text("broadcast-wordmark", "Т О В А Р И Щ Е С Т В О");

  const countdownWrap = byId("broadcast-countdown-wrap");
  const sessionCard = byId("broadcast-session-card");
  const completedCard = byId("broadcast-completed");
  countdownWrap.hidden = !["scheduled", "preparing"].includes(phase);
  sessionCard.hidden = !["scheduled", "preparing", "live"].includes(phase);
  completedCard.hidden = phase !== "completed";

  if (phase === "idle") {
    text("broadcast-title", "Консенсус.");
    text("broadcast-subtitle", "Светлый круг");
  } else if (phase === "scheduled") {
    text("broadcast-title", `№ ${schedule?.plenary_number || "—"} · Пленарный Консенсус Товарищества`);
    text("broadcast-subtitle", schedule?.title || "Светлый круг готовится к заседанию");
  } else if (phase === "preparing") {
    text("broadcast-title", `№ ${session?.plenary_number || schedule?.plenary_number || "—"} · Пленарный Консенсус Товарищества`);
    text("broadcast-subtitle", "Состав подтверждает участие. Эфир скоро начнётся.");
  } else if (phase === "live") {
    text("broadcast-title", `№ ${session?.plenary_number || "—"} · Пленарный Консенсус`);
    text("broadcast-subtitle", session?.stage === "presentation" ? "Законопроект представлен" : session?.stage_label || "Светлый круг в заседании");
  } else {
    text("broadcast-title", `№ ${completed?.plenary_number || "—"} · Консенсус завершён`);
    text("broadcast-subtitle", "Решения зафиксированы. Протокол сформирован.");
  }

  const deadline = schedule?.scheduled_for;
  text("broadcast-countdown", exactCountdown(deadline));
  text("broadcast-countdown-label", phase === "preparing" ? "ДО НАЧАЛА ЭФИРА" : "ДО НАЧАЛА");
  if (deadline) {
    const date = new Date(deadline);
    text("broadcast-scheduled-at", date.toLocaleString("ru-RU", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }));
  }
  const scheduleAdjustment = byId("broadcast-schedule-adjustment");
  const scheduleShift = Number(schedule?.time_shift_minutes || 0);
  scheduleAdjustment.hidden = !scheduleShift;
  scheduleAdjustment.textContent = scheduleShift
    ? `Время перенесено на ${Math.abs(scheduleShift)} мин. ${scheduleShift > 0 ? "позже" : "раньше"} · новый отсчёт уже действует`
    : "";

  const host = session?.leader?.name || schedule?.host?.name || "Председатель Товарищества";
  const agenda = agendaMetrics(data, session, bill);
  const confirmed = Number(session?.quorum?.confirmed || 0);
  const invited = Number(session?.quorum?.invited || 0);
  const expected = Number(session?.voting?.expected || 0);
  const received = Number(session?.voting?.received || 0);
  const voteProgress = expected > 0
    ? Math.max(0, Math.min(100, received / expected * 100))
    : 0;
  text("broadcast-host", host);
  text("broadcast-stage", session?.stage_label || (phase === "scheduled" ? "Запланировано" : "Подготовка"));
  text("broadcast-agenda", agenda.total ? `${agenda.position} / ${agenda.total}` : "0 / 0");
  text("broadcast-quorum", session ? `${confirmed} / ${invited}` : "—");
  text("broadcast-votes", session ? `${received} / ${expected}` : "—");
  text("broadcast-stage-time", session ? formatTimer(session.timer_deadline) : "—");
  byId("broadcast-bill").hidden = !bill;
  if (bill) {
    text("broadcast-bill-number", `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}`);
    text("broadcast-bill-title", bill.title || "Без названия");
    text("broadcast-bill-summary", bill.summary || "Полный текст доступен в карточке проекта.");
    text("broadcast-bill-author", bill.author?.name || "Автор не указан");
    text("broadcast-bill-category", categoryLabel(bill.decision_category));
    text(
      "broadcast-bill-threshold",
      formatPercent(currentResult(session, bill)?.required_percent || session?.rules?.acceptance_percent),
    );
    text("broadcast-bill-timer", formatTimer(session?.timer_deadline));
    text(
      "broadcast-bill-timer-label",
      session?.stage === "voting" ? "ДО ЗАКРЫТИЯ ВОУТА" : "ВРЕМЯ ЭТАПА",
    );
    text(
      "broadcast-vote-progress-label",
      `${received} из ${expected} голосов · направления скрыты`,
    );
    byId("broadcast-vote-progress-bar").style.width = `${voteProgress}%`;
    const adjustment = byId("broadcast-time-adjustment");
    adjustment.hidden = !Number(session?.timer_added_seconds || 0);
    adjustment.textContent = session?.timer_added_seconds
      ? `${formatAddedTime(session.timer_added_seconds)} · добавлено ведущим`
      : "";
    byId("broadcast-open-bill").onclick = () => showBillDialog(bill, currentResult(session, bill));
  } else {
    byId("broadcast-time-adjustment").hidden = true;
  }

  if (phase === "completed" && completed) {
    text("broadcast-completed-title", `№ ${completed.plenary_number} · Заседание завершено`);
    const resultPage = renderBroadcastResults(completed.results || []);
    text(
      "broadcast-completed-meta",
      `${completed.results?.length || 0} решений · ведущий ${completed.leader?.name || "—"}${resultPage.pageCount > 1 ? ` · итоги ${resultPage.page}/${resultPage.pageCount}` : ""}`,
    );
    const report = byId("broadcast-report-link");
    report.hidden = !completed.key;
    if (completed.key) report.href = `/api/reports/${encodeURIComponent(completed.key)}/consensus.pdf`;
  }
  if (phase === "live" && ambientEnabled) maybePlayConsensusCue(session, bill);
  void refreshBroadcastWeather();
}

function showCommandMessage(message, kind = "success") {
  const node = byId("command-message");
  node.textContent = message;
  node.className = `command-message ${kind}`;
  node.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => {
    node.hidden = true;
  }, 7000);
}

function renderBlocks(blocks = {}) {
  document.querySelectorAll(".block-card").forEach((card) => {
    const stateName = blocks[card.dataset.block] || "pending";
    const [label, className] = BLOCK_STATES[stateName] || BLOCK_STATES.pending;
    card.classList.remove("yes", "no", "abstain", "inactive", "hidden", "pending");
    card.classList.add(className);
    card.querySelector(".block-state").textContent = label;
  });
}

function renderParticipants(participants = []) {
  const container = byId("participants");
  text("participants-count", participants.length);
  const nextSignature = stableSignature(
    participants.map((participant) => [
      participant.id,
      participant.name,
      participant.kind,
      Boolean(participant.confirmed),
      Boolean(participant.voted),
      participant.vote || "",
    ]),
  );
  if (nextSignature === participantSignature) return;
  participantSignature = nextSignature;
  clearNode(container);
  if (!participants.length) {
    container.className = "participants empty-state";
    container.textContent = "Нет активной регистрации";
    return;
  }
  container.className = "participants";
  participants.forEach((participant) => {
    const row = document.createElement("div");
    row.className = "participant";
    const dot = document.createElement("span");
    dot.className = `participant-dot${participant.confirmed ? " ready" : ""}`;
    const identity = document.createElement("div");
    const name = document.createElement("strong");
    const role = document.createElement("small");
    name.textContent = participant.name;
    role.textContent = participant.confirmed
      ? participant.kind
      : `${participant.kind} · ожидает подтверждение`;
    identity.append(name, role);
    const vote = document.createElement("span");
    vote.className = `vote-state${participant.voted ? " done" : ""}`;
    vote.textContent = participant.vote
      ? VOTE_LABELS[participant.vote] || participant.vote
      : participant.voted ? "голос принят" : "ожидание";
    if (participant.vote) vote.classList.add(participant.vote);
    row.append(dot, identity, vote);
    container.append(row);
  });
}

function renderList(containerId, items, kind) {
  const container = byId(containerId);
  const nextSignature = stableSignature(
    items.map((item) => [
      item.id || item.bill_id,
      item.bill_number,
      item.title,
      item.status,
      item.overall_percent,
    ]),
  );
  if (container.dataset.signature === nextSignature) return;
  container.dataset.signature = nextSignature;
  clearNode(container);
  if (!items.length) {
    container.className = "stack-list empty-state";
    container.textContent = kind === "queue" ? "Очередь пуста" : "Решений пока нет";
    return;
  }
  container.className = "stack-list";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-row";
    const number = document.createElement("span");
    number.className = "list-number";
    number.textContent = `№${formatNumber(item.bill_number)}`;
    const title = document.createElement("button");
    title.type = "button";
    title.className = "list-title list-title-button";
    title.textContent = item.title || "Без названия";
    title.addEventListener("click", () => openBillRecord(item));
    const meta = document.createElement("span");
    const status = String(item.status || "");
    meta.className = `list-meta ${status}`;
    meta.textContent = kind === "queue"
      ? "в очереди"
      : `${RESULT_LABELS[status] || status || "решение"} · ${formatPercent(item.overall_percent)}`;
    row.append(number, title, meta);
    container.append(row);
  });
}

function currentResult(session, bill) {
  if (!session || !bill) return null;
  if (
    session.current_result
    && Number(session.current_result.bill_number) === Number(bill.bill_number)
  ) {
    return session.current_result;
  }
  return [...(session.results || [])]
    .reverse()
    .find((item) => Number(item.bill_number) === Number(bill.bill_number)) || null;
}

function renderObserverBlocks(blocks = {}) {
  document.querySelectorAll("#observer-blocks [data-block]").forEach((node) => {
    const stateName = blocks[node.dataset.block] || "pending";
    const [label, className] = BLOCK_STATES[stateName] || BLOCK_STATES.pending;
    node.className = className;
    node.querySelector("em").textContent = label;
  });
}

function observerFeedRow(title, meta, options = {}) {
  const node = options.button
    ? document.createElement("button")
    : options.href
      ? document.createElement("a")
      : document.createElement("div");
  node.className = "observer-feed-row";
  if (options.button) node.type = "button";
  if (options.href) {
    node.href = options.href;
    node.target = "_blank";
    node.rel = "noreferrer";
  }
  const heading = document.createElement("strong");
  const detail = document.createElement("span");
  heading.textContent = title;
  detail.textContent = meta;
  node.append(heading, detail);
  return node;
}

function renderObserverFeed(data) {
  const feed = byId("observer-feed");
  const session = data.session;
  const participants = session?.participants || [];
  const results = session?.results?.length
    ? session.results
    : data.recent_results || [];
  text("observer-participants-count", participants.length);
  text("observer-tab-queue-count", (data.queue || []).length);
  text("observer-results-count", results.length);

  if (!["participants", "queue", "results"].includes(observerTab)) {
    observerTab = "participants";
  }
  document.querySelectorAll("#observer-tabs [data-tab]").forEach((button) => {
    const active = button.dataset.tab === observerTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const sourceItems = observerTab === "participants"
    ? participants.map((item) => [
      item.user_id,
      item.name,
      item.kind,
      item.confirmed,
      item.voted,
    ])
    : observerTab === "queue"
      ? (data.queue || []).map((item) => [
        item.id || item.bill_id,
        item.bill_number,
        item.title,
        item.status,
      ])
      : results.map((item) => [
        item.bill_id || item.id,
        item.bill_number,
        item.title,
        item.status,
        item.overall_percent,
      ]);
  const nextSignature = stableSignature([observerTab, sourceItems]);
  if (nextSignature === observerFeedSignature) return;
  const previousScrollTop = feed.scrollTop;
  const wasNearBottom = feed.scrollHeight - feed.clientHeight - previousScrollTop < 24;
  observerFeedSignature = nextSignature;
  clearNode(feed);

  let items = [];
  if (observerTab === "participants") {
    items = participants;
    participants.forEach((participant) => {
      const meta = participant.confirmed
        ? `${participant.kind} · ${participant.voted ? "голос принят" : "ожидается голос"}`
        : `${participant.kind} · ожидается подтверждение`;
      const row = observerFeedRow(participant.name, meta);
      row.classList.add(participant.confirmed ? "ready" : "waiting");
      feed.append(row);
    });
  } else if (observerTab === "queue") {
    items = data.queue || [];
    items.forEach((item) => {
      const row = observerFeedRow(
        `№${formatNumber(item.bill_number)} · ${item.title || "Без названия"}`,
        "готов к рассмотрению · открыть текст",
        { button: true },
      );
      row.addEventListener("click", () => openBillRecord(item));
      feed.append(row);
    });
  } else {
    items = results;
    results.forEach((item) => {
      const row = observerFeedRow(
        `№${formatNumber(item.bill_number)} · ${item.title || "Без названия"}`,
        `${RESULT_LABELS[item.status] || item.status || "решение"} · общий ${formatPercent(item.overall_percent)}`,
        { button: Boolean(item.bill || item.bill_id) },
      );
      row.classList.add(item.status || "waiting");
      if (item.bill || item.bill_id) {
        row.addEventListener(
          "click",
          () => openBillRecord(item),
        );
      }
      feed.append(row);
    });
  }

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "observer-feed-empty";
    empty.textContent = observerTab === "participants"
      ? "Состав ещё не зарегистрирован"
      : observerTab === "queue"
        ? "Очередь законопроектов пуста"
        : "Зафиксированных решений пока нет";
    feed.append(empty);
  }
  feed.scrollTop = wasNearBottom
    ? feed.scrollHeight
    : Math.min(previousScrollTop, Math.max(0, feed.scrollHeight - feed.clientHeight));
}

function renderObserver(data) {
  const session = data.session;
  const bill = session?.current_bill || null;
  const result = currentResult(session, bill);
  const active = Boolean(data.active && session);
  const resultStatus = byId("observer-result-status");

  text(
    "observer-eyebrow",
    session
      ? `${selectedMode === "simulation" ? "УЧЕБНЫЙ" : "ПЛЕНАРНЫЙ"} КОНСЕНСУС · ${session.plenary_number}`
      : "ТЕКУЩЕЕ ЗАСЕДАНИЕ",
  );
  text(
    "observer-session-title",
    session
      ? active ? "Заседание в процессе" : "Заседание завершено"
      : data.schedule ? "Следующее заседание назначено" : "Консенсус не проводится",
  );
  text(
    "observer-session-detail",
    session
      ? `Ведущий: ${session.leader.name} · ${session.stage_label.toLowerCase()}`
      : data.schedule
        ? "План опубликован председателем. Трансляция откроется здесь автоматически."
        : "Экран ожидает начало следующего пленарного заседания.",
  );
  text("observer-stage", session?.stage_label || "Ожидание");
  byId("observer-stage").className = `stage-badge stage-${visualStage(session)}${active ? "" : " idle"}`;

  text(
    "observer-bill-number",
    bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН",
  );
  text("observer-bill-title", bill?.title || "Между законопроектами");
  text("observer-bill-author", bill?.author?.name || "—");
  text("observer-bill-date", formatDate(bill?.created_at));
  text("observer-bill-category", categoryLabel(bill?.decision_category));
  text(
    "observer-bill-summary",
    bill?.summary || "Полный текст появится после выбора законопроекта.",
  );
  const openBill = byId("open-bill-dialog");
  openBill.disabled = !bill;
  dialogBill = bill;
  dialogResult = result;
  const sourceLink = byId("observer-bill-link");
  sourceLink.hidden = !bill?.source_url;
  if (bill?.source_url) sourceLink.href = bill.source_url;

  const requiredPercent = Number(
    result?.required_percent
    || session?.rules?.acceptance_percent
    || 50,
  );
  text("observer-required-percent", formatPercent(requiredPercent));
  byId("observer-threshold-mark").style.left = `${Math.max(0, Math.min(100, requiredPercent))}%`;

  if (result) {
    const status = String(result.status || "");
    resultStatus.className = `result-status ${status}`;
    resultStatus.textContent = RESULT_LABELS[status] || status || "зафиксирован";
    text("observer-overall-percent", formatPercent(result.overall_percent));
    text(
      "observer-result-detail",
      `${RESULT_LABELS[status] || "Результат зафиксирован"} · точный итог голосования`,
    );
    text("observer-internal-percent", formatPercent(result.internal_percent));
    text("observer-opposed-percent", formatPercent(result.opposed_percent));
    byId("observer-result-bar").style.width = `${Math.max(0, Math.min(100, Number(result.overall_percent) || 0))}%`;
  } else {
    const status = session?.stage === "voting" || session?.stage === "finalizing"
      ? "голосование"
      : session?.stage_label?.toLowerCase() || "ожидание";
    resultStatus.className = "result-status waiting";
    resultStatus.textContent = status;
    text("observer-overall-percent", "скрыт");
    text(
      "observer-result-detail",
      "Точный процент появится после фиксации — текущие направления не раскрываются",
    );
    text("observer-internal-percent", "—");
    text("observer-opposed-percent", "—");
    byId("observer-result-bar").style.width = "0%";
  }

  text(
    "observer-quorum",
    session ? `${session.quorum.confirmed}/${session.quorum.invited}` : "—",
  );
  text(
    "observer-quorum-detail",
    session
      ? `${formatPercent(session.quorum.percent)} · ${session.quorum.ready ? "собран" : "ожидание"}`
      : "нет сессии",
  );
  text(
    "observer-votes",
    session ? `${session.voting.received}/${session.voting.expected}` : "—",
  );
  text("observer-timer", formatTimer(session?.timer_deadline));
  text(
    "observer-timer-detail",
    session?.timer_added_seconds
      ? `добавлено ${formatAddedTime(session.timer_added_seconds)}`
      : "до фиксации",
  );
  text("observer-queue-count", (data.queue || []).length);
  renderObserverBlocks(session?.blocks || {});
  renderObserverFeed(data);
}

function showBillDialog(bill, result = null) {
  if (!bill) return;
  billDialogReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  dialogBill = bill;
  dialogResult = result;
  const billId = Number(bill.id || bill.bill_id || 0);
  byId("copy-bill-link").disabled = !Number.isInteger(billId) || billId <= 0;
  if (Number.isInteger(billId) && billId > 0) {
    const url = new URL(window.location.href);
    url.searchParams.set("bill", String(billId));
    if (selectedMode) url.searchParams.set("mode", selectedMode);
    window.history.replaceState(null, "", url);
  }
  text("bill-dialog-number", `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}`);
  text("bill-dialog-title", bill.title || "Без названия");
  text("bill-dialog-author", bill.author?.name || "Автор не указан");
  text("bill-dialog-date", formatDate(bill.created_at));
  text("bill-dialog-category", categoryLabel(bill.decision_category));
  text("bill-dialog-summary", bill.summary || "Текст предложения не сохранён.");
  const materials = String(bill.materials || "").trim();
  byId("bill-dialog-materials-wrap").hidden = !materials;
  text("bill-dialog-materials", materials || "Материалы не приложены.");
  const link = byId("bill-dialog-link");
  link.hidden = !bill.source_url;
  if (bill.source_url) link.href = bill.source_url;
  const resultPanel = byId("bill-dialog-result");
  resultPanel.hidden = !result;
  if (result) {
    const status = String(result.status || "");
    text(
      "bill-dialog-result-status",
      RESULT_LABELS[status] || status || "зафиксировано",
    );
    byId("bill-dialog-result-status").className = status;
    text("bill-dialog-overall", formatPercent(result.overall_percent));
    text("bill-dialog-internal", formatPercent(result.internal_percent));
    text("bill-dialog-opposed", formatPercent(result.opposed_percent));
    text("bill-dialog-required", formatPercent(result.required_percent));
  }
  const dialog = byId("bill-dialog");
  const dialogArticle = dialog.querySelector("article");
  if (dialogArticle) dialogArticle.scrollTop = 0;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  byId("close-bill-dialog").focus({ preventScroll: true });
}

function closeBillDialog() {
  const dialog = byId("bill-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  if (billDialogReturnFocus?.isConnected) billDialogReturnFocus.focus();
  billDialogReturnFocus = null;
  const url = new URL(window.location.href);
  url.searchParams.delete("bill");
  window.history.replaceState(null, "", url);
}

async function copyBillLink() {
  const billId = Number(dialogBill?.id || dialogBill?.bill_id || 0);
  if (!Number.isInteger(billId) || billId <= 0) return;
  const url = new URL(window.location.href);
  url.searchParams.set("bill", String(billId));
  if (selectedMode) url.searchParams.set("mode", selectedMode);
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url.toString());
    } else {
      const helper = document.createElement("textarea");
      helper.value = url.toString();
      helper.readOnly = true;
      helper.style.position = "fixed";
      helper.style.left = "-9999px";
      document.body.append(helper);
      helper.select();
      const copied = document.execCommand("copy");
      helper.remove();
      if (!copied) throw new Error("clipboard_unavailable");
    }
    showCommandMessage("Ссылка на законопроект скопирована.");
  } catch {
    showCommandMessage("Не удалось скопировать ссылку. Скопируйте адрес браузера.", "error");
  }
}

async function openBillRecord(item) {
  const knownBill = item?.summary !== undefined
    ? item
    : item?.bill?.summary !== undefined
      ? item.bill
      : null;
  const knownResult = item?.result || (
    item?.overall_percent !== undefined ? item : null
  );
  if (knownBill && String(knownBill.summary || "").trim()) {
    showBillDialog(knownBill, knownResult);
    return;
  }
  const billId = Number(item?.id || item?.bill_id || item?.bill?.id || 0);
  if (!Number.isInteger(billId) || billId <= 0) {
    showCommandMessage("Полный текст этого проекта ещё не сохранён.", "error");
    return;
  }
  setConnection("", "загрузка проекта");
  try {
    const response = await fetch(
      `/api/bills/${billId}?mode=${encodeURIComponent(selectedMode)}`,
      {
        headers: authHeaders(),
        credentials: "same-origin",
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    showBillDialog(payload.bill, payload.result);
    setConnection("online", "обновляется");
  } catch (error) {
    showCommandMessage(
      "Не удалось открыть законопроект. Обновите панель и повторите.",
      "error",
    );
    setConnection("offline", "ошибка загрузки");
  }
}

function catalogStatus(item) {
  if (item.result) {
    const status = String(item.result.status || "");
    return `${RESULT_LABELS[status] || status || "решение"} · ${formatPercent(item.result.overall_percent)}`;
  }
  return {
    draft: "готовится",
    queued: "в очереди",
    requeued: "повторное рассмотрение",
    under_consideration: "рассматривается",
  }[String(item.status || "")] || String(item.status || "законопроект");
}

function renderBillLibrary() {
  const list = byId("bill-library-list");
  clearNode(list);
  const query = byId("bill-library-search").value.trim().toLocaleLowerCase("ru-RU");
  const filtered = libraryItems.filter((item) => {
    const matchesFilter = libraryFilter === "all"
      || (libraryFilter === "decided" && Boolean(item.result))
      || (
        libraryFilter === "queue"
        && ["draft", "queued", "requeued", "under_consideration"].includes(
          String(item.status || ""),
        )
        && !item.result
      );
    if (!matchesFilter) return false;
    if (!query) return true;
    return [
      formatNumber(item.bill_number),
      item.title,
      item.author?.name,
    ].some((value) => String(value || "").toLocaleLowerCase("ru-RU").includes(query));
  });
  text(
    "bill-library-count",
    `${filtered.length} из ${libraryItems.length}`,
  );
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "library-empty";
    empty.textContent = libraryLoading
      ? "Загружаем законопроекты…"
      : "По этому запросу законопроектов нет";
    list.append(empty);
    return;
  }
  filtered.forEach((item) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "library-row";
    const number = document.createElement("span");
    number.className = "library-number";
    number.textContent = `№${formatNumber(item.bill_number)}`;
    const content = document.createElement("span");
    content.className = "library-content";
    const title = document.createElement("strong");
    const author = document.createElement("small");
    title.textContent = item.title || "Без названия";
    author.textContent = `${item.author?.name || "Автор не указан"} · ${formatDate(item.created_at)}`;
    content.append(title, author);
    const status = document.createElement("span");
    status.className = `library-status ${item.result?.status || item.status || ""}`;
    status.textContent = catalogStatus(item);
    row.append(number, content, status);
    row.addEventListener("click", async () => {
      closeBillLibrary();
      await openBillRecord(item);
    });
    list.append(row);
  });
}

async function openBillLibrary() {
  const dialog = byId("bill-library-dialog");
  libraryDialogReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  if (libraryLoading) return;
  libraryLoading = true;
  libraryItems = [];
  text("bill-library-status", "обновление каталога");
  renderBillLibrary();
  try {
    const response = await fetch(
      `/api/bills?mode=${encodeURIComponent(selectedMode)}`,
      {
        headers: authHeaders(),
        credentials: "same-origin",
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    libraryItems = Array.isArray(payload.items) ? payload.items : [];
    text("bill-library-status", `контур: ${selectedMode === "simulation" ? "симуляция" : "рабочий"}`);
  } catch (error) {
    text("bill-library-status", "каталог временно недоступен");
  } finally {
    libraryLoading = false;
    renderBillLibrary();
  }
}

function closeBillLibrary() {
  const dialog = byId("bill-library-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  if (libraryDialogReturnFocus?.isConnected) libraryDialogReturnFocus.focus();
  libraryDialogReturnFocus = null;
}

function renderMode(data) {
  const available = new Set(data.available_modes || ["live"]);
  selectedMode = data.mode || "live";
  sessionStorage.setItem("t-consensus-mode", selectedMode);
  document.querySelectorAll("#mode-switch button").forEach((button) => {
    const mode = button.dataset.mode;
    button.disabled = !available.has(mode);
    button.classList.toggle("active", mode === selectedMode);
    button.setAttribute("aria-pressed", String(mode === selectedMode));
  });
  const simulated = selectedMode === "simulation";
  byId("simulation-banner").hidden = !simulated;
  dashboard.classList.toggle("simulation-mode", simulated);
}

function renderViewer(data) {
  const viewer = data.viewer || {};
  const chip = byId("viewer-chip");
  byId("reactor-link").hidden = !viewer.administrator;
  chip.hidden = !viewer.authenticated && !viewer.legacy_read_only;
  if (chip.hidden) return;
  text("viewer-name", viewer.name || "Наблюдатель");
  const role = viewer.leader
    ? "ведущий"
    : viewer.chair
      ? "председатель"
      : viewer.participant
        ? "участник голосования"
      : viewer.legacy_read_only
        ? "только просмотр"
        : "наблюдатель";
  text("viewer-role", role);
}

function renderExperience(data) {
  const viewer = data.viewer || {};
  const available = Boolean(
    selectedMode === "live"
    && viewer.authenticated
    && viewer.ballot_available,
  );
  const switcher = byId("experience-switch");
  switcher.hidden = !available;
  if (!available) selectedExperience = "broadcast";
  if (!new Set(["broadcast", "ballot"]).has(selectedExperience)) {
    selectedExperience = "ballot";
  }
  switcher.querySelectorAll("[data-experience]").forEach((button) => {
    const active = button.dataset.experience === selectedExperience;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const ballotMode = available && selectedExperience === "ballot";
  document.body.classList.toggle("ballot-screen-mode", ballotMode);
  byId("ballot-screen").hidden = !ballotMode;
  return { available, ballotMode };
}

function selectExperience(experience) {
  const apply = () => {
    selectedExperience = experience;
    sessionStorage.setItem("t-consensus-experience", selectedExperience);
    if (state) render(state);
  };
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (typeof document.startViewTransition === "function" && !reducedMotion) {
    document.startViewTransition(apply);
  } else {
    apply();
  }
}

function renderBallotPhases(session, vote) {
  const stage = String(session?.stage || "");
  const phaseIndex = stage === "presentation"
    ? 0
    : stage === "voting"
      ? vote ? 2 : 1
      : stage === "finalizing" || stage === "discussion" || stage === "discussion_type" || stage === "paused"
        ? vote ? 2 : 1
        : ["after_result", "finished"].includes(stage)
          ? 3
          : -1;
  byId("ballot-phase-rail").querySelectorAll("[data-phase]").forEach((step, index) => {
    step.classList.toggle("done", phaseIndex > index);
    step.classList.toggle("active", phaseIndex === index);
  });
  return phaseIndex;
}

function ballotStageCopy(session, viewer) {
  if (!session) return ["Ожидание", "Бюллетень не сформирован", "Следующее заседание ещё не открыто."];
  if (!viewer.confirmed) return ["Нет допуска", "Участие не подтверждено", "Подтвердите участие через личную панель Discord."];
  if (session.stage === "presentation") {
    return ["Представление", "Ведущий представляет проект", "Текст уже доступен. Кнопки откроются после команды «Поставить на воут». "];
  }
  if (session.stage === "voting" && viewer.can_vote) {
    return ["Воут открыт", viewer.vote ? "Ваш голос сохранён" : "Выберите решение", viewer.vote ? "До фиксации результата выбор можно изменить." : "Нажмите вариант и подтвердите его в защищённом окне."];
  }
  if (["discussion", "discussion_type", "paused"].includes(session.stage)) {
    return ["Пауза", "Голосование приостановлено", session.pause_reason || "Дождитесь возвращения заседания к этапу голосования."];
  }
  if (session.stage === "finalizing") {
    return ["Фиксация", "Бюллетень закрыт", "T-Mod фиксирует результат. Изменить голос уже нельзя."];
  }
  if (["after_result", "finished"].includes(session.stage)) {
    return ["Зафиксирован", viewer.vote ? "Ваш голос учтён" : "Голосование завершено", "Результат сохранён в протоколе заседания."];
  }
  return [session.stage_label || "Ожидание", "Бюллетень закрыт", "Голосование сейчас недоступно."];
}

function showBallotNotice(message, kind = "success") {
  const notice = byId("ballot-notice");
  clearTimeout(ballotNoticeTimer);
  notice.textContent = message;
  notice.className = `ballot-notice ${kind}`;
  notice.hidden = false;
  ballotNoticeTimer = setTimeout(() => {
    notice.hidden = true;
  }, 5000);
}

function renderPostSessionMonitor(session) {
  const results = session?.results || [];
  const panel = byId("post-session-monitor");
  panel.hidden = !session;
  if (!session) return;
  text("post-session-title", `№ ${session.plenary_number} · Итоговый монитор`);
  text("post-session-total", results.length);
  text("post-session-accepted", results.filter((item) => item.status === "accepted").length);
  text("post-session-rejected", results.filter((item) => ["rejected", "vetoed"].includes(item.status)).length);
  const started = new Date(session.created_at || 0).getTime();
  const finished = new Date(session.finished_at || Date.now()).getTime();
  const durationMinutes = Number.isFinite(started) && Number.isFinite(finished) && finished >= started
    ? Math.max(1, Math.round((finished - started) / 60000))
    : 0;
  text("post-session-duration", durationMinutes ? `${durationMinutes} мин.` : "—");
  const report = byId("post-session-report");
  report.href = `/api/reports/${encodeURIComponent(session.key)}/consensus.pdf`;
  const list = byId("post-session-results");
  clearNode(list);
  results.forEach((result) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `post-session-result ${result.status || ""}`;
    const number = document.createElement("span");
    const title = document.createElement("strong");
    const outcome = document.createElement("em");
    number.textContent = `№${formatNumber(result.bill_number)}`;
    title.textContent = result.title || "Законопроект";
    outcome.textContent = `${RESULT_LABELS[result.status] || result.status || "решение"} · ${formatPercent(result.overall_percent)}`;
    row.append(number, title, outcome);
    row.addEventListener("click", () => openBillRecord(result));
    list.append(row);
  });
}

function renderBallot(data) {
  const viewer = data.viewer || {};
  const postSession = Boolean(viewer.post_session && data.last_session);
  const session = postSession ? data.last_session : data.session;
  const bill = session?.current_bill || null;
  const result = currentResult(session, bill);
  const [stageLabel, title, detail] = ballotStageCopy(session, viewer);
  const canVote = Boolean(viewer.can_vote && session?.stage === "voting" && bill);
  const vote = String(viewer.vote || "");
  const ballotScreen = byId("ballot-screen");
  byId("post-session-monitor").hidden = !postSession;
  byId("ballot-phase-rail").hidden = postSession;
  ballotScreen.querySelector(".ballot-grid").hidden = postSession;
  ballotScreen.querySelector(".senator-timebar").hidden = postSession;
  ballotScreen.querySelector(".senator-studio").hidden = postSession;
  renderPostSessionMonitor(postSession ? session : null);
  const nextBillKey = bill
    ? `${session?.key || "session"}:${bill.id || bill.bill_number}`
    : `${session?.key || "waiting"}:waiting`;
  if (nextBillKey !== senatorBillKey) {
    senatorBillKey = nextBillKey;
    senatorBillStartedAt = Date.now();
    const notes = byId("senator-notes");
    notes.value = localStorage.getItem(`t-consensus-notes:${senatorBillKey}`) || "";
    text("senator-notes-status", notes.value ? "заметки восстановлены" : "сохранено локально");
    clearSenatorCanvas();
  }
  const phaseIndex = renderBallotPhases(session, vote);
  if (viewer.ballot_available) maybePlayConsensusCue(session, bill);
  ballotScreen.dataset.phase = String(Math.max(0, phaseIndex));
  ballotScreen.dataset.vote = vote || "none";
  byId("ballot-bill-card").dataset.number = bill ? formatNumber(bill.bill_number) : "000";

  text("ballot-eyebrow", session ? `МЕСТО СЕНАТОРА · ЗАСЕДАНИЕ ${session.plenary_number}` : "МЕСТО СЕНАТОРА");
  text("ballot-heading", session ? "Ваше решение имеет вес" : "Место ожидает заседание");
  text("ballot-session-detail", session ? `Ведущий: ${session.leader.name} · ${session.stage_label}` : "Когда заседание начнётся, проект появится здесь автоматически.");
  text("ballot-identity", `${viewer.name || "Участник"} · личность подтверждена`);
  text("ballot-identity-mark", (viewer.name || "T").trim().slice(0, 1).toUpperCase());
  text("ballot-bill-number", bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН");
  text("ballot-bill-title", bill?.title || "Между законопроектами");
  text("ballot-bill-author", bill?.author?.name || "—");
  text("ballot-bill-category", categoryLabel(bill?.decision_category));
  text("ballot-bill-threshold", formatPercent(result?.required_percent || session?.rules?.acceptance_percent));
  text("ballot-bill-summary", bill?.summary || "Текст появится после представления законопроекта.");
  text("ballot-stage", stageLabel);
  byId("ballot-stage").className = `ballot-stage ${canVote ? "open" : vote ? "recorded" : "waiting"}`;
  text("ballot-console-title", title);
  text("ballot-console-detail", detail);
  text("ballot-lock-indicator", canVote ? "открыт" : "закрыт");
  byId("ballot-lock-indicator").className = `ballot-lock-indicator ${canVote ? "open" : "locked"}`;

  const agenda = agendaMetrics(data, session, bill);
  const quorumConfirmed = Number(session?.quorum?.confirmed || 0);
  const quorumInvited = Number(session?.quorum?.invited || 0);
  text("ballot-seat-state", viewer.confirmed ? "Подтверждён" : "Ожидает подтверждения");
  text(
    "ballot-seat-detail",
    viewer.confirmed
      ? "Вы включены в состав и можете голосовать после открытия воута."
      : "Подтвердите участие через Discord, чтобы получить бюллетень.",
  );
  text("ballot-agenda-progress", agenda.total ? `${agenda.position} / ${agenda.total}` : "0 / 0");
  text(
    "ballot-agenda-detail",
    bill
      ? `Сейчас рассматривается проект №${formatNumber(bill.bill_number)}.`
      : session ? "Ведущий готовит следующий проект." : "Заседание ещё не началось.",
  );
  text(
    "ballot-quorum-status",
    session ? `${quorumConfirmed} из ${quorumInvited} · ${session.quorum?.ready ? "готов" : "собирается"}` : "—",
  );
  const decisionHint = vote
    ? ["Голос сохранён", "До закрытия воута вы можете изменить свой выбор."]
    : canVote
      ? ["Примите решение", "Сверьте текст, порог и последствия, затем подтвердите вариант."]
      : session?.stage === "presentation"
        ? ["Изучите проект", "Голосование откроет ведущий после представления."]
        : ["Следите за заседанием", "Панель обновится автоматически при смене этапа."];
  text("ballot-decision-hint", decisionHint[0]);
  text("ballot-decision-detail", decisionHint[1]);

  const materialsWrap = byId("ballot-bill-materials-wrap");
  const materials = String(bill?.materials || "").trim();
  materialsWrap.hidden = !materials;
  text("ballot-bill-materials", materials || "—");

  const deadline = byId("ballot-deadline");
  const timerValue = formatTimer(session?.timer_deadline);
  text("ballot-timer", timerValue);
  text(
    "ballot-timer-caption",
    canVote
      ? session?.timer_deadline ? "ДО ЗАКРЫТИЯ" : "ВРЕМЯ НЕ ОГРАНИЧЕНО"
      : session?.stage === "presentation" ? "ДО ОТКРЫТИЯ ВОУТА" : "СРОК РЕШЕНИЯ",
  );
  deadline.className = `ballot-deadline ${canVote ? "open" : "waiting"}`;
  const ballotAdjustment = byId("ballot-time-adjustment");
  ballotAdjustment.hidden = !Number(session?.timer_added_seconds || 0);
  ballotAdjustment.textContent = session?.timer_added_seconds
    ? `${formatAddedTime(session.timer_added_seconds)} добавлено ведущим · новый срок уже действует`
    : "";
  const totalSeconds = Number(session?.timer_seconds || 0);
  const remainingSeconds = session?.timer_deadline
    ? Math.max(0, (new Date(session.timer_deadline).getTime() - Date.now()) / 1000)
    : 0;
  const timerProgress = totalSeconds > 0
    ? Math.max(0, Math.min(1, remainingSeconds / totalSeconds))
    : 0;
  deadline.style.setProperty("--timer-progress", String(timerProgress));

  const expected = Number(session?.voting?.expected || 0);
  const received = Number(session?.voting?.received || 0);
  const progress = expected > 0 ? Math.max(0, Math.min(100, received / expected * 100)) : 0;
  text("ballot-progress-label", `${received} / ${expected}`);
  byId("ballot-progress-bar").style.width = `${progress}%`;

  const openBill = byId("ballot-open-bill");
  openBill.disabled = !bill;
  openBill.onclick = bill ? () => showBillDialog(bill, result) : null;
  const sourceLink = byId("ballot-source-link");
  sourceLink.hidden = !bill?.source_url;
  if (bill?.source_url) sourceLink.href = bill.source_url;

  document.querySelectorAll("#ballot-choices [data-vote]").forEach((button) => {
    const selected = button.dataset.vote === vote;
    button.disabled = !canVote || commanding;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  text("ballot-vote-value", vote ? `Ваш выбор: ${VOTE_LABELS[vote] || vote}` : "Голос ещё не подан");
  text(
    "ballot-receipt-detail",
    vote && session
      ? `Сохранено в журнале T-Mod · проект ${formatNumber(bill?.bill_number)} · r${session.revision}`
      : "После выбора здесь появится защищённая квитанция",
  );
  const receipt = byId("ballot-receipt");
  if (vote && vote !== renderedBallotVote) {
    receipt.classList.remove("recorded", "seal-arrival");
    void receipt.offsetWidth;
    receipt.classList.add("recorded", "seal-arrival");
  } else {
    receipt.classList.toggle("recorded", Boolean(vote));
  }
  renderedBallotVote = vote;
}

function openVoteConfirmation(vote) {
  if (!state?.viewer?.can_vote || commanding || !VOTE_LABELS[vote]) return;
  pendingBallotVote = vote;
  text("vote-confirm-choice", `«${VOTE_LABELS[vote]}»`);
  const dialog = byId("vote-confirm-dialog");
  dialog.dataset.vote = vote;
  text("vote-confirm-submit", `Подтвердить: ${VOTE_LABELS[vote]}`);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeVoteConfirmation() {
  const dialog = byId("vote-confirm-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  pendingBallotVote = null;
}

function actionButton(label, action, payload = {}, options = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `control-button ${options.kind || "secondary"}`;
  button.textContent = label;
  button.disabled = commanding;
  button.addEventListener("click", async () => {
    let commandPayload = { ...payload };
    if (options.confirm && !window.confirm(options.confirm)) return;
    if (options.reason) {
      const reason = window.prompt(
        "Причина паузы:",
        "Консенсус приостановлен ведущим.",
      );
      if (reason === null) return;
      commandPayload.reason = reason;
    }
    if (options.currentRoster) {
      const unconfirmed = state?.session?.participants
        ?.filter((participant) => !participant.confirmed)
        .map((participant) => participant.name) || [];
      if (unconfirmed.length) {
        const accepted = window.confirm(
          `Не подтвердились: ${unconfirmed.join(", ")}.\n\nНачать текущим составом?`,
        );
        if (!accepted) return;
        commandPayload.confirm_current_roster = true;
      }
    }
    await sendCommand(action, commandPayload);
  });
  return button;
}

function controlGroup(title, description = "") {
  const group = document.createElement("div");
  group.className = "control-group";
  const heading = document.createElement("div");
  const strong = document.createElement("strong");
  const small = document.createElement("small");
  strong.textContent = title;
  small.textContent = description;
  heading.append(strong, small);
  const actions = document.createElement("div");
  actions.className = "control-actions";
  group.append(heading, actions);
  return { group, actions };
}

function renderControls(data) {
  const panel = byId("operator-panel");
  const container = byId("operator-controls");
  const capabilities = new Set(
    (data.capabilities || []).filter((action) => action !== "participant_vote"),
  );
  const viewer = data.viewer || {};
  const session = data.session;
  panel.hidden = capabilities.size === 0;
  document.body.classList.toggle("operator-visible", !panel.hidden);
  const nextSignature = stableSignature({
    mode: selectedMode,
    capabilities: [...capabilities].sort(),
    commanding,
    session_key: session?.key || "",
    stage: session?.stage || "",
    stage_label: session?.stage_label || "",
    bill_id: session?.current_bill?.id || null,
    viewer: viewer.name || "",
  });
  if (nextSignature === controlsSignature) return;
  controlsSignature = nextSignature;
  clearNode(container);
  if (panel.hidden) return;

  text("operator-eyebrow", selectedMode === "simulation" ? "УЧЕБНЫЙ ПУЛЬТ" : "ПУЛЬТ ВЕДУЩЕГО");
  text("operator-title", session ? `Этап: ${session.stage_label}` : "Подготовка заседания");
  text(
    "operator-description",
    selectedMode === "simulation"
      ? "Команды управляют изолированной симуляцией и не затрагивают рабочую базу."
      : "Команды проходят через тот же координатор и сразу отражаются в Discord.",
  );
  text("operator-lock", `доступ: ${viewer.name || "ведущий"}`);

  const scriptGroup = controlGroup(
    "Живой сценарий",
    "Реплики, следующий шаг и вся повестка обновляются автоматически.",
  );
  const scriptLink = document.createElement("a");
  scriptLink.className = "control-button primary host-script-link";
  scriptLink.href = `/host?mode=${encodeURIComponent(selectedMode || "live")}`;
  scriptLink.target = "_blank";
  scriptLink.rel = "noreferrer";
  scriptLink.textContent = "Открыть суфлёр ↗";
  scriptGroup.actions.append(scriptLink);
  container.append(scriptGroup.group);

  if (capabilities.has("open_registration")) {
    const { group, actions } = controlGroup(
      "Новое заседание",
      "Система повторно проверит очередь, голосовой канал и кворум.",
    );
    actions.append(actionButton("Подготовить заседание", "open_registration", {}, { kind: "primary" }));
    container.append(group);
  }

  if (capabilities.has("confirm_next") || capabilities.has("confirm_all")) {
    const { group, actions } = controlGroup("Учебная регистрация", "Подтверждения фейковых участников");
    if (capabilities.has("confirm_next")) actions.append(actionButton("Следующее подтверждение", "confirm_next"));
    if (capabilities.has("confirm_all")) actions.append(actionButton("Подтвердить всех", "confirm_all", {}, { kind: "primary" }));
    container.append(group);
  }

  if (capabilities.has("start_vote") || capabilities.has("resend_invitations")) {
    const { group, actions } = controlGroup("Регистрация", "Приглашения и переход к первому проекту");
    if (capabilities.has("start_vote")) {
      actions.append(actionButton("Представить первый проект", "start_vote", {}, { kind: "primary", currentRoster: true }));
    }
    if (capabilities.has("resend_invitations")) {
      actions.append(actionButton("Повторить приглашения", "resend_invitations"));
    }
    container.append(group);
  }

  if (capabilities.has("open_vote")) {
    const { group, actions } = controlGroup(
      "Законопроект представлен",
      "Сенаторы видят текст, но кнопки выбора появятся только после открытия воута.",
    );
    actions.append(
      actionButton("Поставить на воут", "open_vote", {}, { kind: "primary" }),
    );
    container.append(group);
  }

  if (capabilities.has("leader_vote")) {
    const { group, actions } = controlGroup("Голос ведущего", "Можно изменить до фиксации результата");
    actions.append(
      actionButton("За", "leader_vote", { vote: "yes" }, { kind: "positive" }),
      actionButton("Против", "leader_vote", { vote: "no" }, { kind: "danger" }),
      actionButton("Воздержаться", "leader_vote", { vote: "abstain" }),
    );
    container.append(group);
  }

  if (capabilities.has("set_timer")) {
    const extending = Boolean(session?.timer_deadline && new Date(session.timer_deadline).getTime() > Date.now());
    const { group, actions } = controlGroup(
      extending ? "Добавить время" : "Запустить таймер",
      extending
        ? "Продлевает действующий срок без сброса уже прошедшего времени"
        : "Автоматическая фиксация по истечении времени",
    );
    [
      ["30 сек", 30],
      ["1 мин", 60],
      ["3 мин", 180],
      ["5 мин", 300],
    ].forEach(([label, seconds]) => {
      actions.append(actionButton(`${extending ? "+" : ""}${label}`, "set_timer", { seconds }));
    });
    container.append(group);
  }

  if (capabilities.has("fake_vote") || capabilities.has("fake_scenario")) {
    const { group, actions } = controlGroup("Фейковые участники", "Проверка маршрутов симулятора");
    if (capabilities.has("fake_vote")) actions.append(actionButton("Следующий голос", "fake_vote"));
    if (capabilities.has("fake_scenario")) {
      actions.append(
        actionButton("Все за", "fake_scenario", { scenario: "accepted" }, { kind: "positive" }),
        actionButton("Спорный", "fake_scenario", { scenario: "mixed" }),
        actionButton("Все против", "fake_scenario", { scenario: "rejected" }, { kind: "danger" }),
      );
    }
    container.append(group);
  }

  if (capabilities.has("request_discussion")) {
    const { group, actions } = controlGroup("Дискуссия", "Учебный запрос дискуссии");
    actions.append(actionButton("Запросить дискуссию", "request_discussion"));
    container.append(group);
  }

  if (capabilities.has("choose_discussion")) {
    const { group, actions } = controlGroup("Тип дискуссии", "Будет создан штатный канал Discord");
    ["Правовая", "Фактическая", "Процедурная", "Иная"].forEach((discussionType) => {
      actions.append(actionButton(discussionType, "choose_discussion", { discussion_type: discussionType }));
    });
    container.append(group);
  }

  if (capabilities.has("oral_result")) {
    const { group, actions } = controlGroup("Учебный устный итог", "Используется только в симуляции");
    actions.append(
      actionButton("Устно принято", "oral_result", { status: "accepted" }, { kind: "positive" }),
      actionButton("Устно отклонено", "oral_result", { status: "rejected" }, { kind: "danger" }),
    );
    container.append(group);
  }

  const lifecycle = controlGroup("Управление этапом", "Безопасные переходы текущего заседания");
  if (capabilities.has("finalize_vote")) {
    lifecycle.actions.append(actionButton(
      "Завершить голосование",
      "finalize_vote",
      { confirm: true },
      { confirm: "Зафиксировать текущий результат досрочно?", kind: "primary" },
    ));
  }
  if (capabilities.has("retry_finalization")) {
    lifecycle.actions.append(actionButton("Повторить фиксацию", "retry_finalization", {}, { kind: "primary" }));
  }
  if (capabilities.has("end_discussion")) {
    lifecycle.actions.append(actionButton("Завершить дискуссию", "end_discussion", {}, { kind: "positive" }));
  }
  if (capabilities.has("pause")) {
    lifecycle.actions.append(actionButton("Пауза", "pause", {}, { reason: true }));
  }
  if (capabilities.has("resume")) {
    lifecycle.actions.append(actionButton("Продолжить", "resume", {}, { kind: "positive" }));
  }
  if (capabilities.has("next_bill")) {
    lifecycle.actions.append(actionButton("Следующий проект", "next_bill", {}, { kind: "primary" }));
  }
  if (capabilities.has("veto")) {
    lifecycle.actions.append(actionButton(
      "Применить вето",
      "veto",
      { confirm: true },
      { confirm: "Применить право вето к текущему проекту?", kind: "danger" },
    ));
  }
  if (capabilities.has("cancel_session")) {
    lifecycle.actions.append(actionButton(
      "Отменить регистрацию",
      "cancel_session",
      { confirm: true },
      { confirm: "Отменить заседание без итогового протокола?", kind: "danger" },
    ));
  }
  if (capabilities.has("finish_session")) {
    lifecycle.actions.append(actionButton(
      "Завершить заседание",
      "finish_session",
      { confirm: true },
      { confirm: "Завершить всё заседание и сформировать итоговый протокол?", kind: "danger" },
    ));
  }
  if (lifecycle.actions.childElementCount) container.append(lifecycle.group);
}

function render(data) {
  state = data;
  renderMode(data);
  renderViewer(data);
  applyVisualState(data);
  renderSchedule(data);
  const experience = renderExperience(data);
  const privilegedControls = (data.capabilities || []).some(
    (action) => action !== "participant_vote",
  );
  const legacyOperatorMode = selectedMode === "simulation" && privilegedControls;
  const broadcastMode = !experience.ballotMode && !legacyOperatorMode;
  document.body.classList.toggle("observer-screen-mode", false);
  document.body.classList.toggle("broadcast-screen-mode", broadcastMode);
  byId("broadcast-screen").hidden = !broadcastMode;
  byId("observer-screen").hidden = true;
  renderBroadcast(data);
  renderBallot(data);
  renderControls(data);
  const session = data.session;
  const active = Boolean(data.active && session);
  text("queue-value", data.queue.length);
  renderList("queue-list", data.queue, "queue");
  renderList(
    "results-list",
    session?.results?.length ? session.results : data.recent_results,
    "results",
  );
  text("updated-at", `обновлено ${new Date(data.updated_at).toLocaleTimeString("ru-RU")}`);

  if (!session) {
    text("session-eyebrow", selectedMode === "simulation" ? "СИМУЛЯТОР ГОТОВ" : "СИСТЕМА ГОТОВА");
    text("session-title", selectedMode === "simulation" ? "Симуляция не запущена" : "Консенсус не проводится");
    text(
      "session-subtitle",
      selectedMode === "simulation"
        ? "Запустите симулятор в панели администрирования Discord."
        : "Панель ожидает начало следующего пленарного заседания.",
    );
    text("stage-badge", "Ожидание");
    byId("stage-badge").className = "stage-badge stage-idle idle";
    text("quorum-value", "—");
    text("quorum-detail", "нет активной сессии");
    text("votes-value", "—");
    text("timer-value", "—");
    text("timer-detail", "таймер не запущен");
    text("bill-title", "Проект не выбран");
    text("bill-number", "—");
    byId("bill-link").hidden = true;
    byId("admin-open-current-bill").disabled = true;
    byId("discussion-link").hidden = true;
    renderBlocks({});
    renderParticipants([]);
    renderObserver(data);
    return;
  }

  text(
    "session-eyebrow",
    selectedMode === "simulation"
      ? "УЧЕБНЫЙ КОНСЕНСУС · СИНХРОНИЗИРОВАН С DISCORD"
      : `ПЛЕНАРНЫЙ КОНСЕНСУС · ${session.plenary_number}`,
  );
  text(
    "session-title",
    active
      ? selectedMode === "simulation" ? "Симуляция в процессе" : "Заседание в процессе"
      : selectedMode === "simulation" ? "Симуляция завершена" : "Заседание завершено",
  );
  const stageContext = session.pause_reason
    ? ` · ${session.pause_reason}`
    : session.discussion?.type
      ? ` · ${session.discussion.type} дискуссия`
      : "";
  text("session-subtitle", `Ведущий: ${session.leader.name} · обновление в реальном времени${stageContext}`);
  text("stage-badge", session.stage_label);
  byId("stage-badge").className = `stage-badge stage-${visualStage(session)}${active ? "" : " idle"}`;
  text("quorum-value", `${session.quorum.confirmed}/${session.quorum.invited}`);
  text(
    "quorum-detail",
    session.quorum.ready
      ? `кворум подтверждён · ${session.quorum.percent}%`
      : `кворум ещё не собран · ${session.quorum.percent}%`,
  );
  text("votes-value", `${session.voting.received}/${session.voting.expected}`);
  text(
    "votes-detail",
    data.viewer?.leader
      ? "направления доступны только ведущему"
      : "направления скрыты до результата",
  );
  text("timer-value", formatTimer(session.timer_deadline));
  text(
    "timer-detail",
    session.timer_deadline
      ? `до автоматической фиксации${session.timer_added_seconds ? ` · добавлено ${formatAddedTime(session.timer_added_seconds)}` : ""}`
      : "таймер не запущен",
  );

  const bill = session.current_bill;
  const result = currentResult(session, bill);
  text("bill-title", bill?.title || "Между законопроектами");
  text("bill-number", bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН");
  const adminOpenBill = byId("admin-open-current-bill");
  adminOpenBill.disabled = !bill;
  adminOpenBill.onclick = bill
    ? () => showBillDialog(bill, result)
    : null;
  const billLink = byId("bill-link");
  billLink.hidden = !bill?.source_url;
  if (bill?.source_url) billLink.href = bill.source_url;
  const discussionLink = byId("discussion-link");
  discussionLink.hidden = !session.discussion?.channel_url;
  if (session.discussion?.channel_url) {
    discussionLink.href = session.discussion.channel_url;
  }
  renderBlocks(session.blocks);
  renderParticipants(session.participants);
  renderObserver(data);
}

function senatorCanvasContext() {
  const canvas = byId("senator-canvas");
  const context = canvas?.getContext?.("2d");
  if (!context) return null;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.lineWidth = 3;
  context.strokeStyle = "rgba(219, 245, 224, .88)";
  return context;
}

function clearSenatorCanvas() {
  const canvas = byId("senator-canvas");
  const context = senatorCanvasContext();
  if (canvas && context) context.clearRect(0, 0, canvas.width, canvas.height);
}

function senatorCanvasPoint(event) {
  const canvas = byId("senator-canvas");
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) * (canvas.width / rect.width),
    y: (event.clientY - rect.top) * (canvas.height / rect.height),
  };
}

function initializeSenatorCanvas() {
  if (senatorCanvasReady) return;
  const canvas = byId("senator-canvas");
  if (!canvas) return;
  senatorCanvasReady = true;
  canvas.addEventListener("pointerdown", (event) => {
    senatorCanvasDrawing = true;
    senatorCanvasLast = senatorCanvasPoint(event);
    canvas.setPointerCapture?.(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!senatorCanvasDrawing || !senatorCanvasLast) return;
    const context = senatorCanvasContext();
    const point = senatorCanvasPoint(event);
    context.beginPath();
    context.moveTo(senatorCanvasLast.x, senatorCanvasLast.y);
    context.lineTo(point.x, point.y);
    context.stroke();
    senatorCanvasLast = point;
  });
  const finish = () => {
    senatorCanvasDrawing = false;
    senatorCanvasLast = null;
  };
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);
  canvas.addEventListener("pointerleave", finish);
}

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchState({ first = false, blocking = false } = {}) {
  if (fetching || commanding) return;
  fetching = true;
  if (first || blocking) setDataLoading(true);
  try {
    const query = selectedMode ? `?mode=${encodeURIComponent(selectedMode)}` : "";
    const response = await fetch(`/api/state${query}`, {
      headers: authHeaders(),
      credentials: "same-origin",
      cache: "no-store",
      signal: requestTimeoutSignal(10000),
    });
    if (response.status === 401) {
      sessionStorage.removeItem("t-consensus-token");
      token = "";
      login.hidden = false;
      dashboard.hidden = true;
      if (first) loginError.textContent = "Откройте персональную ссылку в Discord.";
      setConnection("offline", "требуется вход");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const nextSignature = payloadSignature(payload);
    if (!first && stateSignature && nextSignature !== stateSignature && document.hidden) {
      globalThis.TModTabSignal?.pulse("Консенсус обновлён");
    }
    if (first || nextSignature !== stateSignature) {
      render(payload);
      stateSignature = nextSignature;
    }
    login.hidden = true;
    dashboard.hidden = false;
    loginError.textContent = "";
    setConnection("online", "обновляется");
    if (!first && payload.cache_state === "stale") {
      setTimeout(() => void fetchFreshState(), 0);
    }
    if (
      first
      && Number.isInteger(requestedBillId)
      && requestedBillId > 0
    ) {
      const billId = requestedBillId;
      requestedBillId = 0;
      await openBillRecord({ id: billId });
    }
  } catch (error) {
    setConnection("offline", "связь потеряна");
    if (first) loginError.textContent = "Панель недоступна. Проверьте контейнер T-Mod, Caddy и DNS.";
  } finally {
    fetching = false;
    if (first || blocking) setDataLoading(false);
  }
}

async function fetchFreshState() {
  if (fetching || commanding || dashboard.hidden) return;
  fetching = true;
  try {
    const params = new URLSearchParams();
    if (selectedMode) params.set("mode", selectedMode);
    params.set("fresh", "1");
    const response = await fetch(`/api/state?${params.toString()}`, {
      headers: authHeaders(),
      credentials: "same-origin",
      cache: "no-store",
      signal: requestTimeoutSignal(10000),
    });
    if (!response.ok) return;
    const payload = await response.json();
    const nextSignature = payloadSignature(payload);
    if (nextSignature !== stateSignature) {
      render(payload);
      stateSignature = nextSignature;
    }
    setConnection("online", "обновляется");
  } catch {
    // Keep the instant stale projection; the normal poll remains active.
  } finally {
    fetching = false;
  }
}

async function sendCommand(action, payload = {}) {
  if (!state?.viewer?.authenticated || commanding) return;
  commanding = true;
  renderControls(state);
  setConnection("", "выполняется");
  const session = state.session;
  const idempotencyKey = window.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}-command`;
  try {
    const response = await fetch("/api/command", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.viewer.csrf_token || "",
        "X-Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        mode: selectedMode,
        action,
        session_key: session?.key || "",
        revision: session?.revision || 0,
        bill_id: session?.current_bill?.id ?? null,
        payload,
      }),
      signal: requestTimeoutSignal(90000),
    });
    const result = await response.json();
    if (!response.ok) {
      showCommandMessage(result.message || "Команда не выполнена.", "error");
      if (["participant_vote", "leader_vote"].includes(action)) {
        showBallotNotice(result.message || "Голос не принят. Бюллетень обновляется.", "error");
      }
      if (response.status === 401 || response.status === 403) await fetchState();
      else setTimeout(fetchState, 150);
      return;
    }
    render(result.state);
    showCommandMessage(result.message || "Команда выполнена.");
    if (["participant_vote", "leader_vote"].includes(action)) {
      showBallotNotice(result.message || "Голос принят.");
    }
    setConnection("online", "обновляется");
  } catch (error) {
    showCommandMessage("Связь прервалась. Состояние будет проверено автоматически.", "error");
    setConnection("offline", "проверка состояния");
    if (["participant_vote", "leader_vote"].includes(action)) {
      showBallotNotice("Связь прервалась. Не повторяйте выбор — T-Mod проверит запись автоматически.", "error");
    }
    setTimeout(fetchState, 500);
  } finally {
    commanding = false;
    if (state) {
      renderControls(state);
      renderBallot(state);
    }
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  token = tokenInput.value.trim();
  sessionStorage.setItem("t-consensus-token", token);
  await fetchState({ first: true });
});

document.querySelectorAll("#mode-switch button").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.disabled || button.dataset.mode === selectedMode) return;
    selectedMode = button.dataset.mode || "live";
    sessionStorage.setItem("t-consensus-mode", selectedMode);
    libraryItems = [];
    await fetchState({ blocking: true });
  });
});

document.querySelectorAll("#experience-switch [data-experience]").forEach((button) => {
  button.addEventListener("click", () => {
    selectExperience(button.dataset.experience || "broadcast");
  });
});

document.querySelectorAll("#ballot-choices [data-vote]").forEach((button) => {
  button.addEventListener("click", () => openVoteConfirmation(button.dataset.vote || ""));
});
byId("vote-confirm-cancel").addEventListener("click", closeVoteConfirmation);
byId("vote-confirm-submit").addEventListener("click", async () => {
  const vote = pendingBallotVote;
  if (!vote) return;
  closeVoteConfirmation();
  const action = state?.viewer?.leader ? "leader_vote" : "participant_vote";
  await sendCommand(action, { vote });
});
byId("vote-confirm-dialog").addEventListener("click", (event) => {
  if (event.target === byId("vote-confirm-dialog")) closeVoteConfirmation();
});

byId("ballot-sound-toggle").addEventListener("click", () => {
  ballotSoundEnabled = !ballotSoundEnabled;
  localStorage.setItem("t-consensus-sound", ballotSoundEnabled ? "on" : "off");
  renderSoundToggle();
  if (ballotSoundEnabled) playConsensusCue("new-bill");
});
byId("broadcast-sound-toggle").addEventListener("click", () => {
  if (!ambientEnabled || !ambientGain) startConsensusAmbience();
  else stopConsensusAmbience();
});
byId("broadcast-volume").value = String(Math.round(ambientVolume * 100));
byId("broadcast-volume").addEventListener("input", (event) => {
  setAmbientVolume(Number(event.currentTarget.value) / 100);
});
document.addEventListener("pointerdown", () => {
  if (ballotSoundEnabled && !ballotSoundReady) ensureBallotAudio();
}, { once: true, passive: true });

document.querySelectorAll("#observer-tabs [data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    observerTab = button.dataset.tab || "participants";
    sessionStorage.setItem("t-consensus-observer-tab", observerTab);
    if (state) renderObserverFeed(state);
  });
});

byId("open-bill-dialog").addEventListener(
  "click",
  () => showBillDialog(dialogBill, dialogResult),
);
byId("close-bill-dialog").addEventListener("click", closeBillDialog);
byId("bill-dialog-done").addEventListener("click", closeBillDialog);
byId("copy-bill-link").addEventListener("click", copyBillLink);
byId("bill-dialog").addEventListener("click", (event) => {
  if (event.target === byId("bill-dialog")) closeBillDialog();
});
byId("open-bill-library").addEventListener("click", openBillLibrary);
byId("close-bill-library").addEventListener("click", closeBillLibrary);
byId("bill-library-dialog").addEventListener("click", (event) => {
  if (event.target === byId("bill-library-dialog")) closeBillLibrary();
});
byId("bill-library-search").addEventListener("input", renderBillLibrary);
document.querySelectorAll("#bill-library-filters [data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    libraryFilter = button.dataset.filter || "all";
    document.querySelectorAll("#bill-library-filters [data-filter]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderBillLibrary();
  });
});

byId("senator-notes").addEventListener("input", (event) => {
  localStorage.setItem(`t-consensus-notes:${senatorBillKey || "waiting"}`, event.currentTarget.value);
  text("senator-notes-status", "сохранено локально");
});
byId("senator-notes-clear").addEventListener("click", () => {
  byId("senator-notes").value = "";
  localStorage.removeItem(`t-consensus-notes:${senatorBillKey || "waiting"}`);
  text("senator-notes-status", "блокнот очищен");
});
byId("senator-canvas-clear").addEventListener("click", clearSenatorCanvas);
byId("senator-canvas-save").addEventListener("click", () => {
  const link = document.createElement("a");
  link.download = `consensus-notes-${Date.now()}.png`;
  link.href = byId("senator-canvas").toDataURL("image/png");
  link.click();
});
initializeSenatorCanvas();

clockTimer = setInterval(() => {
  const now = new Date();
  const localTime = now.toLocaleTimeString("ru-RU");
  const moscowTime = now.toLocaleTimeString("ru-RU", { timeZone: "Europe/Moscow" });
  text("clock", localTime);
  text("broadcast-local-time", localTime);
  text("broadcast-moscow-time", moscowTime);
  text("ballot-local-time", localTime);
  text("ballot-moscow-time", moscowTime);
  text("broadcast-local-date", now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" }));
  text("broadcast-moscow-date", now.toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow", weekday: "long", day: "numeric", month: "long" }));
  const billElapsedSeconds = Math.max(0, Math.floor((Date.now() - senatorBillStartedAt) / 1000));
  text("ballot-bill-elapsed", `${String(Math.floor(billElapsedSeconds / 60)).padStart(2, "0")}:${String(billElapsedSeconds % 60).padStart(2, "0")}`);
  if (state?.session) {
    const timer = formatTimer(state.session.timer_deadline);
    text("timer-value", timer);
    text("observer-timer", timer);
    text("ballot-timer", timer);
    text("broadcast-bill-timer", timer);
    const totalSeconds = Number(state.session.timer_seconds || 0);
    const remainingSeconds = state.session.timer_deadline
      ? Math.max(0, (new Date(state.session.timer_deadline).getTime() - Date.now()) / 1000)
      : 0;
    byId("ballot-deadline").style.setProperty(
      "--timer-progress",
      String(totalSeconds > 0 ? Math.max(0, Math.min(1, remainingSeconds / totalSeconds)) : 0),
    );
  }
  if (state?.schedule) {
    text("observer-schedule-countdown", formatScheduleCountdown(state.schedule.scheduled_for));
    text("broadcast-countdown", exactCountdown(state.schedule.scheduled_for));
    const scheduleSeconds = Math.max(0, Math.floor((new Date(state.schedule.scheduled_for).getTime() - Date.now()) / 1000));
    applyCountdownProfile(countdownWindowFor(scheduleSeconds));
    previousScheduleSeconds = scheduleSeconds;
  } else {
    previousScheduleSeconds = null;
    applyCountdownProfile("normal");
  }
  if (state?.broadcast_phase === "completed" && state.last_session) {
    const completed = state.last_session;
    const resultPage = renderBroadcastResults(completed.results || []);
    text(
      "broadcast-completed-meta",
      `${completed.results?.length || 0} решений · ведущий ${completed.leader?.name || "—"}${resultPage.pageCount > 1 ? ` · итоги ${resultPage.page}/${resultPage.pageCount}` : ""}`,
    );
  }
}, 1000);

renderSoundToggle();
fetchState({ first: true }).finally(() => schedulePoll());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !dashboard.hidden) {
    void fetchState();
    schedulePoll();
  } else if (document.hidden) {
    clearTimeout(pollTimer);
  }
});
window.addEventListener("pagehide", () => {
  clearTimeout(pollTimer);
  clearInterval(clockTimer);
}, { once: true });
