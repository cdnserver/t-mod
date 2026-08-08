"use strict";

const PAGE_SIZE = 50;
const MEDIA_REFRESH_INTERVAL = 3000;
const GLOBAL_ACTIVITY_INTERVAL = 60000;
const REFRESH_INTERVALS = Object.freeze({
  overview: 20000,
  craft: 20000,
  discord: 20000,
  communications: 30000,
  atlas: 30000,
  minecraft: 30000,
  system: 30000,
  audit: 45000,
  treasury: 45000,
  market: 45000,
  bills: 60000,
  sgl: 60000,
  members: 60000,
  modules: 120000,
});
const NOTIFICATION_SOUND_KEY = "t-control-notification-sound";
const AUTH_ANIMATION_KEY = "t-control-auth-animation-seen";

const sectionMeta = {
  overview: ["ОПЕРАЦИОННАЯ КАРТИНА", "Обзор системы"],
  modules: ["КАРТА ВОЗМОЖНОСТЕЙ", "Все системы T-Mod"],
  audit: ["УНИВЕРСАЛЬНЫЙ ЖУРНАЛ", "Аудит действий"],
  treasury: ["ФИНАНСОВЫЙ КОНТУР", "Казна"],
  craft: ["ПРОИЗВОДСТВЕННЫЙ КОНТУР", "Крафты"],
  market: ["MAJESTIC MARKET", "Рынок RU15"],
  bills: ["LEGISLATION", "Законопроекты"],
  sgl: ["SGL BUREAU", "Бюро СГЛ"],
  members: ["MEMBER DIRECTORY", "Участники"],
  communications: ["COMMUNICATIONS", "Уведомления"],
  media: ["MEDIA CONTROL", "Музыка и голос"],
  profile: ["PERSONAL SPACE", "Мой профиль"],
  discord: ["DISCORD INTELLIGENCE", "Discord-аудит"],
  system: ["SYSTEM CONTROL", "Технический контур"],
  atlas: ["ATLAS CONTROL", "T-Mod Atlas"],
  minecraft: ["MINECRAFT NODE", "Игровой сервер"],
};

const moduleLabels = {
  admin: "Администрирование",
  broadcast: "Оповещения",
  consensus: "Консенсус",
  craft: "Крафт",
  finance: "Казна",
  market: "Маркет",
  profile: "Профили",
  sgl: "СГЛ",
  storage: "Склад",
  tvrs: "ТВРС",
  voice: "Голос",
};

const financeLabels = {
  daily: "Ежедневный отчёт",
  interim: "Промежуточная сверка",
  deposit: "Поступление",
  withdraw: "Расход",
  undo: "Отмена операции",
};

const craftStageLabels = {
  procurement: "Закупка",
  crafting: "Крафт",
  awaiting_output: "Приём продукции",
  listing: "Выставление",
  selling: "Продажа",
  completed: "Завершён",
  cancelled: "Отменён",
};

const craftEventLabels = {
  plan_created: "План создан",
  purchase_added: "Материал закуплен",
  inventory_checked: "Склад проверен",
  batch_started: "Цикл запущен",
  batch_completed: "Цикл завершён",
  output_recorded: "Продукция учтена",
  price_set: "Цена установлена",
  market_listed: "Товар выставлен",
  sale_added: "Продажа учтена",
  action_undone: "Действие отменено",
};

const discordLabels = {
  message: "Сообщение",
  message_edit: "Редактирование",
  message_delete: "Удаление",
  reaction_add: "Реакция +",
  reaction_remove: "Реакция −",
  voice_join: "Вход в голосовой",
  voice_leave: "Выход из голосового",
  voice_move: "Переход в голосовом",
  voice_status: "Статус голоса",
  typing: "Печатает",
  presence: "Присутствие",
  member_update: "Изменение участника",
  member_join: "Вход на сервер",
  member_leave: "Выход с сервера",
  command: "Команда",
};

const numberFormat = new Intl.NumberFormat("ru-RU");
const compactFormat = new Intl.NumberFormat("ru-RU", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
});
const timeFormat = new Intl.DateTimeFormat("ru-RU", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const appState = {
  section: "overview",
  days: 30,
  loading: false,
  overview: null,
  rows: {
    audit: [],
    finance: [],
    craft: [],
    discord: [],
    market: [],
    bills: [],
    sgl: [],
    members: [],
    communications: [],
  },
  offsets: {
    audit: 0,
    finance: 0,
    craft: 0,
    discord: 0,
    market: 0,
    sgl: 0,
    members: 0,
    communications: 0,
  },
  csrfToken: "",
  guildId: null,
  refreshTimer: null,
  globalRefreshTimer: null,
  mediaRefreshTimer: null,
  mediaRefreshing: false,
  autoRefreshing: false,
  forceFresh: false,
  pendingSectionLoad: false,
  toastTimer: null,
  authTimer: null,
  loadingOverlayTimer: null,
  authorized: false,
  allowedSections: [],
  administrator: false,
  authTransitioning: false,
  soundEnabled: true,
  soundUnlocked: false,
  audioContext: null,
  activitySignatures: new Map(),
  loadedSections: new Set(),
  detailRoute: null,
  openingRoute: false,
  craftRecipes: [],
  operation: null,
};

function byId(id) {
  return document.getElementById(id);
}

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.href) element.href = options.href;
  if (options.target) element.target = options.target;
  if (options.rel) element.rel = options.rel;
  if (options.type) element.type = options.type;
  if (options.title) element.title = options.title;
  if (options.value !== undefined) element.value = options.value;
  if (options.max !== undefined) element.max = options.max;
  if (options.hidden !== undefined) element.hidden = options.hidden;
  for (const child of children) {
    if (child !== null && child !== undefined) element.append(child);
  }
  return element;
}

function replaceChildren(id, children) {
  const target = typeof id === "string" ? byId(id) : id;
  target.replaceChildren(...children);
}

function setText(id, value) {
  const target = byId(id);
  if (target) target.textContent = value === null || value === undefined ? "—" : String(value);
}

function formatNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numberFormat.format(numeric) : "—";
}

function formatCompact(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? compactFormat.format(numeric) : "—";
}

function formatMoney(value, signed = false) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  const sign = signed && numeric > 0 ? "+" : "";
  return `${sign}$${numberFormat.format(numeric)}`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormat.format(date);
}

function formatBytes(value) {
  let numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let unit = 0;
  while (numeric >= 1024 && unit < units.length - 1) {
    numeric /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || numeric >= 10 ? 0 : 1;
  return `${numeric.toFixed(digits)} ${units[unit]}`;
}

function relativeTime(value) {
  if (!value) return "никогда";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
  const ranges = [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
  ];
  for (const [unitSeconds, unit] of ranges) {
    if (Math.abs(seconds) >= unitSeconds) {
      return formatter.format(Math.round(seconds / unitSeconds), unit);
    }
  }
  return formatter.format(seconds, "second");
}

function textPreview(value, fallback = "Без описания") {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  return clean || fallback;
}

function statusPill(label, tone = "") {
  return node("span", {
    className: `status-pill${tone ? ` ${tone}` : ""}`,
    text: label,
  });
}

function storedSoundPreference() {
  try {
    return localStorage.getItem(NOTIFICATION_SOUND_KEY) !== "off";
  } catch {
    return true;
  }
}

function persistSoundPreference(enabled) {
  try {
    localStorage.setItem(NOTIFICATION_SOUND_KEY, enabled ? "on" : "off");
  } catch {
    // Private browsing can make storage unavailable; the in-memory choice still works.
  }
}

function notificationAudioContext() {
  const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AudioContext) return null;
  if (!appState.audioContext) {
    try {
      appState.audioContext = new AudioContext();
    } catch {
      return null;
    }
  }
  return appState.audioContext;
}

async function unlockNotificationSound() {
  if (!appState.soundEnabled) return false;
  const context = notificationAudioContext();
  if (!context) return false;
  try {
    if (context.state === "suspended") await context.resume();
    appState.soundUnlocked = context.state === "running";
  } catch {
    appState.soundUnlocked = false;
  }
  return appState.soundUnlocked;
}

function playNotificationSound(tone = "update") {
  if (!appState.soundEnabled || !appState.soundUnlocked) return;
  const context = notificationAudioContext();
  if (!context || context.state !== "running") return;
  try {
    const frequencies =
      tone === "error" ? [310, 220] : tone === "success" ? [520, 740] : [440, 620];
    const startedAt = context.currentTime;
    frequencies.forEach((frequency, index) => {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const start = startedAt + index * 0.085;
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(frequency, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.055, start + 0.018);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.16);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.18);
    });
  } catch {
    appState.soundUnlocked = false;
  }
}

function updateNotificationToggle() {
  const toggle = byId("notification-toggle");
  if (!toggle) return;
  toggle.setAttribute("aria-pressed", String(appState.soundEnabled));
  toggle.title = appState.soundEnabled
    ? "Звуковые уведомления включены"
    : "Звуковые уведомления выключены";
  setText("notification-label", appState.soundEnabled ? "Звук" : "Без звука");
}

function showToast(message, error = false, options = {}) {
  const toast = byId("toast");
  const tone = error ? "error" : options.tone || "";
  setText("toast-icon", options.icon || (error ? "!" : "✓"));
  setText("toast-title", options.title || (error ? "Не удалось" : "Готово"));
  setText("toast-message", message);
  setText("toast-time", timeFormat.format(new Date()));
  toast.className = `toast${tone ? ` ${tone}` : ""}`;
  toast.hidden = false;
  if (options.sound) playNotificationSound(error ? "error" : options.sound);
  clearTimeout(appState.toastTimer);
  appState.toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, options.duration || 5200);
}

function compactActivityRows(items) {
  return (Array.isArray(items) ? items : []).slice(0, 16).map((item) => [
    item.id ?? item.bill_id ?? item.item_id ?? item.user_id ?? null,
    item.status ?? item.stage ?? item.event_kind ?? item.event_type ?? null,
    item.updated_at ?? item.created_at ?? item.at ?? item.source_updated_at ?? null,
    item.average_price ?? item.amount ?? item.total_count ?? null,
  ]);
}

function activitySignature(section, data) {
  if (!data || typeof data !== "object") return null;
  if (section === "overview") {
    const counts = data.counts || {};
    return JSON.stringify({
      counts: [
        counts.active_crafts,
        counts.actions,
        counts.active_actions,
        counts.overdue_batches,
        counts.outbox_dead,
        counts.outbox_open,
        counts.finance_retries,
      ],
      audit: compactActivityRows(data.audit?.items),
      discord: compactActivityRows(data.discord?.events?.items),
      craft: compactActivityRows(data.craft?.active_plans),
    });
  }
  if (section === "audit") return JSON.stringify(compactActivityRows(data.items));
  if (section === "treasury") return JSON.stringify(compactActivityRows(data.items));
  if (section === "craft") {
    return JSON.stringify({
      events: compactActivityRows(data.events?.items),
      plans: compactActivityRows(data.active_plans),
    });
  }
  if (section === "market") {
    return JSON.stringify({
      items: compactActivityRows(data.items),
      alerts: compactActivityRows(data.my_alerts),
      catalog: [
        data.catalog?.last_success_at,
        data.catalog?.last_error,
      ],
    });
  }
  if (section === "bills") {
    return JSON.stringify({
      bills: compactActivityRows(data.items),
      workspaces: compactActivityRows(data.workspaces),
    });
  }
  if (section === "sgl") {
    return JSON.stringify({
      cases: compactActivityRows(data.cases?.items),
      archives: compactActivityRows(data.archives?.items),
    });
  }
  if (section === "members") return JSON.stringify(compactActivityRows(data.items));
  if (section === "communications") {
    return JSON.stringify(compactActivityRows(data.items));
  }
  if (section === "modules") return JSON.stringify(data.registry || {});
  if (section === "system") {
    return JSON.stringify({
      delivery: data.delivery || {},
      outbox: compactActivityRows(data.outbox_status),
      workspaces: compactActivityRows(data.workspaces),
    });
  }
  if (section === "media") {
    return JSON.stringify({
      music: data.music || {},
    });
  }
  return null;
}

function rememberActivity(data, section = appState.section, notify = appState.autoRefreshing) {
  const signature = activitySignature(section, data);
  if (!signature) return true;
  const previous = appState.activitySignatures.get(section);
  appState.activitySignatures.set(section, signature);
  const changed = !previous || previous !== signature;
  if (!notify || !previous || !changed) return changed;
  const sectionTitle = sectionMeta[section]?.[1] || "Ядерный Реактор";
  showToast(
    `В разделе «${sectionTitle}» появились новые данные.`,
    false,
    {
      title: "Обновление в реальном времени",
      icon: "◆",
      tone: "update",
      sound: "update",
    },
  );
  return true;
}

class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || payload?.error || `HTTP ${status}`);
    this.status = status;
    this.payload = payload || {};
  }
}

function requestTimeoutSignal(timeoutMs) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(timeoutMs);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), timeoutMs);
  return controller.signal;
}

async function fetchJSON(path) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal: requestTimeoutSignal(10000),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

function idempotencyKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random()
    .toString(36)
    .slice(2)}`;
}

async function postJSON(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-CSRF-Token": appState.csrfToken,
      "X-Idempotency-Key": idempotencyKey(),
    },
    body: JSON.stringify(body),
    signal: requestTimeoutSignal(90000),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

function operationInput(field) {
  const label = node("label", { className: field.wide ? "wide" : "" });
  label.append(node("span", { text: field.label }));
  let input;
  if (field.type === "select") {
    input = node("select");
    input.append(
      ...(field.options || []).map((option) =>
        node("option", {
          value: option.value,
          text: option.label,
        }),
      ),
    );
  } else if (field.type === "textarea") {
    input = node("textarea");
  } else {
    input = node("input", { type: field.type || "text" });
  }
  input.name = field.name;
  if (field.value !== undefined && field.value !== null) input.value = String(field.value);
  if (field.placeholder) input.placeholder = field.placeholder;
  if (field.required) input.required = true;
  if (field.min !== undefined) input.min = String(field.min);
  if (field.max !== undefined) input.max = String(field.max);
  if (field.step !== undefined) input.step = String(field.step);
  if (field.maxLength) input.maxLength = Number(field.maxLength);
  label.append(input);
  return label;
}

function selectedOperation() {
  const state = appState.operation;
  const action = byId("operation-action").value;
  return state?.operations?.find((item) => item.id === action) || null;
}

function renderOperationFields() {
  const operation = selectedOperation();
  replaceChildren(
    "operation-fields",
    operation ? (operation.fields || []).map(operationInput) : [],
  );
  const warning = byId("operation-warning");
  warning.textContent = operation?.warning || "Операция будет записана в универсальный аудит T-Mod.";
  warning.classList.toggle("danger", operation?.tone === "danger");
  byId("operation-submit").textContent = operation?.accept || "Проверить и выполнить";
}

function openOperation(config) {
  appState.operation = config;
  setText("operation-eyebrow", config.eyebrow || "CONTROLLED OPERATION");
  setText("operation-title", config.title || "Управление");
  setText("operation-mark", config.mark || "◇");
  setText("operation-context-title", config.contextTitle || "Объект T-Mod");
  setText("operation-context-meta", config.contextMeta || "Единое состояние Discord и веб");
  const select = byId("operation-action");
  select.replaceChildren(
    ...config.operations.map((item) => node("option", { value: item.id, text: item.label })),
  );
  select.value = config.defaultAction || config.operations[0]?.id || "";
  renderOperationFields();
  const dialog = byId("operation-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeOperation() {
  const dialog = byId("operation-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  appState.operation = null;
}

async function submitOperation() {
  const state = appState.operation;
  const operation = selectedOperation();
  if (!state || !operation) return;
  const values = formValues("operation-form");
  let body;
  try {
    body = operation.build ? operation.build(values) : { action: operation.id, ...values };
  } catch (error) {
    showToast(error.message || "Проверьте введённые значения.", true);
    return;
  }
  const approved = window.TModReactor?.confirm
    ? await window.TModReactor.confirm({
        title: operation.confirmTitle || operation.label,
        message: operation.confirmMessage || "T-Mod проверит права, состояние объекта и запишет действие в аудит.",
        accept: operation.accept || "Выполнить",
        tone: operation.tone || "warning",
      })
    : globalThis.confirm(operation.confirmTitle || operation.label);
  if (!approved) return;
  byId("operation-submit").disabled = true;
  setLoading(true);
  try {
    const result = await postJSON(state.endpoint, { ...body, confirmed: true });
    closeOperation();
    showToast(result.message || "Операция выполнена.", false, {
      title: "Состояние синхронизировано",
      icon: "✓",
      sound: "success",
    });
    if (result.projection_warning) {
      showToast("Операция сохранена, но карточка Discord обновится повторно через рабочий цикл.", false);
    }
    await state.reload?.();
  } catch (error) {
    handleError(error);
  } finally {
    byId("operation-submit").disabled = false;
    setLoading(false);
  }
}

function buildQuery(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && String(value).trim() !== "") {
      query.set(key, String(value));
    }
  }
  return query.toString();
}

function showGate(message, allowBack = true) {
  clearTimeout(appState.authTimer);
  appState.authorized = false;
  appState.authTransitioning = false;
  const gate = byId("auth-gate");
  const shell = byId("admin-shell");
  shell.hidden = true;
  shell.classList.remove("is-entering");
  gate.hidden = false;
  gate.dataset.state = "error";
  setText("gate-state", "ДОСТУП НЕ ПОДТВЕРЖДЁН");
  setText("gate-message", message);
  byId("gate-back").hidden = !allowBack;
  byId("gate-login").hidden = false;
}

function showApplication(payload) {
  const gate = byId("auth-gate");
  const shell = byId("admin-shell");
  const changed = rememberActivity(payload);
  appState.loadedSections.add(appState.section);
  if (appState.autoRefreshing && !changed) return false;
  if (!appState.authorized) {
    appState.authorized = true;
    appState.authTransitioning = true;
    gate.hidden = false;
    gate.dataset.state = "verified";
    byId("gate-login").hidden = true;
    setText("gate-state", "ЛИЧНОСТЬ И ПРАВА ПОДТВЕРЖДЕНЫ");
    setText("gate-message", "Защищённая сессия готова. Запускаем Ядерный Реактор…");
    shell.hidden = false;
    const reducedMotion = globalThis.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    let authAnimationSeen = false;
    try {
      authAnimationSeen = sessionStorage.getItem(AUTH_ANIMATION_KEY) === "1";
      sessionStorage.setItem(AUTH_ANIMATION_KEY, "1");
    } catch {
      authAnimationSeen = false;
    }
    const delay = reducedMotion || authAnimationSeen ? 60 : 360;
    clearTimeout(appState.authTimer);
    appState.authTimer = setTimeout(() => {
      appState.authTransitioning = false;
      gate.hidden = true;
      shell.classList.add("is-entering");
      setTimeout(() => shell.classList.remove("is-entering"), 560);
    }, delay);
  } else {
    shell.hidden = false;
    if (!appState.authTransitioning) gate.hidden = true;
  }
  if (payload?.viewer) {
    appState.administrator = payload.viewer.administrator === true;
    appState.csrfToken = payload.viewer.csrf_token || appState.csrfToken;
    setText("viewer-name", payload.viewer.name || "Администратор");
    setText(
      "viewer-avatar",
      String(payload.viewer.name || "A").trim().charAt(0).toUpperCase() || "A",
    );
  }
  if (payload?.guild?.id) appState.guildId = Number(payload.guild.id);
  return true;
}

function handleError(error) {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    const message =
      error.payload?.message ||
      (error.status === 403
        ? "Для этого раздела нужны права администратора."
        : "Персональная сессия завершилась. Откройте админ-центр из Discord.");
    showGate(message);
    return;
  }
  if (
    error instanceof ApiError
    && error.status === 429
    && error.payload?.error === "too_many_attempts"
  ) {
    showGate(
      "Защита временно остановила повторные попытки входа. Подождите несколько минут и откройте свежую ссылку из Discord.",
    );
    return;
  }
  console.error(error);
  showToast(`Не удалось обновить данные: ${error.message}`, true);
}

function formValues(formId) {
  const data = new FormData(byId(formId));
  return Object.fromEntries(
    [...data.entries()].map(([key, value]) => [key, String(value).trim()]),
  );
}

function cleanRoutePart(value) {
  return encodeURIComponent(String(value ?? "").trim());
}

function parseAdminRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const parts = raw
    .split("/")
    .filter(Boolean)
    .map((part) => {
      try {
        return decodeURIComponent(part);
      } catch {
        return "";
      }
    });
  const section = sectionMeta[parts[0]] ? parts[0] : "overview";
  if (parts.length >= 3 && parts[1] && parts[2]) {
    return { section, kind: parts[1], key: parts.slice(2).join("/") };
  }
  return { section, kind: "", key: "" };
}

function routeHash(route) {
  const section = sectionMeta[route?.section] ? route.section : "overview";
  if (!route?.kind || !route?.key) return `#/${cleanRoutePart(section)}`;
  return `#/${cleanRoutePart(section)}/${cleanRoutePart(route.kind)}/${cleanRoutePart(
    route.key,
  )}`;
}

function shareURL(route) {
  const url = new URL("/admin", location.origin);
  url.hash = routeHash(route);
  return url.toString();
}

async function copyText(value) {
  if (navigator.clipboard && globalThis.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const helper = node("textarea", { value });
  helper.setAttribute("readonly", "");
  helper.className = "clipboard-helper";
  document.body.append(helper);
  helper.select();
  const copied = document.execCommand("copy");
  helper.remove();
  if (!copied) throw new Error("clipboard_unavailable");
}

async function copyRoute(route, label = "Ссылка скопирована") {
  try {
    await copyText(shareURL(route));
    showToast(label);
  } catch {
    showToast("Не удалось скопировать ссылку. Скопируйте адрес из браузера.", true);
  }
}

function recordRoute(section, kind, key) {
  return {
    section,
    kind: String(kind || "").trim(),
    key: String(key ?? "").trim(),
  };
}

function cardLinkButton(route, label = "Ссылка") {
  const button = node("button", {
    className: "card-link-button",
    type: "button",
    text: `⛓ ${label}`,
    title: "Скопировать постоянную ссылку",
  });
  button.setAttribute("aria-label", "Скопировать ссылку на карточку");
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    copyRoute(route, "Ссылка на карточку скопирована");
  });
  return button;
}

async function switchSection(section, updateHash = true) {
  if (!sectionMeta[section]) section = "overview";
  if (appState.allowedSections.length && !appState.allowedSections.includes(section)) {
    showToast("Для этого раздела доступ не выдан.", true);
    return;
  }
  appState.section = section;
  document.body.dataset.section = section;
  appState.detailRoute = null;
  const dialog = byId("detail-dialog");
  if (dialog?.open) dialog.close();
  document.querySelectorAll("[data-screen]").forEach((screen) => {
    screen.classList.toggle("active", screen.dataset.screen === section);
  });
  document.querySelectorAll("[data-section]").forEach((button) => {
    const active = button.dataset.section === section;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  setText("section-eyebrow", sectionMeta[section][0]);
  setText("section-title", sectionMeta[section][1]);
  if (updateHash) history.replaceState(null, "", routeHash({ section }));
  document.querySelector(".content")?.scrollTo({ top: 0 });
  if (appState.loading) {
    appState.pendingSectionLoad = true;
    scheduleAutoRefresh();
    return;
  }
  if (appState.loadedSections.has(section)) {
    void refreshCurrentSection(true);
    scheduleAutoRefresh();
    return;
  }
  await loadCurrentSection();
  scheduleAutoRefresh();
}

function setLoading(value) {
  appState.loading = value;
  if (!appState.autoRefreshing) {
    byId("refresh-button").disabled = value;
    byId("refresh-button").classList.toggle("loading", value);
    clearTimeout(appState.loadingOverlayTimer);
    if (value && !appState.authorized) {
      appState.loadingOverlayTimer = setTimeout(() => {
        byId("data-loading").hidden = false;
        byId("admin-shell").setAttribute("inert", "");
      }, 220);
    } else {
      byId("data-loading").hidden = true;
      byId("admin-shell").removeAttribute("inert");
    }
  }
  if (!value && appState.pendingSectionLoad) {
    appState.pendingSectionLoad = false;
    queueMicrotask(() => {
      if (appState.loading) return;
      if (appState.loadedSections.has(appState.section)) {
        void refreshCurrentSection(true);
      } else {
        void loadCurrentSection();
      }
    });
  }
}

function auditTimelineItem(item) {
  const route = recordRoute("audit", "audit", item.id);
  const mark = node("span", {
    className: "timeline-mark",
    text: String(item.module || "T").slice(0, 1).toUpperCase(),
  });
  const copy = node("span", { className: "timeline-copy" }, [
    node("strong", { text: item.summary || item.action_kind || "Действие" }),
    node("small", {
      text: `${moduleLabels[item.module] || item.module || "Система"} · ${
        item.actor_display || "T-Mod"
      }`,
    }),
  ]);
  const time = node("time", {
    text: relativeTime(item.created_at),
    title: formatDate(item.created_at),
  });
  const row = node("div", { className: "timeline-item" }, [mark, copy, time]);
  row.tabIndex = 0;
  row.addEventListener("click", () => openDetails("Запись аудита", item, route));
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDetails("Запись аудита", item, route);
    }
  });
  return row;
}

function compactItem(title, subtitle, value, onClick = null) {
  const item = node("div", { className: "compact-item" }, [
    node("span", {}, [
      node("strong", { text: title }),
      node("small", { text: subtitle }),
    ]),
    node("b", { text: value }),
  ]);
  if (onClick) {
    item.tabIndex = 0;
    item.addEventListener("click", onClick);
  }
  return item;
}

function renderOverview(data) {
  appState.overview = data;
  if (!showApplication(data)) return;
  const counts = data.counts || {};
  const finance = data.finance?.stats || {};
  const financeState = data.finance?.state || {};
  const craft = data.craft?.stats || {};
  const discord = data.discord?.stats || {};

  setText("welcome-title", `${data.guild?.name || "Товарищество"} — контур в норме`);
  setText(
    "welcome-subtitle",
    `Сводка за ${data.days} дн. · данные непосредственно из рабочей базы T-Mod`,
  );
  setText("overview-updated", `обновлено ${timeFormat.format(new Date())}`);
  setText("overview-balance", formatMoney(financeState.estimated_balance));
  setText(
    "overview-net",
    `поток за период ${formatMoney(finance.net_flow, true)}`,
  );
  setText("overview-crafts", formatNumber(counts.active_crafts));
  setText(
    "overview-craft-note",
    `${formatNumber(craft.attempts_completed)} попыток завершено`,
  );
  setText("overview-actions", formatCompact(counts.actions));
  setText(
    "overview-action-note",
    `${formatNumber(counts.active_actions)} активных записей`,
  );
  setText("overview-discord", formatCompact(discord.events));
  setText(
    "overview-discord-note",
    `${formatNumber(discord.users)} активных участников`,
  );

  const attention = [];
  if (Number(counts.overdue_batches) > 0) {
    attention.push(`Просроченных циклов крафта: ${counts.overdue_batches}`);
  }
  if (Number(counts.outbox_dead) > 0) {
    attention.push(`Недоставленных сообщений: ${counts.outbox_dead}`);
  }
  if (Number(counts.finance_retries) > 0) {
    attention.push(`Повторных финансовых уведомлений: ${counts.finance_retries}`);
  }
  if (Number(counts.outbox_open) > 0) {
    attention.push(`Сообщений ожидают доставки: ${counts.outbox_open}`);
  }
  const attentionStrip = byId("attention-strip");
  attentionStrip.hidden = attention.length === 0;
  replaceChildren(
    attentionStrip,
    attention.map((message) => node("span", { className: "attention-item", text: message })),
  );

  const actions = data.audit?.items || [];
  replaceChildren(
    "overview-actions-list",
    actions.length
      ? actions.slice(0, 7).map(auditTimelineItem)
      : [node("div", { className: "empty-state", text: "Действий пока нет." })],
  );

  const system = data.system || {};
  const systemHealthy = Boolean(system.connected) && Number(counts.outbox_dead || 0) === 0;
  const indicator = byId("system-indicator");
  indicator.textContent = systemHealthy ? "контур в норме" : "нужно внимание";
  indicator.className = `health-pill${systemHealthy ? "" : " warning"}`;
  const metrics = [
    ["Discord-соединение", system.connected ? "онлайн" : "нет связи"],
    ["Задержка gateway", system.latency_ms === null ? "—" : `${system.latency_ms} мс`],
    ["Участников в кеше", formatNumber(system.members)],
    ["Каналов в кеше", formatNumber(system.channels)],
    ["Очередь доставки", formatNumber(counts.outbox_open)],
    ["Dead-letter", formatNumber(counts.outbox_dead)],
  ];
  replaceChildren(
    "system-metrics",
    metrics.map(([label, value]) =>
      node("div", { className: "metric-row" }, [
        node("span", { text: label }),
        node("b", { text: value }),
      ]),
    ),
  );

  const plans = data.craft?.active_plans || [];
  replaceChildren(
    "overview-craft-list",
    plans.length
      ? plans.slice(0, 6).map((plan) =>
          compactItem(
            `#${plan.id} · ${plan.product_name}`,
            `${craftStageLabels[plan.stage] || plan.stage} · ${plan.responsible?.name || "без ответственного"}`,
            `${formatNumber(plan.attempts_completed)}/${formatNumber(plan.attempts_total)}`,
            () =>
              openDetails(
                `Крафт #${plan.id}`,
                plan,
                recordRoute("craft", "craft-plan", plan.id),
              ),
          ),
        )
      : [node("div", { className: "empty-state", text: "Активных крафтов нет." })],
  );

  const discordItems = data.discord?.events?.items || [];
  replaceChildren(
    "overview-discord-list",
    discordItems.length
      ? discordItems.slice(0, 6).map((item) =>
          compactItem(
            discordLabels[item.event_type] || item.event_type || "Событие",
            `${item.display_name || "Система"} · ${item.channel_name || "без канала"}`,
            relativeTime(item.at),
            () =>
              openDetails(
                "Discord-событие",
                item,
                recordRoute("discord", "discord-event", item.id),
              ),
          ),
        )
      : [node("div", { className: "empty-state", text: "Событий за период нет." })],
  );
}

async function loadOverview(silent = false) {
  if (!silent) setLoading(true);
  try {
    const fresh = appState.forceFresh ? "&fresh=1" : "";
    const data = await fetchJSON(`/api/admin/overview?days=${appState.days}${fresh}`);
    renderOverview(data);
  } catch (error) {
    handleError(error);
  } finally {
    if (!silent) setLoading(false);
  }
}

function fillSelect(selectId, values, labeler) {
  const select = byId(selectId);
  const selected = select.value;
  const first = select.firstElementChild?.cloneNode(true);
  select.replaceChildren();
  if (first) select.append(first);
  for (const value of values) {
    select.append(node("option", { value, text: labeler(value) }));
  }
  select.value = selected;
}

function tableCell(primary, secondary = "", className = "") {
  return node("td", { className }, [
    node("strong", { text: primary }),
    secondary ? node("small", { text: secondary }) : null,
  ]);
}

function interactiveRow(item, title, cells, route = null) {
  const row = node("tr", {}, cells);
  row.tabIndex = 0;
  row.addEventListener("click", (event) => {
    if (event.target.closest("a, button")) return;
    openDetails(title, item, route);
  });
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter") openDetails(title, item, route);
  });
  return row;
}

function emptyTable(bodyId, columns, message) {
  const cell = node("td", { text: message });
  cell.colSpan = columns;
  replaceChildren(bodyId, [node("tr", {}, [cell])]);
}

