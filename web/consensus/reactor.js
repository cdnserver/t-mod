"use strict";

(() => {
  const api = window.TModAdmin;
  if (!api) return;

  const widgetLabels = {
    attention: ["Центр внимания", "Критические и ожидающие действия"],
    health: ["Здоровье систем", "Discord, база, доставка, рынок"],
    treasury: ["Казна", "Финансовая сводка"],
    craft: ["Крафты", "Активное производство"],
    events: ["Живая лента", "События T-Mod в реальном времени"],
    discord: ["Discord", "Активность сообщества"],
    minecraft: ["Minecraft", "Состояние игрового сервера"],
  };
  const quickCommands = [
    ["overview", "Открыть обзор", "Общая оперативная картина", "#/overview"],
    ["attention", "Центр внимания", "Задачи, ошибки и процессы", "#/overview"],
    ["consensus", "Панель консенсуса", "Ход заседания и законопроекты", "https://consensus.tvr.lat/"],
    ["audit", "Аудит действий", "Кто, что и когда изменил", "#/audit"],
    ["treasury", "Казна", "Баланс и операции", "#/treasury"],
    ["craft", "Крафты", "Производственный контур", "#/craft"],
    ["market", "Рынок RU15", "Предметы, машины и одежда", "#/market"],
    ["sgl", "Бюро СГЛ", "Кейсы и архивы", "#/sgl"],
    ["members", "Участники", "Профили и персонажи", "#/members"],
    ["minecraft", "Minecraft", "Управление mc.tvr.lat", "#/minecraft"],
    ["system", "Здоровье системы", "Очереди и диагностика", "#/system"],
  ];

  const state = {
    attention: null,
    layout: Object.keys(widgetLabels),
    selectedResult: 0,
    searchTimer: null,
    attentionTimer: null,
    minecraftTimer: null,
    eventSource: null,
    eventCursor: 0,
    notificationSignature: "",
    renderSignature: "",
    minecraftSignature: "",
    minecraftTab: "overview",
    minecraftPath: "",
    minecraftParent: "",
    minecraftStorageLoaded: false,
    minecraftRefreshing: false,
    minecraftOperationTimer: null,
    minecraftNameAction: null,
    tabAttention: 0,
    tabUnread: 0,
    attentionUrgent: false,
    inboxUrgent: false,
    audioReady: false,
  };

  const byId = (id) => document.getElementById(id);
  const el = (tag, className = "", text = "") => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== "") node.textContent = String(text);
    return node;
  };

  function syncTabSignal() {
    const count = Math.max(state.tabAttention, state.tabUnread);
    const urgent = state.attentionUrgent || state.inboxUrgent;
    globalThis.TModTabSignal?.set(count, {
      urgent,
      blink: state.tabUnread > 0,
      label: urgent ? "Критическое событие T-Mod" : "Новые события T-Mod",
    });
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function navigate(route) {
    if (!route) return;
    if (/^https?:\/\//i.test(route)) {
      location.assign(route);
      return;
    }
    closeDialog(byId("command-dialog"));
    closeDialog(byId("reactor-inbox-dialog"));
    location.hash = route.startsWith("#") ? route : `#/${route.replace(/^\//, "")}`;
  }

  function confirmAction(options = {}) {
    const dialog = byId("reactor-confirm-dialog");
    if (!dialog) return Promise.resolve(globalThis.confirm(options.message || options.title || "Подтвердить?"));
    byId("reactor-confirm-title").textContent = options.title || "Подтвердите действие";
    byId("reactor-confirm-message").textContent = options.message || "Действие будет записано в аудит.";
    byId("reactor-confirm-accept").textContent = options.accept || "Подтвердить";
    byId("reactor-confirm-mark").dataset.tone = options.tone || "warning";
    openDialog(dialog);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        accept.removeEventListener("click", approve);
        cancel.removeEventListener("click", decline);
        dialog.removeEventListener("cancel", decline);
        closeDialog(dialog);
        resolve(value);
      };
      const approve = () => finish(true);
      const decline = (event) => {
        event?.preventDefault?.();
        finish(false);
      };
      const accept = byId("reactor-confirm-accept");
      const cancel = byId("reactor-confirm-cancel");
      accept.addEventListener("click", approve);
      cancel.addEventListener("click", decline);
      dialog.addEventListener("cancel", decline);
    });
  }

  window.TModReactor = Object.freeze({
    confirm: confirmAction,
    navigate,
    activateMinecraft: () => switchMinecraftTab(state.minecraftTab),
  });

  function attentionRow(item) {
    const row = el("button", `attention-row ${item.severity || "info"}`);
    row.type = "button";
    const mark = el("span", "attention-mark", item.severity === "critical" ? "!" : item.severity === "warning" ? "△" : "•");
    const copy = el("span", "attention-copy");
    copy.append(el("strong", "", item.title || "Событие"), el("small", "", item.detail || ""));
    row.append(mark, copy, el("b", "", item.count || 1));
    row.addEventListener("click", () => navigate(item.route));
    return row;
  }

  function healthNode(item) {
    const node = el("button", `health-node ${item.status || "disabled"}`);
    node.type = "button";
    node.append(
      el("i", "health-node-led"),
      el("strong", "", item.title || item.id),
      el("small", "", item.detail || "нет телеметрии"),
    );
    node.addEventListener("click", () => navigate(item.id === "minecraft" ? "#/minecraft" : "#/system"));
    return node;
  }

  function renderAttention(data) {
    state.attention = data;
    state.layout = Array.isArray(data.layout) ? data.layout : state.layout;
    const items = Array.isArray(data.items) ? data.items : [];
    state.tabAttention = Number(data.total || 0);
    state.attentionUrgent = items.some((item) => item.severity === "critical");
    syncTabSignal();
    const health = data.health || {};
    const signature = JSON.stringify({
      items: items.map((item) => [item.key, item.count, item.severity, item.detail, item.route]),
      health: {
        overall: health.overall,
        components: (health.components || []).map((item) => [item.id, item.status, item.detail]),
        minecraft: health.minecraft,
      },
      layout: state.layout,
    });
    if (signature === state.renderSignature) return false;
    state.renderSignature = signature;
    const list = byId("reactor-attention-list");
    list.replaceChildren(...(items.length
      ? items.map(attentionRow)
      : [el("div", "attention-clear", "✓ Всё спокойно — действий, требующих внимания, нет.")]));
    byId("reactor-attention-count").textContent = String(data.total || 0);
    document.body.dataset.reactorState = health.overall || "nominal";
    byId("reactor-health-state").textContent = health.overall === "nominal" ? "НОРМА" : health.overall === "critical" ? "КРИТИЧНО" : "ВНИМАНИЕ";
    byId("reactor-health-grid").replaceChildren(...(health.components || []).map(healthNode));
    renderMinecraft(health.minecraft || {});
    applyLayout(state.layout);
    return true;
  }

  async function refreshAttention(silent = true) {
    if (!api.authorized || (silent && document.hidden)) return;
    try {
      const data = await api.fetchJSON("/api/admin/reactor/attention");
      const signature = JSON.stringify((data.items || []).map((item) => [item.key, item.count, item.severity]));
      if (state.notificationSignature && signature !== state.notificationSignature && silent) {
        api.showToast("Центр внимания получил новое состояние.", false, {
          title: "Событие Реактора",
          icon: "!",
          tone: "update",
          sound: "update",
        });
      }
      state.notificationSignature = signature;
      renderAttention(data);
      await refreshInbox(false);
    } catch (error) {
      if (!silent) api.showToast(error.message || "Не удалось обновить Реактор.", true);
    }
  }

  function eventRow(item, fresh = false) {
    const row = el("button", `reactor-event${fresh ? " fresh" : ""}`);
    row.type = "button";
    const time = item.created_at ? new Date(item.created_at) : new Date();
    const validTime = Number.isNaN(time.getTime()) ? "—" : time.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    row.append(
      el("time", "", validTime),
      el("i", "", String(item.module || "T").slice(0, 1).toUpperCase()),
      el("span", "", item.summary || item.action_kind || "Событие T-Mod"),
    );
    if (item.id) row.addEventListener("click", () => navigate(`#/audit/audit/${item.id}`));
    return row;
  }

  async function loadEvents() {
    try {
      const data = await api.fetchJSON("/api/admin/reactor/events");
      const items = Array.isArray(data.items) ? data.items : [];
      state.eventCursor = items.reduce((value, item) => Math.max(value, Number(item.id || 0)), 0);
      byId("reactor-event-feed").replaceChildren(...items.slice(-10).reverse().map((item) => eventRow(item)));
      connectEvents();
    } catch {
      byId("reactor-live-indicator").textContent = "RETRY";
    }
  }

  function connectEvents() {
    state.eventSource?.close();
    state.eventSource = new EventSource(`/api/admin/reactor/events/stream?after=${state.eventCursor}`);
    state.eventSource.addEventListener("open", () => {
      byId("reactor-live-indicator").textContent = "LIVE";
      byId("reactor-live-indicator").classList.remove("offline");
    });
    state.eventSource.addEventListener("activity", (event) => {
      try {
        const item = JSON.parse(event.data);
        state.eventCursor = Math.max(state.eventCursor, Number(item.id || 0));
        const feed = byId("reactor-event-feed");
        feed.prepend(eventRow(item, true));
        while (feed.children.length > 12) feed.lastElementChild.remove();
        if (document.hidden) globalThis.TModTabSignal?.pulse("Новое событие в ленте T-Mod");
      } catch {
        // A malformed single event must not break the stream.
      }
    });
    state.eventSource.addEventListener("error", () => {
      byId("reactor-live-indicator").textContent = "RETRY";
      byId("reactor-live-indicator").classList.add("offline");
    });
  }

  function inboxItem(item) {
    const row = el("button", `inbox-item ${item.severity || "info"}${item.read_at ? " read" : ""}`);
    row.type = "button";
    const copy = el("span", "");
    copy.append(el("strong", "", item.title), el("small", "", item.body));
    row.append(el("i", "", item.severity === "critical" ? "!" : "•"), copy);
    row.addEventListener("click", async () => {
      await api.postJSON("/api/reactor/notifications/read", { ids: [item.id] });
      if (item.route) {
        await refreshInbox(false);
        navigate(item.route);
      } else await refreshInbox(true);
    });
    return row;
  }

  async function refreshInbox(render = true) {
    try {
      const data = await api.fetchJSON("/api/reactor/notifications");
      const unread = Number(data.unread || 0);
      state.tabUnread = unread;
      state.inboxUrgent = (data.items || []).some(
        (item) => !item.read_at && item.severity === "critical",
      );
      syncTabSignal();
      const badge = byId("reactor-inbox-badge");
      badge.textContent = unread > 99 ? "99+" : String(unread);
      badge.hidden = unread <= 0;
      if (render) {
        byId("reactor-inbox-summary").textContent = unread ? `Непрочитано: ${unread}` : "Новых уведомлений нет";
        byId("reactor-inbox-list").replaceChildren(...(data.items || []).map(inboxItem));
      }
    } catch {
      if (render) byId("reactor-inbox-summary").textContent = "Не удалось получить уведомления";
    }
  }

  function commandRow(item, index) {
    const row = el("button", `command-result${index === state.selectedResult ? " selected" : ""}`);
    row.type = "button";
    row.dataset.route = item.route;
    row.dataset.index = String(index);
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(index === state.selectedResult));
    row.append(
      el("i", "", String(item.kind || item.id || "T").slice(0, 1).toUpperCase()),
      el("span", "", item.title),
      el("small", "", item.subtitle || "Раздел Ядерного Реактора"),
      el("b", "", "↵"),
    );
    row.addEventListener("click", () => navigate(item.route));
    row.addEventListener("mouseenter", () => selectCommandResult(index));
    return row;
  }

  function renderCommands(items) {
    const list = byId("command-results");
    const normalized = items.map((item) => ({
      ...item,
      route: item.route || `#/${item.id}`,
      subtitle: item.subtitle || item[2],
      title: item.title || item[1],
      kind: item.kind || item[0],
    }));
    list.replaceChildren(...normalized.map(commandRow));
    list.dataset.count = String(normalized.length);
  }

  function selectCommandResult(index) {
    const rows = [...byId("command-results").querySelectorAll(".command-result")];
    if (!rows.length) return;
    state.selectedResult = (index + rows.length) % rows.length;
    rows.forEach((row, position) => {
      row.classList.toggle("selected", position === state.selectedResult);
      row.setAttribute("aria-selected", String(position === state.selectedResult));
    });
    rows[state.selectedResult].scrollIntoView({ block: "nearest" });
  }

  async function runSearch(query) {
    const clean = String(query || "").trim();
    if (clean.length < 2) {
      state.selectedResult = 0;
      renderCommands(quickCommands.map(([id, title, subtitle, route]) => ({ id, title, subtitle, route })));
      return;
    }
    byId("command-results").replaceChildren(el("div", "command-searching", "Ищем во всех контурах T-Mod…"));
    try {
      const data = await api.fetchJSON(`/api/admin/reactor/search?q=${encodeURIComponent(clean)}`);
      state.selectedResult = 0;
      renderCommands(data.items?.length ? data.items : [{ kind: "empty", title: "Ничего не найдено", subtitle: "Попробуйте другой запрос", route: "#/overview" }]);
    } catch {
      renderCommands([{ kind: "error", title: "Поиск временно недоступен", subtitle: "Повторите через несколько секунд", route: "#/overview" }]);
    }
  }

  function showCommandDeck() {
    const dialog = byId("command-dialog");
    openDialog(dialog);
    const input = byId("command-search-input");
    input.value = "";
    state.selectedResult = 0;
    void runSearch("");
    setTimeout(() => input.focus(), 30);
  }

  function applyLayout(layout) {
    const selected = new Set(Array.isArray(layout) ? layout : Object.keys(widgetLabels));
    document.querySelectorAll("[data-reactor-widget]").forEach((node) => {
      node.classList.toggle("reactor-widget-hidden", !selected.has(node.dataset.reactorWidget));
      node.style.order = String((layout || []).indexOf(node.dataset.reactorWidget) + 1 || 99);
    });
  }

  function layoutEditor() {
    const list = byId("reactor-layout-list");
    const active = new Set(state.layout);
    const ordered = [...state.layout, ...Object.keys(widgetLabels).filter((key) => !state.layout.includes(key))];
    const render = () => {
      list.replaceChildren(...ordered.map((key, index) => {
        const row = el("div", "layout-item");
        row.dataset.widget = key;
        const checkbox = el("input");
        checkbox.type = "checkbox";
        checkbox.checked = active.has(key);
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) active.add(key);
          else active.delete(key);
          list.dataset.selected = JSON.stringify([...active]);
        });
        const copy = el("span");
        copy.append(el("strong", "", widgetLabels[key][0]), el("small", "", widgetLabels[key][1]));
        const controls = el("span", "layout-arrows");
        [["↑", -1], ["↓", 1]].forEach(([label, direction]) => {
          const button = el("button", "", label);
          button.type = "button";
          button.disabled = index + direction < 0 || index + direction >= ordered.length;
          button.addEventListener("click", () => {
            const target = index + direction;
            [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
            render();
          });
          controls.append(button);
        });
        row.append(checkbox, copy, controls);
        return row;
      }));
      list.dataset.selected = JSON.stringify([...active]);
      list.dataset.order = JSON.stringify(ordered);
    };
    render();
    openDialog(byId("reactor-layout-dialog"));
  }

  function renderMinecraft(data) {
    const operation = data.lifecycle?.operation || data.operation || null;
    const operationActive = operation && ["queued", "running"].includes(operation.status);
    const signature = JSON.stringify({
      configured: data.configured,
      online: data.online,
      address: data.address,
      players_online: data.players_online,
      players_max: data.players_max,
      players: data.players,
      error: data.error,
      state: data.state,
      operation,
    });
    if (signature === state.minecraftSignature) return false;
    state.minecraftSignature = signature;
    const online = Boolean(data.online);
    const operationLabels = {
      start: "ЗАПУСКАЕТСЯ",
      stop: "ОСТАНАВЛИВАЕТСЯ",
      restart: "ПЕРЕЗАПУСК",
    };
    const stateLabel = operationActive
      ? (operationLabels[operation.action] || "ВЫПОЛНЯЕТСЯ")
      : online ? "ONLINE" : data.state === "error" ? "ОШИБКА" : data.configured ? "OFFLINE" : "НЕ НАСТРОЕН";
    ["minecraft-peek-state", "minecraft-status-pill"].forEach((id) => {
      const node = byId(id);
      if (!node) return;
      node.textContent = stateLabel;
      node.className = id.endsWith("pill") ? `health-pill${online && !operationActive ? "" : " warning"}` : "";
    });
    if (byId("minecraft-address")) byId("minecraft-address").textContent = data.address || "mc.tvr.lat";
    if (byId("minecraft-players")) byId("minecraft-players").textContent = online ? `${data.players_online || 0} / ${data.players_max || 0}` : "— / —";
    const detail = operationActive
      ? `Операция «${operation.action || "изменение состояния"}» выполняется в фоне. Панель обновится автоматически.`
      : online ? "Игровой мир отвечает через внутренний RCON." : data.error || "Сервис ожидает настройки.";
    if (byId("minecraft-detail")) byId("minecraft-detail").textContent = detail;
    if (byId("minecraft-peek-body")) byId("minecraft-peek-body").textContent = online ? `Игроков онлайн: ${data.players_online || 0}. Мир доступен по адресу ${data.address || "mc.tvr.lat"}.` : detail;
    if (byId("minecraft-player-list")) {
      byId("minecraft-player-list").replaceChildren(...(data.players || []).map((name) => el("span", "", name)));
    }
    return true;
  }

  function minecraftBytes(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0) return "—";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let selected = numeric;
    let unit = 0;
    while (selected >= 1024 && unit < units.length - 1) {
      selected /= 1024;
      unit += 1;
    }
    return `${selected.toFixed(unit && selected < 10 ? 1 : 0)} ${units[unit]}`;
  }

  function minecraftDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "—"
      : date.toLocaleString("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  function minecraftButton(label, callback, className = "") {
    const button = el("button", className, label);
    button.type = "button";
    button.addEventListener("click", callback);
    return button;
  }

  function minecraftRequestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }

  function renderMinecraftStorage(storage = {}) {
    const pill = byId("minecraft-storage-pill");
    if (!pill) return;
    if (!storage.available) {
      pill.textContent = "хранилище недоступно";
      pill.classList.add("warning");
      return;
    }
    pill.classList.remove("warning");
    pill.textContent = `${minecraftBytes(storage.disk?.free)} свободно · ${storage.plugins || 0} плагинов`;
    state.minecraftStorageLoaded = true;
  }

  function minecraftFileRow(item) {
    const row = el("div", "minecraft-file-row");
    const main = el("button", "minecraft-file-main");
    main.type = "button";
    const icon = item.kind === "directory" ? "▰" : item.plugin ? "⬡" : item.editable ? "≡" : "◇";
    const copy = el("span");
    copy.append(el("strong", "", item.name), el("small", "", item.path));
    main.append(el("i", "", icon), copy);
    if (item.kind === "directory") {
      main.addEventListener("click", () => void loadMinecraftFiles(item.path));
    } else if (item.editable) {
      main.addEventListener("click", () => void openMinecraftEditor(item.path));
    } else {
      main.disabled = true;
    }
    const actions = el("div", "minecraft-file-actions");
    if (item.editable) {
      actions.append(minecraftButton("Редактировать", () => void openMinecraftEditor(item.path)));
    }
    if (item.downloadable) {
      actions.append(minecraftButton("Скачать", () => {
        location.assign(`/api/admin/reactor/minecraft/download?path=${encodeURIComponent(item.path)}`);
      }));
    }
    actions.append(
      minecraftButton("Переименовать", () => openMinecraftNameDialog("rename", item.path, item.name)),
      minecraftButton("В корзину", () => void trashMinecraftPath(item.path), "danger"),
    );
    row.append(
      main,
      el("span", "", item.kind === "directory" ? "папка" : minecraftBytes(item.size)),
      el("span", "", minecraftDate(item.modified_at)),
      actions,
    );
    return row;
  }

  function renderMinecraftFiles(data) {
    state.minecraftPath = data.path || "";
    state.minecraftParent = data.parent || "";
    byId("minecraft-current-path").textContent = state.minecraftPath ? `/${state.minecraftPath}` : "/";
    byId("minecraft-file-up").disabled = !state.minecraftPath;
    renderMinecraftStorage(data.storage || {});
    const entries = Array.isArray(data.entries) ? data.entries : [];
    byId("minecraft-file-list").replaceChildren(...(
      entries.length
        ? entries.map(minecraftFileRow)
        : [el("div", "minecraft-empty-row", "Папка пуста. Загрузите файл или создайте каталог.")]
    ));
  }

  async function loadMinecraftFiles(path = state.minecraftPath, silent = false) {
    if (!api.authorized) return;
    if (!silent) byId("minecraft-file-list")?.replaceChildren(el("div", "minecraft-loading-row", "Читаем структуру сервера…"));
    try {
      const data = await api.fetchJSON(`/api/admin/reactor/minecraft/files?path=${encodeURIComponent(path || "")}`);
      renderMinecraftFiles(data);
    } catch (error) {
      if (!silent) byId("minecraft-file-list")?.replaceChildren(el("div", "minecraft-empty-row", error.message || "Файлы недоступны."));
      renderMinecraftStorage({ available: false });
    }
  }

  async function openMinecraftEditor(path) {
    try {
      const data = await api.fetchJSON(`/api/admin/reactor/minecraft/file?path=${encodeURIComponent(path)}`);
      const item = data.item || {};
      const dialog = byId("minecraft-editor-dialog");
      dialog.dataset.path = item.path || path;
      dialog.dataset.etag = item.etag || "";
      byId("minecraft-editor-title").textContent = item.path || path;
      byId("minecraft-editor-content").value = item.content || "";
      byId("minecraft-editor-meta").textContent = `${minecraftBytes(item.size)} · UTF-8 · защищённое сохранение`;
      const warning = byId("minecraft-editor-warning");
      const redacted = Array.isArray(item.redacted_keys) ? item.redacted_keys : [];
      warning.hidden = !redacted.length;
      warning.textContent = redacted.length
        ? `Секретные параметры (${redacted.join(", ")}) скрыты и будут сохранены без изменений.`
        : "";
      openDialog(dialog);
      setTimeout(() => byId("minecraft-editor-content").focus(), 40);
    } catch (error) {
      api.showToast(error.message || "Не удалось открыть файл.", true);
    }
  }

  async function saveMinecraftEditor() {
    const dialog = byId("minecraft-editor-dialog");
    const button = byId("minecraft-editor-save");
    button.disabled = true;
    try {
      const result = await api.postJSON("/api/admin/reactor/minecraft/file", {
        action: "save",
        path: dialog.dataset.path,
        etag: dialog.dataset.etag,
        content: byId("minecraft-editor-content").value,
      });
      dialog.dataset.etag = result.item?.etag || dialog.dataset.etag;
      closeDialog(dialog);
      api.showToast("Файл сохранён. Предыдущая версия оставлена в истории.", false, { title: "Minecraft Files", icon: "✓" });
      await loadMinecraftFiles(state.minecraftPath, true);
    } catch (error) {
      api.showToast(error.message || "Не удалось сохранить файл.", true);
    } finally {
      button.disabled = false;
    }
  }

  function openMinecraftNameDialog(action, path = "", value = "") {
    state.minecraftNameAction = { action, path };
    byId("minecraft-name-title").textContent = action === "mkdir"
      ? "Новая папка"
      : action === "create"
        ? "Новый текстовый файл"
        : "Переименовать объект";
    byId("minecraft-name-input").value = value;
    openDialog(byId("minecraft-name-dialog"));
    setTimeout(() => {
      byId("minecraft-name-input").focus();
      byId("minecraft-name-input").select();
    }, 30);
  }

  async function submitMinecraftName(event) {
    event.preventDefault();
    const operation = state.minecraftNameAction;
    if (!operation) return;
    const name = byId("minecraft-name-input").value.trim();
    try {
      let result;
      if (operation.action === "create") {
        const path = [state.minecraftPath, name].filter(Boolean).join("/");
        result = await api.postJSON("/api/admin/reactor/minecraft/file", {
          action: "save",
          path,
          content: "",
          etag: null,
        });
      } else {
        result = await api.postJSON("/api/admin/reactor/minecraft/file", {
          action: operation.action,
          path: operation.action === "mkdir" ? state.minecraftPath : operation.path,
          name,
        });
      }
      closeDialog(byId("minecraft-name-dialog"));
      state.minecraftNameAction = null;
      await loadMinecraftFiles(state.minecraftPath);
      if (operation.action === "create") {
        api.showToast("Файл создан. Открываем редактор.");
        await openMinecraftEditor(result.item?.path);
      } else {
        api.showToast(operation.action === "mkdir" ? "Папка создана." : "Объект переименован.");
      }
    } catch (error) {
      api.showToast(error.message || "Операция с файлом не выполнена.", true);
    }
  }

  async function trashMinecraftPath(path) {
    const approved = await confirmAction({
      title: "Переместить в корзину?",
      message: `${path} исчезнет из сервера, но его можно будет восстановить в разделе «Бэкапы».`,
      accept: "В корзину",
      tone: "critical",
    });
    if (!approved) return;
    try {
      await api.postJSON("/api/admin/reactor/minecraft/file", { action: "trash", path, confirmed: true });
      await Promise.all([loadMinecraftFiles(state.minecraftPath), loadMinecraftPlugins(true), loadMinecraftBackups(true)]);
      api.showToast("Объект перемещён в восстанавливаемую корзину.");
    } catch (error) {
      api.showToast(error.message || "Не удалось переместить объект.", true);
    }
  }

  async function uploadMinecraftFile(file, directory, overwrite = false) {
    if (!file) return;
    const data = new FormData();
    data.append("path", directory || "");
    data.append("overwrite", String(overwrite));
    data.append("file", file, file.name);
    try {
      const response = await fetch("/api/admin/reactor/minecraft/upload", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "X-CSRF-Token": api.csrfToken,
          "X-Idempotency-Key": minecraftRequestId(),
        },
        body: data,
      });
      let payload = {};
      try { payload = await response.json(); } catch { payload = {}; }
      if (!response.ok) {
        if (response.status === 409 && payload.error === "minecraft_upload_conflict" && !overwrite) {
          const approved = await confirmAction({
            title: "Заменить существующий файл?",
            message: `${file.name} уже существует. Старая версия будет перемещена в корзину.`,
            accept: "Заменить",
            tone: "critical",
          });
          if (approved) return uploadMinecraftFile(file, directory, true);
          return null;
        }
        throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      }
      api.showToast(`${file.name} загружен на сервер.`, false, { title: "Minecraft Upload", icon: "↑" });
      await Promise.all([loadMinecraftFiles(directory, true), loadMinecraftPlugins(true)]);
      return payload;
    } catch (error) {
      api.showToast(error.message || "Не удалось загрузить файл.", true);
      return null;
    }
  }

  function minecraftPluginCard(item) {
    const card = el("article", `minecraft-plugin-card${item.enabled ? "" : " disabled"}`);
    const copy = el("span");
    copy.append(el("strong", "", item.display_name || item.name), el("small", "", `${item.name} · ${minecraftBytes(item.size)}`));
    const status = el("b", `minecraft-plugin-state ${item.enabled ? "on" : "off"}`, item.enabled ? "включён" : "отключён");
    const header = el("header");
    header.append(el("i", "", "⬡"), copy, status);
    const actions = el("div", "minecraft-plugin-actions");
    actions.append(
      minecraftButton(item.enabled ? "Отключить" : "Включить", () => void toggleMinecraftPlugin(item)),
      minecraftButton("В корзину", () => void trashMinecraftPath(item.path), "danger-soft-button"),
    );
    card.append(header, el("small", "", `Изменён ${minecraftDate(item.modified_at)} · потребуется перезапуск`), actions);
    return card;
  }

  async function loadMinecraftPlugins(silent = false) {
    if (!silent) byId("minecraft-plugin-list")?.replaceChildren(el("div", "minecraft-loading-row", "Сканируем plugins…"));
    try {
      const data = await api.fetchJSON("/api/admin/reactor/minecraft/plugins");
      const items = Array.isArray(data.items) ? data.items : [];
      byId("minecraft-plugin-list").replaceChildren(...(
        items.length ? items.map(minecraftPluginCard) : [el("div", "minecraft-empty-row", "Плагинов пока нет. Загрузите первый JAR-файл.")]
      ));
    } catch (error) {
      if (!silent) byId("minecraft-plugin-list")?.replaceChildren(el("div", "minecraft-empty-row", error.message || "Плагины недоступны."));
    }
  }

  async function toggleMinecraftPlugin(item) {
    const enabled = !item.enabled;
    const approved = await confirmAction({
      title: `${enabled ? "Включить" : "Отключить"} плагин?`,
      message: `${item.display_name || item.name}: изменение вступит в силу после перезапуска Minecraft.`,
      accept: enabled ? "Включить" : "Отключить",
      tone: enabled ? "warning" : "critical",
    });
    if (!approved) return;
    try {
      await api.postJSON("/api/admin/reactor/minecraft/file", {
        action: "plugin_state",
        path: item.path,
        enabled,
        confirmed: true,
      });
      await loadMinecraftPlugins();
      api.showToast(`Плагин ${enabled ? "включён" : "отключён"}. Перезапустите сервер.`);
    } catch (error) {
      api.showToast(error.message || "Не удалось изменить плагин.", true);
    }
  }

  function minecraftBackupRow(item) {
    const row = el("div", "minecraft-backup-row");
    const copy = el("span");
    copy.append(el("strong", "", item.name || item.id), el("small", "", `${minecraftBytes(item.size)} · ${minecraftDate(item.created_at)}`));
    const actions = el("footer");
    actions.append(
      minecraftButton("Скачать", () => location.assign(`/api/admin/reactor/minecraft/backup/download?id=${encodeURIComponent(item.id)}`), "secondary-button"),
      minecraftButton("Восстановить", () => void restoreMinecraftBackup(item), "secondary-button"),
      minecraftButton("Удалить", () => void deleteMinecraftBackup(item), "danger-soft-button"),
    );
    row.append(el("i", "", "⟳"), copy, actions);
    return row;
  }

  function minecraftTrashRow(item) {
    const row = el("div", "minecraft-trash-row");
    const copy = el("span");
    copy.append(el("strong", "", item.original_path || item.name), el("small", "", `Удалено ${minecraftDate(item.deleted_at)}`));
    const actions = el("footer");
    actions.append(minecraftButton("Восстановить", () => void restoreMinecraftTrash(item), "secondary-button"));
    row.append(el("i", "", "♲"), copy, actions);
    return row;
  }

  async function loadMinecraftBackups(silent = false) {
    if (!silent) byId("minecraft-backup-list")?.replaceChildren(el("div", "minecraft-loading-row", "Получаем список копий…"));
    try {
      const data = await api.fetchJSON("/api/admin/reactor/minecraft/backups");
      const items = Array.isArray(data.items) ? data.items : [];
      const trash = Array.isArray(data.trash) ? data.trash : [];
      byId("minecraft-backup-list").replaceChildren(...(
        items.length ? items.map(minecraftBackupRow) : [el("div", "minecraft-empty-row", "Резервных копий пока нет.")]
      ));
      byId("minecraft-trash-list").replaceChildren(...(
        trash.length ? trash.map(minecraftTrashRow) : [el("div", "minecraft-empty-row", "Корзина пуста.")]
      ));
    } catch (error) {
      if (!silent) byId("minecraft-backup-list")?.replaceChildren(el("div", "minecraft-empty-row", error.message || "Бэкапы недоступны."));
    }
  }

  async function createMinecraftBackup(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const result = await api.postJSON("/api/admin/reactor/minecraft/backups", {
        action: "create",
        label: new FormData(event.currentTarget).get("label"),
      });
      event.currentTarget.reset();
      await loadMinecraftBackups();
      api.showToast(`${result.item?.name || "Резервная копия"} создана.`, false, { title: "Recovery Vault", icon: "⟳" });
    } catch (error) {
      api.showToast(error.message || "Не удалось создать резервную копию.", true);
    } finally {
      button.disabled = false;
    }
  }

  async function restoreMinecraftBackup(item) {
    const approved = await confirmAction({
      title: "Восстановить резервную копию?",
      message: "Minecraft должен быть остановлен. Файлы из копии заменят текущие, а отсутствующие в копии файлы останутся на месте.",
      accept: "Восстановить",
      tone: "critical",
    });
    if (!approved) return;
    try {
      const result = await api.postJSON("/api/admin/reactor/minecraft/backups", { action: "restore", id: item.id, confirmed: true });
      api.showToast(`Восстановлено файлов: ${result.item?.restored_files || 0}.`, false, { title: "Recovery Vault", icon: "✓" });
      await loadMinecraftFiles("", true);
    } catch (error) {
      api.showToast(error.message || "Восстановление не выполнено.", true);
    }
  }

  async function deleteMinecraftBackup(item) {
    const approved = await confirmAction({
      title: "Удалить резервную копию?",
      message: `${item.name || item.id} будет удалена без возможности восстановления.`,
      accept: "Удалить",
      tone: "critical",
    });
    if (!approved) return;
    try {
      await api.postJSON("/api/admin/reactor/minecraft/backups", { action: "delete", id: item.id, confirmed: true });
      await loadMinecraftBackups();
    } catch (error) {
      api.showToast(error.message || "Не удалось удалить копию.", true);
    }
  }

  async function restoreMinecraftTrash(item) {
    const approved = await confirmAction({
      title: "Восстановить из корзины?",
      message: `${item.original_path} вернётся на прежнее место.`,
      accept: "Восстановить",
      tone: "warning",
    });
    if (!approved) return;
    try {
      await api.postJSON("/api/admin/reactor/minecraft/file", { action: "restore_trash", trash_id: item.id, confirmed: true });
      await Promise.all([loadMinecraftBackups(), loadMinecraftFiles(state.minecraftPath, true), loadMinecraftPlugins(true)]);
    } catch (error) {
      api.showToast(error.message || "Не удалось восстановить объект.", true);
    }
  }

  async function loadMinecraftLog(silent = false) {
    try {
      const data = await api.fetchJSON("/api/admin/reactor/minecraft/log?lines=400");
      byId("minecraft-log-output").textContent = data.available ? (data.lines || []).join("\n") : "latest.log пока недоступен.";
      byId("minecraft-log-meta").textContent = data.available
        ? `${minecraftBytes(data.size)} · обновлён ${minecraftDate(data.modified_at)}`
        : "Журнал появится после запуска Minecraft.";
      if (!silent) byId("minecraft-log-output").scrollTop = byId("minecraft-log-output").scrollHeight;
    } catch (error) {
      if (!silent) byId("minecraft-log-output").textContent = error.message || "Не удалось получить журнал.";
    }
  }

  function appendMinecraftConsole(command, response) {
    const output = byId("minecraft-console-output");
    const stamp = new Date().toLocaleTimeString("ru-RU");
    output.textContent += `\n\n[${stamp}] > ${command}\n${response || "Команда принята."}`;
    output.scrollTop = output.scrollHeight;
  }

  async function switchMinecraftTab(tab) {
    const selected = ["overview", "files", "plugins", "console", "backups", "logs"].includes(tab) ? tab : "overview";
    state.minecraftTab = selected;
    document.querySelectorAll("[data-minecraft-tab]").forEach((button) => button.classList.toggle("active", button.dataset.minecraftTab === selected));
    document.querySelectorAll("[data-minecraft-pane]").forEach((pane) => {
      const active = pane.dataset.minecraftPane === selected;
      pane.hidden = !active;
      pane.classList.toggle("active", active);
    });
    if (selected === "overview") {
      await loadMinecraft();
      if (!state.minecraftStorageLoaded) await loadMinecraftFiles("", true);
    }
    if (selected === "files") await loadMinecraftFiles(state.minecraftPath);
    if (selected === "plugins") await loadMinecraftPlugins();
    if (selected === "backups") await loadMinecraftBackups();
    if (selected === "logs") await loadMinecraftLog();
  }

  async function loadMinecraft(force = false) {
    if (!api.authorized || document.hidden || state.minecraftRefreshing) return;
    state.minecraftRefreshing = true;
    try {
      const data = await api.fetchJSON(`/api/admin/reactor/minecraft${force ? "?fresh=1" : ""}`);
      renderMinecraft(data);
      return data;
    } catch (error) {
      renderMinecraft({ configured: true, online: false, error: error.message });
    } finally {
      state.minecraftRefreshing = false;
    }
  }

  function followMinecraftOperation() {
    clearInterval(state.minecraftOperationTimer);
    let attempts = 0;
    state.minecraftOperationTimer = setInterval(async () => {
      attempts += 1;
      const data = await loadMinecraft(true);
      if (!data) {
        if (attempts >= 35) {
          clearInterval(state.minecraftOperationTimer);
          state.minecraftOperationTimer = null;
        }
        return;
      }
      const operation = data?.lifecycle?.operation || data?.operation;
      const active = operation && ["queued", "running"].includes(operation.status);
      if (!active || attempts >= 35) {
        clearInterval(state.minecraftOperationTimer);
        state.minecraftOperationTimer = null;
      }
    }, 1500);
  }

  async function minecraftCommand(action, payload = {}) {
    const destructive = ["stop", "restart", "kick", "whitelist_off"].includes(action);
    const labels = { start: "Запустить", stop: "Остановить", restart: "Перезапустить" };
    const approved = await confirmAction({
      title: destructive ? "Подтвердить команду Minecraft?" : "Выполнить команду Minecraft?",
      message: destructive ? "Команда повлияет на игроков или доступность мира и попадёт в аудит." : "Команда отправится во внутренний контур Minecraft и попадёт в аудит.",
      accept: labels[action] || "Выполнить",
      tone: destructive ? "critical" : "warning",
    });
    if (!approved) return;
    try {
      const result = await api.postJSON("/api/admin/reactor/minecraft", { action, ...payload, confirmed: true });
      api.showToast(result.response || "Команда Minecraft принята.", false, { title: "Minecraft", icon: "▣" });
      if (["start", "stop", "restart"].includes(action)) followMinecraftOperation();
      await loadMinecraft(true);
      return result;
    } catch (error) {
      api.showToast(error.message || "Minecraft не принял команду.", true);
      return null;
    }
  }

  function bind() {
    byId("command-button")?.addEventListener("click", showCommandDeck);
    byId("reactor-inbox-button")?.addEventListener("click", () => {
      openDialog(byId("reactor-inbox-dialog"));
      void refreshInbox(true);
    });
    byId("reactor-layout-button")?.addEventListener("click", layoutEditor);
    byId("reactor-read-all")?.addEventListener("click", async () => {
      await api.postJSON("/api/reactor/notifications/read", { ids: [] });
      await refreshInbox(true);
    });
    byId("reactor-layout-save")?.addEventListener("click", async () => {
      const list = byId("reactor-layout-list");
      const selected = new Set(JSON.parse(list.dataset.selected || "[]"));
      const ordered = JSON.parse(list.dataset.order || "[]").filter((key) => selected.has(key));
      try {
        const result = await api.postJSON("/api/reactor/preferences", { surface: "admin", layout: ordered });
        state.layout = result.layout;
        applyLayout(state.layout);
        closeDialog(byId("reactor-layout-dialog"));
        api.showToast("Ваш главный экран сохранён.");
      } catch (error) {
        api.showToast(error.message || "Не удалось сохранить экран.", true);
      }
    });
    byId("command-search-input")?.addEventListener("input", (event) => {
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(() => void runSearch(event.target.value), 180);
    });
    byId("command-search-input")?.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        selectCommandResult(state.selectedResult + (event.key === "ArrowDown" ? 1 : -1));
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const selected = byId("command-results").querySelector(".command-result.selected");
        if (selected) navigate(selected.dataset.route);
      }
    });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        showCommandDeck();
      }
    });
    document.querySelectorAll("[data-minecraft-action]").forEach((button) => {
      button.addEventListener("click", () => void minecraftCommand(button.dataset.minecraftAction));
    });
    document.querySelectorAll("[data-minecraft-tab]").forEach((button) => {
      button.addEventListener("click", () => void switchMinecraftTab(button.dataset.minecraftTab));
    });
    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(byId(button.dataset.closeDialog)));
    });
    byId("minecraft-file-up")?.addEventListener("click", () => void loadMinecraftFiles(state.minecraftParent));
    byId("minecraft-file-refresh")?.addEventListener("click", () => void loadMinecraftFiles(state.minecraftPath));
    byId("minecraft-folder-create")?.addEventListener("click", () => openMinecraftNameDialog("mkdir"));
    byId("minecraft-file-create")?.addEventListener("click", () => openMinecraftNameDialog("create"));
    byId("minecraft-name-form")?.addEventListener("submit", (event) => void submitMinecraftName(event));
    byId("minecraft-editor-save")?.addEventListener("click", () => void saveMinecraftEditor());
    byId("minecraft-file-upload")?.addEventListener("click", () => byId("minecraft-upload-input").click());
    byId("minecraft-upload-input")?.addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      event.target.value = "";
      if (file) void uploadMinecraftFile(file, state.minecraftPath);
    });
    byId("minecraft-plugin-upload")?.addEventListener("click", () => byId("minecraft-plugin-input").click());
    byId("minecraft-plugin-input")?.addEventListener("change", (event) => {
      const [file] = event.target.files || [];
      event.target.value = "";
      if (file) void uploadMinecraftFile(file, "plugins");
    });
    byId("minecraft-backup-form")?.addEventListener("submit", (event) => void createMinecraftBackup(event));
    byId("minecraft-backup-refresh")?.addEventListener("click", () => void loadMinecraftBackups());
    byId("minecraft-log-refresh")?.addEventListener("click", () => void loadMinecraftLog());
    byId("minecraft-console-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = event.currentTarget.elements.command;
      const command = String(input.value || "").trim();
      if (!command) return;
      const result = await minecraftCommand("console", { command });
      if (result) {
        appendMinecraftConsole(command, result.response);
        input.value = "";
        input.focus();
      }
    });
    byId("minecraft-announce-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      void minecraftCommand("announce", { message: data.get("message") });
    });
    byId("minecraft-player-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(event.currentTarget);
      void minecraftCommand(String(data.get("action") || ""), { player: data.get("player") });
    });
    window.addEventListener("hashchange", () => {
      if (location.hash.startsWith("#/minecraft")) void switchMinecraftTab(state.minecraftTab);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        if (api.administrator) void refreshAttention(true);
        if (location.hash.startsWith("#/minecraft")) void switchMinecraftTab(state.minecraftTab);
      }
    });
    window.addEventListener("pagehide", () => {
      clearTimeout(state.searchTimer);
      clearInterval(state.attentionTimer);
      clearInterval(state.minecraftTimer);
      clearInterval(state.minecraftOperationTimer);
      state.eventSource?.close();
      state.eventSource = null;
    }, { once: true });
  }

  async function start() {
    bind();
    for (let attempt = 0; attempt < 60 && !api.authorized; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!api.authorized) return;
    const initialLoads = api.administrator
      ? [refreshAttention(false), loadEvents()]
      : [];
    if (location.hash.startsWith("#/minecraft")) {
      // Attention already carries the cached Minecraft status. Load storage in
      // parallel instead of waiting for health and then querying RCON twice.
      initialLoads.push(loadMinecraft(), loadMinecraftFiles("", true));
    }
    await Promise.all(initialLoads);
    if (api.administrator) {
      state.attentionTimer = setInterval(() => {
        if (!document.hidden) void refreshAttention(true);
      }, 30000);
    }
    state.minecraftTimer = setInterval(() => {
      if (!location.hash.startsWith("#/minecraft") || document.hidden) return;
      void loadMinecraft();
      if (state.minecraftTab === "logs" && byId("minecraft-log-auto")?.checked) {
        void loadMinecraftLog(true);
      }
    }, 10000);
  }

  void start();
})();
