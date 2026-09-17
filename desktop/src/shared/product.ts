declare const __TMOD_DESKTOP_EDITION__: "tmod" | "blackbird";

export type DesktopEdition = "tmod" | "blackbird";

const edition: DesktopEdition = __TMOD_DESKTOP_EDITION__ === "blackbird" ? "blackbird" : "tmod";
const blackbird = edition === "blackbird";

export const desktopProduct = Object.freeze({
  edition,
  privateEdition: blackbird,
  name: blackbird ? "BLACKBIRD" : "T-Mod",
  fullName: blackbird
    ? "BLACKBIRD — Технологии Товарищества"
    : "T-Mod Desktop",
  organization: blackbird ? "Технологии Товарищества" : "T-Mod",
  mark: blackbird ? "B" : "T",
  appId: blackbird ? "lat.tvr.technology.blackbird" : "lat.tvr.tmod.desktop",
  protocol: blackbird ? "blackbird" : "tmod",
  partition: blackbird ? "persist:tt-blackbird-private-v1" : "persist:tmod-desktop-v1",
  installationFile: blackbird
    ? "blackbird-private-installation.json"
    : "desktop-installation.json",
  preferencesKey: blackbird
    ? "tt-blackbird-preferences-v1"
    : "tmod-desktop-preferences-v1",
  updateChannel: blackbird ? "private" : "beta",
  releaseUrl: blackbird
    ? "https://github.com/cdnserver/t-mod/releases"
    : "https://github.com/cdnserver/t-mod-releases/releases/latest",
} as const);
