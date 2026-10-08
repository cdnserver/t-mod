import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installSmoothScroll, interactionCss } from "../src/shared/interaction-surface";

class Viewport {
  dataset: Record<string, string> = {};
  scrollTop = 0; scrollLeft = 0;
  scrollWidth = 100; clientWidth = 100; scrollHeight = 1200; clientHeight = 300;
  isConnected = true;
  native = false;
  style = { overflowX: "auto", overflowY: "auto", lineHeight: "20px", overscrollBehaviorY: "auto", overscrollBehaviorX: "auto" };
  matches() { return this.native; }
  scrollTo(value: { top: number; left: number }) { this.scrollTop = value.top; this.scrollLeft = value.left; }
}
let listeners: Record<string, (event: any) => void>;
let frames: Map<number, (time: number) => void>;
let root: Viewport;
let now: number;
function advance(count = 1) {
  for (let i = 0; i < count; i++) {
    now += 16;
    const pending = [...frames.values()]; frames.clear();
    pending.forEach(fn => fn(now));
  }
}
function wheel(path: Viewport[], overrides: Record<string, unknown> = {}) {
  const event = { defaultPrevented: false, cancelable: true, deltaX: 0, deltaY: 100, deltaMode: 0,
    composedPath: () => path, preventDefault: vi.fn(), ...overrides };
  listeners.wheel(event);
  return event;
}
beforeEach(() => {
  listeners = {}; frames = new Map(); now = 0; root = new Viewport(); let id = 0;
  vi.stubGlobal("HTMLElement", Viewport);
  vi.stubGlobal("document", { documentElement: root, scrollingElement: root, addEventListener: (name: string, fn: any) => { listeners[name] = fn; } });
  vi.stubGlobal("window", { addEventListener: (name: string, fn: any) => { listeners[name] = fn; } });
  vi.stubGlobal("getComputedStyle", (element: Viewport) => element.style);
  vi.stubGlobal("performance", { now: () => now });
  vi.stubGlobal("requestAnimationFrame", (fn: (time: number) => void) => { frames.set(++id, fn); return id; });
  vi.stubGlobal("cancelAnimationFrame", (key: number) => frames.delete(key));
  installSmoothScroll();
});
afterEach(() => vi.unstubAllGlobals());
describe("Blackbird interaction surface", () => {
  it("eases monotonically to the exact target without a spring or overshoot", () => {
    expect(wheel([root]).preventDefault).toHaveBeenCalledOnce();
    advance(); expect(root.scrollTop).toBeGreaterThan(0); expect(root.scrollTop).toBeLessThan(100);
    let previous = root.scrollTop;
    for (let i = 0; i < 60; i++) { advance(); expect(root.scrollTop).toBeGreaterThanOrEqual(previous); expect(root.scrollTop).toBeLessThanOrEqual(100); previous = root.scrollTop; }
    expect(root.scrollTop).toBe(100); expect(frames.size).toBe(0);
  });
  it("accumulates repeated wheel steps and reverses cleanly", () => {
    wheel([root]); advance(2); wheel([root]); advance(60); expect(root.scrollTop).toBe(200);
    wheel([root], { deltaY: -100 }); advance(60); expect(root.scrollTop).toBe(100);
  });
  it("keeps nested chat scrolling inside its own viewport", () => {
    const child = new Viewport(); wheel([child, root]); advance(60);
    expect(child.scrollTop).toBe(100); expect(root.scrollTop).toBe(0);
    child.scrollTop = 900; child.style.overscrollBehaviorY = "contain";
    expect(wheel([child, root]).preventDefault).not.toHaveBeenCalled(); advance(60); expect(root.scrollTop).toBe(0);
    child.style.overscrollBehaviorY = "auto"; wheel([child, root]); advance(60); expect(root.scrollTop).toBe(100);
  });
  it("preserves trackpad inertia, zoom, range controls and handled events", () => {
    for (const overrides of [{ deltaY: 9 }, { deltaY: 100.5 }, { ctrlKey: true }, { metaKey: true }, { defaultPrevented: true }, { cancelable: false }]) {
      expect(wheel([root], overrides).preventDefault).not.toHaveBeenCalled();
    }
    const slider = new Viewport(); slider.native = true;
    expect(wheel([slider, root]).preventDefault).not.toHaveBeenCalled(); expect(frames.size).toBe(0);
  });
  it("supports line-mode wheels and horizontal shift-wheel", () => {
    wheel([root], { deltaMode: 1, deltaY: 3 }); advance(60); expect(root.scrollTop).toBe(60);
    root.scrollWidth = 1000;
    wheel([root], { shiftKey: true }); advance(60); expect(root.scrollLeft).toBe(100); expect(root.scrollTop).toBe(60);
  });
  it("cancels on direct input, loss of focus and external chat auto-follow", () => {
    for (const event of ["pointerdown", "keydown", "blur", "visibilitychange"]) {
      wheel([root]); listeners[event]({}); advance(60); expect(root.scrollTop).toBe(0);
    }
    wheel([root]); root.scrollTop = 500; advance(60); expect(root.scrollTop).toBe(500); expect(frames.size).toBe(0);
  });
  it("clamps after content changes and installs only once", () => {
    wheel([root]); root.scrollHeight = 320; advance(60); expect(root.scrollTop).toBe(20);
    const original = listeners.wheel; installSmoothScroll(); expect(listeners.wheel).toBe(original);
  });
  it("uses real SVG cursors with semantic pointer/text and native opt-outs", () => {
    expect(interactionCss).toContain("data:image/svg+xml,");
    expect(interactionCss).toContain("contenteditable=true");
    expect(interactionCss).toContain("data-native-cursor");
    expect(interactionCss).not.toContain("cursor: none");
  });
});
