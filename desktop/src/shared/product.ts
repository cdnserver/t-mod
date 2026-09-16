declare const __TMOD_DESKTOP_EDITION__: "tmod" | "lumen";

export type DesktopEdition = "tmod" | "lumen";

const edition: DesktopEdition = __TMOD_DESKTOP_EDITION__ === "lumen" ? "lumen" : "tmod";

export const desktopProduct = Object.freeze({
  edition,
  privateEdition: edition === "lumen",
  name: edition === "lumen" ? "LUMEN" : "T-Mod",
  fullName: edition === "lumen"
    ? "LUMEN — Технологии Товарищества"
    : "T-Mod Desktop",
  organization: edition === "lumen" ? "Технологии Товарищества" : "T-Mod",
  mark: edition === "lumen" ? "L" : "T",
  appId: edition === "lumen" ? "lat.tvr.technology.lumen" : "lat.tvr.tmod.desktop",
  protocol: edition === "lumen" ? "lumen" : "tmod",
  partition: edition === "lumen" ? "persist:tt-lumen-private-v1" : "persist:tmod-desktop-v1",
  installationFile: edition === "lumen"
    ? "lumen-private-installation.json"
    : "desktop-installation.json",
  preferencesKey: edition === "lumen"
    ? "tt-lumen-preferences-v1"
    : "tmod-desktop-preferences-v1",
  updateChannel: edition === "lumen" ? "private" : "beta",
  releaseUrl: edition === "lumen"
    ? "https://github.com/cdnserver/t-mod/releases"
    : "https://github.com/cdnserver/t-mod-releases/releases/latest",
} as const);
