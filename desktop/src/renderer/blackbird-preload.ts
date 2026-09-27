export type PreloadStatus = "pending" | "ready" | "fallback";
export type PreloadTask = { label: string; status: PreloadStatus };

/** Ten seconds for the ident, a black pause, then the lunar dissolve. */
export const BLACKBIRD_IDENT_TIMING = Object.freeze({
  visibleMs: 10_000,
  blackHoldMs: 2_600,
  minimumMs: 12_600,
  dissolveMs: 2_400,
  reducedDissolveMs: 400,
});

export function preloadSummary(tasks: PreloadTask[]) {
  const completed = tasks.filter(task => task.status !== "pending").length;
  const pending = tasks.find(task => task.status === "pending");
  const degraded = tasks.some(task => task.status === "fallback");
  return {
    ready: completed === tasks.length,
    progress: tasks.length ? completed / tasks.length : 1,
    label: pending?.label ?? (degraded ? "Готово · доступен резервный режим" : "Всё готово"),
    degraded,
  };
}
