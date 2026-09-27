export type PreloadStatus = "pending" | "ready" | "fallback";
export type PreloadTask = { label: string; status: PreloadStatus };

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
