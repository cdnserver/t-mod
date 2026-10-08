import { spawnSync } from "node:child_process";
import process from "node:process";
import { createReadStream, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";

const mode = process.argv[2] || "build";
if (!["build", "package", "dist", "custom"].includes(mode)) {
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

if (mode === "custom") {
  // Fail before the expensive client build if the required native SDK is absent.
  const sdk = spawnSync("dotnet", ["--list-sdks"], { encoding: "utf8", windowsHide: true });
  if (sdk.error || sdk.status !== 0 || !/^8\.0\./m.test(sdk.stdout || "")) {
    throw new Error("The custom BLACKBIRD installer requires the .NET 8 SDK on native Windows.");
  }
}

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
  const builder = ["exec", "electron-builder", "--config", "electron-builder.blackbird.yml", "--win", "--x64", "--publish", "never"];
  if (mode === "package" || mode === "custom") builder.push("--dir");
  run(builder);
}

if (mode === "custom") {
  // Explicit opt-in. Never publishes or replaces the existing NSIS update feed.
  const version = readFileSync("electron-builder.blackbird.yml", "utf8").match(/^  version: ([0-9A-Za-z.+-]+)$/m)?.[1];
  if (!version) throw new Error("BLACKBIRD version missing from builder configuration.");
  const native = args => {
    const result = spawnSync("dotnet", args, { cwd: process.cwd(), env: environment, stdio: "inherit", windowsHide: true });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`Native installer step failed: ${result.status}`);
  };
  native(["run", "--project", "installer/Blackbird.Setup.Pack", "--", "release-blackbird/win-unpacked", "installer/Generated", version]);
  native(["publish", "installer/Blackbird.Setup/Blackbird.Setup.csproj", "-c", "Release", "-r", "win-x64", "--self-contained", "true", "-p:PublishSingleFile=true", `-p:Version=${version}`, "-o", "release-blackbird/custom"]);
  const directory = path.resolve("release-blackbird/custom");
  const name = `BLACKBIRD-Custom-Setup-${version}.exe`;
  const installer = path.join(directory, name);
  renameSync(path.join(directory, "Blackbird.Setup.exe"), installer);
  const hash = createHash("sha512");
  for await (const chunk of createReadStream(installer)) hash.update(chunk);
  const sha512 = hash.digest("base64");
  writeFileSync(path.join(directory, "custom-latest.yml"), `version: ${version}\nfiles:\n  - url: ${name}\n    sha512: ${sha512}\n    size: ${statSync(installer).size}\npath: ${name}\nsha512: ${sha512}\nreleaseDate: '${new Date().toISOString()}'\n`);
  console.log(`Custom installer ready: ${installer}. No publication performed.`);
}
