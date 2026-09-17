import { spawnSync } from "node:child_process";
import process from "node:process";

const mode = process.argv[2] || "build";
if (!["build", "package", "dist"].includes(mode)) {
  throw new Error(`Unknown BLACKBIRD build mode: ${mode}`);
}

const executable = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
const environment = {
  ...process.env,
  TMOD_DESKTOP_EDITION: "blackbird",
  CSC_IDENTITY_AUTO_DISCOVERY: process.env.CSC_IDENTITY_AUTO_DISCOVERY || "false",
};

function run(args) {
  const result = spawnSync(executable, args, {
    cwd: process.cwd(),
    env: environment,
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run(["exec", "electron-vite", "build"]);
if (mode !== "build") {
  const builder = ["exec", "electron-builder", "--config", "electron-builder.blackbird.yml"];
  if (mode === "package") builder.push("--dir");
  run(builder);
}
