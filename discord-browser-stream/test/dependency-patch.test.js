import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFile, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import test from "node:test";

const execute = promisify(execFile);
const projectRoot = new URL("../", import.meta.url);
const dependency = new URL(
  "node_modules/puppeteer-stream/dist/PuppeteerStream.js",
  projectRoot,
);
const patcher = new URL(
  "scripts/patch-puppeteer-stream.mjs",
  projectRoot,
);
const streamDependency = new URL(
  "node_modules/@dank074/discord-video-stream/dist/media/newApi.js",
  projectRoot,
);
const streamPatcher = new URL(
  "scripts/patch-discord-video-stream.mjs",
  projectRoot,
);

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

test("puppeteer-stream reliability patch applies and is idempotent", async () => {
  const directory = await mkdtemp(join(tmpdir(), "tmod-puppeteer-stream-"));
  const target = join(directory, "PuppeteerStream.js");

  try {
    await copyFile(dependency, target);
    await execute(process.execPath, [patcher.pathname, target]);
    const first = await readFile(target, "utf8");

    assert.match(first, /--allowlisted-extension-id=/);
    assert.match(first, /let browserClosePromise;/);
    assert.match(first, /stream\.stop = async/);
    assert.match(first, /stream\.destroyed \|\| stream\.writableEnded/);
    assert.match(first, /finally \{\s+unlock\(\);/);

    await execute(process.execPath, [patcher.pathname, target]);
    const second = await readFile(target, "utf8");
    assert.equal(digest(second), digest(first));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("Go Live connection patch applies and is idempotent", async () => {
  const directory = await mkdtemp(join(tmpdir(), "tmod-video-stream-"));
  const target = join(directory, "newApi.js");

  try {
    await copyFile(streamDependency, target);
    await execute(process.execPath, [streamPatcher.pathname, target]);
    const first = await readFile(target, "utf8");

    assert.match(first, /stream_connection_timeout/);
    assert.match(first, /onConnectionAbort/);
    assert.match(first, /streamer\.stopStream\(\);\s+throw error;/);

    await execute(process.execPath, [streamPatcher.pathname, target]);
    const second = await readFile(target, "utf8");
    assert.equal(digest(second), digest(first));
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