function renderPagination(targetId, total, offset, load) {
  const target = byId(targetId);
  const start = total ? offset + 1 : 0;
  const end = Math.min(total, offset + PAGE_SIZE);
  const previous = node("button", { type: "button", text: "←", title: "Назад" });
  const next = node("button", { type: "button", text: "→", title: "Вперёд" });
  previous.disabled = offset <= 0;
  next.disabled = offset + PAGE_SIZE >= total;
  previous.addEventListener("click", () => load(Math.max(0, offset - PAGE_SIZE)));
  next.addEventListener("click", () => load(offset + PAGE_SIZE));
  replaceChildren(target, [
    node("span", { text: `${formatNumber(start)}–${formatNumber(end)} из ${formatNumber(total)}` }),
    previous,
    next,
  ]);
}

function renderAudit(data) {
  if (!showApplication(data)) return;
  appState.rows.audit = data.items || [];
  setText("audit-count", `${formatNumber(data.total)} записей`);
  fillSelect("audit-module", data.modules || [], (value) => moduleLabels[value] || value);
  const rows = appState.rows.audit.map((item) =>
    interactiveRow(item, "Запись аудита", [
      tableCell(formatDate(item.created_at), `#${item.id}`),
      node("td", {}, [statusPill(moduleLabels[item.module] || item.module)]),
      tableCell(item.summary || item.action_kind, item.action_kind),
      tableCell(item.actor_display || "T-Mod", item.actor_id ? `ID ${item.actor_id}` : "системное"),
      tableCell(
        item.target_type || "—",
        item.target_id ? `#${item.target_id}` : "без объекта",
      ),
      node("td", {}, [
        statusPill(
          item.status === "undone" ? "отменено" : "активно",
          item.status === "undone" ? "warning" : "",
        ),
      ]),
    ], recordRoute("audit", "audit", item.id)),
  );
  if (rows.length) replaceChildren("audit-table-body", rows);
  else emptyTable("audit-table-body", 6, "По заданным фильтрам записей нет.");
  renderPagination(
    "audit-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadAudit,
  );
}

async function loadAudit(offset = appState.offsets.audit) {
  appState.offsets.audit = offset;
  setLoading(true);
  const filters = formValues("audit-filters");
  try {
    const query = buildQuery({
      ...filters,
      limit: PAGE_SIZE,
      offset,
    });
    renderAudit(await fetchJSON(`/api/admin/actions?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function rankingItem(title, subtitle, value, maxValue = 1) {
  return node("div", { className: "ranking-item" }, [
    node("span", { className: "ranking-copy" }, [
      node("strong", { text: title }),
      node("small", { text: subtitle }),
    ]),
    node("b", { text: value }),
    node("progress", {
      value: Math.max(0, Number(String(value).replace(/\D/g, "")) || 0),
      max: Math.max(1, Number(maxValue) || 1),
    }),
  ]);
}

function renderFinance(data) {
  if (!showApplication(data)) return;
  appState.rows.finance = data.items || [];
  const stats = data.stats || {};
  const latest = data.state || {};
  setText("finance-balance", formatMoney(latest.estimated_balance));
  setText(
    "finance-report-state",
    latest.latest_report
      ? `сверка ${formatDate(latest.latest_report.created_at)} · операций после: ${latest.movements_after_report}`
      : "контрольная сверка ещё не внесена",
  );
  setText("finance-deposits", formatMoney(stats.deposits));
  setText("finance-withdrawals", formatMoney(stats.withdrawals));
  setText(
    "finance-craft-cost",
    `крафт: ${formatMoney(stats.automatic_craft_expenses)}`,
  );
  setText("finance-net", formatMoney(stats.net_flow, true));
  setText(
    "finance-operations",
    `операций: ${formatNumber(stats.movement_count)} · отмен: ${formatNumber(stats.undo_count)}`,
  );

  const reasons = stats.top_reasons || [];
  const reasonMax = Math.max(...reasons.map((item) => Number(item.total || 0)), 1);
  replaceChildren(
    "finance-reasons",
    reasons.length
      ? reasons.map((item) =>
          rankingItem(
            textPreview(item.reason),
            `${formatNumber(item.operations)} операций`,
            formatMoney(item.total),
            reasonMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Причины пока не накоплены." })],
  );

  const actors = stats.top_actors || [];
  const actorMax = Math.max(...actors.map((item) => Number(item.operations || 0)), 1);
  replaceChildren(
    "finance-actors",
    actors.length
      ? actors.map((item) =>
          rankingItem(
            item.actor_display || `Участник ${item.actor_id}`,
            `+${formatMoney(item.deposits)} · −${formatMoney(item.withdrawals)}`,
            `${formatNumber(item.operations)} оп.`,
            actorMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Операторов за период нет." })],
  );

  const rows = appState.rows.finance.map((item) => {
    const kind = item.event_kind;
    const signedAmount =
      kind === "withdraw"
        ? -Number(item.amount || 0)
        : kind === "deposit"
          ? Number(item.amount || 0)
          : Number(item.amount || 0);
    const linkedPlan = item.craft_purchase_plan_id || item.craft_batch_plan_id;
    return interactiveRow(item, `Финансовая операция #${item.id}`, [
      tableCell(formatDate(item.created_at), `#${item.id}`),
      node("td", {}, [
        statusPill(
          financeLabels[kind] || kind,
          kind === "undo" || item.is_undone ? "warning" : "",
        ),
      ]),
      tableCell(
        formatMoney(signedAmount, kind === "deposit"),
        item.delta !== null ? `Δ ${formatMoney(item.delta, true)}` : "",
        signedAmount < 0 ? "amount-negative" : signedAmount > 0 ? "amount-positive" : "",
      ),
      tableCell(formatMoney(item.balance_after), formatMoney(item.balance_before)),
      tableCell(
        textPreview(item.reason, kind === "daily" ? "Ежедневная сверка" : "Без пояснения"),
        linkedPlan ? `связано с крафтом #${linkedPlan}` : item.report_date || "",
      ),
      tableCell(item.actor_display || `ID ${item.actor_id}`, `ID ${item.actor_id}`),
      node("td", {}, [
        node("div", { text: item.game_code || "—" }),
        !item.is_undone && item.event_kind !== "undo"
          ? (() => {
              const button = node("button", { className: "plan-control-button", type: "button", text: "Отменить" });
              button.addEventListener("click", (event) => {
                event.stopPropagation();
                openFinanceOperation("undo", item);
              });
              return button;
            })()
          : node("small", { text: item.is_undone ? "операция отменена" : "" }),
      ]),
    ], recordRoute("treasury", "finance", item.id));
  });
  if (rows.length) replaceChildren("finance-table-body", rows);
  else emptyTable("finance-table-body", 7, "Операций по заданным фильтрам нет.");
  setText("finance-count", `${formatNumber(data.total)} операций`);
  renderPagination(
    "finance-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadFinance,
  );
}

async function loadFinance(offset = appState.offsets.finance) {
  appState.offsets.finance = offset;
  setLoading(true);
  const filters = formValues("finance-filters");
  try {
    const query = buildQuery({
      ...filters,
      days: appState.days,
      limit: PAGE_SIZE,
      offset,
    });
    renderFinance(await fetchJSON(`/api/admin/finance?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function openFinanceOperation(defaultAction = "deposit", event = null) {
  openOperation({
    endpoint: "/api/admin/finance/command",
    eyebrow: "TREASURY CONTROL",
    title: "Операция с казной",
    mark: "$",
    contextTitle: event ? `Финансовая запись #${event.id}` : "Казна Товарищества",
    contextMeta: "Баланс пересчитывается транзакционно · уведомления сохраняются",
    defaultAction,
    reload: () => loadFinance(appState.offsets.finance),
    operations: [
      {
        id: "deposit",
        label: "Записать поступление",
        fields: [
          { name: "amount", label: "Сумма", type: "number", min: 1, required: true },
          { name: "game_code", label: "Игровой код", placeholder: "ABCD", maxLength: 4, required: true },
          { name: "reason", label: "Причина поступления", type: "textarea", wide: true, required: true },
        ],
        confirmTitle: "Записать поступление в казну?",
        accept: "Записать поступление",
        build: (values) => ({ action: "deposit", amount: Number(values.amount), game_code: values.game_code, reason: values.reason }),
      },
      {
        id: "withdraw",
        label: "Записать расход",
        fields: [
          { name: "amount", label: "Сумма", type: "number", min: 1, required: true },
          { name: "game_code", label: "Игровой код", placeholder: "ABCD", maxLength: 4, required: true },
          { name: "reason", label: "Причина расхода", type: "textarea", wide: true, required: true },
        ],
        confirmTitle: "Записать расход казны?",
        accept: "Записать расход",
        tone: "warning",
        build: (values) => ({ action: "withdraw", amount: Number(values.amount), game_code: values.game_code, reason: values.reason }),
      },
      {
        id: "snapshot",
        label: "Зафиксировать контрольный остаток",
        fields: [{ name: "amount", label: "Фактический остаток", type: "number", min: 0, required: true }],
        warning: "Контрольная сверка становится новой расчётной точкой казны. Используйте фактическое значение из игры.",
        confirmTitle: "Зафиксировать новый остаток казны?",
        accept: "Зафиксировать",
        build: (values) => ({ action: "snapshot", amount: Number(values.amount) }),
      },
      ...(event && !event.is_undone && event.event_kind !== "undo"
        ? [{
            id: "undo",
            label: `Отменить операцию #${event.id}`,
            fields: [],
            warning: "Исходная запись не удаляется. Будет создана отдельная отмена, а баланс пересчитан по журналу.",
            confirmTitle: `Отменить финансовую операцию #${event.id}?`,
            accept: "Отменить операцию",
            tone: "danger",
            build: () => ({ action: "undo", event_id: Number(event.id) }),
          }]
        : []),
    ],
  });
}

function parseMaterialLines(value) {
  const rows = String(value || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(.+?)\s*[:=]\s*(\d+)$/);
      if (!match) throw new Error("Материалы указываются построчно: Название = количество.");
      return { material_name: match[1].trim(), quantity_per_unit: Number(match[2]) };
    });
  if (!rows.length) throw new Error("Добавьте хотя бы один материал.");
  return rows;
}

function openCraftPlanCreate() {
  const activeRecipes = appState.craftRecipes.filter((item) => item.active);
  if (!activeRecipes.length) {
    showToast("Сначала создайте активный рецепт.", true);
    return;
  }
  openOperation({
    endpoint: "/api/admin/crafts/command",
    eyebrow: "PRODUCTION CONTROL",
    title: "Новый производственный план",
    mark: "＋",
    contextTitle: "Мастерская T-Mod",
    contextMeta: "План, карточка Discord и журнал создаются как одна операция",
    reload: () => loadCraft(0),
    operations: [{
      id: "create_plan",
      label: "Создать план из рецепта",
      fields: [
        {
          name: "recipe_id",
          label: "Рецепт",
          type: "select",
          options: activeRecipes.map((item) => ({ value: item.id, label: `#${item.id} · ${item.product_name}` })),
          required: true,
        },
        { name: "attempts_total", label: "Количество попыток", type: "number", min: 1, required: true },
        { name: "responsible_id", label: "Discord ID ответственного", placeholder: "902235631952998410", required: true, wide: true },
      ],
      confirmTitle: "Создать производственный план?",
      accept: "Создать и опубликовать",
      build: (values) => ({
        action: "create_plan",
        recipe_id: Number(values.recipe_id),
        attempts_total: Number(values.attempts_total),
        responsible_id: values.responsible_id,
      }),
    }],
  });
}

function openCraftRecipeCreate() {
  openOperation({
    endpoint: "/api/admin/crafts/command",
    eyebrow: "RECIPE ENGINEERING",
    title: "Новый рецепт",
    mark: "⌁",
    contextTitle: "Версионируемый рецепт производства",
    contextMeta: "Будущие планы сохранят собственный снимок этой версии",
    reload: () => loadCraft(0),
    operations: [{
      id: "create_recipe",
      label: "Создать рецепт",
      fields: [
        { name: "product_name", label: "Название продукта", required: true, wide: true },
        { name: "treasury_cost_per_unit", label: "Расход казны на единицу", type: "number", min: 0, value: 0, required: true },
        { name: "duration_minutes_per_unit", label: "Минут на единицу", type: "number", min: 1, required: true },
        { name: "max_batch_size", label: "Максимум в одном цикле", type: "number", min: 1, required: true },
        { name: "materials", label: "Материалы на единицу", type: "textarea", placeholder: "Железо = 50\nМедь = 20", required: true, wide: true },
      ],
      confirmTitle: "Создать новый рецепт?",
      accept: "Создать рецепт",
      build: (values) => ({
        action: "create_recipe",
        product_name: values.product_name,
        treasury_cost_per_unit: Number(values.treasury_cost_per_unit),
        duration_minutes_per_unit: Number(values.duration_minutes_per_unit),
        max_batch_size: Number(values.max_batch_size),
        materials: parseMaterialLines(values.materials),
      }),
    }],
  });
}

function recipeMaterialText(recipe) {
  return (recipe.materials || [])
    .map((item) => `${item.material_name} = ${item.quantity_per_unit}`)
    .join("\n");
}

function openCraftRecipeOperation(recipe) {
  const sharedFields = [
    { name: "product_name", label: "Название продукта", value: recipe.product_name, required: true, wide: true },
    { name: "treasury_cost_per_unit", label: "Расход казны на единицу", type: "number", min: 0, value: recipe.treasury_cost_per_unit, required: true },
    { name: "duration_minutes_per_unit", label: "Минут на единицу", type: "number", min: 1, value: recipe.duration_minutes_per_unit, required: true },
    { name: "max_batch_size", label: "Максимум в цикле", type: "number", min: 1, value: recipe.max_batch_size, required: true },
    { name: "materials", label: "Материалы на единицу", type: "textarea", value: recipeMaterialText(recipe), required: true, wide: true },
  ];
  openOperation({
    endpoint: "/api/admin/crafts/command",
    eyebrow: "RECIPE ENGINEERING",
    title: `Рецепт #${recipe.id}`,
    mark: "⌁",
    contextTitle: recipe.product_name,
    contextMeta: `версия ${recipe.version || 1} · ${recipe.active ? "активен" : "отключён"}`,
    reload: () => loadCraft(appState.offsets.craft),
    operations: [
      {
        id: "update_recipe",
        label: "Создать новую версию",
        fields: sharedFields,
        warning: "Активные планы сохранят прежний снимок. Новые планы будут использовать обновлённую версию.",
        confirmTitle: `Обновить рецепт «${recipe.product_name}»?`,
        accept: "Сохранить новую версию",
        build: (values) => ({
          action: "update_recipe",
          recipe_id: recipe.id,
          product_name: values.product_name,
          treasury_cost_per_unit: Number(values.treasury_cost_per_unit),
          duration_minutes_per_unit: Number(values.duration_minutes_per_unit),
          max_batch_size: Number(values.max_batch_size),
          materials: parseMaterialLines(values.materials),
        }),
      },
      {
        id: "clone_recipe",
        label: "Создать независимую копию",
        fields: [{ name: "product_name", label: "Название копии", value: `${recipe.product_name} — копия`, required: true, wide: true }],
        build: (values) => ({ action: "clone_recipe", recipe_id: recipe.id, product_name: values.product_name }),
      },
      {
        id: "toggle_recipe",
        label: recipe.active ? "Отключить рецепт" : "Включить рецепт",
        fields: [],
        warning: recipe.active
          ? "Новые планы нельзя будет создавать из рецепта. Существующие планы продолжат работу."
          : "Рецепт снова станет доступен для новых производственных планов.",
        tone: recipe.active ? "danger" : "warning",
        accept: recipe.active ? "Отключить" : "Включить",
        build: () => ({ action: "toggle_recipe", recipe_id: recipe.id, active: !Boolean(recipe.active) }),
      },
    ],
  });
}

