export function notificationVisibilityMs(kind: string): number {
  if (kind === "screenban") return 14_000;
  if (kind === "orl:fullscreen") return 11_000;
  return 9_000;
}

// A toast may pause while the pointer is over it, but must never remain on
// screen indefinitely if its renderer is throttled or stops responding.
export function notificationHardLimitMs(kind: string, toastDurationMs = 9_000): number {
  return kind === "screenban" || kind === "orl:fullscreen" || kind === "orl:overlay"
    ? notificationVisibilityMs(kind) : toastDurationMs;
}
