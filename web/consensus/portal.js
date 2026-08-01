"use strict";

(() => {
  const labels = {
    identity: "Моя карточка",
    treasury: "Казна",
    legislation: "Реестр законопроектов",
    editor: "Законодательная мастерская",
    consensus: "Консенсус",
    notifications: "Уведомления",
  };
  const fields = {
    idea: "bill-idea",
    desired_outcome: "bill-outcome",
    constraints_text: "bill-constraints",
    title: "bill-title",
    summary: "bill-summary",
    materials: "bill-materials",
    implementation_plan: "bill-implementation",
    leadership_actions: "bill-leadership",
  };
  const state = {
    csrf: "",
    layout: Object.keys(labels),
    data: null,
    workspace: null,
    bills: [],
    billsVisible: 6,
    activeEditorTab: "idea",
    dirty: false,
    busy: false,
    refreshTimer: null,
    legislationRefreshTimer: null,
    renderSignature: "",
  };
  const byId = (id) => document.getElementById(id);
  const el = (tag, className = "", text = "") => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== "") node.textContent = String(text);
    return node;
  };

  function requestTimeoutSignal(timeoutMs) {
    if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
      return AbortSignal.timeout(timeoutMs);
    }
    const controller = new AbortController();
    setTimeout(() => controller.abort(), timeoutMs);
    return controller.signal;
  }

  async function request(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      signal: options.signal || requestTimeoutSignal(method === "GET" ? 10000 : 90000),
      headers: {
        Accept: "application/json",
        ...(options.body
          ? { "Content-Type": "application/json", "X-CSRF-Token": state.csrf }
          : {}),
        ...(options.headers || {}),
      },
    });
    let data = {};
    try {
      data = await response.json();
    } catch {
      data = {};
    }
    if (!response.ok) {
      const error = new Error(
        data.message || data.error || `HTTP ${response.status}`,
      );
      error.status = response.status;
      error.code = data.error;
      error.data = data;
      throw error;
    }
    return data;
  }

  function toast(message, kind = "success") {
    const node = byId("portal-toast");
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => {
      node.hidden = true;
    }, 4200);
  }

  function setBusy(
    active,
    title = "Обновление данных",
    detail = "Синхронизируем Реактор…",
  ) {
    state.busy = active;
    byId("portal-busy-title").textContent = title;
    byId("portal-busy-detail").textContent = detail;
    byId("portal-busy").hidden = !active;
  }

  function formatMoney(value, signed = false) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "Нет данных";
    }
    const amount = Number(value);
    const prefix = signed && amount > 0 ? "+" : "";
    return `${prefix}${
      new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(
        amount,
      )
    } $`;
  }

  function formatDate(value, fallback = "дата не указана") {
    if (!value) return fallback;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", {
      day: "numeric",
      month: "short",
      year: "numeric",
    }).format(date);
  }

  function formatMoment(value) {
    if (!value) return "только что";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "только что";
    return new Intl.DateTimeFormat("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      day: "2-digit",
      month: "2-digit",
    }).format(date);
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function applyLayout(layout) {
    const selected = new Set(layout);
    document.querySelectorAll("[data-portal-widget]").forEach((widget) => {
      const key = widget.dataset.portalWidget;
      widget.hidden = !selected.has(key);
      widget.style.order = String(layout.indexOf(key) + 1 || 99);
    });
  }

  function notification(item) {
    const node = el(
      "button",
      `portal-notification ${item.severity || "info"}${
        item.read_at ? " read" : ""
      }`,
    );
    node.type = "button";
    const copy = el("span");
    copy.append(el("strong", "", item.title), el("small", "", item.body));
    node.append(el("i", "", item.severity === "critical" ? "!" : "•"), copy);
    node.addEventListener("click", async () => {
      try {
        await request("/api/reactor/notifications/read", {
          method: "POST",
          body: JSON.stringify({ ids: [item.id] }),
        });
        if (item.route && item.route.startsWith("#/")) {
          if (state.data?.viewer?.administrator) {
            location.assign(`https://reactor.tvr.lat/admin${item.route}`);
          } else await load();
        } else if (item.route) location.assign(item.route);
        else await load();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    return node;
  }

  function renderTreasury(treasury = {}) {
    byId("treasury-balance").textContent = formatMoney(treasury.balance);
    byId("treasury-income").textContent = formatMoney(treasury.deposits);
    byId("treasury-expense").textContent = formatMoney(treasury.withdrawals);
    byId("treasury-net").textContent = formatMoney(treasury.net_flow, true);
    byId("treasury-operations").textContent = String(
      treasury.movement_count || 0,
    );
    byId("treasury-after-report").textContent = String(
      treasury.movements_after_report || 0,
    );
    byId("treasury-freshness").textContent = treasury.report_date
      ? `Последняя сверка: ${formatDate(treasury.report_date)}`
      : "Первый отчёт казны ещё не заполнен";
    const maximum = Math.max(
      Number(treasury.deposits || 0),
      Number(treasury.withdrawals || 0),
      1,
    );
    byId("treasury-income-bar").style.width = `${
      Math.max(2, Number(treasury.deposits || 0) / maximum * 100)
    }%`;
    byId("treasury-expense-bar").style.width = `${
      Math.max(2, Number(treasury.withdrawals || 0) / maximum * 100)
    }%`;
  }

  const billStatus = (bill) => {
    const result = String(bill.result_status || "").toLowerCase();
    if (["accepted", "adopted", "passed"].includes(result)) {
      return ["ПРИНЯТ", "accepted"];
    }
    if (["rejected", "failed"].includes(result)) {
      return ["ОТКЛОНЁН", "rejected"];
    }
    const status = String(bill.status || "").toLowerCase();
    if (status === "publishing") return ["ПУБЛИКУЕТСЯ", "queued"];
    if (["queued", "requeued", "draft"].includes(status)) {
      return ["В ОЧЕРЕДИ", "queued"];
    }
    return ["РАССМОТРЕН", "resolved"];
  };

  function projectWord(count) {
    const absolute = Math.abs(Number(count) || 0);
    const lastTwo = absolute % 100;
    const last = absolute % 10;
    if (lastTwo >= 11 && lastTwo <= 14) return "ПРОЕКТОВ";
    if (last === 1) return "ПРОЕКТ";
    if (last >= 2 && last <= 4) return "ПРОЕКТА";
    return "ПРОЕКТОВ";
  }

  function openBill(bill) {
    const [status] = billStatus(bill);
    byId("bill-dialog-number").textContent = `ЗАКОНОПРОЕКТ № ${
      String(bill.number).padStart(3, "0")
    }`;
    byId("bill-dialog-title").textContent = bill.title;
    byId("bill-dialog-author").textContent = `Автор: ${bill.author} · ${
      formatDate(bill.created_at)
    }`;
    byId("bill-dialog-status").textContent = status;
    byId("bill-dialog-summary").textContent = bill.summary ||
      "Текст не указан.";
    [["bill-dialog-execution", bill.implementation_plan], [
      "bill-dialog-leadership",
      bill.leadership_actions,
    ], ["bill-dialog-materials", bill.materials]].forEach(([id, value]) => {
      const section = byId(id);
      section.hidden = !value;
      if (value) section.querySelector("p").textContent = value;
    });
    const discord = byId("bill-dialog-discord");
    discord.hidden = !bill.discord_url;
    if (bill.discord_url) discord.href = bill.discord_url;
    openDialog(byId("bill-dialog"));
  }

  function billCard(bill) {
    const [status, className] = billStatus(bill);
    const node = el("button", `bill-card ${className}`);
    node.type = "button";
    node.append(
      el("span", "", `ПРОЕКТ № ${String(bill.number).padStart(3, "0")}`),
      el("h3", "", bill.title),
      el("p", "", bill.summary || "Текст проекта пока не опубликован."),
    );
    const footer = el("footer");
    footer.append(el("span", "", bill.author), el("b", "", status));
    node.append(footer);
    node.addEventListener("click", () => openBill(bill));
    return node;
  }

  function renderBills() {
    const query = byId("legislation-search").value.trim().toLocaleLowerCase(
      "ru-RU",
    );
    const filtered = state.bills.filter((bill) =>
      !query ||
      `${bill.number} ${bill.title} ${bill.author} ${bill.summary}`
        .toLocaleLowerCase("ru-RU").includes(query)
    );
    const visible = filtered.slice(0, state.billsVisible);
    byId("legislation-list").replaceChildren(...visible.map(billCard));
    if (!visible.length) {
      byId("legislation-list").append(el(
        "div",
        "portal-empty",
        query
          ? "По вашему запросу ничего не найдено."
          : "Законопроектов пока нет.",
      ));
    }
    byId("legislation-count").textContent = `${state.bills.length} ${
      projectWord(state.bills.length)
    }`;
    byId("legislation-more").hidden = visible.length >= filtered.length;
    byId("legislation-meta").textContent = query
      ? `Найдено: ${filtered.length}`
      : `Следующий номер: ${
        String(state.data?.legislation?.next_number || "—").padStart(3, "0")
      }`;
  }

  function collectWorkspace() {
    const result = {};
    Object.entries(fields).forEach(([key, id]) => {
      result[key] = byId(id).value.trim();
    });
    return result;
  }

  function workspacePayload(action, extra = {}) {
    return {
      action,
      workspace_id: state.workspace?.id,
      expected_revision: state.workspace?.revision,
      ...collectWorkspace(),
      ...extra,
    };
  }

  function updateCounters() {
    document.querySelectorAll("[data-counter-for]").forEach((counter) => {
      const input = byId(counter.dataset.counterFor);
      counter.textContent = `${input.value.length} / ${input.maxLength}`;
    });
  }

  function localRequirements(values = collectWorkspace()) {
    return {
      idea: values.idea.length > 0,
      outcome: values.desired_outcome.length > 0,
      title: values.title.length >= 5,
      text: values.summary.length >= 20,
      implementation: values.implementation_plan.length >= 5,
      leadership: values.leadership_actions.length >= 5,
    };
  }

  function updateEditorVisuals() {
    const values = collectWorkspace();
    const requirements = localRequirements(values);
    const progress = Math.round(
      Object.values(requirements).filter(Boolean).length /
        Object.keys(requirements).length * 100,
    );
    byId("editor-progress").textContent = `${progress}%`;
    byId("editor-progress-bar").style.width = `${progress}%`;
    document.documentElement.style.setProperty(
      "--editor-progress",
      `${progress}%`,
    );
    const completedSteps = {
      idea: requirements.idea && requirements.outcome,
      draft: requirements.title && requirements.text,
      execution: requirements.implementation && requirements.leadership,
      review: Object.values(requirements).every(Boolean),
    };
    document.querySelectorAll("[data-editor-tab]").forEach((button) => {
      const selected = button.dataset.editorTab === state.activeEditorTab;
      button.classList.toggle(
        "complete",
        completedSteps[button.dataset.editorTab],
      );
      if (selected) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    });
    byId("editor-state").textContent = !state.workspace
      ? "НЕТ ЧЕРНОВИКА"
      : progress === 100
      ? "ГОТОВ К ПОДАЧЕ"
      : "ЧЕРНОВИК";
    byId("preview-title").textContent = values.title ||
      "Название будущего законопроекта";
    byId("preview-summary").textContent = values.summary ||
      "Здесь появится полный текст проекта. Заполните поля вручную или попросите ИИ подготовить первую редакцию.";
    [["preview-materials", values.materials], [
      "preview-implementation",
      values.implementation_plan,
    ]].forEach(([id, value]) => {
      const section = byId(id);
      section.hidden = !value;
      section.querySelector("p").textContent = value;
    });
    const checks = [
      ["idea", "Замысел описан", "ИИ и редактор понимают цель проекта"],
      [
        "outcome",
        "Результат определён",
        "Понятно, что изменится после принятия",
      ],
      ["title", "Название готово", "Не короче пяти символов"],
      ["text", "Полный текст готов", "Не короче двадцати символов"],
      ["implementation", "Исполнение продумано", "Есть понятный план действий"],
      ["leadership", "Руководство получит задачи", "Есть конкретный чек-лист"],
    ].map(([key, title, detail]) => {
      const row = el("div", `editor-check${requirements[key] ? " ok" : ""}`);
      const copy = el("span");
      copy.append(el("strong", "", title), el("small", "", detail));
      row.append(el("i", "", requirements[key] ? "✓" : "·"), copy);
      return row;
    });
    byId("editor-checklist").replaceChildren(...checks);
    byId("editor-publish").disabled =
      !Object.values(requirements).every(Boolean) || state.busy;
    updateCounters();
  }

  function hydrateWorkspace(workspace, force = false) {
    if (state.dirty && !force) {
      // Background refreshes must never replace text currently being edited.
      // The stale revision is intentional: an explicit save will either commit
      // this version or return a visible conflict with the newer server copy.
      return;
    }
    const changedWorkspace =
      Number(workspace?.id || 0) !== Number(state.workspace?.id || 0);
    state.workspace = workspace || null;
    byId("editor-empty").hidden = Boolean(workspace);
    byId("editor-workspace").hidden = !workspace;
    if (!workspace) {
      Object.values(fields).forEach((id) => {
        byId(id).value = "";
      });
      byId("preview-number").textContent = `№ ${
        String(state.data?.legislation?.next_number || "—").padStart(3, "0")
      }`;
      updateEditorVisuals();
      return;
    }
    if (force || changedWorkspace || !state.dirty) {
      Object.entries(fields).forEach(([key, id]) => {
        byId(id).value = workspace[key] || "";
      });
      state.dirty = false;
    }
    byId("preview-number").textContent = `№ ${
      String(state.data?.legislation?.next_number || "—").padStart(3, "0")
    }`;
    updateEditorVisuals();
  }

  function showEditorTab(tab) {
    state.activeEditorTab = tab;
    document.querySelectorAll("[data-editor-tab]").forEach((button) =>
      button.classList.toggle("active", button.dataset.editorTab === tab)
    );
    document.querySelectorAll("[data-editor-pane]").forEach((pane) => {
      const active = pane.dataset.editorPane === tab;
      pane.hidden = !active;
      pane.classList.toggle("active", active);
    });
    if (tab === "review") updateEditorVisuals();
  }

  function recoverWorkspace(error, preserveLocalConflict = false) {
    if (!error.data?.workspace) return;
    if (
      preserveLocalConflict &&
      error.code === "bill_workspace_revision_conflict"
    ) {
      // Keep the text visible instead of destroying unsaved local edits. The
      // server revision is advanced so the author can review and save again.
      state.workspace = error.data.workspace;
      state.dirty = true;
      updateEditorVisuals();
      return;
    }
    hydrateWorkspace(error.data.workspace, true);
  }

  async function saveDraft({ quiet = false } = {}) {
    if (!state.workspace || state.busy) return false;
    setBusy(
      true,
      "Сохраняем черновик",
      "Фиксируем новую редакцию в защищённом пространстве…",
    );
    try {
      const result = await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify(workspacePayload("save")),
      });
      hydrateWorkspace(result.workspace, true);
      state.dirty = false;
      if (!quiet) toast("Черновик сохранён.");
      return true;
    } catch (error) {
      recoverWorkspace(error, true);
      toast(error.message, "error");
      return false;
    } finally {
      setBusy(false);
      updateEditorVisuals();
    }
  }

  async function runAiEditor() {
    if (!state.workspace || state.busy) return;
    setBusy(
      true,
      "ИИ готовит редакцию",
      "Сохраняем замысел, очищаем формулировки и собираем план исполнения…",
    );
    try {
      const result = await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify(workspacePayload("ai")),
      });
      hydrateWorkspace(result.workspace, true);
      showEditorTab("draft");
      toast(
        result.clarification
          ? `Редакция готова. Стоит уточнить: ${result.clarification}`
          : "Первая редакция готова. Проверьте текст перед публикацией.",
      );
    } catch (error) {
      recoverWorkspace(error, true);
      toast(error.message, "error");
    } finally {
      setBusy(false);
      updateEditorVisuals();
    }
  }

  function confirmAction({ title, message, label, danger = false }) {
    return new Promise((resolve) => {
      const dialog = byId("confirm-dialog");
      byId("confirm-title").textContent = title;
      byId("confirm-message").textContent = message;
      byId("confirm-accept").textContent = label;
      byId("confirm-accept").style.background = danger
        ? "var(--red)"
        : "var(--mint)";
      byId("confirm-accept").style.borderColor = danger
        ? "var(--red)"
        : "var(--mint)";
      let finished = false;
      const done = (value) => {
        if (finished) return;
        finished = true;
        closeDialog(dialog);
        resolve(value);
      };
      byId("confirm-accept").onclick = () => done(true);
      byId("confirm-cancel").onclick = () => done(false);
      dialog.addEventListener("close", () => done(false), { once: true });
      openDialog(dialog);
    });
  }

  async function publishDraft() {
    if (!state.workspace || state.busy) return;
    if (state.dirty && !(await saveDraft({ quiet: true }))) return;
    const confirmed = await confirmAction({
      title: "Опубликовать законопроект?",
      message:
        "Проект получит постоянный номер и попадёт в надёжную очередь публикации Discord. Перед продолжением убедитесь, что текст выражает именно вашу волю.",
      label: "Да, опубликовать",
    });
    if (!confirmed) return;
    setBusy(
      true,
      "Публикуем законопроект",
      "Присваиваем номер и передаём проект в устойчивую очередь…",
    );
    try {
      const result = await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify(workspacePayload("publish", { confirmed: true })),
      });
      state.workspace = null;
      state.dirty = false;
      await load(true, true);
      await loadLegislation(true);
      toast(
        `Законопроект № ${
          String(result.bill.number).padStart(3, "0")
        } принят системой.`,
      );
      document.querySelector("#legislation")?.scrollIntoView({
        behavior: "smooth",
      });
    } catch (error) {
      recoverWorkspace(error);
      toast(error.message, "error");
    } finally {
      setBusy(false);
      updateEditorVisuals();
    }
  }

  async function cancelDraft() {
    if (!state.workspace || state.busy) return;
    const confirmed = await confirmAction({
      title: "Отменить черновик?",
      message:
        "Текущая редакция будет закрыта. Опубликованные законопроекты и реестр не изменятся.",
      label: "Отменить черновик",
      danger: true,
    });
    if (!confirmed) return;
    setBusy(
      true,
      "Закрываем черновик",
      "Фиксируем отмену без изменения реестра…",
    );
    try {
      await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify(workspacePayload("cancel", { confirmed: true })),
      });
      hydrateWorkspace(null, true);
      showEditorTab("idea");
      toast("Черновик отменён.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function render(data, forceWorkspace = false) {
    state.data = data;
    state.csrf = data.viewer.csrf_token;
    state.layout = Array.isArray(data.layout) ? data.layout : state.layout;
    const { cache_state: _cacheState, ...stableData } = data;
    const signature = JSON.stringify(stableData);
    if (signature === state.renderSignature && !forceWorkspace) return false;
    state.renderSignature = signature;
    const name = data.viewer.name || "Участник";
    byId("portal-welcome").textContent = `${name}, ваш Реактор готов`;
    byId("identity-name").textContent = name;
    byId("identity-legal").textContent = data.legal_status || "Прихожанин";
    byId("portal-avatar").textContent = name.charAt(0).toUpperCase();
    byId("identity-avatar").textContent = name.charAt(0).toUpperCase();
    byId("preview-author").textContent = `Автор: ${name}`;
    byId("portal-admin-link").hidden = !data.viewer.administrator;
    byId("portal-sync-time").textContent = `обновлено ${
      formatMoment(new Date().toISOString())
    }`;
    const profile = data.profile || {};
    byId("portal-profile-status").textContent = profile.status || "активен";
    byId("identity-note").textContent = profile.status_note ||
      profile.responsibilities ||
      "Настройки профиля доступны через /profile в Discord.";
    byId("identity-positions").replaceChildren(
      ...(data.legal_positions || []).map((item) =>
        el("span", "", `${item.emoji} ${item.label}`)
      ),
    );

    renderTreasury(data.treasury || {});
    if (!state.bills.length || forceWorkspace) {
      state.bills = data.legislation?.bills || [];
    } else {
      data.legislation.bills = state.bills;
    }
    renderBills();
    hydrateWorkspace(data.legislation?.workspace || null, forceWorkspace);

    const consensus = data.consensus || {};
    byId("consensus-state").textContent = consensus.active
      ? "В ЭФИРЕ"
      : "ОЖИДАНИЕ";
    byId("consensus-title").textContent = consensus.active
      ? `Консенсус · ${consensus.stage || "активен"}`
      : "Сейчас заседания нет";
    const queuedBills = Math.max(
      Number(consensus.queued_bills || 0),
      Number(data.legislation?.queued || 0),
    );
    byId("consensus-detail").textContent = consensus.active
      ? `Пленарное заседание ${
        consensus.plenary_number || ""
      }. Откройте панель, чтобы увидеть текущий законопроект.`
      : `В очереди законопроектов: ${queuedBills}.`;

    const inbox = data.notifications || { items: [], unread: 0 };
    const unread = Number(inbox.unread || 0);
    const urgent = (inbox.items || []).some((item) =>
      !item.read_at && item.severity === "critical"
    );
    document.body.dataset.reactorState = urgent
      ? "alert"
      : consensus.active
      ? "live"
      : "stable";
    globalThis.TModTabSignal?.set(unread, {
      urgent,
      blink: unread > 0,
      label: urgent ? "Важное уведомление T-Mod" : "Новые уведомления T-Mod",
    });
    byId("portal-unread").textContent = `${unread} НОВЫХ`;
    byId("portal-notification-list").replaceChildren(
      ...(inbox.items || []).slice(0, 8).map(notification),
    );
    if (!(inbox.items || []).length) {
      byId("portal-notification-list").append(
        el("div", "portal-empty", "Новых событий нет."),
      );
    }
    applyLayout(state.layout);
    return true;
  }

  async function load(silent = false, forceWorkspace = false) {
    try {
      const data = await request("/api/reactor/home");
      render(data, forceWorkspace);
      byId("portal-gate").hidden = true;
      byId("portal-shell").hidden = false;
      requestAnimationFrame(() => byId("portal-shell").classList.add("ready"));
      if (data.cache_state === "stale") {
        setTimeout(() => void loadFreshHome(), 0);
      }
    } catch (error) {
      if (silent) return;
      byId("portal-gate-message").textContent = error.status === 401
        ? "Сессия не найдена. Войдите по логину и восьмизначному PIN либо откройте персональную ссылку из Discord."
        : "Реактор временно не получил данные. Обновите страницу через несколько секунд.";
      byId("portal-login").hidden = error.status !== 401;
    }
  }

  async function loadFreshHome() {
    try {
      const data = await request("/api/reactor/home?fresh=1");
      render(data);
    } catch {
      // The stale snapshot remains usable; the regular smart refresh retries.
    }
  }

  async function loadLegislation(silent = false) {
    try {
      const data = await request("/api/reactor/legislation");
      if (!state.data) return;
      state.data.legislation = {
        workspace: data.workspace,
        bills: data.bills,
        next_number: data.next_number,
        queued: data.queued,
      };
      state.bills = data.bills || [];
      renderBills();
      hydrateWorkspace(data.workspace || null);
      if (data.cache_state === "stale") {
        setTimeout(() => void loadFreshLegislation(), 0);
      }
    } catch (error) {
      if (!silent) toast(error.message, "error");
    }
  }

  async function loadFreshLegislation() {
    try {
      const data = await request("/api/reactor/legislation?fresh=1");
      if (!state.data) return;
      state.data.legislation = {
        workspace: data.workspace,
        bills: data.bills,
        next_number: data.next_number,
        queued: data.queued,
      };
      state.bills = data.bills || [];
      renderBills();
      hydrateWorkspace(data.workspace || null);
    } catch {
      // Keep the already rendered registry and retry on the next smart tick.
    }
  }

  function openLayout() {
    const selected = new Set(state.layout);
    byId("portal-layout-list").replaceChildren(
      ...Object.entries(labels).map(([key, label]) => {
        const row = el("label");
        const input = el("input");
        input.type = "checkbox";
        input.value = key;
        input.checked = selected.has(key);
        row.append(input, el("span", "", label));
        return row;
      }),
    );
    openDialog(byId("portal-layout-dialog"));
  }

  byId("portal-customize").addEventListener("click", openLayout);
  byId("portal-layout-save").addEventListener("click", async () => {
    const layout = [
      ...byId("portal-layout-list").querySelectorAll("input:checked"),
    ].map((item) => item.value);
    try {
      const result = await request("/api/reactor/preferences", {
        method: "POST",
        body: JSON.stringify({ surface: "member", layout }),
      });
      state.layout = result.layout;
      applyLayout(state.layout);
      closeDialog(byId("portal-layout-dialog"));
      toast("Личный холст сохранён.");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  byId("portal-read-all").addEventListener("click", async () => {
    try {
      await request("/api/reactor/notifications/read", {
        method: "POST",
        body: JSON.stringify({ ids: [] }),
      });
      await load(true);
      toast("Уведомления отмечены прочитанными.");
    } catch (error) {
      toast(error.message, "error");
    }
  });
  byId("legislation-search").addEventListener("input", () => {
    state.billsVisible = 6;
    renderBills();
  });
  byId("legislation-more").addEventListener("click", () => {
    state.billsVisible += 6;
    renderBills();
  });
  byId("editor-create").addEventListener("click", async () => {
    if (state.busy) return;
    setBusy(
      true,
      "Создаём пространство",
      "Открываем личный защищённый черновик…",
    );
    try {
      const result = await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify({ action: "create" }),
      });
      hydrateWorkspace(result.workspace, true);
      showEditorTab("idea");
      toast(
        result.created
          ? "Личный черновик создан."
          : "Открыт ваш существующий черновик.",
      );
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  });
  document.querySelectorAll("[data-editor-tab]").forEach((button) =>
    button.addEventListener(
      "click",
      () => showEditorTab(button.dataset.editorTab),
    )
  );
  document.querySelectorAll("[data-editor-back]").forEach((button) =>
    button.addEventListener(
      "click",
      () => showEditorTab(button.dataset.editorBack),
    )
  );
  document.querySelectorAll("[data-editor-save]").forEach((button) =>
    button.addEventListener("click", () => void saveDraft())
  );
  Object.values(fields).forEach((id) =>
    byId(id).addEventListener("input", () => {
      state.dirty = true;
      updateEditorVisuals();
    })
  );
  byId("editor-ai").addEventListener("click", () => void runAiEditor());
  byId("editor-publish").addEventListener("click", () => void publishDraft());
  byId("editor-cancel").addEventListener("click", () => void cancelDraft());

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.busy) {
      void load(true);
      void loadLegislation(true);
    }
  });
  state.refreshTimer = setInterval(() => {
    if (!document.hidden && !state.busy) void load(true);
  }, 45000);
  state.legislationRefreshTimer = setInterval(() => {
    if (!document.hidden && !state.busy) void loadLegislation(true);
  }, 90000);
  globalThis.addEventListener(
    "pagehide",
    () => {
      clearInterval(state.refreshTimer);
      clearInterval(state.legislationRefreshTimer);
    },
  );
  globalThis.addEventListener("beforeunload", (event) => {
    if (state.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  updateCounters();
  void (async () => {
    await load();
    await loadLegislation(true);
  })();
})();