function recipeControlCard(recipe) {
  const button = node("button", {
    className: "plan-control-button",
    type: "button",
    text: "Управлять",
  });
  button.addEventListener("click", () => openCraftRecipeOperation(recipe));
  return node("article", { className: "recipe-control-card" }, [
    node("header", {}, [
      node("span", { text: `R-${String(recipe.id).padStart(3, "0")} · V${recipe.version || 1}` }),
      statusPill(recipe.active ? "Активен" : "Отключён", recipe.active ? "" : "warning"),
    ]),
    node("h3", { text: recipe.product_name }),
    node("p", { text: `${formatNumber(recipe.duration_minutes_per_unit)} мин/шт · цикл до ${formatNumber(recipe.max_batch_size)} · ${(recipe.materials || []).length} материалов` }),
    node("footer", {}, [
      node("span", { text: `казна ${formatMoney(recipe.treasury_cost_per_unit)} / шт.` }),
      button,
    ]),
  ]);
}

function openCraftPlanOperation(plan) {
  const operations = [];
  if (["procurement", "crafting"].includes(plan.stage)) {
    operations.push({
      id: "purchase",
      label: "Добавить закупку материала",
      fields: [
        { name: "plan_material_id", label: "Материал", type: "select", options: (plan.materials || []).map((item) => ({ value: item.id, label: `${item.name} · склад ${formatNumber(item.stock_quantity)}` })), required: true, wide: true },
        { name: "quantity", label: "Закуплено единиц", type: "number", min: 1, required: true },
        { name: "total_cost", label: "Общая стоимость", type: "number", min: 0, value: 0, required: true },
        { name: "finance_code", label: "Финансовый код — необязательно", placeholder: "ABCD", maxLength: 4, wide: true },
      ],
      confirmTitle: `Записать закупку для плана #${plan.id}?`,
      accept: "Записать закупку",
      build: (values) => ({ action: "purchase", plan_id: plan.id, plan_material_id: Number(values.plan_material_id), quantity: Number(values.quantity), total_cost: Number(values.total_cost), finance_code: values.finance_code }),
    });
    operations.push({
      id: "start_batch",
      label: "Запустить цикл крафта",
      fields: [{ name: "quantity", label: "Количество в цикле", type: "number", min: 1, required: true }],
      warning: "T-Mod повторно проверит остаток плана, лимит цикла, материалы и расчётную казну.",
      confirmTitle: `Запустить цикл плана #${plan.id}?`,
      accept: "Запустить цикл",
      build: (values) => ({ action: "start_batch", plan_id: plan.id, quantity: Number(values.quantity) }),
    });
  }
  if (plan.stage === "awaiting_output") {
    operations.push({
      id: "final_output",
      label: "Зафиксировать итог производства",
      fields: [{ name: "product_quantity", label: "Получено готовых предметов", type: "number", min: 0, required: true }],
      confirmTitle: `Зафиксировать итог плана #${plan.id}?`,
      accept: "Сохранить итог",
      build: (values) => ({ action: "final_output", plan_id: plan.id, product_quantity: Number(values.product_quantity) }),
    });
  }
  if (["listing", "selling"].includes(plan.stage)) {
    operations.push({
      id: "estimated_price",
      label: "Установить ожидаемую цену",
      fields: [{ name: "unit_price", label: "Цена за единицу", type: "number", min: 1, value: plan.estimated_unit_price || "", required: true }],
      build: (values) => ({ action: "estimated_price", plan_id: plan.id, unit_price: Number(values.unit_price) }),
    });
  }
  if (plan.stage === "listing") {
    operations.push({
      id: "market_listing",
      label: "Записать выставление на маркет",
      fields: [{ name: "quantity", label: "Выставлено единиц", type: "number", min: 1, required: true }],
      build: (values) => ({ action: "market_listing", plan_id: plan.id, quantity: Number(values.quantity) }),
    });
  }
  if (plan.stage === "selling") {
    operations.push({
      id: "sale",
      label: "Записать продажу",
      fields: [
        { name: "quantity", label: "Продано единиц", type: "number", min: 1, required: true },
        { name: "total_amount", label: "Получено всего", type: "number", min: 1, required: true },
      ],
      confirmTitle: `Записать продажу плана #${plan.id}?`,
      accept: "Записать продажу",
      build: (values) => ({ action: "sale", plan_id: plan.id, quantity: Number(values.quantity), total_amount: Number(values.total_amount) }),
    });
  }
  if (!["completed", "cancelled"].includes(plan.stage)) {
    operations.push({
      id: "inventory",
      label: "Провести сверку склада",
      fields: [
        ...(plan.materials || []).map((item) => ({ name: `material_${item.id}`, label: item.name, type: "number", min: 0, value: item.stock_quantity, required: true })),
        { name: "product_quantity", label: "Готовый продукт на складе", type: "number", min: 0, value: plan.product_stock || 0, required: true },
        { name: "note", label: "Комментарий", type: "textarea", wide: true },
      ],
      warning: "Сверка заменяет расчётные остатки фактическими. Проверьте каждое значение перед подтверждением.",
      confirmTitle: `Применить фактические остатки плана #${plan.id}?`,
      accept: "Применить сверку",
      build: (values) => ({
        action: "inventory",
        plan_id: plan.id,
        material_quantities: Object.fromEntries((plan.materials || []).map((item) => [item.id, Number(values[`material_${item.id}`])])),
        product_quantity: Number(values.product_quantity),
        note: values.note,
      }),
    });
  }
  if (!operations.length) {
    showToast("Завершённый план доступен только для просмотра и аудита.", true);
    return;
  }
  openOperation({
    endpoint: "/api/admin/crafts/command",
    eyebrow: "PRODUCTION CONTROL",
    title: `Управление планом #${plan.id}`,
    mark: "◇",
    contextTitle: plan.product_name,
    contextMeta: `${craftStageLabels[plan.stage] || plan.stage} · ответственный ${plan.responsible?.name || "не назначен"}`,
    reload: () => loadCraft(appState.offsets.craft),
    operations,
  });
}

function planCard(plan) {
  const route = recordRoute("craft", "craft-plan", plan.id);
  const completed = Number(plan.attempts_completed || 0);
  const queued = Number(plan.attempts_queued || 0);
  const total = Math.max(1, Number(plan.attempts_total || 0));
  const batch = plan.active_batch;
  const materials = (plan.materials || []).slice(0, 4);
  const card = node("article", { className: "plan-card" }, [
    node("div", { className: "plan-card-head" }, [
      node("div", {}, [
        node("h3", { text: `#${plan.id} · ${plan.product_name}` }),
        node("small", {
          text: `${plan.responsible?.name || "Без ответственного"} · версия ${plan.recipe_version}`,
        }),
      ]),
      statusPill(
        craftStageLabels[plan.stage] || plan.stage,
        batch && new Date(batch.due_at).getTime() < Date.now() ? "warning" : "",
      ),
    ]),
    node("div", { className: "plan-progress" }, [
      node("span", {}, [
        node("span", { text: "Прогресс попыток" }),
        node("b", { text: `${completed} / ${total}` }),
      ]),
      node("progress", { value: Math.min(total, completed), max: total }),
    ]),
    node("div", { className: "plan-metrics" }, [
      node("span", {}, [
        node("small", { text: "В очереди" }),
        node("b", { text: formatNumber(queued) }),
      ]),
      node("span", {}, [
        node("small", { text: "Затраты" }),
        node("b", { text: formatMoney(plan.purchase_cost_total) }),
      ]),
      node("span", {}, [
        node("small", { text: "Выручка" }),
        node("b", { text: formatMoney(plan.total_revenue) }),
      ]),
    ]),
    node(
      "div",
      { className: "material-list" },
      materials.length
        ? materials.map((material) =>
            node("div", { className: "material-line" }, [
              node("span", { text: material.name }),
              node("b", {
                text: `${formatNumber(material.stock_quantity)} / ${formatNumber(material.required_total)}`,
              }),
            ]),
          )
        : [node("div", { className: "material-line" }, [node("span", { text: "Материалы не указаны" })])],
    ),
    node("div", { className: "plan-foot" }, [
      node("span", {
        text: batch
          ? `цикл до ${formatDate(batch.due_at)}`
          : `обновлён ${relativeTime(plan.updated_at)}`,
      }),
      node("span", { className: "card-actions" }, [
        (() => {
          const button = node("button", {
            className: "plan-control-button",
            type: "button",
            text: "Управлять",
          });
          button.addEventListener("click", () => openCraftPlanOperation(plan));
          return button;
        })(),
        cardLinkButton(route),
        plan.discord_url
          ? node("a", {
              text: "Discord ↗",
              href: plan.discord_url,
              target: "_blank",
              rel: "noopener noreferrer",
            })
          : node("span", { text: `план #${plan.id}` }),
      ]),
    ]),
  ]);
  card.tabIndex = 0;
  card.addEventListener("click", (event) => {
    if (!event.target.closest("a, button")) {
      openDetails(`Крафт #${plan.id}`, plan, route);
    }
  });
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter") openDetails(`Крафт #${plan.id}`, plan, route);
  });
  return card;
}

function detailsPreview(details) {
  if (!details || typeof details !== "object") return "—";
  const parts = Object.entries(details)
    .slice(0, 3)
    .map(([key, value]) => `${key}: ${String(value)}`);
  return parts.join(" · ") || "—";
}

function renderCraft(data) {
  if (!showApplication(data)) return;
  const stats = data.stats || {};
  const plans = data.active_plans || [];
  appState.craftRecipes = data.recipes || [];
  const events = data.events || {};
  appState.rows.craft = events.items || [];
  setText("craft-active", formatNumber(stats.active_count));
  setText("craft-planned", `запланировано попыток: ${formatNumber(stats.attempts_planned)}`);
  setText("craft-completed", formatNumber(stats.completed_count));
  setText("craft-output", `готовой продукции: ${formatNumber(stats.output_total)}`);
  setText("craft-revenue", formatMoney(stats.revenue));
  setText("craft-sold", `продано: ${formatNumber(stats.sold_total)}`);
  setText("craft-profit", formatMoney(stats.profit, true));
  setText(
    "craft-costs",
    `закупки ${formatMoney(stats.purchase_cost)} · работа ${formatMoney(stats.craft_fees)}`,
  );
  setText("active-plan-count", `Производство сейчас · ${formatNumber(plans.length)}`);
  replaceChildren(
    "craft-plan-grid",
    plans.length
      ? plans.map(planCard)
      : [node("div", { className: "empty-state", text: "Сейчас активных планов нет." })],
  );
  setText("craft-recipe-count", `Версии производства · ${formatNumber(appState.craftRecipes.length)}`);
  replaceChildren(
    "craft-recipe-grid",
    appState.craftRecipes.length
      ? appState.craftRecipes.map(recipeControlCard)
      : [node("div", { className: "empty-state", text: "Рецепты ещё не созданы." })],
  );

  const rows = appState.rows.craft.map((item) =>
    interactiveRow(item, `Событие крафта #${item.id}`, [
      tableCell(formatDate(item.created_at), `#${item.id}`),
      tableCell(`#${item.plan_id} · ${item.product_name}`, "производственный план"),
      node("td", {}, [statusPill(craftEventLabels[item.event_kind] || item.event_kind)]),
      tableCell(item.actor_display || "T-Mod", item.actor_id ? `ID ${item.actor_id}` : "система"),
      tableCell(craftStageLabels[item.stage] || item.stage),
      tableCell(detailsPreview(item.details)),
    ], recordRoute("craft", "craft-event", item.id)),
  );
  if (rows.length) replaceChildren("craft-table-body", rows);
  else emptyTable("craft-table-body", 6, "Событий по заданным фильтрам нет.");
  setText("craft-event-count", `${formatNumber(events.total)} записей`);
  renderPagination(
    "craft-pagination",
    Number(events.total || 0),
    Number(events.offset || 0),
    loadCraft,
  );
}

