import { Client, StageChannel } from "discord.js-selfbot-v13";
import {
  Encoders,
  Streamer,
  Utils,
  playStream,
  prepareStream,
} from "@dank074/discord-video-stream";
import { getStream, launch } from "puppeteer-stream";
import { timingSafeEqual } from "node:crypto";
import { rm } from "node:fs/promises";
import { createServer } from "node:http";
import { join } from "node:path";
import {
  canReuseVoiceConnection,
  createLifecycleQueue,
  delay,
  isExpectedBrowserCloseError,
  isRetryableBrowserLaunchError,
  waitForVoiceConnection,
} from "./lifecycle.js";

const CAPTURE_EXTENSION_ID = "jjndjgheafjngoipoacpjgeicjeomjli";

function integer(name, fallback, minimum, maximum) {
  const raw = process.env[name];
  const value = raw === undefined || raw === "" ? fallback : Number(raw);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}

function boolean(name, fallback = false) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  if (raw === "true") return true;
  if (raw === "false") return false;
  throw new Error(`${name} must be true or false`);
}

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const config = {
  token: required("BROWSER_STREAM_DISCORD_TOKEN"),
  allowedUsers: new Set(
    required("BROWSER_STREAM_ALLOWED_USER_IDS")
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean),
  ),
  commandChannelId:
    process.env.BROWSER_STREAM_COMMAND_CHANNEL_ID?.trim() || null,
  guildId: process.env.BROWSER_STREAM_GUILD_ID?.trim() || null,
  voiceChannelId:
    process.env.BROWSER_STREAM_VOICE_CHANNEL_ID?.trim() || null,
  startUrl:
    process.env.BROWSER_STREAM_START_URL?.trim() || "https://example.org",
  autoStart: boolean("BROWSER_STREAM_AUTO_START"),
  prefix: process.env.BROWSER_STREAM_COMMAND_PREFIX?.trim() || "!screen",
  width: integer("BROWSER_STREAM_WIDTH", 1280, 320, 3840),
  height: integer("BROWSER_STREAM_HEIGHT", 720, 240, 2160),
  fps: integer("BROWSER_STREAM_FPS", 30, 5, 60),
  bitrate: integer("BROWSER_STREAM_BITRATE_KBPS", 2500, 200, 20000),
  maxBitrate: integer("BROWSER_STREAM_MAX_BITRATE_KBPS", 4000, 200, 30000),
  pageTimeout: integer(
    "BROWSER_STREAM_PAGE_LOAD_TIMEOUT_MS",
    45000,
    1000,
    180000,
  ),
  captureStartupDelay: integer(
    "BROWSER_STREAM_CAPTURE_STARTUP_DELAY_MS",
    1250,
    250,
    10000,
  ),
  captureFocusDelay: integer(
    "BROWSER_STREAM_CAPTURE_FOCUS_DELAY_MS",
    250,
    0,
    5000,
  ),
  browserLaunchAttempts: integer(
    "BROWSER_STREAM_BROWSER_LAUNCH_ATTEMPTS",
    2,
    1,
    3,
  ),
  chromiumLogs: boolean("BROWSER_STREAM_CHROMIUM_LOGS"),
  voiceConnectTimeout: integer(
    "BROWSER_STREAM_VOICE_CONNECT_TIMEOUT_MS",
    20000,
    5000,
    60000,
  ),
  ignoreHttpsErrors: boolean("BROWSER_STREAM_IGNORE_HTTPS_ERRORS"),
  profileDir:
    process.env.BROWSER_STREAM_PROFILE_DIR?.trim() || "/data/chrome",
  controlToken: required("BROWSER_STREAM_CONTROL_TOKEN"),
  apiPort: integer("BROWSER_STREAM_API_PORT", 8790, 1024, 65535),
};

if (config.maxBitrate < config.bitrate) {
  throw new Error(
    "BROWSER_STREAM_MAX_BITRATE_KBPS cannot be lower than " +
      "BROWSER_STREAM_BITRATE_KBPS",
  );
}

