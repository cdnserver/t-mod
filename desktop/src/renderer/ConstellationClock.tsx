import { useEffect, useRef, type RefObject } from "react";

const WIDTH = 560;
const HEIGHT = 158;
const SLOT_X = [9, 118, 241, 303, 412];
const TOP = 25;

type Point = readonly [number, number];
type Glyph = readonly (readonly Point[])[];

// A few connected, independently moving stars form each numeral.
// They are constellations, not pixels sampled from a font outline.
const DIGITS: Record<string, Glyph> = {
  "0": [[[36, 0], [61, 9], [72, 31], [72, 75], [60, 99], [36, 108], [12, 99], [0, 75], [0, 32], [12, 9], [36, 0]]],
  "1": [[[8, 24], [30, 7], [39, 0], [39, 35], [39, 70], [39, 108]], [[10, 108], [39, 108], [68, 108]]],
  "2": [[[1, 25], [13, 7], [37, 0], [62, 9], [72, 29], [63, 48], [41, 67], [21, 85], [0, 108], [36, 108], [73, 108]]],
  "3": [[[1, 12], [34, 0], [62, 9], [71, 30], [58, 50], [36, 54], [61, 63], [73, 82], [61, 101], [35, 109], [2, 97]]],
  "4": [[[58, 0], [57, 28], [57, 57], [57, 82], [57, 108]], [[58, 0], [37, 26], [17, 51], [0, 76], [31, 76], [57, 76], [73, 76]]],
  "5": [[[70, 0], [37, 0], [5, 0], [3, 28], [0, 52], [31, 49], [58, 59], [72, 80], [59, 102], [34, 109], [3, 98]]],
  "6": [[[63, 1], [39, 14], [17, 36], [2, 65], [12, 93], [36, 108], [60, 99], [72, 78], [58, 56], [36, 53], [11, 65]]],
  "7": [[[0, 0], [38, 0], [73, 0], [58, 25], [43, 52], [30, 80], [19, 108]]],
  "8": [[[35, 0], [61, 10], [69, 30], [57, 48], [36, 54], [13, 47], [3, 29], [12, 9], [35, 0]], [[36, 54], [62, 65], [72, 85], [59, 102], [35, 109], [11, 101], [0, 84], [13, 65], [36, 54]]],
  "9": [[[62, 46], [39, 55], [13, 44], [1, 21], [15, 4], [40, 0], [64, 15], [72, 43], [69, 75], [48, 98], [14, 108]]],
};

type Star = {
  x: number; y: number; fromX: number; fromY: number; toX: number; toY: number;
  alpha: number; fromAlpha: number; toAlpha: number; phase: number; radius: number;
  source: Point; delay: number; newArrival: boolean;
};

type ReturningStar = { star: Star; key: string; delay: number };

function hash(value: string): number {
  let result = 2166136261;
  for (const char of value) result = Math.imul(result ^ char.charCodeAt(0), 16777619);
  return (result >>> 0) / 4294967295;
}

