export const INTRO_STYLES = ["letters", "veil", "light"] as const;
export type IntroStyle = typeof INTRO_STYLES[number];
export function normalizeIntroStyle(value: unknown): IntroStyle {
  return INTRO_STYLES.includes(value as IntroStyle) ? value as IntroStyle : "letters";
}
export function shellInsets(privateEdition: boolean, vertical: boolean, collapsed: boolean) {
  const sidebar = collapsed ? 78 : 286;
  return { left: sidebar, top: privateEdition ? (vertical ? 0 : 44) : 70, right: privateEdition && vertical ? 48 : 0 };
}
export function serviceBounds(width: number, height: number, privateEdition: boolean, vertical: boolean, collapsed: boolean) {
  const insets = shellInsets(privateEdition, vertical, collapsed);
  return { x: insets.left, y: insets.top, width: Math.max(1, width - insets.left - insets.right), height: Math.max(1, height - insets.top) };
}