const client = new Client({ checkUpdate: false });
const streamer = new Streamer(client);
let active = null;
let lastUrl = config.startUrl;
let lastError = null;
let apiServer = null;
const enqueueLifecycle = createLifecycleQueue();
let browserCloseGraceUntil = 0;

function log(message, error) {
  const suffix = error ? `\n${error.stack || error}` : "";
  console.log(`[${new Date().toISOString()}] ${message}${suffix}`);
}

function parseUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("URL is invalid");
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("Only http:// and https:// URLs are supported");
  }
  return url.toString();
}

function streamStatus() {
  return {
    ok: true,
    state: active?.state || "idle",
    active: Boolean(active),
    url: active?.url || null,
    started_at: active?.startedAt?.toISOString() || null,
    account: client.user?.tag || null,
    guild_id: active?.guildId || config.guildId,
    voice_channel_id: active?.voiceChannelId || config.voiceChannelId,
    last_error: lastError?.message || null,
    last_error_at: lastError?.at?.toISOString() || null,
  };
}

function tokenMatches(value) {
  const supplied = Buffer.from(String(value || ""));
  const expected = Buffer.from(config.controlToken);
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}

function authorized(request) {
  const header = String(request.headers.authorization || "");
  return header.startsWith("Bearer ") && tokenMatches(header.slice(7));
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}

async function readJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 16 * 1024) throw new Error("request_too_large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function isAbortError(error) {
  return error?.name === "AbortError";
}

async function ensureVoiceConnection(session) {
  const current = streamer.voiceConnection;
  if (canReuseVoiceConnection(current, session.guildId, session.voiceChannelId)) {
    log(`Reusing voice connection ${session.guildId}/${session.voiceChannelId}`);
    return current;
  }

  if (current) {
    streamer.leaveVoice();
    await delay(250);
  }

  log(`Joining voice channel ${session.guildId}/${session.voiceChannelId}`);
  try {
    return await waitForVoiceConnection(
      streamer.joinVoice(session.guildId, session.voiceChannelId),
      session.controller.signal,
      config.voiceConnectTimeout,
    );
  } catch (error) {
    streamer.leaveVoice();
    throw error;
  }
}

async function clearStaleProfileLocks(profileDir) {
  await Promise.all(
    ["SingletonLock", "SingletonCookie", "SingletonSocket"].map((name) =>
      rm(join(profileDir, name), { force: true }).catch(() => {}),
    ),
  );
}

function chromiumLaunchOptions(profileDir, diagnostic = false) {
  return {
    executablePath:
      process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium",
    headless: false,
    dumpio: config.chromiumLogs || diagnostic,
    startDelay: config.captureStartupDelay,
    defaultViewport: { width: config.width, height: config.height },
    userDataDir: profileDir,
    acceptInsecureCerts: config.ignoreHttpsErrors,
    args: [
      "--no-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--autoplay-policy=no-user-gesture-required",
      `--allowlisted-extension-id=${CAPTURE_EXTENSION_ID}`,
      "--window-position=0,0",
      `--window-size=${config.width},${config.height}`,
    ],
  };
}

async function launchBrowser(session) {
  let lastError;
  for (
    let attempt = 1;
    attempt <= config.browserLaunchAttempts;
    attempt += 1
  ) {
    session.controller.signal.throwIfAborted();
    const recovery = attempt > 1;
    const profileDir = recovery
      ? join("/tmp", `tmod-chromium-recovery-${process.pid}`)
      : config.profileDir;
    if (recovery) {
      await rm(profileDir, { recursive: true, force: true });
      session.recoveryProfileDir = profileDir;
      log("Retrying Chromium with an isolated recovery profile");
    } else {
      await clearStaleProfileLocks(profileDir);
    }
    try {
      return await launch(chromiumLaunchOptions(profileDir, recovery));
    } catch (error) {
      lastError = error;
      if (
        !isRetryableBrowserLaunchError(error) ||
        attempt >= config.browserLaunchAttempts
      ) {
        throw error;
      }
      log(
        `Chromium exited during startup; retrying ` +
          `(${attempt}/${config.browserLaunchAttempts})`,
      );
      await delay(750);
    }
  }
  throw lastError;
}