function skyProjection(image: HTMLImageElement): (source: Point) => Point {
  const rect = image.getBoundingClientRect();
  const scale = Math.max(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
  return (source: Point): Point => [
    rect.left + (rect.width - image.naturalWidth * scale) / 2 + source[0] * scale,
    rect.top + (rect.height - image.naturalHeight * scale) / 2 + source[1] * scale,
  ];
}

/** Locate genuine bright stars in the visible background photograph. */
function collectSkyStars(image: HTMLImageElement): Point[] {
  const surface = document.createElement("canvas");
  surface.width = image.naturalWidth;
  surface.height = image.naturalHeight;
  const context = surface.getContext("2d", { willReadFrequently: true });
  if (!context) return [];
  context.drawImage(image, 0, 0);
  const { data } = context.getImageData(0, 0, surface.width, surface.height);
  const rect = image.getBoundingClientRect();
  const scale = Math.max(rect.width / surface.width, rect.height / surface.height);
  const left = rect.left + (rect.width - surface.width * scale) / 2;
  const top = rect.top + (rect.height - surface.height * scale) / 2;
  const candidates: { point: Point; brightness: number; screen: Point }[] = [];
  for (let y = 8; y < surface.height - 8; y += 3) {
    for (let x = 8; x < surface.width - 8; x += 3) {
      const index = (y * surface.width + x) * 4;
      const brightness = data[index] * .23 + data[index + 1] * .43 + data[index + 2] * .34;
      if (brightness < 82) continue;
      const screen: Point = [left + x * scale, top + y * scale];
      if (screen[0] < 35 || screen[0] > window.innerWidth * .74 || screen[1] < 30 || screen[1] > window.innerHeight * .49) continue;
      if (screen[0] > window.innerWidth * .57 && screen[1] > window.innerHeight * .11) continue;
      if (screen[0] < window.innerWidth * .55 && screen[1] > window.innerHeight * .32) continue;
      let maximum = true;
      for (let dy = -3; dy <= 3 && maximum; dy += 3) for (let dx = -3; dx <= 3; dx += 3) {
        if (dx === 0 && dy === 0) continue;
        const neighbor = ((y + dy) * surface.width + x + dx) * 4;
        const value = data[neighbor] * .23 + data[neighbor + 1] * .43 + data[neighbor + 2] * .34;
        if (value > brightness) { maximum = false; break; }
      }
      if (maximum) candidates.push({ point: [x, y], screen, brightness });
    }
  }
  candidates.sort((a, b) => b.brightness - a.brightness);
  const selected: typeof candidates = [];
  for (const candidate of candidates) {
    if (selected.every(other => Math.hypot(candidate.screen[0] - other.screen[0], candidate.screen[1] - other.screen[1]) > 17)) selected.push(candidate);
    if (selected.length === 110) break;
  }
  return selected.map(candidate => candidate.point);
}

function ease(value: number): number {
  const clamped = Math.max(0, Math.min(1, value));
  return clamped * clamped * (3 - 2 * clamped);
}

function flightPoint(from: Point, to: Point, progress: number, arc: number): Point {
  const t = Math.max(0, Math.min(1, progress));
  const controlX = (from[0] + to[0]) / 2 + arc;
  const controlY = (from[1] + to[1]) / 2 - Math.abs(arc) * .18;
  return [
    (1 - t) ** 2 * from[0] + 2 * (1 - t) * t * controlX + t ** 2 * to[0],
    (1 - t) ** 2 * from[1] + 2 * (1 - t) * t * controlY + t ** 2 * to[1],
  ];
}

function constellation(time: string): { targets: Map<string, Point>; links: [string, string][] } {
  const targets = new Map<string, Point>();
  const links: [string, string][] = [];
  [...time].forEach((character, slot) => {
    if (character === ":") {
      targets.set(`${slot}:0`, [SLOT_X[slot], TOP + 37]);
      targets.set(`${slot}:1`, [SLOT_X[slot], TOP + 80]);
      return;
    }
    let index = 0;
    for (const path of DIGITS[character] ?? []) {
      let previous = "";
      for (const [x, y] of path) {
        const key = `${slot}:${index++}`;
        // Imperfect coordinates keep the constellations from reading as a CAD font.
        targets.set(key, [SLOT_X[slot] + x + (hash(`x:${key}`) - .5) * 3.4, TOP + y + (hash(`y:${key}`) - .5) * 3.4]);
        if (previous) links.push([previous, key]);
        previous = key;
      }
    }
  });
  return { targets, links };
}

/** The clock assembles from real points sampled from the image behind it. */
export function ConstellationClock({ time, reduced = false, skyImage, starflight }: {
  time: string; reduced?: boolean;
  skyImage: RefObject<HTMLImageElement | null>;
  starflight: RefObject<HTMLCanvasElement | null>;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const currentTime = useRef(time);
  const motionReduced = useRef(reduced);
  currentTime.current = time;
  motionReduced.current = reduced;

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext("2d", { alpha: true });
    if (!element || !context) return;
    const flightElement = starflight.current;
    const flightContext = flightElement?.getContext("2d", { alpha: true });
    const image = skyImage.current;
    if (!flightElement || !flightContext || !image) return;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    const resize = () => {
      element.width = WIDTH * pixelRatio;
      element.height = HEIGHT * pixelRatio;
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      flightElement.width = Math.round(window.innerWidth * pixelRatio);
      flightElement.height = Math.round(window.innerHeight * pixelRatio);
      flightContext.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    };
    resize();
    let stars = new Map<string, Star>();
    let links: [string, string][] = [];
    let returning: ReturningStar[] = [];
    let returningLinks: [string, string][] = [];
    let skyStars: Point[] = [];
    let skyReady = false;
    let shownTime = "";
    let firstAssembly = true;
    let transitionAt = 0;
    let animationFrame = 0;
    let timeout = 0;
    let lastDraw = 0;
    let visitorSource: Point | null = null;
    let visitorKey = "";
    let visitorAt = 0;

    const loadSky = () => {
      try { skyStars = image.naturalWidth ? collectSkyStars(image) : []; }
      catch { skyStars = []; }
      skyReady = skyStars.length > 0;
      // If the image cannot be read, show a static clock rather than fake sky flights.
      if (!skyReady) skyStars = [[0, 0]];
    };
    if (image.complete) loadSky();
    else image.addEventListener("load", loadSky);
    const imageError = () => { skyStars = [[0, 0]]; skyReady = false; };
    image.addEventListener("error", imageError);

    const retarget = (next: string, now: number) => {
      const { targets, links: nextLinks } = constellation(next);
      const previous = stars;
      const changed = new Set<number>();
      for (let slot = 0; slot < next.length; slot++) if (next[slot] !== shownTime[slot]) changed.add(slot);
      firstAssembly = !shownTime;
      returning = firstAssembly || motionReduced.current ? [] : [...previous]
        .filter(([key, star]) => changed.has(Number(key.charAt(0))) && star.toAlpha > .5)
        .map(([key, star]) => ({ key, star: { ...star }, delay: Number(key.split(":")[1]) * 55 }));
      returningLinks = returning.length ? links.filter(([a, b]) => changed.has(Number(a.charAt(0))) && changed.has(Number(b.charAt(0)))) : [];
      stars = new Map();
      let sequence = 0;
      const changedIndex = new Map<number, number>();
      for (const [key, [x, y]] of targets) {
        const old = previous.get(key);
        const slot = Number(key.charAt(0));
        if (!changed.has(slot) && old) {
          stars.set(key, { ...old, x, y, fromX: x, fromY: y, toX: x, toY: y, alpha: 1, fromAlpha: 1, toAlpha: 1, newArrival: false });
          sequence++;
          continue;
        }
        const phase = hash(key) * Math.PI * 2;
        const localIndex = changedIndex.get(slot) ?? 0;
        changedIndex.set(slot, localIndex + 1);
        const returningInSlot = returning.filter(item => Number(item.key.charAt(0)) === slot);
        const source = returningInSlot[localIndex]?.star.source ?? skyStars[(sequence * 13 + Math.floor(sequence / 7) * 5) % skyStars.length];
        const delay = motionReduced.current || !skyReady ? 0 : firstAssembly
          ? Math.floor(sequence / 10) * 390 + (sequence % 10) * 105
          : 2050 + localIndex * 125;
        stars.set(key, {
          x, y, fromX: x, fromY: y, toX: x, toY: y,
          alpha: 0, fromAlpha: 0, toAlpha: 1,
          phase, radius: hash(`major:${key}`) > .64 ? 2.2 : 1.35,
          source, delay, newArrival: skyReady,
        });
        sequence++;
      }
      links = nextLinks;
      shownTime = next;
      transitionAt = now;
      visitorSource = null;
      visitorKey = "";
      visitorAt = now;
    };

    const frame = (now: number) => {
      if (!motionReduced.current && !document.hidden && now - lastDraw < 1000 / 30) {
        animationFrame = window.requestAnimationFrame(frame);
        return;
      }
      lastDraw = now;
      if (!skyStars.length && image.complete) loadSky();
      if (!skyStars.length) {
        animationFrame = window.requestAnimationFrame(frame);
        return;
      }
      if (shownTime !== currentTime.current) retarget(currentTime.current, now);
      context.clearRect(0, 0, WIDTH, HEIGHT);
      flightContext.clearRect(0, 0, window.innerWidth, window.innerHeight);
      const duration = motionReduced.current ? 1 : firstAssembly ? 1900 : 1600;
      const fraction = Math.min(1, (now - transitionAt) / duration);
      const easing = ease(fraction);
      const drift = motionReduced.current ? 0 : 1.2;
      const elapsed = Math.max(0, now - transitionAt);
      const clockRect = element.getBoundingClientRect();
      const clockPosition = (point: Point): Point => [
        clockRect.left + point[0] / WIDTH * clockRect.width,
        clockRect.top + point[1] / HEIGHT * clockRect.height,
      ];
      const sourcePosition = skyReady ? skyProjection(image) : (_: Point): Point => [0, 0];
      // No elastic morph: the old figure holds, then its lights fade independently.
      // Their corresponding sky points become visible again as the mask dissolves.
      const retreat = new Map<string, { position: Point; alpha: number }>();
      for (const { key, star, delay } of returning) {
        const progress = Math.max(0, Math.min(1, (elapsed - 650 - delay) / 950));
        if (progress >= 1) continue;
        const position: Point = [star.x, star.y];
        const alpha = star.alpha * (1 - ease(progress));
        retreat.set(key, { position, alpha });
        if (!skyReady) continue;
        const source = sourcePosition(star.source);
        const mask = flightContext.createRadialGradient(source[0], source[1], 0, source[0], source[1], 9);
        mask.addColorStop(0, `rgba(3,6,10,${.88 * (1 - ease(progress))})`);
        mask.addColorStop(1, "rgba(3,6,10,0)");
        flightContext.fillStyle = mask;
        flightContext.beginPath(); flightContext.arc(source[0], source[1], 9, 0, Math.PI * 2); flightContext.fill();
      }
      context.strokeStyle = "#b6d9f7";
      context.lineWidth = .65;
      for (const [first, second] of returningLinks) {
        const a = retreat.get(first);
        const b = retreat.get(second);
        if (!a || !b) continue;
        context.globalAlpha = Math.min(a.alpha, b.alpha) * .32;
        context.beginPath(); context.moveTo(...a.position); context.lineTo(...b.position); context.stroke();
      }
      for (const { position: [x, y], alpha } of retreat.values()) {
        if (alpha < .01) continue;
        context.globalAlpha = alpha;
        context.fillStyle = "#dcefff";
        context.beginPath(); context.arc(x, y, 1.45, 0, Math.PI * 2); context.fill();
      }
      context.globalAlpha = 1;
      if (!motionReduced.current && skyReady && elapsed > (firstAssembly ? 4800 : 7000) && now - visitorAt > 6700) {
        visitorAt = now;
        const used = new Set([...stars.values()].map(star => star.source.join(",")));
        const available = skyStars.filter(point => !used.has(point.join(",")));
        visitorSource = available[Math.floor(now / 6700) % available.length] ?? null;
        const live = [...stars].filter(([, star]) => star.toAlpha > .5);
        visitorKey = live[Math.floor(now / 6700) % live.length]?.[0] ?? "";
      }
      // Each vacant point in the image is dimmed while its star lives in the clock.
      for (const star of stars.values()) {
        if (star.toAlpha === 0 || !skyReady) continue;
        const source = sourcePosition(star.source);
        const arrival = !star.newArrival || motionReduced.current ? 1 : Math.max(0, Math.min(1, (elapsed - star.delay) / duration));
        if (arrival > .03) {
          const mask = flightContext.createRadialGradient(source[0], source[1], 0, source[0], source[1], 9);
          mask.addColorStop(0, `rgba(3,6,10,${.88 * ease(arrival * 2)})`);
          mask.addColorStop(1, "rgba(3,6,10,0)");
          flightContext.fillStyle = mask;
          flightContext.beginPath();
          flightContext.arc(source[0], source[1], 9, 0, Math.PI * 2);
          flightContext.fill();
        }
        if (!star.newArrival || arrival <= 0 || arrival >= 1 || motionReduced.current) continue;
        const target = clockPosition([star.toX, star.toY]);
        const arc = (hash(`arc:${star.phase}`) - .5) * 30;
        const [x, y] = flightPoint(source, target, arrival, arc);
        const fade = Math.min(1, arrival * 7, (1 - arrival) * 5);
        flightContext.globalAlpha = fade * .52;
        const halo = flightContext.createRadialGradient(x, y, 0, x, y, 6);
        halo.addColorStop(0, "rgba(237,248,255,.95)");
        halo.addColorStop(.3, "rgba(170,212,255,.46)");
        halo.addColorStop(1, "rgba(170,212,255,0)");
        flightContext.fillStyle = halo;
        flightContext.beginPath(); flightContext.arc(x, y, 6, 0, Math.PI * 2); flightContext.fill();
      }
      flightContext.globalAlpha = 1;
      for (const star of stars.values()) {
        const arrival = star.newArrival ? (motionReduced.current ? 1 : Math.max(0, Math.min(1, (elapsed - star.delay) / duration))) : fraction;
        const travel = ease(arrival);
        star.x = star.fromX + (star.toX - star.fromX) * travel;
        star.y = star.fromY + (star.toY - star.fromY) * travel;
        star.alpha = star.newArrival ? ease((arrival - .68) / .32) : star.fromAlpha + (star.toAlpha - star.fromAlpha) * easing;
      }
      const positions = new Map<string, Point>();
      for (const [key, star] of stars) {
        const slot = Number(key.charAt(0));
        positions.set(key, [
          star.x + Math.sin(now * .00037 + star.phase) * drift + Math.sin(now * .00025 + slot * .68) * drift * .55,
          star.y + Math.cos(now * .00043 + star.phase * 1.3) * drift + Math.cos(now * .00028 + slot * .75) * drift * .45,
        ]);
      }
      context.strokeStyle = "#b6d9f7";
      for (const [first, second] of links) {
        const a = stars.get(first);
        const b = stars.get(second);
        if (!a || !b) continue;
        const [ax, ay] = positions.get(first)!;
        const [bx, by] = positions.get(second)!;
        context.lineWidth = .65;
        context.globalAlpha = Math.min(a.alpha, b.alpha) * .32;
        context.beginPath();
        context.moveTo(ax, ay);
        context.lineTo(bx, by);
        context.stroke();
      }
      for (const [key, star] of stars) {
        if (star.alpha <= .02) continue;
        const [x, y] = positions.get(key)!;
        const shimmer = motionReduced.current ? 1 : .82 + .18 * Math.sin(now * .0015 + star.phase * 2);
        context.globalAlpha = star.alpha * shimmer;
        const haloSize = star.radius * 5;
        const halo = context.createRadialGradient(x, y, 0, x, y, haloSize);
        halo.addColorStop(0, "rgba(202,230,255,.54)");
        halo.addColorStop(1, "rgba(202,230,255,0)");
        context.fillStyle = halo;
        context.beginPath();
        context.arc(x, y, haloSize, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = star.radius > 2 ? "#ffffff" : "#d8ebff";
        context.beginPath();
        context.arc(x, y, star.radius, 0, Math.PI * 2);
        context.fill();
      }
      // A single quiet arrival continues the relationship with the background.
      if (visitorSource && visitorKey && !motionReduced.current) {
        const phase = (now - visitorAt) / 2400;
        if (phase >= 1) {
          const destination = stars.get(visitorKey);
          if (destination) destination.source = visitorSource;
          visitorSource = null;
          visitorKey = "";
        }
        else if (phase > 0) {
          const destination = stars.get(visitorKey);
          if (destination) {
            const source = sourcePosition(visitorSource);
            const target = clockPosition([destination.x, destination.y]);
            const [x, y] = flightPoint(source, target, phase, 16);
            const mask = flightContext.createRadialGradient(source[0], source[1], 0, source[0], source[1], 9);
            mask.addColorStop(0, `rgba(3,6,10,${.82 * ease(phase * 3)})`);
            mask.addColorStop(1, "rgba(3,6,10,0)");
            flightContext.fillStyle = mask;
            flightContext.beginPath(); flightContext.arc(source[0], source[1], 9, 0, Math.PI * 2); flightContext.fill();
            const halo = flightContext.createRadialGradient(x, y, 0, x, y, 7);
            halo.addColorStop(0, "rgba(238,248,255,.85)");
            halo.addColorStop(1, "rgba(155,206,255,0)");
            flightContext.globalAlpha = Math.min(1, phase * 5, (1 - phase) * 4) * .65;
            flightContext.fillStyle = halo;
            flightContext.beginPath(); flightContext.arc(x, y, 7, 0, Math.PI * 2); flightContext.fill();
          }
        }
      }
      context.globalAlpha = 1;
      flightContext.globalAlpha = 1;
      if (elapsed > 2800) { returning = []; returningLinks = []; }
      if (motionReduced.current || document.hidden) timeout = window.setTimeout(() => { animationFrame = window.requestAnimationFrame(frame); }, 750);
      else animationFrame = window.requestAnimationFrame(frame);
    };
    animationFrame = window.requestAnimationFrame(frame);
    window.addEventListener("resize", resize);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.clearTimeout(timeout);
      window.removeEventListener("resize", resize);
      image.removeEventListener("load", loadSky);
      image.removeEventListener("error", imageError);
    };
  }, []);

  return <canvas ref={canvas} className="bbi-clock" role="img" aria-label={time}/>;
}
