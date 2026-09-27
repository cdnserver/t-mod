import { spawnSync } from "node:child_process";
import process from "node:process";

const mode = process.argv[2] || "build";
if (!["build", "package", "dist"].includes(mode)) {
  throw new Error(`Unknown BLACKBIRD build mode: ${mode}`);
}

const windows = process.platform === "win32";
if (!windows && mode !== "build") throw new Error("BLACKBIRD installers are built on native Windows only.");
const executable = windows ? process.env.ComSpec || "cmd.exe" : "pnpm";
const environment = {
  ...process.env,
  TMOD_DESKTOP_EDITION: "blackbird",
  CSC_IDENTITY_AUTO_DISCOVERY: process.env.CSC_IDENTITY_AUTO_DISCOVERY || "false",
};

function run(args) {
  // Windows .cmd launchers cannot be executed directly by spawnSync on modern
  // Node. Arguments here are a fixed, validated list, never user-supplied text.
  const commandArgs = windows ? ["/d", "/s", "/c", `pnpm.cmd ${args.join(" ")}`] : args;
  const result = spawnSync(executable, commandArgs, {
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
  const builder = ["exec", "electron-builder", "--config", "electron-builder.blackbird.yml", "--win", "--x64"];
  if (mode === "package") builder.push("--dir");
  run(builder);
}