async function closeSessionBrowser(session) {
  if (session.cleanupPromise) return session.cleanupPromise;

  session.cleanupPromise = (async () => {
    session.state = "stopping";
    if (!session.controller.signal.aborted) session.controller.abort();

    const browserMedia = session.browserMedia;
    session.browserMedia = null;
    if (browserMedia) {
      await browserMedia.stop().catch((error) => {
        if (!isExpectedBrowserCloseError(error)) {
          log("Cannot stop Chromium capture cleanly", error);
        }
      });
    }

    const browser = session.browser;
    session.browser = null;
    if (!browser) {
      if (session.recoveryProfileDir) {
        await rm(session.recoveryProfileDir, {
          recursive: true,
          force: true,
        }).catch(() => {});
      }
      return;
    }

    browserCloseGraceUntil = Date.now() + 5000;
    try {
      await Promise.race([
        browser.close(),
        delay(5000).then(() => {
          throw new Error("chromium_close_timeout");
        }),
      ]);
    } catch (error) {
      if (!isExpectedBrowserCloseError(error)) {
        log("Chromium did not close cleanly; terminating its process", error);
      }
      browser.process()?.kill("SIGKILL");
    } finally {
      browserCloseGraceUntil = Date.now() + 2000;
      if (session.recoveryProfileDir) {
        await rm(session.recoveryProfileDir, {
          recursive: true,
          force: true,
        }).catch(() => {});
      }
    }
  })();

  return session.cleanupPromise;
}

async function stopActiveStream({ leaveVoice = false } = {}) {
  const session = active;
  if (session) {
    session.state = "stopping";
    if (!session.controller.signal.aborted) session.controller.abort();
    await session.runPromise;
  }
  if (leaveVoice) streamer.leaveVoice();
}

async function executeStream(session) {
  const { url, guildId, voiceChannelId } = session;
  try {
    await ensureVoiceConnection(session);
    session.controller.signal.throwIfAborted();

    const voiceChannel = await client.channels.fetch(voiceChannelId);
    if (voiceChannel instanceof StageChannel) {
      await client.user?.voice?.setSuppressed(false);
    }
    session.controller.signal.throwIfAborted();

    session.browser = await launchBrowser(session);

    log(`Chromium ready: ${await session.browser.version()}`);
    session.controller.signal.throwIfAborted();

    // puppeteer-stream owns a separate extension page. Always create a fresh
    // content page so the extension can never become the capture target.
    const page = await session.browser.newPage();
    page.setDefaultNavigationTimeout(config.pageTimeout);
    await page.setViewport({ width: config.width, height: config.height });
    await page.goto(url, { waitUntil: "domcontentloaded" });
    await page.bringToFront();
    if (config.captureFocusDelay) {
      await delay(config.captureFocusDelay);
    }
    session.controller.signal.throwIfAborted();

    try {
      session.browserMedia = await getStream(page, {
        audio: true,
        video: true,
        mimeType: "video/webm;codecs=vp8,opus",
        videoBitsPerSecond: config.maxBitrate * 1000,
        frameSize: Math.max(10, Math.round(1000 / config.fps)),
      });
    } catch (error) {
      if (
        String(error?.message || error).includes(
          "Extension has not been invoked for the current page",
        )
      ) {
        throw new Error(
          "Chromium rejected tab capture permission. Rebuild the " +
            "discord-browser-stream image so the extension allowlist flag " +
            "is applied.",
          { cause: error },
        );
      }
      throw error;
    }
    session.controller.signal.throwIfAborted();

    const encoder = Encoders.software({
      x264: { preset: "veryfast", tune: "zerolatency" },
    });
    const { command, output } = prepareStream(
      session.browserMedia,
      {
        encoder,
        width: config.width,
        height: config.height,
        frameRate: config.fps,
        bitrateVideo: config.bitrate,
        bitrateVideoMax: config.maxBitrate,
        bitrateAudio: 128,
        includeAudio: true,
        minimizeLatency: true,
        videoCodec: Utils.normalizeVideoCodec("H264"),
      },
      session.controller.signal,
    );

    command.on("error", (error, _stdout, stderr) => {
      if (!session.controller.signal.aborted) {
        log(`FFmpeg failed: ${stderr || "no stderr"}`, error);
      }
    });

    log(`Streaming ${url}`);
    session.state = "streaming";
    await playStream(
      output,
      streamer,
      { type: "go-live", readrateInitialBurst: 2 },
      session.controller.signal,
    );
  } catch (error) {
    if (!isAbortError(error)) {
      lastError = {
        message: String(error?.message || error),
        at: new Date(),
      };
      log(session.state === "streaming" ? "Stream failed" : "Cannot start stream", error);
    }
  } finally {
    await closeSessionBrowser(session);
    if (active === session) active = null;
    log(`Stream ended: ${url}`);
  }
}

