"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { csrf: "", cases: [], events: {}, filter: "active", selected: null, busy: false };
  const statuses = {
    new: ["НОВЫЕ", "Ожидают сотрудника"],
    screening: ["ПРОВЕРКА", "Досье в работе"],
    needs_info: ["СВЕДЕНИЯ", "Нужно дополнение"],
    closed: ["РЕШЕНИЕ", "Завершённые дела"],
  };
  const statusLabels = { new: "Новое", screening: "Проверка", needs_info: "Нужны сведения", approved: "Допущен", denied: "Отказ" };
  const riskLabels = { unrated: "Риск не определён", low: "Низкий риск", medium: "Средний риск", high: "Высокий риск", critical: "Критический риск" };
  const actionLabels = { created: "Дело зарегистрировано", claim: "Дело принято", note: "Добавлена запись", update: "Досье обновлено", needs_info: "Запрошены сведения", approve: "Кандидат допущен", deny: "В допуске отказано" };

  const node = (tag, className = "", text = "") => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== "") item.textContent = String(text);
    return item;
  };

  function timeoutSignal(ms) {
    if (globalThis.AbortSignal?.timeout) return AbortSignal.timeout(ms);
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      signal: options.signal || timeoutSignal(options.method === "POST" ? 45000 : 12000),
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json", "X-CSRF-Token": state.csrf } : {}),
        ...(options.headers || {}),
      },
    });
    let data = {};
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const error = new Error(data.message || data.error || `HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function toast(message, kind = "success") {
    const item = byId("ovr-toast");
    item.textContent = message;
    item.dataset.kind = kind;
    item.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { item.hidden = true; }, 4200);
  }

  function setBusy(active) {
    state.busy = active;
    byId("ovr-busy").hidden = !active;
  }

  function formatMoment(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  function deadlineMeta(item) {
    if (["approved", "denied"].includes(item.status)) return { label: `Закрыто ${formatMoment(item.decided_at)}`, tone: "" };
    const time = new Date(item.due_at).getTime();
    if (!Number.isFinite(time)) return { label: "Срок не указан", tone: "" };
    const hours = (time - Date.now()) / 3600000;
    if (hours < 0) return { label: `Просрочено на ${Math.max(1, Math.ceil(-hours))} ч`, tone: "overdue" };
    if (hours < 12) return { label: `Осталось ${Math.max(1, Math.ceil(hours))} ч`, tone: "soon" };
    return { label: `До ${formatMoment(item.due_at)}`, tone: "" };
  }

  function laneFor(item) {
    return ["approved", "denied"].includes(item.status) ? "closed" : item.status;
  }

  function visibleCases() {
    const query = byId("ovr-search").value.trim().toLowerCase();
    return state.cases.filter((item) => {
      const isClosed = ["approved", "denied"].includes(item.status);
      if (state.filter === "active" && isClosed) return false;
      if (state.filter === "closed" && !isClosed) return false;
      if (!query) return true;
      return [item.case_number, item.first_name, item.last_name, item.static_id, item.discord_text]
        .some((value) => String(value || "").toLowerCase().includes(query));
    });
  }

  function renderMetrics() {
    const active = state.cases.filter((item) => !["approved", "denied"].includes(item.status));
    byId("metric-active").textContent = String(active.length);
    byId("metric-info").textContent = String(active.filter((item) => item.status === "needs_info").length);
    byId("metric-due").textContent = String(active.filter((item) => {
      const hours = (new Date(item.due_at).getTime() - Date.now()) / 3600000;
      return Number.isFinite(hours) && hours < 12;
    }).length);
    byId("metric-closed").textContent = String(state.cases.length - active.length);
  }

  function caseCard(item) {
    const card = node("button", "ovr-card");
    card.type = "button";
    card.dataset.risk = item.risk_level || "unrated";
    const top = node("div", "card-top");
    top.append(node("span", "", `ОВР-${String(item.case_number).padStart(3, "0")}`));
    const risk = node("b", "risk-pill", riskLabels[item.risk_level] || riskLabels.unrated);
    risk.dataset.risk = item.risk_level || "unrated";
    top.append(risk);
    const meta = node("div", "card-meta");
    meta.append(node("span", "", `Статик ${item.static_id}`), node("span", "", "·"), node("span", "", item.discord_text));
    const footer = node("footer");
    footer.append(node("span", "", item.assigned_to_display || "Не назначено"));
    const deadline = deadlineMeta(item);
    footer.append(node("span", `deadline ${deadline.tone}`, deadline.label));
    card.append(top, node("h3", "", `${item.first_name} ${item.last_name}`), meta, footer);
    card.addEventListener("click", () => openCase(item));
    return card;
  }

  function renderBoard() {
    const items = visibleCases();
    byId("ovr-board").replaceChildren(...Object.entries(statuses).map(([status, [title, subtitle]]) => {
      const lane = node("section", "case-lane");
      const selected = items.filter((item) => laneFor(item) === status);
      const heading = node("header");
      const copy = node("div");
      copy.append(node("strong", "", title), node("small", "", subtitle));
      heading.append(copy, node("b", "", selected.length));
      const list = node("div", "case-list");
      if (selected.length) list.append(...selected.map(caseCard));
      else list.append(node("div", "lane-empty", "В этой группе дел нет."));
      lane.append(heading, list);
      return lane;
    }));
    renderMetrics();
  }

  function fact(label, value, href = "") {
    const item = node("span");
    item.append(node("small", "", label));
    if (href) {
      const link = node("a", "", value || "Открыть"); link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer"; item.append(link);
    } else item.append(node("strong", "", value || "—"));
    return item;
  }

  function renderTimeline(item) {
    const events = state.events[String(item.id)] || [];
    byId("case-timeline").replaceChildren(...events.map((event) => {
      const row = node("article", "timeline-item");
      row.append(
        node("strong", "", actionLabels[event.action] || event.action),
        node("p", "", event.note || "Без дополнительной записи."),
        node("small", "", `${event.actor_display || "T-Mod"} · ${formatMoment(event.created_at)}`),
      );
      return row;
    }));
    if (!events.length) byId("case-timeline").append(node("div", "lane-empty", "История пока пуста."));
  }

  function actionButton(label, action, tone = "secondary") {
    const button = node("button", `button button-${tone}`, label);
    button.type = "button";
    button.addEventListener("click", () => void updateCase(action));
    return button;
  }

  function openCase(item) {
    state.selected = item;
    byId("case-kicker").textContent = `ДЕЛО ОВР-${String(item.case_number).padStart(3, "0")} · ${statusLabels[item.status] || item.status}`;
    byId("case-title").textContent = `${item.first_name} ${item.last_name}`;
    byId("case-subtitle").textContent = `${deadlineMeta(item).label} · ответственный: ${item.assigned_to_display || "не назначен"}`;
    byId("case-facts").replaceChildren(
      fact("СТАТИК", item.static_id), fact("DISCORD", item.discord_text), fact("ФОРУМ", item.forum_url ? "Открыть профиль" : "Не указан", item.forum_url || ""),
      fact("ИНИЦИАТОР", item.created_by_display), fact("СОЗДАНО", formatMoment(item.created_at)), fact("РИСК", riskLabels[item.risk_level] || riskLabels.unrated),
    );
    byId("case-findings").value = item.findings || "";
    byId("case-nowa").value = item.nowa_links || "";
    byId("case-risk").value = item.risk_level || "unrated";
    byId("case-note").value = "";
    const actions = byId("case-actions");
    actions.replaceChildren();
    const closed = ["approved", "denied"].includes(item.status);
    [byId("case-findings"), byId("case-nowa"), byId("case-risk"), byId("case-note")].forEach((field) => { field.disabled = closed; });
    if (!closed) {
      if (!item.assigned_to_id) actions.append(actionButton("Взять дело", "claim", "secondary"));
      actions.append(actionButton("Сохранить досье", "update", "secondary"), actionButton("Запросить сведения", "needs_info", "warning"), actionButton("Допустить", "approve", "primary"), actionButton("Отказать", "deny", "danger"));
    } else actions.append(node("span", "status-pill", `Дело закрыто: ${statusLabels[item.status]}`));
    renderTimeline(item);
    byId("ovr-case-dialog").showModal();
  }

  async function updateCase(action) {
    const item = state.selected;
    if (!item || state.busy) return;
    const note = byId("case-note").value.trim();
    if (["needs_info", "approve", "deny"].includes(action) && note.length < 3) {
      toast("Добавьте служебное основание действия.", "error"); return;
    }
    setBusy(true);
    try {
      await request("/api/ovr", { method: "POST", body: JSON.stringify({ action, case_id: item.id, expected_revision: item.revision, findings: byId("case-findings").value, nowa_links: byId("case-nowa").value, risk_level: byId("case-risk").value, note }) });
      byId("ovr-case-dialog").close();
      await load(true);
      toast(action === "approve" ? "Кандидат допущен." : action === "deny" ? "Решение об отказе сохранено." : "Досье обновлено.");
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(false); }
  }

  async function load(silent = false) {
    try {
      const data = await request("/api/ovr");
      state.csrf = data.viewer.csrf_token;
      state.cases = Array.isArray(data.cases) ? data.cases : [];
      state.events = data.events || {};
      renderBoard();
      byId("ovr-sync").textContent = `Синхронизировано ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
      byId("ovr-gate").hidden = true;
      byId("ovr-shell").hidden = false;
      requestAnimationFrame(() => byId("ovr-shell").classList.add("ready"));
    } catch (error) {
      if (error.status === 401) {
        byId("ovr-gate-message").textContent = "Сессия не найдена. Войдите с логином и восьмизначным PIN.";
        byId("ovr-login").hidden = false;
      } else if (!silent) {
        byId("ovr-gate-message").textContent = error.message;
        toast(error.message, "error");
      }
    }
  }

  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => byId(button.dataset.close).close()));
  byId("ovr-new").addEventListener("click", () => byId("ovr-create-dialog").showModal());
  byId("ovr-refresh").addEventListener("click", () => void load());
  byId("ovr-search").addEventListener("input", renderBoard);
  byId("ovr-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    state.filter = button.dataset.filter;
    byId("ovr-filters").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    renderBoard();
  });
  byId("ovr-create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy) return;
    setBusy(true);
    try {
      await request("/api/ovr", { method: "POST", body: JSON.stringify({ action: "create", first_name: byId("ovr-first-name").value.trim(), last_name: byId("ovr-last-name").value.trim(), static_id: byId("ovr-static").value.trim(), discord_text: byId("ovr-discord").value.trim(), forum_url: byId("ovr-forum").value.trim(), additional_info: byId("ovr-additional").value.trim() }) });
      byId("ovr-create-dialog").close(); event.currentTarget.reset(); await load(true); toast("Дело зарегистрировано. Срок проверки — 48 часов.");
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(false); }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.busy) void load(true); });
  setInterval(() => { if (!document.hidden && !state.busy) void load(true); }, 60000);
  void load();
})();
