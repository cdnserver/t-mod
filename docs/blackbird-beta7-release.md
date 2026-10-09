# BLACKBIRD 1.3.6-beta.7 release checkpoint

Released and authenticated auto-update feed activated on 2026-10-09.

## Installer and source

- Private prerelease: https://github.com/cdnserver/blackbird-releases/releases/tag/blackbird-v1.3.6-beta.7
- Native Windows x64 build, without Wine or GitHub Actions.
- Installer: `BLACKBIRD-Private-Setup-1.3.6-beta.7.exe`, **123837778 bytes**.
- SHA-256: `37cf55954e2e5c6f36169d2a0e1f7c6689894c54cc77eee4ba6322772dba04d1`.
- SHA-512 (base64): `FCniz6LONZSlBf/lgs5pMH7HbsnvPX4XC+lMNTKmuQSBopJbwdijPcbXTDJfDFGYh8pPEsCNsgPivNtl8oLA/w==`.
- Client snapshot: `382d039`; private release includes its desktop source ZIP and reproducible brand/test inputs. Later integrated commits are backend/test changes, not a different client bundle.
- Source ZIP SHA-256: `3145640a1b4cf9a09f0fd448be89bc0d861e8e56380abb4c4d63478f21a3e33d`.
- Standard NSIS installer retained for update compatibility; experimental custom installer is not shipped.
- Local artifacts: `~/Downloads/Blackbird-1.3.6-beta.7/`.

## Voice changes

Custom agent names are validated on the client and server and saved per account on the current computer. A literal alias is included in the greeting and conversation; it cannot change Atlas permissions or instruct the model. Four predefined ElevenLabs voices are available through OpenRouter.

Calls use the existing Atlas low-latency field path. Exact agent-identity questions skip legal retrieval. IC/OOC comparison questions receive an actual glossary explanation instead of an incident clarification. Legal queries retain scoped retrieval and grounding. Ticket bodies survive observability middleware, and ticket response credentials are excluded from payload logging.

## Backend deployment

Public backend code revision: `f918797627ab97094a77fe7079caa8528a37853a`. Running image config digest after the final build: `sha256:4fae1336ddc8e8105f91575c4ee7bd48945748cff5e4faafdf99945fba5b5cfc`.

Bot, web gateway, API and worker were rebuilt/recreated and verified `running healthy`. PostgreSQL volumes and unrelated services were not recreated; no new database migration was needed. The persistent overlay configuration was switched from its explicit Grok override to Eleven v4 Turbo, with a backup of the previous environment file. No provider credentials are included in this report or the release archives.

## Validation evidence

- Windows: **263 passed, 3 intentionally skipped**; type checking, renderer/main/preload build and parsing passed.
- Final backend group: **144 passed**, both locally and in the isolated new production image. Includes Atlas, voice socket/integration, speech and gateway checks.
- Additional authentication/security/logging/bootstrap group: **40 passed**.
- Packaged ASAR: version beta.7, main/preload voice bridge, microphone worklet, alias settings and authenticated updater configuration verified.
- Live synthetic Russian audio traversed the public HTTPS/WebSocket gateway, OpenRouter STT, Atlas and ElevenLabs TTS. Greeting, chosen name, three successive turns in one scoped history thread and generated audio passed.
- Final sample, measured from submitting a completed WAV utterance to first returned audio: identity **1.69 s**, IC/OOC explanation **3.76 s**, normal model answer **6.28 s**. These are observations, not latency guarantees or microphone-to-ear measurements. Glossary synthesis completed in **6.47 s**.
- Anonymous configuration/socket access, CSRF rejection, invalid names and single-use ticket replay protections passed on the live gateway.

This is a validated beta, not a claim of zero defects. Synthetic audio and automated tests do not validate every physical microphone, Bluetooth output device, loudspeaker echo scenario or end-user installation. Real-device acceptance checks remain useful after installation.

## Auto-update activation and rollback

Installer and blockmap were copied and checksummed before an atomic replacement of `latest.yml`. The previous beta.6 manifest was retained as a timestamped `latest-before-beta7-*.yml`; previous installers were kept. The original backend image remains available as `tmod-discord-bot:before-call-beta7-20261009`.

The authenticated first-party feed advertises **1.3.6-beta.7**. A request carrying beta.6 client headers downloaded all **123837778 bytes** through the public feed and verified both SHA-256 and manifest SHA-512. HTTP range downloads and gzip blockmap v2 also passed; anonymous feed access remained HTTP 401.

Signed-in installed clients check at launch and every 30 minutes, download in the background and install on application exit. Closing to tray is not application exit. Actual installation on an end user's PC is not established by the feed-download test.

To roll back the feed, atomically restore the retained beta.6 manifest only after verifying its old assets remain present. If the runtime needs rollback, recreate the four application services with the retained image; do not remove database volumes. Never publish a manifest before its matching installer is available.

## Known transport/privacy limits

WebSocket remains connected, but OpenRouter Scribe recognition works on complete utterances, not provider-side realtime STT. Microphone recordings and generated audio are not persisted by the new call code; completed text exchanges enter the existing Atlas history. Provider retention policies still apply.

Completed STT cost is recorded from returned provider usage; TTS cost is currently an explicit per-character estimate. Interrupted incomplete upstream requests and final invoice reconciliation are not fully reconciled. Tickets and active-call limits are process-local; horizontal scaling requires shared state or sticky routing before adding runtime workers.