async function replaceStream(url, guildId, voiceChannelId) {
  url = parseUrl(url);
  if (!guildId || !voiceChannelId) {
    throw new Error(
      "Voice target is unknown; join a voice channel or set " +
        "BROWSER_STREAM_GUILD_ID and BROWSER_STREAM_VOICE_CHANNEL_ID",
    );
  }

  await stopActiveStream();
  const controller = new AbortController();
  const session = {
    controller,
    browser: null,
    browserMedia: null,
    recoveryProfileDir: null,
    cleanupPromise: null,
    runPromise: null,
    url,
    guildId,
    voiceChannelId,
    startedAt: new Date(),
    state: "starting",
  };
  active = session;
  lastUrl = url;
  lastError = null;
  session.runPromise = executeStream(session);
}

function runStream(url, guildId, voiceChannelId) {
  void enqueueLifecycle(() =>
    replaceStream(url, guildId, voiceChannelId),
  ).catch((error) => {
    if (error?.name !== "AbortError") {
      lastError = {
        message: String(error?.message || error),
        at: new Date(),
      };
      log("Cannot start stream", error);
    }
  });
}

async function handleApi(request, response) {
  const requestUrl = new URL(request.url || "/", "http://browser-stream.local");
  if (request.method === "GET" && requestUrl.pathname === "/health") {
    sendJson(response, 200, {
      ok: true,
      ready: Boolean(client.user),
      state: active?.state || "idle",
    });
    return;
  }
  if (!authorized(request)) {
    sendJson(response, 401, { ok: false, error: "unauthorized" });
    return;
  }
  if (request.method === "GET" && requestUrl.pathname === "/status") {
    sendJson(response, 200, streamStatus());
    return;
  }
  if (request.method !== "POST" || requestUrl.pathname !== "/command") {
    sendJson(response, 404, { ok: false, error: "not_found" });
    return;
  }

  try {
    const body = await readJson(request);
    const action = String(body.action || "").trim().toLowerCase();
    if (action === "start") {
      const url = parseUrl(String(body.url || lastUrl));
      const guildId = String(body.guild_id || config.guildId || "").trim();
      const voiceChannelId = String(
        body.voice_channel_id || config.voiceChannelId || "",
      ).trim();
      if (!guildId || !voiceChannelId) {
        sendJson(response, 400, {
          ok: false,
          error: "voice_target_required",
        });
        return;
      }
      runStream(url, guildId, voiceChannelId);
      sendJson(response, 202, {
        ...streamStatus(),
        state: "starting",
        active: true,
        url,
        guild_id: guildId,
        voice_channel_id: voiceChannelId,
      });
      return;
    }
    if (action === "stop" || action === "leave") {
      await enqueueLifecycle(() =>
        stopActiveStream({ leaveVoice: action === "leave" }),
      );
      sendJson(response, 200, streamStatus());
      return;
    }
    sendJson(response, 400, { ok: false, error: "unknown_action" });
  } catch (error) {
    log("Browser stream API command failed", error);
    sendJson(response, 400, {
      ok: false,
      error: error instanceof SyntaxError ? "invalid_json" : String(error.message || error),
    });
  }
}

