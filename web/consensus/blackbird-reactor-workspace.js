"use strict";

// Blackbird has its own information architecture. Reuse the Reactor's live
// controls and forms, but move them into a purpose-built desktop workspace.
(() => {
  if (!/\bBLACKBIRD\//i.test(navigator.userAgent) &&
      !document.documentElement.classList.contains("blackbird-reactor")) return;

  const $ = (selector) => document.querySelector(selector);
  const create = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text) element.textContent = text;
    return element;
  };
  const frame = $(".portal-app-frame");
  const sidebar = $(".portal-sidebar");
  const main = frame?.querySelector("main");
  const nav = $(".portal-section-nav");
  const hero = $(".portal-hero");
  const overview = $(".portal-overview");
  const canvas = $("#portal-canvas");
  const viewHeader = $(".portal-view-header");
  if (!frame || !sidebar || !main || !nav || !hero || !overview || !canvas || !viewHeader) return;

  document.documentElement.classList.add("blackbird-reactor", "bb-workspace-ready");
  document.title = "Личный Реактор — Blackbird";
  frame.classList.add("bb-reactor-frame");
  main.classList.add("bb-reactor-main");
  sidebar.hidden = true;
  const webHeader = $(".portal-header");
  if (webHeader) webHeader.hidden = true;
  const heroDescription = hero.querySelector(".portal-hero-copy > p");
  if (heroDescription) {
    heroDescription.textContent = "Ваши решения, подготовка к консенсусу и события Сената — здесь.";
  }
  const bar = create("div", "bb-local-bar");
  const barName = create("div", "bb-local-name");
  barName.append(create("small", "", "СЕНАТ ТОВАРИЩЕСТВА"), create("strong", "", "Личный реактор"));
  const freshness = $("#portal-view-freshness");
  if (freshness) bar.append(barName, freshness);
  else bar.append(barName);
  nav.classList.add("bb-local-nav");
  nav.setAttribute("aria-label", "Разделы личного Реактора");
  const primary = create("div", "bb-primary-nav");
  const overflow = create("details", "bb-more-nav");
  overflow.append(create("summary", "", "Ещё"));
  const overflowItems = create("div", "bb-more-items");
  const navigation = [
    ["overview", "Обзор", primary],
    ["mandate", "Профиль", primary],
    ["projects", "Проекты", primary],
    ["consensus", "Консенсус", primary],
    ["notifications", "События", primary],
    ["editor", "Создать проект", overflowItems],
    ["treasury", "Казна", overflowItems],
    ["games", "Игры", overflowItems],
  ];
  navigation.forEach(([target, label, container]) => {
    const button = nav.querySelector(`[data-portal-target="${target}"]`);
    if (!button) return;
    const text = button.querySelector("b");
    if (text) text.textContent = label;
    button.setAttribute("aria-label", label);
    container.append(button);
  });
  [["portal-customize", "Настроить вид"], ["portal-connections", "Подключения"]].forEach(([id, label]) => {
    const button = document.getElementById(id);
    if (!button) return;
    button.textContent = label;
    button.setAttribute("aria-label", label);
    overflowItems.append(button);
  });
  overflow.append(overflowItems);
  nav.append(primary, overflow);
  overflowItems.addEventListener("click", (event) => {
    if (event.target.closest("[data-portal-target]")) overflow.open = false;
  });
  document.addEventListener("portal:view-activated", (event) => {
    document.documentElement.dataset.bbView = event.detail?.view || "overview";
    overflow.open = false;
  });
  main.prepend(bar, nav);
  viewHeader.classList.add("bb-view-header");
  nav.after(viewHeader);

  const dashboard = create("section", "bb-dashboard");
  dashboard.dataset.portalView = "overview";
  const actions = create("div", "bb-dashboard-actions");
  const treasuryLink = overview.querySelector('.overview-grid > [data-portal-target="treasury"]');
  const cards = create("div", "bb-action-grid");
  ["projects", "consensus", "mandate", "notifications"].forEach((target) => {
    const button = overview.querySelector(`.overview-grid > [data-portal-target="${target}"]`);
    if (button) cards.append(button);
  });
  const notificationCaption = cards.querySelector('[data-portal-target="notifications"] > span');
  if (notificationCaption) notificationCaption.textContent = "Личные события Сената";
  const dashboardHead = create("div", "bb-dashboard-heading");
  dashboardHead.append(create("small", "", "РАБОЧИЕ НАПРАВЛЕНИЯ"), create("h2", "", "Что требует внимания"));
  if (treasuryLink) {
    treasuryLink.classList.add("bb-treasury-link");
    actions.append(treasuryLink);
  }
  dashboard.append(hero, dashboardHead, cards, actions);
  overview.remove();
  viewHeader.after(dashboard);

  const workspace = create("div", "bb-section-stack");
  const sections = [
    ["mandate", "01 / ЛИЧНОСТЬ", "Ваши полномочия, персонаж и место в Товариществе."],
    ["projects", "02 / ИНИЦИАТИВЫ", "Личные проекты и реестр принятых решений."],
    ["editor", "03 / МАСТЕРСКАЯ", "От идеи до готового текста — в одном рабочем процессе."],
    ["treasury", "04 / РЕСУРСЫ", "Баланс, движение средств и прозрачная история."],
    ["consensus", "05 / ЗАСЕДАНИЯ", "Текущий эфир и ваши листы подготовки."],
    ["games", "06 / ДОСУГ", "Шахматы и нарды вашего контура."],
    ["notifications", "07 / СОБЫТИЯ", "Личные события, требующие вашего внимания."],
  ];
  sections.forEach(([view, index, description]) => {
    const panel = create("section", "bb-section");
    panel.dataset.portalView = view;
    const sectionHead = create("header", "bb-section-heading");
    const copy = create("div", "");
    copy.append(create("small", "", index), create("p", "", description));
    const back = create("button", "bb-back-home", "←  К обзору");
    back.type = "button";
    back.dataset.portalTarget = "overview";
    sectionHead.append(copy, back);
    panel.append(sectionHead);
    canvas.querySelectorAll(`[data-portal-view="${view}"]`).forEach((widget) => panel.append(widget));
    workspace.append(panel);
  });
  const projectsPanel = workspace.querySelector('.bb-section[data-portal-view="projects"]');
  if (projectsPanel) {
    const tabs = create("div", "bb-project-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Законодательные пространства");
    [["my_bills", "Мои проекты"], ["legislation", "Реестр решений"]].forEach(([key, label], index) => {
      const button = create("button", index === 0 ? "active" : "", label);
      button.type = "button";
      button.dataset.bbProjectTab = key;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", index === 0 ? "true" : "false");
      tabs.append(button);
    });
    projectsPanel.querySelector(".bb-section-heading")?.after(tabs);
    let selectedProjectTab = "my_bills";
    const showProjectTab = (requested) => {
      const available = ["my_bills", "legislation"].filter((key) =>
        projectsPanel.querySelector(`[data-portal-widget="${key}"]`)?.dataset.layoutEnabled !== "false");
      selectedProjectTab = available.includes(requested) ? requested : available[0] || requested;
      tabs.querySelectorAll("button").forEach((button) => {
        const active = button.dataset.bbProjectTab === selectedProjectTab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", String(active));
        button.hidden = !available.includes(button.dataset.bbProjectTab);
      });
      projectsPanel.querySelectorAll("[data-portal-widget]").forEach((widget) => {
        widget.hidden = widget.dataset.portalWidget !== selectedProjectTab ||
          widget.dataset.layoutEnabled === "false" || projectsPanel.hidden;
      });
    };
    tabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-bb-project-tab]");
      if (button) showProjectTab(button.dataset.bbProjectTab);
    });
    document.addEventListener("portal:view-activated", (event) => {
      if (event.detail?.view === "projects") showProjectTab(selectedProjectTab);
    });
    hero.querySelector('.portal-hero-actions [data-portal-target="projects"]')?.addEventListener("click", () =>
      showProjectTab("legislation"));
    showProjectTab(selectedProjectTab);
  }
  const mandate = workspace.querySelector(".identity-widget");
  if (mandate) {
    const grid = create("div", "bb-mandate-grid");
    const primaryDetails = create("div", "bb-mandate-primary");
    const otherDetails = create("div", "bb-mandate-details");
    [".identity-main", ".identity-positions", ".mandate-biography"].forEach((selector) => {
      const node = mandate.querySelector(selector);
      if (node) primaryDetails.append(node);
    });
    [".mandate-facts", ".mandate-copy", ".identity-note"].forEach((selector) => {
      const node = mandate.querySelector(selector);
      if (node) otherDetails.append(node);
    });
    grid.append(primaryDetails, otherDetails);
    mandate.querySelector(":scope > header")?.after(grid);
  }
  const editor = workspace.querySelector(".editor-widget");
  if (editor) {
    const title = editor.querySelector(".editor-titlebar h2");
    const description = editor.querySelector(".editor-titlebar p");
    const emptyDescription = editor.querySelector("#editor-empty p");
    if (title) title.textContent = "Личный черновик";
    if (description) description.textContent = "Сформулируйте идею, подготовьте текст и проверьте проект перед подачей.";
    if (emptyDescription) emptyDescription.textContent = "Создайте черновик — он будет сохранён в вашем аккаунте.";
  }
  canvas.append(workspace);
  dashboard.after(canvas);
})();
