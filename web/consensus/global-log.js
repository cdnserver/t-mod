(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const authShell = $("#authShell");
  const appShell = $("#appShell");
  const SAVED_VIEWS_KEY = "tmod.global-log.saved-views.v2";
  const LIVE_INTERVAL = 10000;
  const RETRYABLE_STATUSES = new Set([500, 502, 503, 504]);
  const FILTER_LABELS = {
    q: "Поиск", source: "Сервис", source_type: "Тип", event_type: "Событие",
    severity: "Важность", actor: "Участник", channel: "Канал", status: "HTTP",
    from: "После", to: "До",
  };
  const RELATION_LABELS = {
    request_id: "Запрос", session_id: "Сессия", message_id: "Сообщение",
    actor_user_id: "Участник", channel_id: "Канал", target_id: "Объект",
  };
  const GROUP_LABELS = {
    request_id: "Запрос", session_id: "Сессия", actor_user_id: "Участник",
    message_id: "Сообщение", source_service: "Сервис",
  };

  const state = {
    cursor: null,
    events: [],
    filters: {},
    localRelation: null,
    loading: false,
    timer: null,
    healthTimer: null,
    live: true,
    view: "timeline",
    group: "none",
    freshIds: new Set(),
    collapsedGroups: new Set(),
    selectedEvent: null,
    savedViews: [],
    initialized: false,
  };

  const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const safeDate = (value) => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const formatTime = (value) => {
    const date = safeDate(value);
    return date ? new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "medium" }).format(date) : "—";
  };
  const formatTimelineTime = (value) => {
    const date = safeDate(value);
    if (!date) return { time: "—", date: "" };
    return {
      time: date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
      date: date.toLocaleDateString("ru-RU", { day: "2-digit", month: "short" }),
    };
  };
  const localDateTimeValue = (date) => {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 16);
  };
  const displayValue = (value) => value === null || value === undefined || value === "" ? "—" : String(value);
  const truncate = (value, length = 72) => {
    const text = displayValue(value);
    return text.length > length ? `${text.slice(0, length - 1)}…` : text;
  };

  function friendlyError(error) {
    if (error && error.name === "AbortError") return "Сервис отвечает слишком долго. Запрос остановлен безопасно.";
    if (error && error.status === 504) return "Шлюз не дождался ответа. Данные сохранены — повторите запрос через несколько секунд.";
    if (error && RETRYABLE_STATUSES.has(Number(error.status))) return `Сервис временно недоступен (HTTP ${error.status}). Панель продолжит повторные попытки.`;
    return error && error.message ? error.message : "Не удалось получить данные.";
  }

  async function api(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const attempts = method === "GET" && options.retry !== false ? 3 : 1;
    let lastError = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), Number(options.timeout || 15000));
      try {
        const response = await fetch(path, {
          credentials: "same-origin",
          ...options,
          signal: controller.signal,
          headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401 && !path.endsWith("/login")) showLogin();
        if (!response.ok) {
          const error = Object.assign(new Error(data.error || `HTTP ${response.status}`), { status: response.status, data });
          if (attempt + 1 < attempts && RETRYABLE_STATUSES.has(response.status)) {
            lastError = error;
            await sleep(450 * (attempt + 1));
            continue;
          }
          throw error;
        }
        return data;
      } catch (error) {
        lastError = error;
        const retryableNetworkError = error && (error.name === "AbortError" || error instanceof TypeError);
        if (attempt + 1 < attempts && retryableNetworkError) {
          await sleep(450 * (attempt + 1));
          continue;
        }
        throw error;
      } finally {
        clearTimeout(timeout);
      }
    }
    throw lastError || new Error("request_failed");
  }

  function showLogin() {
    authShell.hidden = false;
    appShell.hidden = true;
    clearInterval(state.timer);
    clearInterval(state.healthTimer);
    state.timer = null;
    state.healthTimer = null;
  }

  function showApp() {
    authShell.hidden = true;
    appShell.hidden = false;
  }

  function showNotice(message, retry = true) {
    $("#systemNoticeText").textContent = message;
    $("#noticeRetry").hidden = !retry;
    $("#systemNotice").hidden = false;
  }

  function hideNotice() {
    $("#systemNotice").hidden = true;
  }

  let toastTimer = null;
  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
  }

  function applyRuntimeHealth(runtime) {
    const health = runtime || {};
    const healthy = Boolean(health.running && !health.last_error);
    const pill = $("#runtimeHealth");
    pill.className = `health-pill ${healthy ? "ok" : "fail"}`;
    $("span", pill).textContent = healthy ? "Журнал активен" : "Нарушение потока";
    $("#queueEvents").textContent = Number(health.queued || 0).toLocaleString("ru-RU");
    if (health.last_error) showNotice(`Поток записи сообщает об ошибке: ${health.last_error}`);
  }

  function fillSelect(name, items) {
    const select = document.querySelector(`[name="${name}"]`);
    if (!select) return;
    const current = select.value;
    while (select.options.length > 1) select.remove(1);
    for (const item of items || []) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = `${item.value} · ${item.count}`;
      select.append(option);
    }
    select.value = current;
  }

  async function loadFacets() {
    try {
      const data = await api("/api/global-log/facets");
      $("#totalEvents").textContent = Number(data.total || 0).toLocaleString("ru-RU");
      $("#eventRange").textContent = data.first_at ? `${formatTime(data.first_at)} — ${formatTime(data.last_at)}` : "Журнал пока пуст";
      fillSelect("source", data.sources);
      fillSelect("source_type", data.source_types);
      fillSelect("event_type", data.event_types);
      fillSelect("severity", data.severities);
    } catch (error) {
      showNotice(`Сводные данные временно недоступны. ${friendlyError(error)}`);
    }
  }

  function query(filters, cursor) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
    if (cursor) params.set("cursor", cursor);
    params.set("limit", "100");
    return params.toString();
  }

  function relationEntries(event) {
    return ["request_id", "session_id", "message_id", "actor_user_id", "channel_id", "target_id"]
      .filter((key) => event && event[key] !== null && event[key] !== undefined && event[key] !== "")
      .map((key) => ({ key, value: String(event[key]), label: RELATION_LABELS[key] || key }));
  }

  function relationMatches(event, relation) {
    return !relation || String(event[relation.key] ?? "") === String(relation.value);
  }

  function visibleEvents() {
    return state.localRelation ? state.events.filter((event) => relationMatches(event, state.localRelation)) : state.events;
  }

  function groupEvents(events) {
    if (state.group === "none") return [{ key: "all", label: "", events }];
    const groups = new Map();
    for (const event of events) {
      const raw = event[state.group];
      const key = raw === null || raw === undefined || raw === "" ? "__ungrouped__" : String(raw);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(event);
    }
    return Array.from(groups, ([key, groupItems]) => ({
      key,
      label: key === "__ungrouped__" ? `Без связи · ${GROUP_LABELS[state.group] || state.group}` : `${GROUP_LABELS[state.group] || state.group}: ${key}`,
      events: groupItems,
    }));
  }

  function statusNode(event) {
    if (!event.status_code) return null;
    const node = element("span", Number(event.status_code) < 400 ? "status-good" : "status-bad", event.status_code);
    node.title = `HTTP ${event.status_code}`;
    return node;
  }

  function openByKeyboard(node, callback) {
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.addEventListener("click", callback);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        callback();
      }
    });
  }

  function renderTimeline(events) {
    const list = element("div", "timeline-list");
    for (const event of events) {
      const node = element("article", `event-node severity-${event.severity || "info"}${state.freshIds.has(String(event.id)) ? " new-event" : ""}`);
      const timeValue = formatTimelineTime(event.occurred_at);
      const time = element("time", "event-time");
      time.dateTime = event.occurred_at || "";
      time.append(element("b", "", timeValue.time), document.createTextNode(timeValue.date));
      const marker = element("span", "node-marker");
      const panel = element("div", "event-panel");
      openByKeyboard(panel, () => openDetail(event));

      const primary = element("div", "event-primary");
      const titleRow = element("div", "event-title-row");
      titleRow.append(element("strong", "event-title", event.event_type || "event"));
      titleRow.append(element("span", `badge ${event.severity || ""}`, event.source_service || "unknown"));
      primary.append(titleRow, element("div", "event-summary", event.summary || "Без описания"));

      const side = element("div", "event-side");
      const status = statusNode(event);
      if (status) side.append(status);
      if (event.duration_ms !== null && event.duration_ms !== undefined) side.append(element("span", "duration", `${event.duration_ms} ms`));

      const context = element("div", "event-context");
      const actor = event.actor_display || event.actor_user_id;
      if (actor) context.append(element("span", "relation-badge", `@ ${truncate(actor, 40)}`));
      for (const relation of relationEntries(event).filter((item) => item.key !== "actor_user_id").slice(0, 4)) {
        context.append(element("span", "relation-badge", `${relation.label}: ${truncate(relation.value, 46)}`));
      }

      panel.append(primary, side, context);
      node.append(time, marker, panel);
      list.append(node);
    }
    return list;
  }

  function renderTable(events) {
    const wrap = element("div", "event-table-wrap");
    const table = element("table", "event-table");
    const head = element("thead");
    const headRow = element("tr");
    for (const label of ["Время", "Источник", "Событие", "Участник", "Сводка", "Связь", "Результат"]) headRow.append(element("th", "", label));
    head.append(headRow);
    const body = element("tbody");
    for (const event of events) {
      const row = element("tr", state.freshIds.has(String(event.id)) ? "new-event" : "");
      openByKeyboard(row, () => openDetail(event));
      row.append(element("td", "mono-cell", formatTime(event.occurred_at)));
      const sourceCell = element("td");
      sourceCell.append(element("span", `badge ${event.severity || ""}`, event.source_service || "unknown"));
      row.append(sourceCell, element("td", "event-title", event.event_type || "event"));
      row.append(element("td", "", event.actor_display || event.actor_user_id || "Система"));
      row.append(element("td", "summary-cell", event.summary || "Без описания"));
      const relation = relationEntries(event)[0];
      row.append(element("td", "mono-cell", relation ? `${relation.label}: ${truncate(relation.value, 36)}` : "—"));
      const result = element("td");
      const status = statusNode(event);
      result.append(status || document.createTextNode("—"));
      row.append(result);
      body.append(row);
    }
    table.append(head, body);
    wrap.append(table);
    return wrap;
  }

  function renderEventCollection(events) {
    return state.view === "table" ? renderTable(events) : renderTimeline(events);
  }

  function renderEvents() {
    const surface = $("#eventsSurface");
    surface.replaceChildren();
    const events = visibleEvents();
    const groups = groupEvents(events);

    if (state.group === "none") {
      surface.append(renderEventCollection(events));
    } else {
      for (const group of groups) {
        const wrapper = element("section", `event-group${state.collapsedGroups.has(group.key) ? " collapsed" : ""}`);
        const head = element("button", "group-head");
        head.type = "button";
        const chevron = element("span", "chevron", "⌄");
        const title = element("strong", group.key === "__ungrouped__" ? "ungrouped-label" : "", group.label);
        const count = element("small", "", `${group.events.length} событий`);
        head.append(chevron, title, count);
        head.addEventListener("click", () => {
          wrapper.classList.toggle("collapsed");
          if (wrapper.classList.contains("collapsed")) state.collapsedGroups.add(group.key);
          else state.collapsedGroups.delete(group.key);
        });
        const content = element("div", "group-content");
        content.append(renderEventCollection(group.events));
        wrapper.append(head, content);
        surface.append(wrapper);
      }
    }

    $("#emptyState").hidden = events.length > 0;
    surface.hidden = events.length === 0;
    const relationSuffix = state.localRelation ? ` · цепочка «${state.localRelation.label}»` : "";
    $("#resultLabel").textContent = `${events.length.toLocaleString("ru-RU")} событий загружено${relationSuffix}`;
    renderActiveFilters();
    updateQuickChips();
    setTimeout(() => state.freshIds.clear(), 1300);
  }

  async function loadEvents({ append = false, silent = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    if (!silent) $("#resultLabel").textContent = "Загрузка событий…";
    try {
      const data = await api(`/api/global-log/events?${query(state.filters, append ? state.cursor : null)}`);
      const received = Array.isArray(data.events) ? data.events : [];
      const previousIds = new Set(state.events.map((item) => String(item.id)));
      if (append) {
        const known = new Set(previousIds);
        state.events.push(...received.filter((item) => !known.has(String(item.id))));
        state.cursor = data.next_cursor;
      } else if (silent && state.events.length) {
        state.freshIds = new Set(received.filter((item) => !previousIds.has(String(item.id))).map((item) => String(item.id)));
        const incoming = new Set(received.map((item) => String(item.id)));
        state.events = [...received, ...state.events.filter((item) => !incoming.has(String(item.id)))].slice(0, 2000);
      } else {
        state.freshIds = new Set();
        state.events = received;
        state.cursor = data.next_cursor;
      }
      if (!silent || !state.cursor) state.cursor = data.next_cursor;
      renderEvents();
      $("#loadMore").hidden = !data.has_more && !append;
      if (append) $("#loadMore").hidden = !data.has_more;
      const now = new Date();
      $("#lastRefresh").textContent = now.toLocaleTimeString("ru-RU");
      $("#refreshDetail").textContent = state.live ? "поток синхронизирован" : "автообновление приостановлено";
      hideNotice();
      openDeepLinkedEvent();
    } catch (error) {
      $("#resultLabel").textContent = `Не удалось обновить · ${friendlyError(error)}`;
      showNotice(friendlyError(error));
    } finally {
      state.loading = false;
    }
  }

  function formFilters() {
    const data = new FormData($("#filterForm"));
    const output = {};
    for (const [key, value] of data) if (String(value).trim()) output[key] = String(value).trim();
    return output;
  }

  function applyFiltersToForm() {
    for (const name of Object.keys(FILTER_LABELS)) {
      const control = document.querySelector(`[name="${name}"]`);
      if (control) control.value = state.filters[name] || "";
    }
  }

  function performSearch({ clearRelation = true } = {}) {
    state.filters = formFilters();
    if (clearRelation) state.localRelation = null;
    state.cursor = null;
    $("#savedViews").value = "";
    $("#deleteView").disabled = true;
    loadEvents();
  }

  function renderActiveFilters() {
    const root = $("#activeFilters");
    root.replaceChildren();
    for (const [key, value] of Object.entries(state.filters)) {
      if (!value) continue;
      const token = element("span", "filter-token");
      token.append(element("b", "", `${FILTER_LABELS[key] || key}: ${truncate(value, 72)}`));
      const remove = element("button", "", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Убрать фильтр ${FILTER_LABELS[key] || key}`);
      remove.addEventListener("click", () => {
        delete state.filters[key];
        const control = document.querySelector(`[name="${key}"]`);
        if (control) control.value = "";
        state.cursor = null;
        loadEvents();
      });
      token.append(remove);
      root.append(token);
    }
    if (state.localRelation) {
      const token = element("span", "filter-token relation");
      token.append(element("b", "", `Цепочка · ${state.localRelation.label}: ${truncate(state.localRelation.value, 54)}`));
      const remove = element("button", "", "×");
      remove.type = "button";
      remove.addEventListener("click", () => { state.localRelation = null; renderEvents(); });
      token.append(remove);
      root.append(token);
    }
  }

  function updateQuickChips() {
    for (const chip of $$(".quick-chip")) {
      if (chip.dataset.filter) chip.classList.toggle("active", state.filters[chip.dataset.filter] === chip.dataset.value);
      else if (chip.dataset.range === "today") chip.classList.toggle("active", Boolean(state.filters.from && state.filters.from.endsWith("T00:00")));
      else chip.classList.remove("active");
    }
  }

  function useQuickFilter(button) {
    if (button.dataset.filter) {
      const key = button.dataset.filter;
      if (state.filters[key] === button.dataset.value) delete state.filters[key];
      else state.filters[key] = button.dataset.value;
    } else if (button.dataset.range === "today") {
      const start = new Date();
      start.setHours(0, 0, 0, 0);
      state.filters.from = localDateTimeValue(start);
      delete state.filters.to;
    } else if (button.dataset.range === "hour") {
      state.filters.from = localDateTimeValue(new Date(Date.now() - 60 * 60 * 1000));
      delete state.filters.to;
    }
    applyFiltersToForm();
    state.localRelation = null;
    state.cursor = null;
    loadEvents();
  }

  function relatedEvents(event) {
    const relations = relationEntries(event);
    return state.events
      .filter((candidate) => String(candidate.id) !== String(event.id))
      .map((candidate) => ({
        candidate,
        score: relations.reduce((total, relation) => total + (relationMatches(candidate, relation) ? 1 : 0), 0),
      }))
      .filter((item) => item.score > 0)
      .sort((left, right) => right.score - left.score || Number(right.candidate.id) - Number(left.candidate.id))
      .slice(0, 6)
      .map((item) => item.candidate);
  }

  function appendRelatedItem(root, item, relation = null) {
    const node = element("button", "related-item");
    node.type = "button";
    const reasons = relation && Array.isArray(relation.reasons)
      ? ` · ${relation.reasons.map((reason) => RELATION_LABELS[`${reason}_id`] || RELATION_LABELS[reason] || reason).join(" + ")}`
      : "";
    node.append(
      element("b", "", item.summary || item.event_type),
      document.createTextNode(`${formatTimelineTime(item.occurred_at).time} · ${item.source_service}${reasons}`),
    );
    node.addEventListener("click", () => openDetail(item));
    root.append(node);
  }

  async function loadRemoteRelations(event, root) {
    const eventId = String(event.id);
    try {
      const data = await api(`/api/global-log/events/${encodeURIComponent(eventId)}/related?limit=120`, { timeout: 20000 });
      if (!state.selectedEvent || String(state.selectedEvent.id) !== eventId) return;
      root.replaceChildren();
      const events = Array.isArray(data.events) ? data.events : [];
      root.append(element("span", "", events.length ? `Полная цепочка: ${events.length} событий` : "Связанные события не найдены"));
      for (const item of events.slice(-12)) appendRelatedItem(root, item, item.relation);
    } catch (error) {
      if (!state.selectedEvent || String(state.selectedEvent.id) !== eventId) return;
      root.append(element("span", "relation-muted", `Полная цепочка временно недоступна · ${friendlyError(error)}`));
    }
  }

  function openDetail(event) {
    state.selectedEvent = event;
    $("#detailMeta").textContent = `#${event.id} · ${formatTime(event.occurred_at)}`;
    $("#detailTitle").textContent = event.summary || event.event_type || "Детали события";
    const facts = $("#detailFacts");
    facts.replaceChildren();
    const values = [
      ["Сервис", event.source_service], ["Тип", event.source_type], ["Событие", event.event_type],
      ["Важность", event.severity], ["Пользователь", event.actor_display || event.actor_user_id],
      ["Guild", event.guild_id], ["Канал", event.channel_id], ["Сообщение", event.message_id],
      ["HTTP", event.status_code], ["Время", event.duration_ms !== null && event.duration_ms !== undefined ? `${event.duration_ms} ms` : null],
      ["Request ID", event.request_id], ["Session ID", event.session_id], ["Цель", event.target_id],
      ["Принято", formatTime(event.ingested_at)],
    ];
    for (const [label, value] of values) {
      if (value === null || value === undefined || value === "") continue;
      const node = element("div", "fact");
      node.append(element("span", "", label), element("strong", "", value));
      facts.append(node);
    }

    const actions = $("#relationActions");
    actions.replaceChildren();
    for (const relation of relationEntries(event)) {
      const button = element("button", "relation-action");
      button.type = "button";
      button.append(document.createTextNode(`${relation.label} `), element("b", "", relation.value));
      button.addEventListener("click", () => {
        state.localRelation = relation;
        $("#detailDialog").close();
        renderEvents();
        $("#eventsSurface").scrollIntoView({ behavior: "smooth", block: "start" });
        toast(`Фокус: ${relation.label}. Показаны связанные события из загруженной выборки.`);
      });
      actions.append(button);
    }
    if (!actions.children.length) actions.append(element("span", "relation-badge", "Идентификаторы связей отсутствуют"));

    const preview = $("#relationPreview");
    preview.replaceChildren();
    const related = relatedEvents(event);
    preview.append(element("span", "", related.length ? `Загружено рядом: ${related.length} · строим полную цепочку…` : "Строим полную цепочку…"));
    for (const item of related) appendRelatedItem(preview, item);
    void loadRemoteRelations(event, preview);

    $("#detailContent").textContent = event.content_text || "Содержимое не записывалось.";
    $("#detailJson").textContent = JSON.stringify(event.details || {}, null, 2);
    $("#previousHash").textContent = event.previous_hash || "—";
    $("#eventHash").textContent = event.event_hash || "—";
    history.replaceState(null, "", `${location.pathname}${location.search}#event=${encodeURIComponent(event.id)}`);
    const dialog = $("#detailDialog");
    if (!dialog.open) dialog.showModal();
  }

  function closeDetail() {
    $("#detailDialog").close();
    state.selectedEvent = null;
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }

  function openDeepLinkedEvent() {
    if ($("#detailDialog").open) return;
    const match = /^#event=([^&]+)/.exec(location.hash);
    if (!match) return;
    const id = decodeURIComponent(match[1]);
    const event = state.events.find((item) => String(item.id) === id);
    if (event) openDetail(event);
  }

  async function copyText(value, message) {
    try {
      await navigator.clipboard.writeText(value);
      toast(message);
    } catch {
      const textarea = element("textarea");
      textarea.value = value;
      document.body.append(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
      toast(message);
    }
  }

  function effectiveExportEvents() {
    return visibleEvents();
  }

  function downloadClientExport(format) {
    const events = effectiveExportEvents();
    let payload;
    let type;
    if (format === "csv") {
      const columns = ["id", "occurred_at", "source_service", "source_type", "event_type", "severity", "actor_user_id", "actor_display", "channel_id", "message_id", "request_id", "session_id", "status_code", "duration_ms", "summary", "content_text", "details"];
      const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      payload = `\ufeff${columns.map(quote).join(",")}\r\n${events.map((event) => columns.map((column) => quote(column === "details" ? JSON.stringify(event.details || {}) : event[column])).join(",")).join("\r\n")}`;
      type = "text/csv;charset=utf-8";
    } else {
      payload = JSON.stringify({ events }, null, 2);
      type = "application/json;charset=utf-8";
    }
    const url = URL.createObjectURL(new Blob([payload], { type }));
    const link = element("a");
    link.href = url;
    link.download = `tmod-global-log-chain.${format}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Экспортирована текущая связанная цепочка: ${events.length} событий.`);
  }

  function exportEvents(format) {
    if (state.localRelation) {
      downloadClientExport(format);
      return;
    }
    const params = new URLSearchParams(state.filters);
    params.set("format", format);
    window.location.href = `/api/global-log/export?${params}`;
  }

  function readSavedViews() {
    try {
      const value = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]");
      state.savedViews = Array.isArray(value) ? value.filter((item) => item && item.id && item.name) : [];
    } catch {
      state.savedViews = [];
    }
  }

  function persistSavedViews() {
    try {
      localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(state.savedViews.slice(0, 30)));
    } catch {
      toast("Браузер запретил локальное сохранение представлений.");
    }
  }

  function renderSavedViews() {
    const select = $("#savedViews");
    const current = select.value;
    select.replaceChildren(new Option("Текущий поиск", ""));
    for (const view of state.savedViews) select.append(new Option(view.name, view.id));
    select.value = state.savedViews.some((item) => item.id === current) ? current : "";
    $("#deleteView").disabled = !select.value;
  }

  function saveCurrentView(name) {
    const cleaned = String(name || "").trim();
    if (!cleaned) return;
    const view = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      name: cleaned,
      filters: { ...state.filters },
      group: state.group,
      view: state.view,
    };
    state.savedViews.unshift(view);
    persistSavedViews();
    renderSavedViews();
    $("#savedViews").value = view.id;
    $("#deleteView").disabled = false;
    toast(`Представление «${cleaned}» сохранено.`);
  }

  function applySavedView(id) {
    const view = state.savedViews.find((item) => item.id === id);
    if (!view) return;
    state.filters = { ...(view.filters || {}) };
    state.group = view.group || "none";
    state.view = view.view || "timeline";
    state.localRelation = null;
    state.cursor = null;
    applyFiltersToForm();
    $("#groupMode").value = state.group;
    updateViewButtons();
    $("#deleteView").disabled = false;
    loadEvents();
  }

  function updateViewButtons() {
    for (const button of $$("[data-view]")) {
      const active = button.dataset.view === state.view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    }
  }

  function scheduleLive() {
    clearInterval(state.timer);
    if (!state.live) return;
    state.timer = setInterval(() => {
      if (!document.hidden) loadEvents({ silent: true });
    }, LIVE_INTERVAL);
  }

  function updateLiveUi() {
    const button = $("#liveToggle");
    button.classList.toggle("paused", !state.live);
    button.setAttribute("aria-pressed", String(state.live));
    $("span", button).textContent = state.live ? "Поток включён" : "Поток на паузе";
    $("#liveIndicator").classList.toggle("paused", !state.live);
    $("#liveIndicator").lastChild.textContent = state.live ? "LIVE" : "PAUSE";
    $("#refreshDetail").textContent = state.live ? "обновление каждые 10 секунд" : "автообновление приостановлено";
  }

  async function refreshSessionHealth() {
    try {
      const session = await api("/api/global-log/session", { timeout: 8000, retry: false });
      if (session.authenticated) applyRuntimeHealth(session.runtime);
    } catch (error) {
      if (error.status !== 401) showNotice(friendlyError(error));
    }
  }

  async function bootstrap() {
    clearInterval(state.timer);
    clearInterval(state.healthTimer);
    try {
      const session = await api("/api/global-log/session", { retry: false });
      if (!session.authenticated) {
        showLogin();
        return;
      }
      showApp();
      applyRuntimeHealth(session.runtime);
      readSavedViews();
      renderSavedViews();
      const deepLink = /^#event=([^&]+)/.exec(location.hash);
      if (deepLink) {
        state.filters.q = decodeURIComponent(deepLink[1]);
        applyFiltersToForm();
      }
      const results = await Promise.allSettled([loadFacets(), loadEvents()]);
      if (results.every((item) => item.status === "rejected")) showNotice("Панель открыта, но данные пока недоступны. Повторная попытка продолжится автоматически.");
      state.initialized = true;
      scheduleLive();
      state.healthTimer = setInterval(refreshSessionHealth, 60000);
    } catch (error) {
      if (error.status === 401) showLogin();
      else {
        showApp();
        showNotice(friendlyError(error));
      }
    }
  }

  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const alert = $("#loginAlert");
    alert.hidden = true;
    const button = event.submitter;
    button.disabled = true;
    try {
      await api("/api/global-log/login", {
        method: "POST",
        body: JSON.stringify({ user_id: $("#userId").value, code: $("#accessCode").value }),
        retry: false,
      });
      $("#accessCode").value = "";
      await bootstrap();
    } catch (error) {
      if (error.status === 429) alert.textContent = "Слишком много попыток. Подождите несколько минут.";
      else if (RETRYABLE_STATUSES.has(Number(error.status)) || error.name === "AbortError") alert.textContent = "Контур временно не отвечает. Код не погашен — повторите вход через несколько секунд.";
      else alert.textContent = "Код неверен, истёк или уже был использован.";
      alert.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  $("#filterForm").addEventListener("submit", (event) => {
    event.preventDefault();
    performSearch();
  });
  $("#advancedToggle").addEventListener("click", (event) => {
    const form = $("#filterForm");
    form.hidden = !form.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!form.hidden));
    $("span", event.currentTarget).textContent = form.hidden ? "⌄" : "⌃";
  });
  $("#resetFilters").addEventListener("click", () => {
    $("#filterForm").reset();
    state.filters = {};
    state.localRelation = null;
    state.cursor = null;
    $("#savedViews").value = "";
    $("#deleteView").disabled = true;
    loadEvents();
  });
  for (const button of $$(".quick-chip")) button.addEventListener("click", () => useQuickFilter(button));
  $("#loadMore").addEventListener("click", () => loadEvents({ append: true }));
  $("#exportCsv").addEventListener("click", () => exportEvents("csv"));
  $("#exportJson").addEventListener("click", () => exportEvents("json"));
  $("#groupMode").addEventListener("change", (event) => {
    state.group = event.currentTarget.value;
    state.collapsedGroups.clear();
    renderEvents();
  });
  for (const button of $$("[data-view]")) button.addEventListener("click", () => {
    state.view = button.dataset.view;
    updateViewButtons();
    renderEvents();
  });
  $("#liveToggle").addEventListener("click", () => {
    state.live = !state.live;
    updateLiveUi();
    scheduleLive();
    if (state.live) loadEvents({ silent: true });
  });
  $("#noticeRetry").addEventListener("click", () => Promise.allSettled([refreshSessionHealth(), loadFacets(), loadEvents()]));
  $("#noticeClose").addEventListener("click", hideNotice);

  $("#detailClose").addEventListener("click", closeDetail);
  $("#detailDialog").addEventListener("cancel", () => {
    state.selectedEvent = null;
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  });
  $("#copyEventLink").addEventListener("click", () => {
    if (!state.selectedEvent) return;
    copyText(`${location.origin}${location.pathname}${location.search}#event=${encodeURIComponent(state.selectedEvent.id)}`, "Ссылка на событие скопирована.");
  });
  $("#copyEventJson").addEventListener("click", () => {
    if (!state.selectedEvent) return;
    copyText(JSON.stringify(state.selectedEvent, null, 2), "JSON события скопирован.");
  });

  $("#saveView").addEventListener("click", () => {
    $("#viewName").value = "";
    $("#viewDialog").showModal();
    setTimeout(() => $("#viewName").focus(), 50);
  });
  $("#saveViewForm").addEventListener("submit", (event) => {
    event.preventDefault();
    saveCurrentView($("#viewName").value);
    $("#viewDialog").close();
  });
  $("#viewClose").addEventListener("click", () => $("#viewDialog").close());
  $("#viewCancel").addEventListener("click", () => $("#viewDialog").close());
  $("#savedViews").addEventListener("change", (event) => {
    $("#deleteView").disabled = !event.currentTarget.value;
    if (event.currentTarget.value) applySavedView(event.currentTarget.value);
  });
  $("#deleteView").addEventListener("click", () => {
    const id = $("#savedViews").value;
    const view = state.savedViews.find((item) => item.id === id);
    if (!view) return;
    state.savedViews = state.savedViews.filter((item) => item.id !== id);
    persistSavedViews();
    renderSavedViews();
    toast(`Представление «${view.name}» удалено.`);
  });

  $("#logoutButton").addEventListener("click", async () => {
    try { await api("/api/global-log/logout", { method: "POST", body: "{}", retry: false }); }
    finally { showLogin(); }
  });
  $("#integrityButton").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    $("#chainStatus").textContent = "Проверка…";
    try {
      const result = await api("/api/global-log/integrity", { timeout: 45000 });
      $("#chainStatus").textContent = result.ok ? "Цепочка цела" : "Нарушена";
      $("#chainDetail").textContent = result.ok ? `Проверено записей: ${result.checked}` : `Разрыв на записи #${result.broken_at}`;
    } catch (error) {
      $("#chainStatus").textContent = "Не проверена";
      $("#chainDetail").textContent = friendlyError(error);
      showNotice(friendlyError(error));
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  document.addEventListener("keydown", (event) => {
    const activeTag = document.activeElement && document.activeElement.tagName;
    const typing = activeTag === "INPUT" || activeTag === "TEXTAREA" || activeTag === "SELECT";
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#globalSearch").focus();
      $("#globalSearch").select();
    } else if (event.key === "/" && !typing && !$("#detailDialog").open) {
      event.preventDefault();
      $("#globalSearch").focus();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.live && state.initialized) loadEvents({ silent: true });
  });

  updateLiveUi();
  updateViewButtons();
  bootstrap();
})();