async function startApi() {
  apiServer = createServer((request, response) => {
    void handleApi(request, response).catch((error) => {
      log("Browser stream API failed", error);
      if (!response.headersSent) {
        sendJson(response, 500, { ok: false, error: "internal_error" });
      } else {
        response.end();
      }
    });
  });
  await new Promise((resolve, reject) => {
    apiServer.once("error", reject);
    apiServer.listen(config.apiPort, "0.0.0.0", resolve);
  });
  log(`Internal control API listening on port ${config.apiPort}`);
}

async function reply(message, text) {
  await message.reply(text).catch((error) => log("Cannot send command reply", error));
}

client.on("ready", () => {
  log(`Authorized account ready: ${client.user?.tag}`);
  if (config.autoStart) {
    runStream(config.startUrl, config.guildId, config.voiceChannelId);
  }
});

client.on("messageCreate", async (message) => {
  if (message.author.id === client.user?.id || message.author.bot) return;
  if (!config.allowedUsers.has(message.author.id)) return;
  if (config.commandChannelId && message.channelId !== config.commandChannelId) return;
  if (!message.content.startsWith(config.prefix)) return;

  const input = message.content.slice(config.prefix.length).trim();
  const [command = "help", ...args] = input.split(/\s+/);

  if (command === "start") {
    let url;
    try {
      url = parseUrl(args.join(" ") || lastUrl);
    } catch (error) {
      await reply(message, `Не удалось открыть страницу: ${error.message}`);
      return;
    }
    const guildId = message.guildId || config.guildId;
    const voiceChannelId = message.member?.voice?.channelId || config.voiceChannelId;
    await reply(message, `Запускаю трансляцию: ${url}`);
    runStream(url, guildId, voiceChannelId);
    return;
  }

  if (command === "stop") {
    await enqueueLifecycle(() => stopActiveStream());
    await reply(message, "Трансляция остановлена.");
    return;
  }

  if (command === "leave") {
    await enqueueLifecycle(() => stopActiveStream({ leaveVoice: true }));
    await reply(message, "Трансляция остановлена, голосовой канал покинут.");
    return;
  }

  if (command === "status") {
    const status = active
      ? `${active.state === "starting" ? "Запускается" : "Стрим идёт"}: ` +
        `${active.url} (с ${active.startedAt.toISOString()})`
      : lastError
        ? `Сейчас трансляции нет. Последняя ошибка: ${lastError.message}`
        : "Сейчас трансляции нет.";
    await reply(message, status);
    return;
  }

  await reply(
    message,
    [
      `Команды:`,
      `\`${config.prefix} start <url>\` — открыть страницу и начать стрим`,
      `\`${config.prefix} stop\` — остановить стрим`,
      `\`${config.prefix} leave\` — остановить и выйти из голоса`,
      `\`${config.prefix} status\` — показать состояние`,
    ].join("\n"),
  );
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    log(`Received ${signal}`);
    await enqueueLifecycle(() => stopActiveStream({ leaveVoice: true }));
    if (apiServer) {
      await new Promise((resolve) => apiServer.close(resolve));
    }
    client.destroy();
    process.exit(0);
  });
}

process.on("unhandledRejection", (error) => {
  if (
    Date.now() <= browserCloseGraceUntil &&
    isExpectedBrowserCloseError(error)
  ) {
    return;
  }
  log("Unhandled rejection", error);
});
await client.login(config.token);
await startApi();
