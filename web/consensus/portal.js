"use strict";

(() => {
  const labels = {
    identity: "Мой мандат",
    treasury: "Казна",
    legislation: "Реестр законопроектов",
    my_bills: "Мои законопроекты",
    editor: "Законодательная мастерская",
    games: "T-Mod Games",
    consensus: "Консенсус",
    preparation: "Листы подготовки",
    notifications: "Уведомления",
  };
  const viewTitles = {
    overview: "Обзор",
    mandate: "Мой мандат",
    projects: "Законопроекты",
    editor: "Создать законопроект",
    treasury: "Казна Товарищества",
    consensus: "Консенсус",
    games: "T-Mod Games",
    notifications: "Центр уведомлений",
  };
  const viewWidgets = {
    mandate: ["identity"],
    projects: ["my_bills", "legislation"],
    editor: ["editor"],
    treasury: ["treasury"],
    consensus: ["consensus", "preparation"],
    games: ["games"],
    notifications: ["notifications"],
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
    executionBlocks: [],
    bills: [],
    billsVisible: 6,
    activeEditorTab: "idea",
    dirty: false,
    busy: false,
    refreshTimer: null,
    legislationRefreshTimer: null,
    renderSignature: "",
    onboardingRequired: false,
    onboardingOpened: false,
    activeView: "overview",
    preparation: {
      items: [],
      loaded: false,
      bill: null,
      sheet: null,
      sourceChanged: false,
      dirty: false,
      saving: false,
      saveTimer: null,
    },
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

  function chosenName(data = state.data) {
    const selected = String(data?.profile?.preferred_name || "").trim();
    if (selected) return selected;
    const discordName = String(data?.viewer?.name || "Участник");
    const projected = discordName.split("|").at(-1)?.trim();
    return projected || discordName;
  }

  function greeting(name) {
    const hour = new Date().getHours();
    if (hour < 5) return `Доброй ночи, ${name}`;
    if (hour < 12) return `Доброе утро, ${name}`;
    if (hour < 18) return `Добрый день, ${name}`;
    return `Добрый вечер, ${name}`;
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
      widget.dataset.layoutEnabled = selected.has(key) ? "true" : "false";
      widget.style.order = String(layout.indexOf(key) + 1 || 99);
    });
    document.querySelectorAll("[data-portal-target]").forEach((control) => {
      const target = control.dataset.portalTarget;
      const required = viewWidgets[target];
      control.hidden = Boolean(required && !required.some((key) => selected.has(key)));
    });
    const currentRequired = viewWidgets[state.activeView];
    if (currentRequired && !currentRequired.some((key) => selected.has(key))) {
      state.activeView = "overview";
    }
    activateView(state.activeView, false);
  }

  function normalizeView(value) {
    const raw = String(value || "").replace(/^#\/?/, "").split(/[?&]/)[0];
    const aliases = {
      "": "overview",
      home: "overview",
      identity: "mandate",
      legislation: "projects",
      "my-bills": "projects",
      my_bills: "projects",
    };
    const target = aliases[raw] || raw;
    return Object.hasOwn(viewTitles, target) ? target : "overview";
  }

  function activateView(value, push = true) {
    const target = normalizeView(value);
    state.activeView = target;
    document.querySelectorAll("[data-portal-view]").forEach((node) => {
      const viewMatches = node.dataset.portalView === target;
      const layoutEnabled = node.dataset.portalWidget
        ? node.dataset.layoutEnabled !== "false"
        : true;
      node.hidden = !(viewMatches && layoutEnabled);
    });
    document.querySelectorAll("[data-portal-target]").forEach((control) => {
      control.classList.toggle("active", control.dataset.portalTarget === target);
      if (control.closest(".portal-section-nav")) {
        control.setAttribute(
          "aria-current",
          control.dataset.portalTarget === target ? "page" : "false",
        );
      }
    });
    byId("portal-view-title").textContent = viewTitles[target];
    document.title = `${viewTitles[target]} — Реактор T-Mod`;
    if (push && location.hash !== `#${target}`) {
      history.pushState({ portalView: target }, "", `#${target}`);
    }
    if (push) {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    if (target === "consensus" && !state.preparation.loaded) {
      void loadPreparation(true);
    }
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
    byId("overview-treasury").textContent = formatMoney(treasury.balance);
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

  function fillOnboarding(data, editing = false) {
    const profile = data.profile || {};
    const hasCharacter = Boolean(data.onboarding?.has_character);
    byId("onboarding-character-fields").hidden = hasCharacter;
    byId("onboarding-character-name").required = !hasCharacter;
    byId("onboarding-character-static").required = !hasCharacter;
    byId("onboarding-preferred-name").value = profile.preferred_name ||
      chosenName(data);
    byId("onboarding-character-name").value = "";
    byId("onboarding-character-static").value = "";
    byId("onboarding-biography").value = profile.biography || "";
    byId("onboarding-contribution").value = profile.contribution || "";
    byId("onboarding-responsibilities").value = profile.responsibilities || "";
    byId("onboarding-membership-since").value = profile.membership_since ||
      new Date().toISOString().slice(0, 10);
    byId("onboarding-close").hidden = !editing;
    byId("onboarding-submit").textContent = editing
      ? "Сохранить мой мандат"
      : "Активировать личный Реактор";
    byId("onboarding-mode").textContent = editing
      ? "НАСТРОЙКА МАНДАТА"
      : "ВСТУПЛЕНИЕ В КОНТУР";
    updateNicknamePreview();
  }

  function updateNicknamePreview() {
    const character = byId("onboarding-character-name").value.trim();
    const staticId = byId("onboarding-character-static").value.trim();
    const preferred = byId("onboarding-preferred-name").value.trim();
    const parts = character.split(/\s+/).filter(Boolean);
    const compact = parts.length > 1
      ? `${parts[0].charAt(0).toUpperCase()}. ${parts.slice(1).join(" ")}`
      : "S. Goodman";
    byId("onboarding-nickname-preview").textContent =
      `${compact} | ${staticId || "263345"} | ${preferred || "Иван"}`;
  }

  function onboardingFeedback(message = "", kind = "") {
    const node = byId("onboarding-feedback");
    node.textContent = message;
    node.dataset.kind = kind;
    node.hidden = !message;
  }

  function validateOnboarding() {
    const characterRequired = !byId("onboarding-character-fields").hidden;
    const checks = [
      ["onboarding-preferred-name", (value) => value.length >= 2 && value.length <= 24 && !value.includes("|"), "Укажите, как к вам обращаться: от 2 до 24 символов."],
      ...(characterRequired ? [
        ["onboarding-character-name", (value) => value.length >= 2 && value.split(/\s+/).filter(Boolean).length >= 2, "Укажите имя и фамилию персонажа через пробел."],
        ["onboarding-character-static", (value) => /^[0-9]{1,12}$/.test(value), "Статик должен состоять из 1–12 цифр."],
      ] : []),
      ["onboarding-biography", (value) => value.length >= 3, "Коротко расскажите о себе — минимум 3 символа."],
      ["onboarding-contribution", (value) => value.length >= 3, "Укажите, чем вы занимаетесь — минимум 3 символа."],
      ["onboarding-responsibilities", (value) => value.length >= 3, "Укажите зону ответственности — минимум 3 символа."],
      ["onboarding-membership-since", (value) => Boolean(value), "Укажите дату вступления."],
    ];
    document.querySelectorAll("#member-onboarding-form [aria-invalid='true']")
      .forEach((node) => node.removeAttribute("aria-invalid"));
    for (const [id, valid, message] of checks) {
      const input = byId(id);
      if (!valid(input.value.trim())) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
        onboardingFeedback(message, "error");
        return false;
      }
    }
    onboardingFeedback();
    return true;
  }

  function openOnboarding(editing = false) {
    if (!state.data) return;
    fillOnboarding(state.data, editing);
    const dialog = byId("member-onboarding-dialog");
    dialog.dataset.required = editing ? "false" : "true";
    openDialog(dialog);
  }

  function renderMandate(data, name) {
    const profile = data.profile || {};
    const primaryId = Number(profile.primary_character_id || 0);
    const characters = data.characters || [];
    const character = characters.find((item) => Number(item.id) === primaryId) ||
      characters[0];
    byId("mandate-character").textContent = character
      ? `${character.nickname} · #${character.static_id}`
      : "Персонаж ещё не добавлен";
    byId("overview-character").textContent = character
      ? `${character.nickname} · #${character.static_id}`
      : "Персонаж не выбран";
    byId("mandate-since").textContent = profile.membership_since
      ? formatDate(profile.membership_since)
      : formatDate(data.mandate?.joined_at, "не зафиксировано");
    byId("mandate-contribution").textContent = profile.contribution ||
      "Направление деятельности пока не описано.";
    byId("mandate-responsibilities").textContent = profile.responsibilities ||
      "Зона ответственности пока не указана.";
    byId("mandate-biography").textContent = profile.biography ||
      `${name} ещё не заполнил краткую карточку участника.`;
    const nickname = data.mandate?.nickname || {};
    byId("mandate-nickname-state").textContent = nickname.exempt
      ? "Ник администратора не изменяется"
      : nickname.synced
      ? "Discord-ник синхронизирован"
      : "Discord-ник ожидает синхронизации";
    byId("mandate-nickname-state").dataset.state = nickname.synced ||
        nickname.exempt
      ? "ok"
      : "pending";
    byId("mandate-sync-nickname").hidden = Boolean(
      nickname.synced || nickname.exempt || !nickname.expected,
    );
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

  const PREPARATION_MAX_QUESTIONS = 12;

  function defaultPreparationSheet() {
    return {
      questions: [],
      notes: "",
      preliminary_vote: null,
      preliminary_vote_reason: "",
      review_flags: {
        read_text: false,
        verify_sources: false,
        need_discussion: false,
      },
      source_bill_updated_at: null,
      revision: 0,
      created_at: null,
      updated_at: null,
    };
  }

  function normalisePreparationSheet(value) {
    const fallback = defaultPreparationSheet();
    const source = value && typeof value === "object" ? value : {};
    const flags = source.review_flags && typeof source.review_flags === "object"
      ? source.review_flags
      : {};
    const questions = Array.isArray(source.questions)
      ? source.questions.slice(0, PREPARATION_MAX_QUESTIONS).flatMap((item, index) => {
        if (!item || typeof item !== "object") return [];
        return [{
          id: String(item.id || `question-${index + 1}`),
          text: String(item.text || ""),
          resolved: Boolean(item.resolved),
        }];
      })
      : [];
    const vote = ["yes", "no", "abstain"].includes(String(source.preliminary_vote || ""))
      ? String(source.preliminary_vote)
      : null;
    return {
      ...fallback,
      questions,
      notes: String(source.notes || ""),
      preliminary_vote: vote,
      preliminary_vote_reason: String(source.preliminary_vote_reason || ""),
      review_flags: {
        read_text: Boolean(flags.read_text),
        verify_sources: Boolean(flags.verify_sources),
        need_discussion: Boolean(flags.need_discussion),
      },
      source_bill_updated_at: source.source_bill_updated_at || null,
      revision: Math.max(0, Number(source.revision) || 0),
      created_at: source.created_at || null,
      updated_at: source.updated_at || null,
    };
  }

  function preparationNumber(bill) {
    const value = Number(bill?.bill_number ?? bill?.number ?? 0);
    return Number.isInteger(value) && value > 0 ? String(value).padStart(3, "0") : "—";
  }

  function preparationAuthor(bill) {
    if (bill?.author && typeof bill.author === "object") {
      return String(bill.author.name || "Автор не указан");
    }
    return String(bill?.author || "Автор не указан");
  }

  function preparationDraft() {
    const sheet = state.preparation.sheet || defaultPreparationSheet();
    return {
      questions: sheet.questions
        .filter((item) => String(item?.text || "").trim())
        .map((item) => ({
          id: String(item.id || ""),
          text: String(item.text || ""),
          resolved: Boolean(item.resolved),
        })),
      notes: String(sheet.notes || ""),
      preliminary_vote: sheet.preliminary_vote || null,
      preliminary_vote_reason: String(sheet.preliminary_vote_reason || ""),
      review_flags: {
        read_text: Boolean(sheet.review_flags?.read_text),
        verify_sources: Boolean(sheet.review_flags?.verify_sources),
        need_discussion: Boolean(sheet.review_flags?.need_discussion),
      },
    };
  }

  function preparationDraftSignature() {
    return JSON.stringify(preparationDraft());
  }

  function preparationDraftHasContent() {
    const draft = preparationDraft();
    return Boolean(
      draft.questions.length ||
      draft.notes.trim() ||
      draft.preliminary_vote ||
      draft.preliminary_vote_reason.trim() ||
      Object.values(draft.review_flags).some(Boolean),
    );
  }

  function setPreparationSaveState(label, kind = "") {
    const node = byId("reactor-preparation-save-state");
    node.textContent = label;
    node.dataset.state = kind;
  }

  function preparationStatusLabel(marker) {
    const vote = String(marker?.preliminary_vote || "");
    if (vote === "yes") return "ПОЗИЦИЯ: ПОДДЕРЖАТЬ";
    if (vote === "no") return "ПОЗИЦИЯ: НЕ ПОДДЕРЖИВАТЬ";
    if (vote === "abstain") return "ПОЗИЦИЯ: ВОЗДЕРЖАТЬСЯ";
    return marker?.prepared ? "ЛИСТ В РАБОТЕ" : "НЕ НАЧАТ";
  }

  function updatePreparationSummary() {
    const items = state.preparation.items || [];
    const prepared = items.filter((item) => item.preparation?.prepared).length;
    byId("preparation-progress").textContent = `${prepared} / ${items.length}`;
    if (state.data && !state.data.consensus?.active) {
      byId("overview-consensus-detail").textContent = items.length
        ? `Подготовлено: ${prepared} из ${items.length}`
        : "Законопроектов в очереди нет";
    }
  }

  function renderPreparation() {
    const list = byId("preparation-list");
    const items = state.preparation.items || [];
    const nodes = items.map((item) => {
      const marker = item.preparation || {};
      const row = el("article", `preparation-row${marker.prepared ? " prepared" : ""}`);
      const copy = el("div", "preparation-row-copy");
      copy.append(
        el("small", "", `ПРОЕКТ № ${preparationNumber(item)} · ${preparationStatusLabel(marker)}`),
        el("h3", "", item.title || "Без названия"),
        el("p", "", item.summary || "Текст проекта доступен в личном листе подготовки."),
      );
      const meta = el("footer");
      meta.append(
        el("span", "", preparationAuthor(item)),
        el("span", "", marker.updated_at ? formatMoment(marker.updated_at) : ""),
      );
      copy.append(meta);
      const button = el("button", "", marker.prepared ? "Продолжить" : "Открыть лист");
      button.type = "button";
      button.addEventListener("click", () => void openPreparationSheet(item));
      row.append(copy, button);
      return row;
    });
    list.replaceChildren(...nodes);
    if (!nodes.length) {
      list.append(el("div", "portal-empty", "В очереди пока нет законопроектов для подготовки."));
    }
    updatePreparationSummary();
  }

  function renderPreparationVotes() {
    const vote = state.preparation.sheet?.preliminary_vote || "";
    document.querySelectorAll("[data-reactor-preparation-vote]").forEach((button) => {
      const selected = button.dataset.reactorPreparationVote === vote;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function newPreparationQuestionId() {
    const suffix = globalThis.crypto?.randomUUID?.() ||
      `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    return `question-${suffix}`.slice(0, 72);
  }

  function renderPreparationQuestions() {
    const list = byId("reactor-preparation-questions");
    const questions = state.preparation.sheet?.questions || [];
    const nodes = questions.map((question, index) => {
      const item = el("article", `preparation-question${question.resolved ? " resolved" : ""}`);
      const number = el("span", "", String(index + 1).padStart(2, "0"));
      const field = el("textarea");
      field.rows = 2;
      field.maxLength = 1000;
      field.placeholder = "Сформулируйте вопрос…";
      field.value = question.text;
      field.setAttribute("aria-label", `Вопрос ${index + 1}`);
      field.addEventListener("input", () => {
        const current = state.preparation.sheet?.questions.find((entry) => entry.id === question.id);
        if (!current) return;
        current.text = field.value;
        markPreparationDirty();
      });
      const actions = el("div", "preparation-question-actions");
      const resolve = el("button", "", question.resolved ? "↺" : "✓");
      resolve.type = "button";
      resolve.title = question.resolved ? "Сделать вопрос открытым" : "Отметить как решённый";
      resolve.setAttribute("aria-label", resolve.title);
      resolve.addEventListener("click", () => {
        const current = state.preparation.sheet?.questions.find((entry) => entry.id === question.id);
        if (!current) return;
        current.resolved = !current.resolved;
        renderPreparationQuestions();
        markPreparationDirty();
      });
      const remove = el("button", "", "×");
      remove.type = "button";
      remove.title = "Удалить вопрос";
      remove.setAttribute("aria-label", remove.title);
      remove.addEventListener("click", () => {
        if (!state.preparation.sheet) return;
        state.preparation.sheet.questions = state.preparation.sheet.questions.filter(
          (entry) => entry.id !== question.id,
        );
        renderPreparationQuestions();
        markPreparationDirty();
      });
      actions.append(resolve, remove);
      item.append(number, field, actions);
      return item;
    });
    list.replaceChildren(...nodes);
    byId("reactor-preparation-questions-empty").hidden = questions.length > 0;
  }

  function renderPreparationSheet() {
    const prep = state.preparation;
    const bill = prep.bill || {};
    const sheet = prep.sheet || defaultPreparationSheet();
    byId("reactor-preparation-number").textContent = `ЛИЧНЫЙ ЛИСТ · ПРОЕКТ № ${preparationNumber(bill)}`;
    byId("reactor-preparation-title").textContent = bill.title || "Подготовка к рассмотрению";
    byId("reactor-preparation-author").textContent = `Автор: ${preparationAuthor(bill)}`;
    byId("reactor-preparation-summary").textContent = bill.summary || "Текст законопроекта не сохранён.";
    const materials = String(bill.materials || "").trim();
    const materialsSection = byId("reactor-preparation-materials");
    materialsSection.hidden = !materials;
    if (materials) materialsSection.querySelector("p").textContent = materials;
    const sourceState = byId("reactor-preparation-source-state");
    sourceState.textContent = prep.sourceChanged ? "ТЕКСТ ОБНОВЛЁН" : "АКТУАЛЬНАЯ РЕДАКЦИЯ";
    sourceState.dataset.changed = String(Boolean(prep.sourceChanged));
    byId("reactor-preparation-reason").value = sheet.preliminary_vote_reason;
    byId("reactor-preparation-notes").value = sheet.notes;
    byId("reactor-preparation-notes-count").textContent = `${sheet.notes.length} / 16000`;
    byId("reactor-preparation-read-text").checked = Boolean(sheet.review_flags.read_text);
    byId("reactor-preparation-verify-sources").checked = Boolean(sheet.review_flags.verify_sources);
    byId("reactor-preparation-need-discussion").checked = Boolean(sheet.review_flags.need_discussion);
    renderPreparationVotes();
    renderPreparationQuestions();
    setPreparationSaveState(
      sheet.updated_at ? `Сохранено ${formatMoment(sheet.updated_at)}` : "Изменения сохраняются автоматически",
      sheet.updated_at ? "saved" : "",
    );
  }

  function schedulePreparationSave() {
    clearTimeout(state.preparation.saveTimer);
    state.preparation.saveTimer = setTimeout(() => {
      void savePreparation({ silent: true });
    }, 850);
  }

  function markPreparationDirty({ schedule = true } = {}) {
    if (!state.preparation.sheet) return;
    state.preparation.dirty = true;
    setPreparationSaveState("Есть несохранённые изменения", "dirty");
    if (schedule) schedulePreparationSave();
  }

  function markPreparationListItemSaved() {
    const prep = state.preparation;
    const billId = Number(prep.bill?.id || 0);
    const item = prep.items.find((candidate) => Number(candidate.id || 0) === billId);
    if (!item || !prep.sheet) return;
    item.preparation = {
      prepared: true,
      preliminary_vote: prep.sheet.preliminary_vote || null,
      updated_at: prep.sheet.updated_at || new Date().toISOString(),
    };
    renderPreparation();
  }

  async function savePreparation({ silent = false } = {}) {
    const prep = state.preparation;
    const billId = Number(prep.bill?.id || 0);
    if (!billId || !prep.sheet || prep.saving) return false;
    if (!prep.dirty && prep.sheet.revision > 0) return true;
    clearTimeout(prep.saveTimer);
    if (prep.sheet.revision === 0 && !preparationDraftHasContent()) {
      prep.dirty = false;
      setPreparationSaveState("Лист ожидает первой записи");
      return true;
    }
    prep.saving = true;
    const beforeSave = preparationDraftSignature();
    const expectedRevision = prep.sheet.revision;
    setPreparationSaveState("Сохраняем личный лист…", "saving");
    try {
      const result = await request(`/api/bills/${billId}/preparation`, {
        method: "POST",
        body: JSON.stringify({
          expected_revision: expectedRevision,
          ...preparationDraft(),
        }),
      });
      const saved = normalisePreparationSheet(result.sheet);
      if (preparationDraftSignature() === beforeSave) {
        prep.sheet = saved;
        prep.dirty = false;
        prep.sourceChanged = Boolean(result.source_changed);
        renderPreparationSheet();
      } else {
        prep.sheet.revision = saved.revision;
        prep.sheet.updated_at = saved.updated_at;
        prep.dirty = true;
        setPreparationSaveState("Есть новые изменения после сохранения", "dirty");
        schedulePreparationSave();
      }
      markPreparationListItemSaved();
      return true;
    } catch (error) {
      setPreparationSaveState("Не удалось сохранить лист", "error");
      if (!silent) toast(error.message || "Проверьте соединение и повторите.", "error");
      return false;
    } finally {
      prep.saving = false;
    }
  }

  function setPreparationControlsDisabled(disabled) {
    document.querySelectorAll(
      "#reactor-preparation-dialog .preparation-workspace button, " +
      "#reactor-preparation-dialog .preparation-workspace textarea, " +
      "#reactor-preparation-dialog .preparation-workspace input, " +
      "#reactor-preparation-save, #reactor-preparation-download",
    ).forEach((control) => {
      control.disabled = disabled;
    });
  }

  async function openPreparationSheet(item) {
    const billId = Number(item?.id || 0);
    if (!Number.isInteger(billId) || billId <= 0) {
      toast("Карточка законопроекта пока недоступна.", "error");
      return;
    }
    const prep = state.preparation;
    if (prep.bill && prep.dirty && !(await savePreparation({ silent: true }))) {
      toast("Сначала сохраните текущий лист подготовки.", "error");
      return;
    }
    prep.bill = { ...item, id: billId };
    prep.sheet = defaultPreparationSheet();
    prep.sourceChanged = false;
    prep.dirty = false;
    renderPreparationSheet();
    openDialog(byId("reactor-preparation-dialog"));
    setPreparationControlsDisabled(true);
    setPreparationSaveState("Загружаем личный лист…", "saving");
    try {
      const result = await request(`/api/bills/${billId}/preparation`);
      prep.bill = result.bill || prep.bill;
      prep.sheet = normalisePreparationSheet(result.sheet);
      prep.sourceChanged = Boolean(result.source_changed);
      prep.dirty = false;
      renderPreparationSheet();
    } catch (error) {
      setPreparationSaveState("Лист временно недоступен", "error");
      toast(error.message || "Обновите страницу и повторите.", "error");
    } finally {
      setPreparationControlsDisabled(false);
    }
  }

  async function downloadPreparationPdf() {
    const prep = state.preparation;
    const billId = Number(prep.bill?.id || 0);
    if (!billId) return;
    if (prep.dirty && !(await savePreparation())) return;
    const button = byId("reactor-preparation-download");
    button.disabled = true;
    button.textContent = "Собираем PDF…";
    try {
      const response = await fetch(`/api/bills/${billId}/preparation.pdf`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `consensus-preparation-${preparationNumber(prep.bill)}.pdf`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setPreparationSaveState("PDF подготовлен · файл можно редактировать офлайн", "saved");
    } catch {
      setPreparationSaveState("Не удалось собрать PDF", "error");
      toast("Не удалось скачать PDF. Попробуйте ещё раз.", "error");
    } finally {
      button.disabled = false;
      button.textContent = "Скачать PDF";
    }
  }

  async function loadPreparation(silent = false) {
    try {
      const payload = await request("/api/reactor/preparation");
      state.preparation.items = Array.isArray(payload.items) ? payload.items : [];
      state.preparation.loaded = true;
      renderPreparation();
    } catch (error) {
      if (!silent) toast(error.message || "Не удалось загрузить листы подготовки.", "error");
    }
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
      execution_blocks: state.executionBlocks,
      ...extra,
    };
  }

  const blockLabels = {
    rule_change: "Изменить правило",
    task: "Выполнить задачу",
    communication: "Сообщить участникам",
    appointment: "Назначить ответственного",
    integration: "Настроить систему",
    review: "Проверить результат",
  };

  function addExecutionBlock(type, preset = {}) {
    state.executionBlocks.push({
      id: preset.id || `block-${Date.now()}-${state.executionBlocks.length}`,
      type: type || "task",
      title: preset.title || blockLabels[type] || "Новый шаг",
      description: preset.description || "",
      owner: preset.owner || "",
      deadline: preset.deadline || "",
    });
    state.dirty = true;
    renderExecutionChain();
  }

  function renderExecutionChain() {
    const root = byId("execution-chain");
    if (!root) return;
    byId("execution-count").textContent = `${state.executionBlocks.length} БЛОКОВ`;
    if (!state.executionBlocks.length) {
      root.replaceChildren(el("li", "execution-empty", "Добавьте первый шаг или вставьте готовый пример."));
      return;
    }
    root.replaceChildren(...state.executionBlocks.map((block, index) => {
      const row = el("li", "execution-block");
      const fieldsNode = el("div", "execution-block-fields");
      const type = el("select");
      Object.entries(blockLabels).forEach(([value, label]) => {
        const option = el("option", "", label);
        option.value = value;
        option.selected = value === block.type;
        type.append(option);
      });
      const title = el("input");
      title.value = block.title || "";
      title.maxLength = 180;
      title.placeholder = "Название шага";
      const description = el("textarea");
      description.value = block.description || "";
      description.maxLength = 1500;
      description.rows = 2;
      description.placeholder = "Что именно должно произойти";
      const owner = el("input");
      owner.value = block.owner || "";
      owner.maxLength = 120;
      owner.placeholder = "Ответственный";
      const deadline = el("input");
      deadline.value = block.deadline || "";
      deadline.maxLength = 80;
      deadline.placeholder = "Срок: например, 7 дней";
      [[type, "type"], [title, "title"], [description, "description"], [owner, "owner"], [deadline, "deadline"]]
        .forEach(([input, key]) => input.addEventListener("input", () => {
          state.executionBlocks[index][key] = input.value;
          state.dirty = true;
        }));
      fieldsNode.append(type, title, description, owner, deadline);
      const remove = el("button", "", "×");
      remove.type = "button";
      remove.title = "Удалить блок";
      remove.addEventListener("click", () => {
        state.executionBlocks.splice(index, 1);
        state.dirty = true;
        renderExecutionChain();
      });
      row.append(fieldsNode, remove);
      return row;
    }));
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
      state.executionBlocks = [];
      renderExecutionChain();
      byId("preview-number").textContent = "№ ПОСЛЕ МОДЕРАЦИИ";
      updateEditorVisuals();
      return;
    }
    if (force || changedWorkspace || !state.dirty) {
      Object.entries(fields).forEach(([key, id]) => {
        byId(id).value = workspace[key] || "";
      });
      state.executionBlocks = Array.isArray(workspace.execution_blocks)
        ? workspace.execution_blocks.map((item) => ({ ...item }))
        : [];
      renderExecutionChain();
      state.dirty = false;
    }
    byId("preview-number").textContent = "№ ПОСЛЕ МОДЕРАЦИИ";
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
      title: "Отправить законопроект на модерацию?",
      message:
        "Модерация проверит текст и цепочку исполнения. Номер появится только после одобрения.",
      label: "Отправить на модерацию",
    });
    if (!confirmed) return;
    setBusy(
      true,
      "Передаём на модерацию",
      "Фиксируем редакцию и открываем карточку для руководства…",
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
      toast(result.message || "Законопроект передан на модерацию.");
      document.querySelector("#my-bills")?.scrollIntoView({
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

  const workspaceStatus = (workspace) => ({
    draft: ["ЧЕРНОВИК", "draft"],
    review: ["ГОТОВИТСЯ", "draft"],
    moderation: ["НА МОДЕРАЦИИ", "pending"],
    changes_requested: ["НУЖНО ДОПОЛНИТЬ", "warning"],
    rejected: ["ОТКЛОНЁН", "rejected"],
    submitted: ["ОДОБРЕН", "accepted"],
    cancelled: ["ОТМЕНЁН", "resolved"],
  }[String(workspace.status || "draft")] || ["В РАБОТЕ", "draft"]);

  function workspaceCard(workspace, moderator = false) {
    const [label, kind] = workspaceStatus(workspace);
    const node = el("button", `${moderator ? "moderation-card" : "governance-card"} ${kind}`);
    node.type = "button";
    node.append(
      el("small", "", moderator ? `РАУНД ${workspace.moderation?.round || 1} · ${workspace.author || "Участник"}` : `РЕДАКЦИЯ ${workspace.revision || 1}`),
      el("h3", "", workspace.title || "Проект без названия"),
      el("p", "", workspace.summary || workspace.idea || "Текст ещё не заполнен."),
    );
    const footer = el("footer");
    footer.append(el("span", "", formatMoment(workspace.updated_at)), el("b", "", label));
    node.append(footer);
    node.addEventListener("click", () => openWorkspaceCard(workspace, moderator));
    return node;
  }

  function openWorkspaceCard(workspace, moderator = false) {
    const dialog = byId("governance-dialog");
    byId("governance-dialog-kicker").textContent = moderator ? "МОДЕРАЦИЯ" : "МОЙ ЗАКОНОПРОЕКТ";
    byId("governance-dialog-title").textContent = workspace.title || "Проект без названия";
    const body = byId("governance-dialog-body");
    const intro = el("article");
    intro.append(el("h3", "", "Текст проекта"), el("p", "", workspace.summary || "Текст ещё не заполнен."));
    const chain = el("article");
    chain.append(el("h3", "", "Цепочка исполнения"));
    (workspace.execution_blocks || []).forEach((block, index) => {
      chain.append(el("p", "", `${String(index + 1).padStart(2, "0")} · ${blockLabels[block.type] || "Шаг"}: ${block.title}${block.owner ? ` · ${block.owner}` : ""}`));
    });
    if (!(workspace.execution_blocks || []).length) chain.append(el("p", "", "Отдельные блоки не добавлены."));
    body.replaceChildren(intro, chain);
    if (workspace.moderation?.note) {
      const note = el("article");
      note.append(el("h3", "", "Комментарий модерации"), el("p", "", workspace.moderation.note));
      body.append(note);
    }
    if (moderator) {
      const note = el("textarea");
      note.rows = 4;
      note.maxLength = 2000;
      note.placeholder = "Комментарий автору или примечание к одобрению";
      const actions = el("div", "moderation-actions");
      [["approved", "Одобрить"], ["changes_requested", "На дополнение"], ["rejected", "Отклонить"]].forEach(([decision, label]) => {
        const button = el("button", "", label);
        button.type = "button";
        button.dataset.decision = decision;
        button.addEventListener("click", () => void moderateBill(workspace, decision, note.value));
        actions.append(button);
      });
      body.append(note, actions);
    }
    openDialog(dialog);
  }

  async function moderateBill(workspace, decision, note) {
    if (decision !== "approved" && note.trim().length < 3) {
      toast("Добавьте понятный комментарий автору.", "error");
      return;
    }
    setBusy(true, "Фиксируем решение", "Обновляем историю и очередь законопроектов…");
    try {
      await request("/api/reactor/legislation", {
        method: "POST",
        body: JSON.stringify({ action: "moderate", workspace_id: workspace.id, expected_revision: workspace.revision, decision, note: note.trim() }),
      });
      closeDialog(byId("governance-dialog"));
      await loadLegislation(true);
      toast(decision === "approved" ? "Законопроект одобрен и поставлен в очередь." : decision === "changes_requested" ? "Проект возвращён автору на дополнение." : "Проект отклонён.");
    } catch (error) { toast(error.message, "error"); }
    finally { setBusy(false); }
  }

  function renderGovernance(legislation = {}) {
    const mine = legislation.my_workspaces || [];
    byId("my-bills-count").textContent = String(mine.length);
    byId("my-bills-list").replaceChildren(...mine.slice(0, 12).map((item) => workspaceCard(item)));
    if (!mine.length) byId("my-bills-list").append(el("div", "portal-empty", "У вас пока нет законопроектов."));
    const moderation = legislation.moderation || {};
    byId("moderation-desk").hidden = !moderation.allowed;
    if (moderation.allowed) {
      byId("moderation-list").replaceChildren(...(moderation.queue || []).map((item) => workspaceCard(item, true)));
      if (!(moderation.queue || []).length) byId("moderation-list").append(el("div", "portal-empty", "Очередь модерации пуста."));
    }
  }

  function renderDesktopRecommendation(data) {
    const banner = byId("desktop-recommendation");
    if (!banner) return;
    const inDesktop = /(?:TModDesktop\/|\bElectron\/)/i.test(navigator.userAgent);
    const dismissed = localStorage.getItem("tmod-desktop-recommendation-v1") === "dismissed";
    const senator = (data.legal_positions || []).some((item) =>
      String(item.label || "").trim() === "Сенатор Товарищества"
    );
    banner.hidden = inDesktop || dismissed || !senator;
  }

  function render(data, forceWorkspace = false) {
    state.data = data;
    state.csrf = data.viewer.csrf_token;
    state.layout = Array.isArray(data.layout) ? data.layout : state.layout;
    const currentName = chosenName(data);
    byId("portal-welcome").textContent = greeting(currentName);
    renderDesktopRecommendation(data);
    const { cache_state: _cacheState, ...stableData } = data;
    const signature = JSON.stringify(stableData);
    if (signature === state.renderSignature && !forceWorkspace) return false;
    state.renderSignature = signature;
    const name = currentName;
    byId("portal-side-name").textContent = name;
    byId("portal-side-avatar").textContent = name.charAt(0).toUpperCase();
    byId("identity-name").textContent = name;
    byId("identity-legal").textContent = data.legal_status || "Прихожанин";
    byId("overview-legal").textContent = data.legal_status || "Прихожанин";
    byId("portal-avatar").textContent = name.charAt(0).toUpperCase();
    byId("identity-avatar").textContent = name.charAt(0).toUpperCase();
    byId("preview-author").textContent = `Автор: ${name}`;
    byId("portal-admin-link").hidden = !data.viewer.administrator;
    byId("portal-sync-time").textContent = `обновлено ${
      formatMoment(new Date().toISOString())
    }`;
    byId("portal-view-freshness").innerHTML = "<i></i> Данные актуальны";
    const profile = data.profile || {};
    byId("portal-profile-status").textContent = profile.status || "активен";
    byId("identity-note").textContent = profile.status_note ||
      profile.responsibilities ||
      "Настройки аккаунта доступны через /account в Discord.";
    byId("identity-positions").replaceChildren(
      ...(data.legal_positions || []).map((item) =>
        el("span", "", `${item.emoji} ${item.label}`)
      ),
    );
    renderMandate(data, name);

    renderTreasury(data.treasury || {});
    if (!state.bills.length || forceWorkspace) {
      state.bills = data.legislation?.bills || [];
    } else {
      data.legislation.bills = state.bills;
    }
    renderBills();
    hydrateWorkspace(data.legislation?.workspace || null, forceWorkspace);
    renderGovernance(data.legislation || {});
    byId("overview-bills").textContent = String(
      (data.legislation?.my_workspaces || []).length,
    );

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
    byId("overview-consensus").textContent = consensus.active ? "В эфире" : "Ожидание";
    byId("overview-consensus-detail").textContent = consensus.active
      ? `Пленарное заседание ${consensus.plenary_number || ""}`.trim()
      : `В очереди: ${queuedBills}`;
    if (state.preparation.loaded) updatePreparationSummary();

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
    byId("overview-notifications").textContent = `${unread} новых`;
    byId("portal-side-unread").textContent = unread ? `${unread} новых` : "Нет новых";
    byId("portal-notification-list").replaceChildren(
      ...(inbox.items || []).slice(0, 8).map(notification),
    );
    if (!(inbox.items || []).length) {
      byId("portal-notification-list").append(
        el("div", "portal-empty", "Новых событий нет."),
      );
    }
    applyLayout(state.layout);
    state.onboardingRequired = Boolean(data.onboarding?.required);
    if (state.onboardingRequired && !state.onboardingOpened) {
      state.onboardingOpened = true;
      requestAnimationFrame(() => openOnboarding(false));
    }
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
      state.data.legislation = data;
      state.bills = data.bills || [];
      renderBills();
      hydrateWorkspace(data.workspace || null);
      renderGovernance(data);
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
      state.data.legislation = data;
      state.bills = data.bills || [];
      renderBills();
      hydrateWorkspace(data.workspace || null);
      renderGovernance(data);
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
  byId("portal-sidebar-customize").addEventListener("click", openLayout);
  document.querySelectorAll("[data-portal-target]").forEach((control) => {
    control.addEventListener("click", () => activateView(control.dataset.portalTarget));
  });
  globalThis.addEventListener("hashchange", () => activateView(location.hash, false));
  byId("mandate-edit").addEventListener("click", () => openOnboarding(true));
  byId("member-onboarding-dialog").addEventListener("cancel", (event) => {
    if (byId("member-onboarding-dialog").dataset.required === "true") {
      event.preventDefault();
    }
  });
  ["onboarding-preferred-name", "onboarding-character-name", "onboarding-character-static"]
    .forEach((id) => byId(id).addEventListener("input", updateNicknamePreview));
  byId("member-onboarding-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.busy) return;
    if (!validateOnboarding()) return;
    const submit = byId("onboarding-submit");
    const originalLabel = submit.textContent;
    state.busy = true;
    submit.disabled = true;
    submit.textContent = "Активируем…";
    onboardingFeedback("Сохраняем личность и подключаем личный Реактор…");
    try {
      const result = await request("/api/reactor/onboarding", {
        method: "POST",
        body: JSON.stringify({
          action: state.onboardingRequired ? "complete" : "save",
          preferred_name: byId("onboarding-preferred-name").value.trim(),
          character_nickname: byId("onboarding-character-name").value.trim(),
          character_static: byId("onboarding-character-static").value.trim(),
          biography: byId("onboarding-biography").value.trim(),
          contribution: byId("onboarding-contribution").value.trim(),
          responsibilities: byId("onboarding-responsibilities").value.trim(),
          membership_since: byId("onboarding-membership-since").value,
        }),
      });
      state.onboardingRequired = false;
      state.onboardingOpened = false;
      closeDialog(byId("member-onboarding-dialog"));
      await loadFreshHome();
      toast(result.nickname?.synced || result.nickname?.exempt
        ? "Мандат сформирован. Добро пожаловать в Реактор."
        : "Мандат сохранён. Ник Discord можно синхронизировать повторно в карточке.");
    } catch (error) {
      onboardingFeedback(error.message || "Не удалось активировать Реактор. Повторите попытку.", "error");
    } finally {
      state.busy = false;
      submit.disabled = false;
      submit.textContent = state.onboardingRequired
        ? originalLabel
        : "Сохранить мой мандат";
    }
  });
  byId("mandate-sync-nickname").addEventListener("click", async () => {
    setBusy(true, "Синхронизируем Discord", "Устанавливаем единый ник участника…");
    try {
      const result = await request("/api/reactor/onboarding", {
        method: "POST",
        body: JSON.stringify({ action: "sync_nickname" }),
      });
      await loadFreshHome();
      toast(result.nickname?.synced
        ? "Discord-ник синхронизирован."
        : "Discord не разрешил сменить ник. Администратор получил запись в журнале.",
        result.nickname?.synced ? "success" : "error");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(false);
    }
  });
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
  document.querySelectorAll("[data-block-type]").forEach((button) =>
    button.addEventListener("click", () => addExecutionBlock(button.dataset.blockType))
  );
  byId("execution-example").addEventListener("click", async () => {
    if (state.executionBlocks.length && !(await confirmAction({ title:"Вставить пример цепочки?", message:"Текущие блоки будут заменены готовым примером исполнения.", label:"Вставить пример" }))) return;
    state.executionBlocks = [];
    [
      ["rule_change", "Закрепить новый порядок", "Внести утверждённое изменение в действующие правила."],
      ["appointment", "Назначить ответственное направление", "Определить человека или отдел, отвечающий за исполнение."],
      ["communication", "Сообщить участникам", "Опубликовать понятную памятку о принятом решении."],
      ["review", "Проверить результат", "Через 30 дней оценить, достигнута ли цель законопроекта."],
    ].forEach(([type, title, description]) => addExecutionBlock(type, { title, description }));
    renderExecutionChain();
  });
  Object.values(fields).forEach((id) =>
    byId(id).addEventListener("input", () => {
      state.dirty = true;
      updateEditorVisuals();
    })
  );
  byId("editor-ai").addEventListener("click", () => void runAiEditor());
  byId("editor-publish").addEventListener("click", () => void publishDraft());
  byId("editor-cancel").addEventListener("click", () => void cancelDraft());
  byId("desktop-recommendation-dismiss")?.addEventListener("click", () => {
    localStorage.setItem("tmod-desktop-recommendation-v1", "dismissed");
    byId("desktop-recommendation").hidden = true;
  });
  document.querySelectorAll("[data-reactor-preparation-vote]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!state.preparation.sheet) return;
      state.preparation.sheet.preliminary_vote = button.dataset.reactorPreparationVote || null;
      renderPreparationVotes();
      markPreparationDirty();
    });
  });
  byId("reactor-preparation-reason").addEventListener("input", (event) => {
    if (!state.preparation.sheet) return;
    state.preparation.sheet.preliminary_vote_reason = event.currentTarget.value;
    markPreparationDirty();
  });
  byId("reactor-preparation-notes").addEventListener("input", (event) => {
    if (!state.preparation.sheet) return;
    state.preparation.sheet.notes = event.currentTarget.value;
    byId("reactor-preparation-notes-count").textContent = `${state.preparation.sheet.notes.length} / 16000`;
    markPreparationDirty();
  });
  [
    ["reactor-preparation-read-text", "read_text"],
    ["reactor-preparation-verify-sources", "verify_sources"],
    ["reactor-preparation-need-discussion", "need_discussion"],
  ].forEach(([id, key]) => {
    byId(id).addEventListener("change", (event) => {
      if (!state.preparation.sheet) return;
      state.preparation.sheet.review_flags[key] = Boolean(event.currentTarget.checked);
      markPreparationDirty();
    });
  });
  byId("reactor-preparation-add-question").addEventListener("click", () => {
    const sheet = state.preparation.sheet;
    if (!sheet) return;
    if (sheet.questions.length >= PREPARATION_MAX_QUESTIONS) {
      toast(`Можно добавить не более ${PREPARATION_MAX_QUESTIONS} вопросов.`, "error");
      return;
    }
    sheet.questions.push({ id: newPreparationQuestionId(), text: "", resolved: false });
    renderPreparationQuestions();
    byId("reactor-preparation-questions").querySelector("textarea:last-of-type")?.focus();
  });
  byId("reactor-preparation-save").addEventListener("click", () => void savePreparation());
  byId("reactor-preparation-download").addEventListener("click", () => void downloadPreparationPdf());
  byId("reactor-preparation-dialog").addEventListener("close", () => {
    if (state.preparation.dirty && !state.preparation.saving) {
      void savePreparation({ silent: true });
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !state.busy) {
      void load(true);
      void loadLegislation(true);
      void loadPreparation(true);
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
    if (state.dirty || state.preparation.dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  updateCounters();
  state.activeView = normalizeView(location.hash);
  activateView(state.activeView, false);
  void (async () => {
    await load();
    await loadLegislation(true);
    await loadPreparation(true);
  })();
})();
