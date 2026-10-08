import { app } from "electron";
import { interactionCss, installSmoothScroll } from "../shared/interaction-surface";
import { isTrustedTModUrl } from "../shared/services";

export function enableInteractionSurfaces(): void {
  app.commandLine.appendSwitch("enable-smooth-scrolling");
  app.on("web-contents-created", (_event, contents) => {
    contents.on("dom-ready", () => {
      const url = contents.getURL();
      const local = url.startsWith("file:") || /^http:\/\/(localhost|127\.0\.0\.1):\d+\//.test(url);
      if (!local && !isTrustedTModUrl(url)) return;
      // User-origin CSS survives the differing themes of the embedded services.
      void contents.insertCSS(interactionCss, { cssOrigin: "user" }).catch(() => {});
      void contents.executeJavaScriptInIsolatedWorld(1007, [{ code: `(${installSmoothScroll.toString()})()` }]).catch(() => {});
    });
  });
}