async function loadCraft(offset = appState.offsets.craft) {
  appState.offsets.craft = offset;
  setLoading(true);
  const filters = formValues("craft-filters");
  try {
    const query = buildQuery({
      ...filters,
      days: appState.days,
      limit: PAGE_SIZE,
      offset,
    });
    renderCraft(await fetchJSON(`/api/admin/crafts?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function discordSourceCell(item) {
  if (!item.discord_url) return tableCell("—", item.message_id ? `сообщение ${item.message_id}` : "");
  const cell = node("td");
  cell.append(
    node("a", {
      text: "Открыть ↗",
      href: item.discord_url,
      target: "_blank",
      rel: "noopener noreferrer",
    }),
  );
  return cell;
}

function renderDiscord(data) {
  if (!showApplication(data)) return;
  const stats = data.stats || {};
  appState.rows.discord = data.items || [];
  setText("discord-events", formatCompact(stats.events));
  setText("discord-users", formatNumber(stats.users));
  setText("discord-channels", formatNumber(stats.channels));
  setText("discord-last", relativeTime(stats.last_event_at));
  setText("discord-window", `окно наблюдения: ${stats.days || appState.days} дн.`);

  const types = stats.top_types || [];
  const typeMax = Math.max(...types.map((item) => Number(item.events || 0)), 1);
  replaceChildren(
    "discord-types",
    types.length
      ? types.map((item) =>
          rankingItem(
            discordLabels[item.event_type] || item.event_type,
            item.event_type,
            formatNumber(item.events),
            typeMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Нет событий за период." })],
  );
  fillSelect(
    "discord-type",
    types.map((item) => item.event_type),
    (value) => discordLabels[value] || value,
  );

  const channels = stats.top_channels || [];
  const channelMax = Math.max(...channels.map((item) => Number(item.events || 0)), 1);
  replaceChildren(
    "discord-top-channels",
    channels.length
      ? channels.map((item) =>
          rankingItem(
            `# ${item.channel_name || item.channel_id}`,
            `ID ${item.channel_id}`,
            formatNumber(item.events),
            channelMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Нет активности каналов." })],
  );

  const users = stats.top_users || [];
  const userMax = Math.max(...users.map((item) => Number(item.events || 0)), 1);
  replaceChildren(
    "discord-top-users",
    users.length
      ? users.map((item) =>
          rankingItem(
            item.display_name || `Участник ${item.user_id}`,
            item.is_bot ? "бот" : `ID ${item.user_id}`,
            formatNumber(item.events),
            userMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Нет активных участников." })],
  );

  const rows = appState.rows.discord.map((item) =>
    interactiveRow(item, "Discord-событие", [
      tableCell(formatDate(item.at), `#${item.id}`),
      node("td", {}, [statusPill(discordLabels[item.event_type] || item.event_type)]),
      tableCell(
        item.display_name || `Участник ${item.user_id}`,
        item.is_bot ? "бот" : `ID ${item.user_id}`,
      ),
      tableCell(item.channel_name ? `# ${item.channel_name}` : "—", item.category_name || ""),
      tableCell(
        textPreview(item.event_text, discordLabels[item.event_type] || "Событие"),
        textPreview(item.details, ""),
      ),
      discordSourceCell(item),
    ], recordRoute("discord", "discord-event", item.id)),
  );
  if (rows.length) replaceChildren("discord-table-body", rows);
  else emptyTable("discord-table-body", 6, "Событий по заданным фильтрам нет.");
  setText("discord-count", `${formatNumber(data.total)} событий`);
  renderPagination(
    "discord-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadDiscord,
  );
}

async function loadDiscord(offset = appState.offsets.discord) {
  appState.offsets.discord = offset;
  setLoading(true);
  const filters = formValues("discord-filters");
  try {
    const query = buildQuery({
      ...filters,
      days: appState.days,
      limit: PAGE_SIZE,
      offset,
    });
    renderDiscord(await fetchJSON(`/api/admin/discord?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function capabilityCard(capability) {
  const card = node("button", { className: "capability-card", type: "button" }, [
    node("span", { className: "capability-card-head" }, [
      node("span", { text: String(capability.id || "T").slice(0, 2).toUpperCase() }),
      statusPill(capability.state === "integrated" ? "подключено" : capability.state),
    ]),
    node("h3", { text: capability.title }),
    node("p", { text: capability.description }),
    node("span", { className: "capability-card-foot" }, [
      node("span", {
        text: capability.scope === "administrator" ? "Администраторы" : "Участники",
      }),
      node("b", { text: "Открыть →" }),
    ]),
  ]);
  card.addEventListener("click", () => switchSection(capability.section));
  return card;
}

function renderRegistry(data) {
  if (!showApplication(data)) return;
  const registry = data.registry || {};
  const kpis = [
    ["Участники", registry.members, `${formatNumber(registry.profiles)} профилей`, "accent-blue"],
    ["Законопроекты", registry.bills, `${formatNumber(registry.bills_open)} активных`, "accent-violet"],
    ["Кейсы СГЛ", registry.sgl_cases, `${formatNumber(registry.sgl_open)} открытых`, "accent-amber"],
    ["Доставка", registry.deliveries_open, `${formatNumber(registry.deliveries_dead)} dead-letter`, "accent-mint"],
  ];
  replaceChildren(
    "registry-kpis",
    kpis.map(([label, value, note, accent]) =>
      node("article", { className: `kpi-card ${accent}` }, [
        node("div", { className: "kpi-head" }, [
          node("span", { text: label }),
          node("i", { text: "T" }),
        ]),
        node("strong", { text: formatNumber(value) }),
        node("small", { text: note }),
      ]),
    ),
  );
  replaceChildren(
    "capability-grid",
    (data.capabilities || []).map(capabilityCard),
  );

  const catalogs = registry.market_catalogs || [];
  replaceChildren(
    "registry-catalogs",
    catalogs.length
      ? catalogs.map((item) =>
          compactItem(
            `${item.server_id} · ${marketCategoryLabel(item.category)}`,
            item.last_error
              ? `ошибка: ${textPreview(item.last_error)}`
              : `снимок ${formatDate(item.source_updated_at || item.last_success_at)}`,
            formatNumber(item.record_count),
            () => openDetails("Каталог Majestic", item),
          ),
        )
      : [node("div", { className: "empty-state", text: "Каталоги ещё не синхронизированы." })],
  );

  const attention = [];
  if (Number(registry.deliveries_dead) > 0) {
    attention.push(["Dead-letter очередь", `${registry.deliveries_dead} сообщений`, "system"]);
  }
  if (Number(registry.sgl_open) > 0) {
    attention.push(["Открытые кейсы СГЛ", `${registry.sgl_open} в работе`, "sgl"]);
  }
  if (Number(registry.bills_open) > 0) {
    attention.push(["Законопроекты", `${registry.bills_open} ожидают решения`, "bills"]);
  }
  const brokenCatalogs = catalogs.filter(
    (item) => item.last_error || Number(item.consecutive_failures || 0) > 0,
  );
  if (brokenCatalogs.length) {
    attention.push(["Majestic API", `${brokenCatalogs.length} каталогов с ошибкой`, "market"]);
  }
  replaceChildren(
    "registry-attention",
    attention.length
      ? attention.map(([title, note, section]) =>
          compactItem(title, note, "→", () => switchSection(section)),
        )
      : [node("div", { className: "empty-state", text: "Все контрольные точки в норме." })],
  );
  const healthy = Number(registry.deliveries_dead || 0) === 0 && !brokenCatalogs.length;
  const health = byId("registry-health");
  health.textContent = healthy ? "все контуры на связи" : "нужно внимание";
  health.className = `health-pill${healthy ? "" : " warning"}`;
}

async function loadRegistry() {
  setLoading(true);
  try {
    renderRegistry(await fetchJSON("/api/admin/registry"));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function marketCategoryLabel(category) {
  return {
    items: "Предметы",
    vehicles: "Автомобили",
    clothes: "Одежда",
  }[category] || category || "Каталог";
}

function marketCard(item) {
  const route = recordRoute(
    "market",
    "market",
    `${item.server_id}.${item.category}.${item.item_id}`,
  );
  const alertButton = node("button", {
    className: "market-alert-button",
    type: "button",
    text: "Поставить сигнал",
  });
  alertButton.addEventListener("click", (event) => {
    event.stopPropagation();
    openMarketAlert(item);
  });
  const card = node("article", { className: "market-card" }, [
    node("header", {}, [
      node("span", { text: marketCategoryLabel(item.category) }),
      node("small", { text: item.external_id || `#${item.item_id}` }),
    ]),
    node("h3", { text: item.item_name || `Объект #${item.item_id}` }),
    node("strong", {
      className: "market-price",
      text: item.average_price === null ? "Нет цены" : formatMoney(item.average_price),
    }),
    node("div", { className: "market-card-metrics" }, [
      node("span", {}, [
        node("small", { text: "Диапазон" }),
        node("b", {
          text:
            item.min_price === null
              ? "—"
              : `${formatMoney(item.min_price)} — ${formatMoney(item.max_price)}`,
        }),
      ]),
      node("span", {}, [
        node("small", { text: "На рынке / продано" }),
        node("b", { text: `${formatNumber(item.total_count)} / ${formatNumber(item.sold_count)}` }),
      ]),
    ]),
    node("footer", {}, [
      node("span", {
        text: `Снимок ${formatDate(item.source_updated_at || item.fetched_at)}`,
      }),
      node("span", { className: "card-actions" }, [
        cardLinkButton(route),
        alertButton,
      ]),
    ]),
  ]);
  card.tabIndex = 0;
  card.addEventListener("click", (event) => {
    if (!event.target.closest("button")) openDetails(item.item_name, item, route);
  });
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter") openDetails(item.item_name, item, route);
  });
  return card;
}

function openMarketAlert(item) {
  const form = byId("market-alert-form");
  form.elements.namedItem("item_id").value = String(item.item_id);
  form.elements.namedItem("category").value = String(item.category);
  form.elements.namedItem("server_id").value = String(item.server_id);
  form.elements.namedItem("target_price").value = String(
    item.min_price || item.average_price || "",
  );
  form.elements.namedItem("min_quantity").value = "1";
  setText("market-alert-item", item.item_name || `Объект #${item.item_id}`);
  form.hidden = false;
  form.elements.namedItem("target_price").focus();
}

async function sendMarketAlertAction(action, payload) {
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/market/alert", { action, ...payload });
    const created = action === "upsert";
    showToast(
      result.message || "Сигнал обновлён.",
      false,
      {
        title: created ? "Сигнал поставлен" : "Наблюдение обновлено",
        icon: created ? "◉" : "✓",
        tone: "update",
        sound: created ? "success" : false,
      },
    );
    byId("market-alert-form").hidden = true;
    await loadMarket(appState.offsets.market);
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function marketAlertItem(item) {
  const controls = node("span", { className: "compact-actions" });
  const toggle = node("button", {
    type: "button",
    text: item.status === "active" ? "Пауза" : "Включить",
  });
  const remove = node("button", { type: "button", text: "Удалить" });
  toggle.addEventListener("click", () =>
    sendMarketAlertAction(item.status === "active" ? "pause" : "resume", {
      alert_id: item.id,
    }),
  );
  remove.addEventListener("click", () =>
    sendMarketAlertAction("delete", { alert_id: item.id }),
  );
  controls.append(toggle, remove);
  const active = item.status === "active";
  return node("article", { className: `signal-card${active ? "" : " paused"}` }, [
    node("header", { className: "signal-card-head" }, [
      node("span", { className: "signal-card-identity" }, [
        node("small", {
          text: `${marketCategoryLabel(item.category)} · ${item.server_id || "RU15"}`,
        }),
        node("strong", { text: item.item_name || `Объект #${item.item_id}` }),
      ]),
      node("span", {
        className: "signal-state",
        text: active ? "наблюдает" : "на паузе",
      }),
    ]),
    node("div", { className: "signal-card-thresholds" }, [
      node("span", {}, [
        node("small", { text: "Цена срабатывания" }),
        node("b", { text: `≤ ${formatMoney(item.target_price)}` }),
      ]),
      node("span", {}, [
        node("small", { text: "Количество" }),
        node("b", { text: `от ${formatNumber(item.min_quantity)} шт.` }),
      ]),
    ]),
    node("footer", { className: "signal-card-foot" }, [
      node("small", {
        text: item.last_triggered_at
          ? `Последнее срабатывание ${relativeTime(item.last_triggered_at)}`
          : "Личное уведомление будет отправлено в Discord",
      }),
      controls,
    ]),
  ]);
}

function renderMarket(data) {
  if (!showApplication(data)) return;
  appState.rows.market = data.items || [];
  const catalog = data.catalog || {};
  const alerts = data.my_alerts || [];
  const alertStats = data.alert_stats || {};
  setText("market-total", formatNumber(data.total));
  setText(
    "market-category-note",
    `${marketCategoryLabel(data.category)} · ${data.server_id || "RU15"}`,
  );
  setText("market-updated", relativeTime(catalog.source_updated_at || catalog.last_success_at));
  setText("market-my-alerts", formatNumber(alerts.length));
  setText("market-active-alerts", formatNumber(alertStats.active));
  setText("market-count", `${formatNumber(data.total)} позиций`);
  const fresh = Boolean(catalog.last_success_at) && !catalog.last_error;
  const freshness = byId("market-freshness");
  freshness.textContent = fresh ? `данные ${relativeTime(catalog.last_success_at)}` : "снимок недоступен";
  freshness.className = `health-pill${fresh ? "" : " warning"}`;
  replaceChildren(
    "market-grid",
    appState.rows.market.length
      ? appState.rows.market.map(marketCard)
      : [node("div", { className: "empty-state", text: "Ничего не найдено в локальном снимке рынка." })],
  );
  replaceChildren(
    "market-alert-list",
    alerts.length
      ? alerts.map(marketAlertItem)
      : [
          node("div", {
            className: "empty-state",
            text: "Сигналов нет. Выберите товар в каталоге и нажмите «Поставить сигнал».",
          }),
        ],
  );
  byId("market-alert-list").className = alerts.length
    ? "market-signal-list"
    : "compact-list";
  renderPagination(
    "market-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadMarket,
  );
}

async function loadMarket(offset = appState.offsets.market) {
  appState.offsets.market = offset;
  setLoading(true);
  const filters = formValues("market-filters");
  try {
    const query = buildQuery({ ...filters, limit: PAGE_SIZE, offset });
    renderMarket(await fetchJSON(`/api/admin/market?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function billStatusLabel(status) {
  return {
    draft: "Черновик",
    submitted: "В очереди",
    voting: "На рассмотрении",
    accepted: "Принят",
    rejected: "Отклонён",
    vetoed: "Вето",
  }[status] || status || "Без статуса";
}

function openBillOperation(item) {
  const editable = [
    { id: "title", label: "Изменить название", value: item.title || "", type: "text" },
    { id: "summary", label: "Изменить полный текст", value: item.summary || "", type: "textarea" },
    { id: "materials", label: "Изменить материалы", value: item.materials || "", type: "textarea" },
  ].map((field) => ({
    id: `update_${field.id}`,
    label: field.label,
    fields: [{ name: "value", label: field.label, type: field.type, value: field.value, required: true, wide: true }],
    warning: "Активный консенсус и незавершённая доставка автоматически заблокируют изменение.",
    confirmTitle: `${field.label} законопроекта №${item.bill_number}?`,
    accept: "Сохранить редакцию",
    build: (values) => ({ action: "update", bill_number: item.bill_number, field: field.id, value: values.value }),
  }));
  const statuses = ["draft", "queued", "requeued", "accepted", "rejected", "vetoed"];
  editable.push({
    id: "update_status",
    label: "Изменить административный статус",
    fields: [{ name: "value", label: "Новый статус", type: "select", value: item.status, options: statuses.map((status) => ({ value: status, label: billStatusLabel(status) })) }],
    warning: "Статус не заменяет голосование и не создаёт результат консенсуса.",
    build: (values) => ({ action: "update", bill_number: item.bill_number, field: "status", value: values.value }),
  });
  editable.push({
    id: "delete",
    label: "Удалить законопроект",
    fields: [],
    warning: "Проект, его голоса и результаты будут удалены из рабочих таблиц. Снимок останется в универсальном аудите для восстановления.",
    confirmTitle: `Удалить законопроект №${item.bill_number}?`,
    confirmMessage: "Действие допустимо только вне активного консенсуса. T-Mod сохранит полный снимок в аудите.",
    accept: "Удалить с сохранением снимка",
    tone: "danger",
    build: () => ({ action: "delete", bill_number: item.bill_number }),
  });
  openOperation({
    endpoint: "/api/admin/bills/command",
    eyebrow: "LEGISLATIVE CONTROL",
    title: `Законопроект №${item.bill_number}`,
    mark: "§",
    contextTitle: item.title || "Без названия",
    contextMeta: `${billStatusLabel(item.status)} · ${item.author_display || item.author_id}`,
    reload: loadBills,
    operations: editable,
  });
}

function billCard(item) {
  const route = recordRoute("bills", "bill", item.id);
  const percent = item.result_overall_percent;
  const source =
    appState.guildId && item.channel_id && item.message_id
      ? `https://discord.com/channels/${appState.guildId}/${item.channel_id}/${item.message_id}`
      : null;
  const card = node("article", { className: "bill-card" }, [
    node("span", { className: "bill-number", text: `№ ${item.bill_number}` }),
    node("div", { className: "bill-copy" }, [
      node("h3", { text: item.title || "Без названия" }),
      node("p", { text: textPreview(item.summary, "Текст проекта доступен в карточке.") }),
      node("small", {
        text: `${item.author_display || `Автор ${item.author_id}`} · ${formatDate(item.created_at)}`,
      }),
    ]),
    node("div", {}, [
      statusPill(billStatusLabel(item.status)),
      node("small", {
        text: percent === null || percent === undefined ? "решения ещё нет" : `${Number(percent).toFixed(1)}%`,
      }),
      cardLinkButton(route),
      (() => {
        const button = node("button", {
          className: "bill-control-button",
          type: "button",
          text: "Управлять",
        });
        button.addEventListener("click", () => openBillOperation(item));
        return button;
      })(),
      source
        ? node("a", {
            text: "Оригинал ↗",
            href: source,
            target: "_blank",
            rel: "noopener noreferrer",
          })
        : null,
    ]),
  ]);
  card.tabIndex = 0;
  card.addEventListener("click", (event) => {
    if (!event.target.closest("a, button")) {
      openDetails(`Законопроект №${item.bill_number}`, item, route);
    }
  });
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      openDetails(`Законопроект №${item.bill_number}`, item, route);
    }
  });
  return card;
}

function renderBills(data) {
  if (!showApplication(data)) return;
  appState.rows.bills = data.items || [];
  const workspaces = data.workspaces || [];
  const sessions = data.active_sessions || [];
  const results = data.recent_results || [];
  setText("bills-total", formatNumber(data.total));
  setText("bill-workspaces", formatNumber(workspaces.length));
  setText("bill-sessions", formatNumber(sessions.length));
  setText("bill-results", formatNumber(results.length));
  setText("bills-count", `${formatNumber(data.total)} проектов`);
  replaceChildren(
    "bills-list",
    appState.rows.bills.length
      ? appState.rows.bills.map(billCard)
      : [node("div", { className: "empty-state", text: "Законопроектов по фильтру нет." })],
  );
  replaceChildren(
    "workspace-list",
    workspaces.length
      ? workspaces.slice(0, 30).map((item) =>
          compactItem(
            item.title || `Редактор #${item.id}`,
            `${item.author_display || `Автор ${item.author_id}`} · версия ${item.revision || 1}`,
            item.status || "draft",
            () =>
              openDetails(
                `Редактор #${item.id}`,
                item,
                recordRoute("bills", "workspace", item.id),
              ),
          ),
        )
      : [node("div", { className: "empty-state", text: "Открытых редакторских пространств нет." })],
  );
}

async function loadBills() {
  setLoading(true);
  try {
    const query = buildQuery(formValues("bills-filters"));
    renderBills(await fetchJSON(`/api/admin/bills?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function sglStatusLabel(status) {
  return {
    reserved: "Зарезервирован",
    collecting: "Сбор данных",
    active: "В работе",
    waiting: "Ожидание",
    closed: "Закрыт",
    archived: "Архивирован",
  }[status] || status || "Не указан";
}

function renderSgl(data) {
  if (!showApplication(data)) return;
  const cases = data.cases || {};
  const archives = data.archives || {};
  appState.rows.sgl = cases.items || [];
  fillSelect("sgl-status", cases.statuses || [], sglStatusLabel);
  const archiveRows = archives.items || [];
  const openCount = appState.rows.sgl.filter((item) => item.status !== "closed").length;
  const bytes = archiveRows.reduce((sum, item) => sum + Number(item.total_bytes || 0), 0);
  setText("sgl-total", formatNumber(cases.total));
  setText("sgl-open", formatNumber(openCount));
  setText("sgl-archives", formatNumber(archives.total));
  setText("sgl-bytes", formatBytes(bytes));
  setText("sgl-count", `${formatNumber(cases.total)} кейсов`);
  const rows = appState.rows.sgl.map((item) =>
    interactiveRow(item, `Кейс СГЛ №${item.case_number}`, [
      tableCell(`№ ${item.case_number}`, item.channel_id ? `канал ${item.channel_id}` : `#${item.id}`),
      tableCell(item.client_display || item.client_nick || `ID ${item.client_id}`, item.static_id || ""),
      tableCell(item.lead_lawyer_display || `ID ${item.lead_lawyer_id}`),
      tableCell(item.secretary_display || "Не назначен"),
      tableCell(item.request_type || "Не указан"),
      node("td", {}, [statusPill(sglStatusLabel(item.status))]),
      tableCell(
        `${formatNumber(item.event_count)} событий`,
        `${formatNumber(item.receipt_count)} чеков · ${formatDate(item.updated_at)}`,
      ),
    ], recordRoute("sgl", "case", item.id)),
  );
  if (rows.length) replaceChildren("sgl-table-body", rows);
  else emptyTable("sgl-table-body", 7, "Кейсов по фильтру нет.");
  replaceChildren(
    "sgl-archive-list",
    archiveRows.length
      ? archiveRows.map((item) => {
          const card = node("article", { className: "archive-card" }, [
            node("header", {}, [
              node("h3", { text: `Кейс №${item.case_number}` }),
              statusPill(item.status || "archive"),
            ]),
            node("p", { text: item.original_channel_name || `Канал ${item.original_channel_id}` }),
            node("footer", {}, [
              node("span", { text: `${formatNumber(item.message_count)} сообщений` }),
              node("span", { text: `${formatNumber(item.attachment_count)} вложений` }),
              node("span", { text: formatBytes(item.total_bytes) }),
            ]),
          ]);
          const route = recordRoute("sgl", "archive", item.id);
          const restore = node("button", {
            className: "archive-restore-button",
            type: "button",
            text: "Восстановить канал",
          });
          restore.addEventListener("click", async (event) => {
            event.stopPropagation();
            const approved = window.TModReactor?.confirm
              ? await window.TModReactor.confirm({
                  title: `Восстановить архив кейса №${item.case_number}?`,
                  message: "T-Mod создаст временный защищённый канал и восстановит сообщения в фоне. Повторный запрос не создаст дубликат.",
                  accept: "Восстановить",
                  tone: "warning",
                })
              : globalThis.confirm(`Восстановить архив кейса №${item.case_number}?`);
            if (!approved) return;
            setLoading(true);
            try {
              const result = await postJSON("/api/admin/sgl/command", {
                action: "restore_archive",
                case_number: item.case_number,
                confirmed: true,
              });
              showToast(result.message || "Архив восстанавливается.");
              if (result.discord_url) globalThis.open(result.discord_url, "_blank", "noopener");
              await loadSgl(appState.offsets.sgl);
            } catch (error) {
              handleError(error);
            } finally {
              setLoading(false);
            }
          });
          card.append(restore, cardLinkButton(route));
          card.tabIndex = 0;
          card.addEventListener("click", (event) => {
            if (!event.target.closest("button")) {
              openDetails(`Архив кейса №${item.case_number}`, item, route);
            }
          });
          return card;
        })
      : [node("div", { className: "empty-state", text: "Архивных снимков пока нет." })],
  );
  renderPagination("sgl-pagination", Number(cases.total || 0), Number(cases.offset || 0), loadSgl);
}

async function loadSgl(offset = appState.offsets.sgl) {
  appState.offsets.sgl = offset;
  setLoading(true);
  try {
    const query = buildQuery({
      ...formValues("sgl-filters"),
      limit: PAGE_SIZE,
      offset,
    });
    renderSgl(await fetchJSON(`/api/admin/sgl?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function renderMembers(data) {
  if (!showApplication(data)) return;
  appState.rows.members = data.items || [];
  setText("members-count", `${formatNumber(data.total)} участников`);
  const rows = appState.rows.members.map((item) =>
    interactiveRow(item, item.display_name || item.name || `Участник ${item.user_id}`, [
      tableCell(
        item.display_name || item.name || `Участник ${item.user_id}`,
        item.is_bot ? `бот · ${item.user_id}` : `Discord ID ${item.user_id}`,
      ),
      tableCell(
        item.legal_status || "Прихожанин",
        item.status
          ? `Доступность: ${item.status}${item.status_note ? ` · ${item.status_note}` : ""}`
          : "",
      ),
      tableCell(
        formatNumber(item.character_count),
        item.show_directory ? "в справочнике" : "профиль скрыт",
      ),
      tableCell(textPreview(item.contribution, "Не заполнено")),
      tableCell(textPreview(item.responsibilities, "Не заполнено")),
      tableCell(relativeTime(item.last_activity_at || item.updated_at), item.last_event_text || ""),
    ], recordRoute("members", "member", item.user_id)),
  );
  if (rows.length) replaceChildren("members-table-body", rows);
  else emptyTable("members-table-body", 6, "Участники по фильтру не найдены.");
  renderPagination(
    "members-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadMembers,
  );
}

async function loadMembers(offset = appState.offsets.members) {
  appState.offsets.members = offset;
  setLoading(true);
  try {
    const query = buildQuery({
      ...formValues("members-filters"),
      limit: PAGE_SIZE,
      offset,
    });
    renderMembers(await fetchJSON(`/api/admin/members?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function renderCommunications(data) {
  if (!showApplication(data)) return;
  appState.rows.communications = data.items || [];
  const delivered = appState.rows.communications.reduce(
    (sum, item) => sum + Number(item.delivered_count || 0),
    0,
  );
  const queued = appState.rows.communications.reduce(
    (sum, item) => sum + Number(item.queued_count || 0),
    0,
  );
  const problems = appState.rows.communications.reduce(
    (sum, item) => sum + Number(item.problem_count || 0),
    0,
  );
  setText("comms-total", formatNumber(data.total));
  setText("comms-delivered", formatNumber(delivered));
  setText("comms-queued", formatNumber(queued));
  setText("comms-problems", formatNumber(problems));
  setText("comms-count", `${formatNumber(data.total)} кампаний`);
  const rows = appState.rows.communications.map((item) =>
    interactiveRow(item, `Кампания #${item.id}`, [
      tableCell(formatDate(item.created_at), `#${item.id}`),
      node("td", {}, [statusPill(item.kind || "global")]),
      tableCell(item.title || "Без заголовка", textPreview(item.body)),
      tableCell(item.author_display || `ID ${item.author_id}`),
      tableCell(formatNumber(item.recipient_count)),
      tableCell(
        `${formatNumber(item.delivered_count)} доставлено`,
        `${formatNumber(item.queued_count)} в очереди · ${formatNumber(item.problem_count)} проблем`,
      ),
      node("td", {}, [statusPill(item.status || "draft")]),
    ], recordRoute("communications", "broadcast", item.id)),
  );
  if (rows.length) replaceChildren("comms-table-body", rows);
  else emptyTable("comms-table-body", 7, "Кампаний пока нет.");
  renderPagination(
    "comms-pagination",
    Number(data.total || 0),
    Number(data.offset || 0),
    loadCommunications,
  );
}

async function loadCommunications(offset = appState.offsets.communications) {
  appState.offsets.communications = offset;
  setLoading(true);
  try {
    const query = buildQuery({ limit: PAGE_SIZE, offset });
    renderCommunications(await fetchJSON(`/api/admin/communications?${query}`));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

async function sendBroadcast() {
  const values = formValues("broadcast-form");
  const approved = window.TModReactor?.confirm
    ? await window.TModReactor.confirm({
        title: "Отправить уведомление всем сенаторам?",
        message: `Кампания «${values.title || "без заголовка"}» будет поставлена в надёжную очередь личных сообщений.`,
        accept: "Поставить в очередь",
        tone: "warning",
      })
    : globalThis.confirm("Отправить уведомление всем сенаторам?");
  if (!approved) return;
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/communications/send", {
      kind: values.kind,
      title: values.title,
      body: values.body,
      link_url: values.link_url,
      confirmed: true,
    });
    byId("broadcast-form").reset();
    showToast(result.message || "Уведомление поставлено в очередь.");
    appState.offsets.communications = 0;
    await loadCommunications(0);
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function renderMedia(data) {
  if (!showApplication(data)) return;
  const music = data.music || {};
  const musicState = music.connected
    ? music.paused
      ? "пауза"
      : music.playing
        ? "играет"
        : "подключён"
    : "offline";
  setText("music-title", music.connected ? "T-Mod в голосовом канале" : "Музыка не подключена");
  setText("music-status", musicState);
  setText("music-current", music.current?.title || "Сейчас ничего не играет");
  setText(
    "music-current-meta",
    music.current
      ? `${music.current.requested_by_display || "T-Mod"} · очередь ${formatNumber(
          (music.queue || []).length,
        )}`
      : music.message || "Подключитесь к голосовому каналу",
  );
  const volume = Math.max(5, Math.min(100, Number(music.volume_percent || 50)));
  byId("music-volume").value = String(volume);
  setText("music-volume-value", `${volume}%`);
  replaceChildren(
    "music-queue",
    (music.queue || []).length
      ? music.queue.map((track, index) =>
          compactItem(
            `${index + 1}. ${track.title || "Без названия"}`,
            track.requested_by_display || "T-Mod",
            track.duration_label || "в очереди",
          ),
        )
      : [node("div", { className: "empty-state", text: "Очередь пуста." })],
  );

  const healthy = music.available !== false;
  const state = byId("media-state");
  state.textContent = healthy ? "управление готово" : "частично недоступно";
  state.className = `health-pill${healthy ? "" : " warning"}`;
}

async function loadMedia(silent = false) {
  if (appState.mediaRefreshing) return;
  appState.mediaRefreshing = true;
  const previousAutoRefreshing = appState.autoRefreshing;
  if (silent) appState.autoRefreshing = true;
  if (!silent) setLoading(true);
  try {
    renderMedia(await fetchJSON("/api/admin/media"));
  } catch (error) {
    handleError(error);
  } finally {
    appState.mediaRefreshing = false;
    appState.autoRefreshing = previousAutoRefreshing;
    if (!silent) setLoading(false);
  }
}

async function sendMediaCommand(target, action, payload = {}) {
  if (["stop", "disconnect", "leave"].includes(action)) {
    const approved = window.TModReactor?.confirm
      ? await window.TModReactor.confirm({
          title: "Остановить активный медиаконтур?",
          message: "Воспроизведение будет завершено для всех участников.",
          accept: "Остановить",
          tone: "critical",
        })
      : globalThis.confirm("Остановить активный медиаконтур?");
    if (!approved) return;
  }
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/media/command", {
      target,
      action,
      ...payload,
    });
    showToast(result.message || "Команда выполнена.");
    await loadMedia(true);
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function preferenceItem(label, enabled, note = "", preference = "") {
  const item = node(preference ? "button" : "div", {
    className: "preference-item",
    type: preference ? "button" : undefined,
  }, [
    node("span", {}, [
      node("strong", { text: label }),
      note ? node("span", { text: note }) : null,
    ]),
    node("i", {
      className: `preference-switch${enabled ? " on" : ""}`,
      text: enabled ? "вкл" : "выкл",
    }),
  ]);
  if (preference) {
    item.title = `Нажмите, чтобы ${enabled ? "отключить" : "включить"}`;
    item.addEventListener("click", () =>
      updateProfilePreference(preference, !enabled),
    );
  }
  return item;
}

async function updateProfilePreference(preference, enabled) {
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/profile/preference", {
      preference,
      enabled,
    });
    showToast(result.message || "Настройка сохранена.");
    await loadProfile();
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

async function updateCharacterVisibility(characterId, isPublic) {
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/profile/character", {
      character_id: characterId,
      is_public: isPublic,
    });
    showToast(result.message || "Видимость персонажа изменена.");
    await loadProfile();
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function renderProfile(data) {
  if (!showApplication(data)) return;
  const profile = data.profile || {};
  const characters = data.characters || [];
  const voice = data.voice || {};
  const viewer = data.viewer || {};
  const legalPositions = data.legal_positions || [];
  const displayName = viewer.name || "Администратор";
  const profileFlag = (key, fallback = true) =>
    profile[key] === undefined || profile[key] === null
      ? fallback
      : Boolean(profile[key]);
  setText("profile-name", displayName);
  setText("profile-avatar", displayName.trim().charAt(0).toUpperCase() || "A");
  setText(
    "profile-status",
    profile.status
      ? `${profile.status}${profile.status_note ? ` · ${profile.status_note}` : ""}`
      : "Статус не задан",
  );
  const directory = [
    [
      "Правовой статус",
      legalPositions.length
        ? legalPositions.map((item) => `${item.emoji} ${item.label}`).join(" · ")
        : "🕯️ Прихожанин",
    ],
    ["О себе", profile.biography],
    ["Вклад", profile.contribution],
    ["Ответственность", profile.responsibilities],
    ["В Товариществе", profile.membership_since ? formatDate(profile.membership_since) : null],
  ];
  replaceChildren(
    "profile-directory",
    directory.map(([label, value]) =>
      node("div", { className: "detail-row" }, [
        node("span", { text: label }),
        node("div", { text: textPreview(value, "Не заполнено") }),
      ]),
    ),
  );
  replaceChildren(
    "profile-characters",
    characters.length
      ? characters.map((character) => {
          const card = node("button", { className: "character-card", type: "button" }, [
            node("span", { text: `#${character.position}` }),
            node("strong", { text: character.nickname }),
            node("small", { text: `Статик ${character.static_id}` }),
            statusPill(character.is_public ? "виден" : "скрыт"),
          ]);
          card.title = character.is_public
            ? "Скрыть этого персонажа"
            : "Показать этого персонажа";
          card.addEventListener("click", () =>
            updateCharacterVisibility(character.id, !character.is_public),
          );
          return card;
        })
      : [node("div", { className: "empty-state", text: "Персонажи ещё не добавлены." })],
  );
  const quietStart = Math.floor(Number(profile.quiet_start_minute || 0) / 60);
  const quietEnd = Math.floor(
    Number(
      profile.quiet_end_minute === undefined
        ? 480
        : profile.quiet_end_minute,
    ) / 60,
  );
  replaceChildren(
    "profile-preferences",
    [
      ["Уведомления в ЛС", profileFlag("dm_notifications"), "", "dm_notifications"],
      ["Сигналы рынка", profileFlag("dm_market"), "", "dm_market"],
      ["Крафты", profileFlag("dm_craft"), "", "dm_craft"],
      ["Консенсус", profileFlag("dm_consensus"), "", "dm_consensus"],
      ["Финансы", profileFlag("dm_finance"), "", "dm_finance"],
      ["Системные события", profileFlag("dm_system"), "", "dm_system"],
      [
        "Тихие часы",
        profile.quiet_hours_enabled,
        profile.quiet_hours_enabled
          ? `${String(quietStart).padStart(2, "0")}:00–${String(quietEnd).padStart(2, "0")}:00`
          : "",
        "quiet_hours_enabled",
      ],
      ["Публичный справочник", profileFlag("show_directory"), "", "show_directory"],
      ["Показывать активность", profileFlag("show_activity"), "", "show_activity"],
      ["Показывать доступность", profileFlag("show_availability"), "", "show_availability"],
      ["Показывать роли", profileFlag("show_position"), "", "show_position"],
      ["Показывать персонажей", profileFlag("show_characters"), "", "show_characters"],
      ["Показывать дату вступления", profileFlag("show_join_date"), "", "show_join_date"],
    ].map(([label, enabled, note, preference]) =>
      preferenceItem(label, Boolean(enabled), note || "", preference),
    ),
  );
  const voiceMetrics = [
    ["Качество", voice.quality_score === undefined ? "нет калибровки" : `${voice.quality_score}/100`],
    ["Signal / noise", voice.snr_db === undefined ? "—" : `${Number(voice.snr_db).toFixed(1)} dB`],
    ["Клиппинг", voice.clipping_percent === undefined ? "—" : `${Number(voice.clipping_percent).toFixed(1)}%`],
    [
      "Успешность",
      voice.commands_total
        ? `${Math.round(
            ((Number(voice.commands_total) - Number(voice.failures_total || 0)) /
              Number(voice.commands_total)) *
              100,
          )}%`
        : "—",
    ],
    [
      "Средняя задержка",
      voice.latency_samples
        ? `${Math.round(Number(voice.latency_total_ms) / Number(voice.latency_samples))} мс`
        : "—",
    ],
    ["Движок", voice.last_engine || "—"],
  ];
  replaceChildren(
    "profile-voice",
    voiceMetrics.map(([label, value]) =>
      node("div", { className: "voice-metric" }, [
        node("span", { text: label }),
        node("strong", { text: value }),
      ]),
    ),
  );
}

async function loadProfile() {
  setLoading(true);
  try {
    renderProfile(await fetchJSON("/api/admin/profile"));
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

function renderReliability(reliability = {}) {
  const overall = String(reliability.overall || "warning");
  const overallNode = byId("reliability-overall");
  overallNode.textContent = overall === "ok" ? "контур в норме" : overall === "critical" ? "критическая проблема" : "нужно внимание";
  overallNode.className = `health-pill${overall === "ok" ? "" : " warning"}`;
  setText("reliability-release", `release ${reliability.release || "unknown"}`);

  const database = reliability.database || {};
  const latest = database.latest || {};
  const integrity = database.last_integrity || {};
  setText(
    "db-protection-state",
    database.status === "ok"
      ? `${formatNumber(database.backup_count || 0)} защищённых снимков`
      : database.status === "critical"
        ? "защита требует немедленного внимания"
        : "защита ещё набирает историю",
  );
  setText("db-latest-backup", latest.created_at ? relativeTime(latest.created_at) : "копий пока нет");
  setText(
    "db-integrity-state",
    integrity.ok === true
      ? `OK · ${relativeTime(integrity.checked_at)}`
      : integrity.ok === false
        ? "ОШИБКА"
        : "ещё не проверена",
  );
  setText("db-size", formatBytes(database.database_size_bytes));
  setText("db-free-space", formatBytes(database.free_bytes));

  const domains = Array.isArray(reliability.domains) ? reliability.domains : [];
  replaceChildren(
    "reliability-domain-list",
    domains.length
      ? domains.map((item) => {
          const status = String(item.status || "warning");
          const certificate = Number.isFinite(Number(item.certificate_days_remaining))
            ? `TLS ${formatNumber(item.certificate_days_remaining)} дн.`
            : textPreview(item.error, "TLS неизвестен");
          return node("div", { className: `domain-health-item ${status}`, title: item.error || item.url || "" }, [
            node("i"),
            node("div", {}, [
              node("strong", { text: item.title || item.host || "Веб-контур" }),
              node("small", { text: `${item.http_status || "—"} · ${certificate}` }),
            ]),
            node("b", { text: item.latency_ms === null || item.latency_ms === undefined ? "—" : `${Math.round(Number(item.latency_ms))} мс` }),
          ]);
        })
      : [node("div", { className: "empty-state", text: "Домены ещё не проверены." })],
  );

  const update = reliability.update || {};
  const updateState = String(update.state || "unknown");
  const updateNode = byId("update-state");
  const updateLabels = {
    success: "обновление подтверждено",
    rolled_back: "выполнен автоматический откат",
    failed: "обновление не принято",
    testing: "проверка кандидата",
    deploying: "развёртывание",
    blocked_local_changes: "обновление отложено",
    offline: "GitHub временно недоступен",
    unknown: "нет истории обновлений",
  };
  updateNode.textContent = updateLabels[updateState] || updateState;
  updateNode.className = ["failed", "rolled_back"].includes(updateState)
    ? "failed"
    : ["testing", "deploying", "blocked_local_changes", "offline"].includes(updateState)
      ? "warning"
      : "";
  setText("update-version", update.release || update.new_commit || reliability.release || "unknown");
  setText(
    "update-detail",
    update.message || update.detail || (updateState === "unknown"
      ? "Следующее обновление через Desktop Launcher будет проверено до переключения рабочей версии."
      : "Транзакция обновления завершена."),
  );
  setText("update-time", update.updated_at ? formatDate(update.updated_at) : `аптайм ${formatNumber(reliability.uptime_seconds || 0)} сек.`);
}

function renderSystem(data) {
  if (!showApplication(data)) return;
  const runtime = data.runtime || {};
  const statusRows = data.outbox_status || [];
  const topics = data.outbox_topics || [];
  const deliveries = data.recent_deliveries || [];
  const audio = data.audio_generations || [];
  const workspaces = data.bill_workspaces || [];
  const sessions = data.consensus_sessions || [];
  byId("section-access-card").hidden = data.viewer?.administrator !== true;
  if (data.viewer?.administrator === true) void loadSectionAccess();
  const openStatuses = new Set(["pending", "processing", "retry"]);
  const open = statusRows
    .filter((item) => openStatuses.has(item.status))
    .reduce((sum, item) => sum + Number(item.items || 0), 0);
  const dead = statusRows
    .filter((item) => item.status === "dead")
    .reduce((sum, item) => sum + Number(item.items || 0), 0);
  setText("runtime-ready", runtime.ready ? "online" : "offline");
  setText(
    "runtime-latency",
    runtime.latency_ms === null || runtime.latency_ms === undefined
      ? "задержка неизвестна"
      : `${runtime.latency_ms} мс`,
  );
  setText("system-outbox-open", formatNumber(open));
  setText("system-outbox-dead", formatNumber(dead));
  setText("system-background", formatNumber(audio.length + workspaces.length + sessions.length));
  const health = byId("system-health-badge");
  const healthy = Boolean(runtime.ready) && dead === 0;
  health.textContent = healthy ? "контур в норме" : "требуется внимание";
  health.className = `health-pill${healthy ? "" : " warning"}`;
  renderReliability(data.reliability || {});

  const statusMax = Math.max(...statusRows.map((item) => Number(item.items || 0)), 1);
  replaceChildren(
    "outbox-status-list",
    statusRows.length
      ? statusRows.map((item) =>
          rankingItem(
            item.status || "unknown",
            `${formatNumber(item.attempts)} попыток`,
            formatNumber(item.items),
            statusMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Очередь пуста." })],
  );
  const topicMax = Math.max(...topics.map((item) => Number(item.items || 0)), 1);
  replaceChildren(
    "outbox-topic-list",
    topics.length
      ? topics.map((item) =>
          rankingItem(
            item.topic || "unknown",
            `${formatNumber(item.dead)} dead-letter`,
            formatNumber(item.items),
            topicMax,
          ),
        )
      : [node("div", { className: "empty-state", text: "Потоков доставки нет." })],
  );
  const runtimeRows = [
    ["Guild", runtime.guild_available ? "доступен" : "нет"],
    ["Участников в кеше", formatNumber(runtime.members_cached)],
    ["Каналов в кеше", formatNumber(runtime.channels_cached)],
    ["Музыка", data.music?.available === false ? "отключена" : "готова"],
  ];
  replaceChildren(
    "runtime-metrics",
    runtimeRows.map(([label, value]) =>
      node("div", { className: "metric-row" }, [
        node("span", { text: label }),
        node("b", { text: value }),
      ]),
    ),
  );
  const deliveryRows = deliveries.map((item) =>
    interactiveRow(item, `Доставка #${item.id}`, [
      tableCell(`#${item.id}`),
      tableCell(item.topic || "—"),
      node("td", {}, [statusPill(item.status || "unknown", item.status === "dead" ? "warning" : "")]),
      tableCell(`${formatNumber(item.attempts)} / ${formatNumber(item.max_attempts)}`),
      tableCell(formatDate(item.available_at)),
      tableCell(textPreview(item.last_error, "Без ошибки")),
    ]),
  );
  if (deliveryRows.length) replaceChildren("system-delivery-body", deliveryRows);
  else emptyTable("system-delivery-body", 6, "Проблемных доставок нет.");
  replaceChildren(
    "audio-job-list",
    audio.length
      ? audio.map((item) =>
          compactItem(
            textPreview(item.prompt, `Генерация #${item.id}`),
            `${item.user_display || item.user_id} · ${formatDate(item.created_at)}`,
            item.status,
            () => openDetails(`Аудиогенерация #${item.id}`, item),
          ),
        )
      : [node("div", { className: "empty-state", text: "Аудиогенераций нет." })],
  );
  const allWork = [
    ...sessions.map((item) => ({
      title: `Консенсус №${item.plenary_number}`,
      subtitle: `этап ${item.stage} · ${formatDate(item.updated_at)}`,
      state: `rev ${item.revision}`,
      details: item,
    })),
    ...workspaces.map((item) => ({
      title: item.title || `Редактор #${item.id}`,
      subtitle: `${item.author_display || item.author_id} · ${formatDate(item.updated_at)}`,
      state: item.status,
      details: item,
    })),
  ];
  replaceChildren(
    "system-workspace-list",
    allWork.length
      ? allWork.map((item) =>
          compactItem(item.title, item.subtitle, item.state, () =>
            openDetails(item.title, item.details),
          ),
        )
      : [node("div", { className: "empty-state", text: "Активных процессов нет." })],
  );
}

async function loadSectionAccess() {
  try {
    const data = await fetchJSON("/api/admin/access");
    const select = byId("section-access-select");
    select.replaceChildren(
      ...data.sections.map((section) =>
        node("option", { value: section.id, text: section.label })),
    );
    setText("section-access-count", `${data.grants.length} активных`);
    replaceChildren(
      "section-access-list",
      data.grants.length
        ? data.grants.map((grant) => {
            const revoke = node("button", {
              className: "access-revoke",
              type: "button",
              text: "Отозвать",
            });
            revoke.addEventListener("click", async () => {
              try {
                const result = await postJSON("/api/admin/access", {
                  user_id: grant.user_id_text || String(grant.user_id),
                  section: grant.section,
                  enabled: false,
                });
                showToast(result.message || "Доступ отозван.");
                await loadSectionAccess();
              } catch (error) {
                handleError(error);
              }
            });
            return node("article", { className: "access-grant" }, [
              node("span", {
                className: "access-grant-avatar",
                text: String(grant.member_name || "T").charAt(0).toUpperCase(),
              }),
              node("div", { className: "access-grant-copy" }, [
                node("strong", {
                  text: grant.member_name || `Discord ${grant.user_id}`,
                }),
                node("small", {
                  text: `${grant.section_label} · ${grant.user_id}`,
                }),
              ]),
              revoke,
            ]);
          })
        : [
            node("div", {
              className: "empty-state",
              text: "Точечных доступов пока нет.",
            }),
          ],
    );
  } catch (error) {
    handleError(error);
  }
}

async function loadSystem(silent = false, fresh = false) {
  if (!silent) setLoading(true);
  try {
    renderSystem(await fetchJSON(`/api/admin/system${fresh ? "?fresh=1" : ""}`));
  } catch (error) {
    handleError(error);
  } finally {
    if (!silent) setLoading(false);
  }
}

async function sendSystemAction(action, payload = {}) {
  if (action === "backup_database") {
    const approved = window.TModReactor?.confirm
      ? await window.TModReactor.confirm({
          title: "Создать резервную копию SQLite?",
          message: "T-Mod сделает консистентный снимок через SQLite Backup API и сразу проверит его целостность.",
          accept: "Создать копию",
          tone: "warning",
        })
      : globalThis.confirm("Создать проверенную резервную копию SQLite?");
    if (!approved) return;
  }
  setLoading(true);
  try {
    const result = await postJSON("/api/admin/system/action", { action, ...payload });
    showToast(result.message || "Операция завершена.", result.result?.ok === false);
    await loadSystem(true);
  } catch (error) {
    handleError(error);
  } finally {
    setLoading(false);
  }
}

async function loadAtlas(silent = false) {
  if (!silent) setLoading(true);
  try {
    const data = await fetchJSON("/api/admin/atlas");
    if (!showApplication(data)) return;
    const totals = data.totals || {};
    setText("atlas-organizations", formatNumber(totals.organizations || 0));
    setText("atlas-members", formatNumber(totals.members || 0));
    setText("atlas-documents", formatNumber(totals.documents || 0));
    setText("atlas-sources", formatNumber(totals.indexed_sources || 0));
    const organizations = Array.isArray(data.organizations) ? data.organizations : [];
    const catalog = data.catalog || {};
    const catalogChip = (item) => node(
      "span",
      { className: `atlas-catalog-chip${item.enabled === false || item.enabled === 0 ? " disabled" : ""}` },
      [node("i"), node("b", { text: item.label || item.short_name || item.name || item.code })],
    );
    replaceChildren(
      "atlas-server-list",
      (catalog.servers || []).map(catalogChip),
    );
    replaceChildren(
      "atlas-faction-list",
      (catalog.factions || []).map(catalogChip),
    );
    const fillCatalogSelect = (id, items) => {
      const select = byId(id);
      const selected = select.value;
      select.replaceChildren(...items
        .filter((item) => item.enabled !== false && item.enabled !== 0)
        .map((item) => node("option", { value: item.code, text: item.label || item.name })));
      if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    };
    fillCatalogSelect("atlas-space-server", catalog.servers || []);
    fillCatalogSelect("atlas-space-faction", catalog.factions || []);
    setText("atlas-space-state", `${organizations.length} пространств`);
    replaceChildren(
      "atlas-organization-list",
      organizations.length
        ? organizations.map((item) => compactItem(
            item.name || item.slug,
            `${item.kind || "project"} · ${item.member_count || 0} участников`,
            `${item.document_count || 0} док.`,
            () => openDetails("Пространство Atlas", item),
          ))
        : [node("div", { className: "empty-state", text: "Пространства появятся после первого входа в Atlas." })],
    );
    const ai = data.ai || {};
    const aiReady = ai.configured && ai.qdrant === "ok";
    const aiState = byId("atlas-ai-state");
    aiState.className = `health-pill ${aiReady ? "ok" : "warning"}`;
    aiState.textContent = aiReady ? "готов" : "требует настройки";
    replaceChildren("atlas-ai-metrics", [
      compactItem("OpenRouter", ai.openrouter || "disabled", ai.chat_model || "—"),
      compactItem("Qdrant", ai.qdrant || "disabled", ai.collection || "—"),
      compactItem("Embeddings", "векторизация базы знаний", ai.embedding_model || "—"),
      compactItem("Ошибки индекса", "источники для повторной обработки", formatNumber(totals.failed_sources || 0)),
    ]);
    const events = Array.isArray(data.recent_events) ? data.recent_events : [];
    replaceChildren(
      "atlas-event-list",
      events.length
        ? events.map((item) => compactItem(
            item.summary || item.event_type,
            item.target_type || "Atlas",
            relativeTime(item.created_at),
            () => openDetails("Событие Atlas", item),
          ))
        : [node("div", { className: "empty-state", text: "Событий Atlas пока нет." })],
    );
  } catch (error) {
    handleError(error);
  } finally {
    if (!silent) setLoading(false);
  }
}

function friendlyKey(key) {
  const labels = {
    id: "ID записи",
    actor_id: "ID исполнителя",
    actor_display: "Исполнитель",
    module: "Модуль",
    action_kind: "Тип действия",
    target_type: "Тип объекта",
    target_id: "ID объекта",
    summary: "Сводка",
    status: "Статус",
    created_at: "Создано",
    undone_at: "Отменено",
    undo_reason: "Причина отмены",
    event_kind: "Тип события",
    event_type: "Тип события",
    event_text: "Текст события",
    channel_name: "Канал",
    category_name: "Категория",
    display_name: "Участник",
    details: "Подробности",
    reason: "Причина",
    amount: "Сумма",
    delta: "Изменение",
    balance_before: "Баланс до",
    balance_after: "Баланс после",
    game_code: "Игровой код",
    product_name: "Продукт",
    plan_id: "План",
    stage: "Стадия",
    responsible: "Ответственный",
    materials: "Материалы",
    active_batch: "Активный цикл",
    discord_url: "Источник Discord",
  };
  return labels[key] || key.replace(/_/g, " ");
}

function detailValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  if (String(value).match(/^\d{4}-\d{2}-\d{2}T/)) return formatDate(value);
  return String(value);
}

function openDetails(title, item, route = null, updateHash = true) {
  appState.detailRoute = route?.kind && route?.key ? route : null;
  byId("copy-detail-link").hidden = !appState.detailRoute;
  if (appState.detailRoute && updateHash) {
    history.replaceState(null, "", routeHash(appState.detailRoute));
  }
  setText("detail-title", title);
  const ignored = new Set(["payload_json"]);
  const rows = Object.entries(item || {})
    .filter(([key, value]) => !ignored.has(key) && value !== null && value !== "")
    .map(([key, value]) => {
      const content =
        key === "discord_url" && value
          ? node("a", {
              text: "Открыть сообщение в Discord ↗",
              href: String(value),
              target: "_blank",
              rel: "noopener noreferrer",
            })
          : node("div", { text: detailValue(value) });
      return node("div", { className: "detail-row" }, [
        node("span", { text: friendlyKey(key) }),
        content,
      ]);
    });
  replaceChildren(
    "detail-content",
    rows.length ? rows : [node("div", { className: "empty-state", text: "Нет дополнительных данных." })],
  );
  const dialog = byId("detail-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

async function openLinkedRecord(route) {
  if (!route?.kind || !route?.key || appState.openingRoute) return;
  appState.openingRoute = true;
  try {
    const data = await fetchJSON(
      `/api/admin/link/${cleanRoutePart(route.kind)}/${cleanRoutePart(route.key)}`,
    );
    showApplication(data);
    openDetails(data.title || "Карточка", data.item || {}, route, false);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      showToast(error.payload?.message || "Запись по ссылке не найдена.", true);
      history.replaceState(null, "", routeHash({ section: route.section }));
    } else {
      handleError(error);
    }
  } finally {
    appState.openingRoute = false;
  }
}

function csvCell(value) {
  const text = typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "");
  return `"${text.replace(/"/g, '""').replace(/\r?\n/g, " ")}"`;
}

function exportCSV(filename, rows) {
  if (!rows.length) {
    showToast("В текущей выборке нет строк для экспорта.", true);
    return;
  }
  const keys = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const csv = [
    keys.map(csvCell).join(";"),
    ...rows.map((row) => keys.map((key) => csvCell(row[key])).join(";")),
  ].join("\n");
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = node("a", { href: url });
  link.download = `${filename}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showToast(`Экспортировано строк: ${rows.length}`);
}

async function loadCurrentSection() {
  if (appState.loading) return;
  if (appState.section === "overview") await loadOverview();
  if (appState.section === "modules") await loadRegistry();
  if (appState.section === "audit") await loadAudit();
  if (appState.section === "treasury") await loadFinance();
  if (appState.section === "craft") await loadCraft();
  if (appState.section === "market") await loadMarket();
  if (appState.section === "bills") await loadBills();
  if (appState.section === "sgl") await loadSgl();
  if (appState.section === "members") await loadMembers();
  if (appState.section === "communications") await loadCommunications();
  if (appState.section === "media") await loadMedia();
  if (appState.section === "profile") await loadProfile();
  if (appState.section === "discord") await loadDiscord();
  if (appState.section === "system") await loadSystem();
  if (appState.section === "atlas") await loadAtlas();
  if (appState.section === "minecraft") {
    await globalThis.TModReactor?.activateMinecraft?.();
  }
}

async function refreshCurrentSection(silent = false, forceFresh = false) {
  if (appState.loading || appState.autoRefreshing) return;
  appState.autoRefreshing = silent;
  appState.forceFresh = forceFresh;
  try {
    await loadCurrentSection();
  } finally {
    appState.autoRefreshing = false;
    appState.forceFresh = false;
  }
}

function currentRefreshDelay() {
  return REFRESH_INTERVALS[appState.section] || 60000;
}

function scheduleAutoRefresh(delay = currentRefreshDelay()) {
  clearTimeout(appState.refreshTimer);
  const seconds = Math.max(1, Math.round(delay / 1000));
  setText("refresh-label", `Авто · ${seconds} сек`);
  appState.refreshTimer = setTimeout(async () => {
    try {
      const excluded = ["media", "profile", "minecraft"].includes(appState.section);
      if (
        !document.hidden
        && appState.authorized
        && !appState.loading
        && !byId("admin-shell").hidden
        && !excluded
      ) {
        await refreshCurrentSection(true);
      }
    } finally {
      scheduleAutoRefresh();
    }
  }, delay);
}

async function pollGlobalActivity() {
  if (
    ["overview", "audit", "craft", "discord"].includes(appState.section)
    || !appState.authorized
    || !appState.allowedSections.includes("overview")
  ) {
    return;
  }
  try {
    const data = await fetchJSON(`/api/admin/overview?days=${appState.days}`);
    rememberActivity(data, "overview", true);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      handleError(error);
    }
  }
}

function bindEvents() {
  byId("operation-action").addEventListener("change", renderOperationFields);
  byId("operation-close").addEventListener("click", closeOperation);
  byId("operation-cancel").addEventListener("click", closeOperation);
  byId("operation-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitOperation();
  });
  byId("finance-command").addEventListener("click", () => openFinanceOperation());
  byId("craft-plan-create").addEventListener("click", openCraftPlanCreate);
  byId("craft-recipe-create").addEventListener("click", openCraftRecipeCreate);
  byId("section-access-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = formValues("section-access-form");
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const result = await postJSON("/api/admin/access", {
        user_id: values.user_id,
        section: values.section,
        enabled: true,
      });
      showToast(result.message || "Доступ выдан.", false, {
        title: result.dm_sent
          ? "Доступ и уведомление готовы"
          : "Доступ готов",
        icon: "⌁",
        sound: "success",
      });
      event.currentTarget.reset();
      await loadSectionAccess();
    } catch (error) {
      handleError(error);
    } finally {
      button.disabled = false;
    }
  });
  byId("atlas-server-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = formValues("atlas-server-form");
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await postJSON("/api/admin/atlas/catalog", {
        resource: "server",
        code: values.code,
        name: values.name,
        number: values.number || null,
        enabled: true,
      });
      event.currentTarget.reset();
      showToast("Сервер добавлен в Atlas.", false, { title: "Каталог обновлён", icon: "◎" });
      await loadAtlas(true);
    } catch (error) { handleError(error); }
    finally { button.disabled = false; }
  });
  byId("atlas-faction-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = formValues("atlas-faction-form");
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await postJSON("/api/admin/atlas/catalog", {
        resource: "faction",
        code: values.code,
        name: values.name,
        short_name: values.short_name,
        enabled: true,
      });
      event.currentTarget.reset();
      showToast("Организация добавлена для всех серверов Atlas.", false, { title: "Каталог обновлён", icon: "◇" });
      await loadAtlas(true);
    } catch (error) { handleError(error); }
    finally { button.disabled = false; }
  });
  byId("atlas-space-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = formValues("atlas-space-form");
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await postJSON("/api/admin/atlas/spaces", values);
      event.currentTarget.reset();
      showToast("Частное пространство Atlas создано.", false, { title: "Контур готов", icon: "✦" });
      await loadAtlas(true);
    } catch (error) { handleError(error); }
    finally { button.disabled = false; }
  });
  document.querySelectorAll("[data-section]").forEach((button) => {
    button.addEventListener("click", () => switchSection(button.dataset.section));
  });
  document.querySelectorAll("[data-go]").forEach((button) => {
    button.addEventListener("click", () => switchSection(button.dataset.go));
  });
  byId("refresh-button").addEventListener("click", () =>
    refreshCurrentSection(false, true),
  );
  byId("system-refresh-reliability").addEventListener("click", () =>
    loadSystem(false, true),
  );
  byId("system-backup-database").addEventListener("click", () =>
    sendSystemAction("backup_database"),
  );
  byId("system-check-database").addEventListener("click", () =>
    sendSystemAction("check_database"),
  );
  byId("notification-toggle").addEventListener("click", async () => {
    appState.soundEnabled = !appState.soundEnabled;
    persistSoundPreference(appState.soundEnabled);
    updateNotificationToggle();
    if (appState.soundEnabled) {
      await unlockNotificationSound();
      playNotificationSound("success");
      showToast(
        "Новые события будут сопровождаться мягким звуковым сигналом.",
        false,
        { title: "Звук включён", icon: "◉" },
      );
    } else {
      showToast(
        "Визуальные уведомления останутся активными.",
        false,
        { title: "Звук выключен", icon: "○" },
      );
    }
  });
  document.addEventListener(
    "pointerdown",
    () => {
      void unlockNotificationSound();
    },
    { once: true, capture: true },
  );
  document.addEventListener("visibilitychange", () => {
    if (document.hidden || !appState.authorized) return;
    void refreshCurrentSection(true);
    void pollGlobalActivity();
    scheduleAutoRefresh();
  });
  byId("copy-section-link").addEventListener("click", () =>
    copyRoute(
      { section: appState.section },
      `Ссылка на раздел «${sectionMeta[appState.section][1]}» скопирована`,
    ),
  );
  byId("copy-detail-link").addEventListener("click", () => {
    if (appState.detailRoute) {
      copyRoute(appState.detailRoute, "Ссылка на карточку скопирована");
    }
  });
  byId("detail-dialog").addEventListener("close", () => {
    if (!appState.detailRoute) return;
    appState.detailRoute = null;
    byId("copy-detail-link").hidden = true;
    history.replaceState(null, "", routeHash({ section: appState.section }));
  });
  byId("period-select").addEventListener("change", (event) => {
    appState.days = Number(event.target.value) || 30;
    for (const key of Object.keys(appState.offsets)) appState.offsets[key] = 0;
    loadCurrentSection();
  });

  const forms = [
    ["audit-filters", () => loadAudit(0)],
    ["finance-filters", () => loadFinance(0)],
    ["craft-filters", () => loadCraft(0)],
    ["market-filters", () => loadMarket(0)],
    ["bills-filters", loadBills],
    ["sgl-filters", () => loadSgl(0)],
    ["members-filters", () => loadMembers(0)],
    ["discord-filters", () => loadDiscord(0)],
  ];
  for (const [id, callback] of forms) {
    byId(id).addEventListener("submit", (event) => {
      event.preventDefault();
      callback();
    });
  }

  byId("audit-export").addEventListener("click", () =>
    exportCSV("tmod-audit", appState.rows.audit),
  );
  byId("finance-export").addEventListener("click", () =>
    exportCSV("tmod-treasury", appState.rows.finance),
  );
  byId("craft-export").addEventListener("click", () =>
    exportCSV("tmod-crafts", appState.rows.craft),
  );
  byId("discord-export").addEventListener("click", () =>
    exportCSV("tmod-discord-audit", appState.rows.discord),
  );
  byId("sgl-export").addEventListener("click", () =>
    exportCSV("tmod-sgl-cases", appState.rows.sgl),
  );
  byId("members-export").addEventListener("click", () =>
    exportCSV("tmod-members", appState.rows.members),
  );
  document.querySelectorAll("[data-media-target][data-media-action]").forEach((button) => {
    button.addEventListener("click", () =>
      sendMediaCommand(button.dataset.mediaTarget, button.dataset.mediaAction),
    );
  });
  byId("music-enqueue").addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues("music-enqueue");
    if (!values.query) return;
    sendMediaCommand("music", "enqueue", { query: values.query }).then(() => {
      event.target.reset();
    });
  });
  byId("music-volume").addEventListener("input", (event) => {
    setText("music-volume-value", `${event.target.value}%`);
  });
  byId("music-volume").addEventListener("change", (event) => {
    sendMediaCommand("music", "volume", { percent: Number(event.target.value) });
  });
  byId("market-alert-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues("market-alert-form");
    sendMarketAlertAction("upsert", {
      server_id: values.server_id,
      category: values.category,
      item_id: Number(values.item_id),
      target_price: Number(values.target_price),
      min_quantity: Number(values.min_quantity),
    });
  });
  byId("market-alert-cancel").addEventListener("click", () => {
    byId("market-alert-form").hidden = true;
  });
  byId("broadcast-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendBroadcast();
  });
  window.addEventListener("hashchange", async () => {
    const route = parseAdminRoute();
    if (route.section !== appState.section) {
      await switchSection(route.section, false);
    } else if (!route.kind) {
      appState.detailRoute = null;
      const dialog = byId("detail-dialog");
      if (dialog.open) dialog.close();
    }
    if (route.kind && route.key) {
      await openLinkedRecord(route);
    }
  });
}

async function bootstrap() {
  bindEvents();
  appState.soundEnabled = storedSoundPreference();
  updateNotificationToggle();
  let requestedRoute = parseAdminRoute();
  try {
    const access = await fetchJSON("/api/admin/access/self");
    appState.allowedSections = access.sections || [];
    document.querySelectorAll("[data-section]").forEach((button) => {
      button.hidden = !appState.allowedSections.includes(button.dataset.section);
    });
    if (!appState.allowedSections.includes(requestedRoute.section)) {
      requestedRoute = { section: appState.allowedSections[0], kind: "", key: "" };
      history.replaceState(null, "", routeHash(requestedRoute));
    }
    appState.section = requestedRoute.section;
    if (requestedRoute.section === "minecraft") showApplication(access);
  } catch (error) {
    handleError(error);
    return;
  }
  appState.section = requestedRoute.section;
  document.body.dataset.section = appState.section;
  document.querySelectorAll("[data-screen]").forEach((screen) => {
    screen.classList.toggle("active", screen.dataset.screen === appState.section);
  });
  document.querySelectorAll("[data-section]").forEach((button) => {
    button.classList.toggle("active", button.dataset.section === appState.section);
  });
  setText("section-eyebrow", sectionMeta[appState.section][0]);
  setText("section-title", sectionMeta[appState.section][1]);

  // Every protected section carries the viewer context, so direct links no
  // longer wait for the heavy overview before loading their actual content.
  // Minecraft is rendered by reactor.js and needs one lightweight cached
  // overview request to establish the shared authenticated shell first.
  if (appState.section === "minecraft") {
    // access/self already established the authenticated shell without exposing overview data.
  }
  else await loadCurrentSection();
  if (!byId("admin-shell").hidden && requestedRoute.kind && requestedRoute.key) {
    await openLinkedRecord(requestedRoute);
  }
  scheduleAutoRefresh();
  appState.globalRefreshTimer = setInterval(() => {
    if (!document.hidden && appState.authorized && !byId("admin-shell").hidden) {
      void pollGlobalActivity();
    }
  }, GLOBAL_ACTIVITY_INTERVAL);
  appState.mediaRefreshTimer = setInterval(() => {
    if (
      !document.hidden &&
      !byId("admin-shell").hidden &&
      appState.section === "media" &&
      !appState.loading
    ) {
      loadMedia(true);
    }
  }, MEDIA_REFRESH_INTERVAL);
  window.addEventListener("pagehide", () => {
    clearTimeout(appState.refreshTimer);
    clearInterval(appState.globalRefreshTimer);
    clearInterval(appState.mediaRefreshTimer);
  }, { once: true });
}

window.TModAdmin = Object.freeze({
  fetchJSON,
  postJSON,
  showToast,
  switchSection,
  formValues,
  get csrfToken() {
    return appState.csrfToken;
  },
  get authorized() {
    return appState.authorized;
  },
  get administrator() {
    return appState.administrator;
  },
});

bootstrap();
