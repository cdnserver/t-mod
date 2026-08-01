"use strict";

(() => {
  const baseTitle = document.title;
  const favicon = document.querySelector("link[rel~='icon']") || document.createElement("link");
  const reducedMotion = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  const state = {
    count: 0,
    blink: false,
    urgent: false,
    label: "Новое событие",
    transient: false,
    transientUrgent: false,
    transientLabel: "Состояние обновилось",
    timer: null,
    alternate: false,
    signature: "",
  };

  favicon.rel = "icon";
  favicon.type = "image/svg+xml";
  favicon.dataset.tmodFavicon = "true";
  if (!favicon.parentNode) document.head.append(favicon);

  function iconSvg(count, urgent) {
    const badge = Math.max(0, Math.min(99, Number(count) || 0));
    const badgeLabel = badge > 9 ? "9+" : String(badge || "!");
    const badgeColor = urgent ? "#ff6079" : "#ffd06b";
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
      <defs><linearGradient id="b" x1="8" y1="5" x2="57" y2="61"><stop stop-color="#17251f"/><stop offset="1" stop-color="#070b0a"/></linearGradient></defs>
      <rect x="3" y="3" width="58" height="58" rx="17" fill="url(#b)" stroke="#375348" stroke-width="2"/>
      <ellipse cx="29" cy="34" rx="20" ry="8" fill="none" stroke="#9cf2bd" stroke-width="1.7" transform="rotate(-28 29 34)"/>
      <path d="M19 22h20v6h-7v20h-6V28h-7z" fill="#a8f5c5"/>
      <circle cx="50" cy="15" r="13" fill="#080c0a" stroke="${badgeColor}" stroke-width="2"/>
      <circle cx="50" cy="15" r="10" fill="${badgeColor}"/>
      <text x="50" y="19" fill="#12100b" font-family="Arial,sans-serif" font-size="11" font-weight="900" text-anchor="middle">${badgeLabel}</text>
    </svg>`;
  }

  function stopTimer() {
    clearInterval(state.timer);
    state.timer = null;
    state.alternate = false;
  }

  function alertTitle() {
    const count = Math.max(state.count, state.transient ? 1 : 0);
    const label = state.transient ? state.transientLabel : state.label;
    return `${state.urgent || state.transientUrgent ? "🔴" : "●"} ${count > 1 ? `${count} · ` : ""}${label}`;
  }

  function render() {
    const active = state.count > 0 || state.transient;
    const shouldBlink = (state.blink && state.count > 0) || state.transient;
    favicon.href = active
      ? `data:image/svg+xml,${encodeURIComponent(iconSvg(Math.max(state.count, state.transient ? 1 : 0), state.urgent || state.transientUrgent))}`
      : "/assets/favicon.svg";
    stopTimer();
    if (!shouldBlink || !document.hidden) {
      document.title = baseTitle;
      return;
    }
    document.title = alertTitle();
    if (reducedMotion) return;
    state.timer = setInterval(() => {
      state.alternate = !state.alternate;
      document.title = state.alternate ? baseTitle : alertTitle();
    }, 1100);
  }

  function set(count, options = {}) {
    const nextCount = Math.max(0, Number(count) || 0);
    const nextUrgent = Boolean(options.urgent);
    const nextBlink = options.blink === undefined ? nextCount > 0 : Boolean(options.blink);
    const nextLabel = String(options.label || "Новое событие").trim().slice(0, 80) || "Новое событие";
    const signature = `${nextCount}:${nextBlink}:${nextUrgent}:${nextLabel}`;
    if (signature === state.signature) return;
    state.count = nextCount;
    state.blink = nextBlink;
    state.urgent = nextUrgent;
    state.label = nextLabel;
    state.signature = signature;
    render();
  }

  function pulse(label = "Состояние обновилось", options = {}) {
    state.transient = true;
    state.transientUrgent = Boolean(options.urgent);
    state.transientLabel = String(label || "Состояние обновилось").trim().slice(0, 80);
    render();
  }

  function clear() {
    state.count = 0;
    state.blink = false;
    state.urgent = false;
    state.transient = false;
    state.transientUrgent = false;
    state.signature = "";
    render();
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      state.transient = false;
      state.transientUrgent = false;
    }
    render();
  });
  window.addEventListener("pagehide", stopTimer);

  globalThis.TModTabSignal = Object.freeze({ set, pulse, clear });
  render();
})();
