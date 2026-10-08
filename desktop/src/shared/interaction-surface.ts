/** Standalone: also serialized into trusted service renderers in an isolated world. */
export function installSmoothScroll(): void {
  const root = document.documentElement;
  if (root.dataset.blackbirdScroll === "1") return;
  root.dataset.blackbirdScroll = "1";
  type Motion = { element: HTMLElement; x: number; y: number; lastX: number; lastY: number; time: number };
  let motion: Motion | undefined;
  let frame = 0;
  const stop = () => { cancelAnimationFrame(frame); frame = 0; motion = undefined; };
  const tick = (time: number) => {
    const m = motion;
    if (!m || !m.element.isConnected) { stop(); return; }
    // Respect a reader, chat auto-follow or any code that moves the same viewport.
    if (Math.abs(m.element.scrollTop - m.lastY) > 2 || Math.abs(m.element.scrollLeft - m.lastX) > 2) { stop(); return; }
    m.x = Math.max(0, Math.min(m.x, m.element.scrollWidth - m.element.clientWidth));
    m.y = Math.max(0, Math.min(m.y, m.element.scrollHeight - m.element.clientHeight));
    const alpha = 1 - Math.exp(-Math.min(48, Math.max(1, time - m.time)) / 85);
    m.time = time;
    const x = m.element.scrollLeft + (m.x - m.element.scrollLeft) * alpha;
    const y = m.element.scrollTop + (m.y - m.element.scrollTop) * alpha;
    const done = Math.abs(m.x - x) < 0.65 && Math.abs(m.y - y) < 0.65;
    // "instant" prevents CSS scroll-behavior starting a second animation per frame.
    m.element.scrollTo({ left: done ? m.x : x, top: done ? m.y : y, behavior: "instant" });
    m.lastX = m.element.scrollLeft; m.lastY = m.element.scrollTop;
    if (done) { stop(); return; }
    frame = requestAnimationFrame(tick);
  };
  document.addEventListener("wheel", event => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    // Precision trackpads already supply an inertial stream. Do not double-ease it.
    if (event.deltaMode === 0 && (Math.max(Math.abs(event.deltaX), Math.abs(event.deltaY)) < 40
      || !Number.isInteger(event.deltaX) || !Number.isInteger(event.deltaY))) { stop(); return; }
    let dx = event.deltaX, dy = event.deltaY;
    if (event.shiftKey && !dx) { dx = dy; dy = 0; }
    if (!dx && !dy) return;
    for (const node of event.composedPath()) {
      if (!(node instanceof HTMLElement)) continue;
      if (node.matches("select,input[type=range],[data-native-scroll]")) { stop(); return; }
      const style = getComputedStyle(node);
      const isRoot = node === document.scrollingElement;
      const unit = event.deltaMode === 1 ? Math.max(16, parseFloat(style.lineHeight) || 20)
        : event.deltaMode === 2 ? node.clientHeight : 1;
      const x = dx * unit, y = dy * unit;
      const mx = node.scrollWidth - node.clientWidth, my = node.scrollHeight - node.clientHeight;
      const canX = x && mx > 0 && (isRoot || /auto|scroll/.test(style.overflowX));
      const canY = y && my > 0 && (isRoot || /auto|scroll/.test(style.overflowY));
      const current = motion?.element === node ? motion : undefined;
      const baseX = current?.x ?? node.scrollLeft, baseY = current?.y ?? node.scrollTop;
      const targetX = canX ? Math.max(0, Math.min(mx, baseX + Math.max(-node.clientWidth, Math.min(node.clientWidth, x)))) : node.scrollLeft;
      const targetY = canY ? Math.max(0, Math.min(my, baseY + Math.max(-node.clientHeight, Math.min(node.clientHeight, y)))) : node.scrollTop;
      if (targetX !== baseX || targetY !== baseY) {
        if (!event.cancelable) return;
        event.preventDefault();
        if (!current) stop();
        motion = { element: node, x: targetX, y: targetY, lastX: node.scrollLeft, lastY: node.scrollTop, time: current?.time ?? performance.now() };
        if (!frame) frame = requestAnimationFrame(tick);
        return;
      }
      if ((canY && /contain|none/.test(style.overscrollBehaviorY)) || (canX && /contain|none/.test(style.overscrollBehaviorX))) return;
    }
  }, { passive: false });
  document.addEventListener("pointerdown", stop, { passive: true });
  document.addEventListener("keydown", stop);
  document.addEventListener("visibilitychange", stop);
  window.addEventListener("blur", stop);
}

const cursor = (shape: string, x: number, y: number, fallback: string) => {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><defs><filter id="g" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="1.7"/></filter></defs><g fill="none" stroke="#72d7eb" stroke-width="2.2" opacity=".65" filter="url(#g)">${shape}</g><g fill="#10191e" stroke="#dfedf1" stroke-width="1.25" stroke-linejoin="round" stroke-linecap="round">${shape}</g></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}") ${x} ${y}, ${fallback}`;
};

/** Native OS cursor: no lagging DOM follower, no hidden pointer or click interception. */
export const interactionCss = `
html { scroll-behavior: smooth; }
html, body { cursor: ${cursor('<path d="M6 4L6 23L11 18L15 26L19 24L15 16L22 16Z"/>', 6, 4, "auto")} !important; }
a[href],button,summary,[role=button],[role=link],label[for],input[type=checkbox],input[type=radio],select { cursor: ${cursor('<path d="M12 16V7a2 2 0 014 0v7-2a2 2 0 014 0v3-1a2 2 0 014 0v7c0 5-3 7-7 7h-2c-3 0-4-2-6-5l-3-4a2 2 0 013-3l3 2"/>', 14, 5, "pointer")} !important; }
input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]),textarea,[contenteditable=true] { cursor: ${cursor('<path d="M12 5h8M16 5v22M12 27h8"/>', 16, 16, "text")} !important; }
button:disabled,[aria-disabled=true] { cursor: not-allowed !important; }
[data-native-cursor] { cursor: auto !important; }
`;

export function installInteractionSurface(): void {
  if (!document.getElementById("blackbird-interaction-surface")) {
    const style = document.createElement("style");
    style.id = "blackbird-interaction-surface";
    style.textContent = interactionCss;
    document.head.append(style);
  }
  installSmoothScroll();
}
