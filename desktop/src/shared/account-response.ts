export function csrfTokenFromAccountSnapshot(snapshot: Record<string, unknown>): string | undefined {
  if (typeof snapshot.csrf_token === "string" && snapshot.csrf_token) return snapshot.csrf_token;
  const viewer = snapshot.viewer;
  if (viewer && typeof viewer === "object" && !Array.isArray(viewer)) {
    const token = (viewer as Record<string, unknown>).csrf_token;
    if (typeof token === "string" && token) return token;
  }
  return undefined;
}
