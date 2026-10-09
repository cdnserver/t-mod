# Atlas calls in Blackbird

## Current transport

The Blackbird shell owns one persistent WebSocket to
`wss://dash.tvr.lat/api/atlas/call/ws`. The public web gateway relays this socket
to the existing Atlas runtime; Caddy retains its usual reverse-proxy routing.
Calls are not available to remote embedded service pages through Electron IPC.

The microphone is enabled only after the user starts a call and grants permission.
An AudioWorklet and local energy VAD collect complete utterances with a 240 ms
pre-roll and a 650 ms silence endpoint. Mono PCM16 WAV at 16 kHz is sent in binary
frames. One utterance is capped at 30 seconds. Headphones are recommended:
browser echo cancellation cannot guarantee perfect barge-in on loudspeakers.

**This is not provider-side realtime STT.** OpenRouter currently accepts complete
audio clips for ElevenLabs Scribe v2, not its realtime WebSocket API. The client
socket stays open, while each finished utterance is transcribed by OpenRouter.
Atlas uses its existing scoped retrieval, models and conversation history. Its
voice prompt requests short, conversational answers without inventing legal norms.
Complete phrases are synthesized as the answer is generated and played in order.

Speech detection stops old playback immediately. A monotonically increasing turn
number rejects queued audio from old responses, including frames already in transit.
Ending, logging out, locking, a global ban, or a lost connection stops microphone
tracks and releases audio resources. Network failures do not automatically resume
microphone capture: the user explicitly calls again.

## Configuration

Only the server sees provider credentials. Defaults:

| Variable | Default / behaviour |
| --- | --- |
| `OPENROUTER_API_KEY` | Existing server key |
| `ATLAS_CALL_API_KEY` | Optional call-only override |
| `ATLAS_CALL_ENABLED` | `1`; `0`, `false`, `off` disable new calls |
| `ATLAS_CALL_STT_MODEL` | `elevenlabs/scribe-v2` |
| `ATLAS_CALL_TTS_MODEL` | `elevenlabs/eleven-v4-turbo` |
| `ATLAS_TTS_MODEL` | Overlay defaults to `elevenlabs/eleven-v4-turbo` |
| `ATLAS_TTS_VOICES` | Optional overlay voice allowlist |
| `ATLAS_TTS_DEFAULT_VOICE` | Overlay voice defaults to `george` |

Calls expose four predefined voices: George, Sarah, Daniel and River. Voice
cloning and arbitrary provider instructions are not exposed. Eleven v4 receives
explicit MP3 output and no unsupported `speed` field. Existing explicit overlay
provider settings are preserved; cached Grok voice preferences are mapped to the
new default when ElevenLabs is selected.

## Authentication and limits

1. Authenticated desktop GET `/api/atlas/call/config` checks Atlas access and balance.
2. POST `/api/atlas/call/ticket` also verifies CSRF and returns a random, single-use
   ticket with a 30-second lifetime.
3. The first socket **frame**, within five seconds, carries this ticket. Credentials
   must never be put in the socket URL, logs or renderer storage.
4. The session rechecks account access before and after provider work and every
   five seconds while idle. Revocation closes the socket; a request already sent
   to a provider cannot retroactively be withdrawn.

There is one active call per account, at most 32 sockets including pending
authentication, at most 256 pending tickets, 120 input frames/minute/socket, four
concurrent speech-provider requests, and a 30-minute call limit. Provider calls
and gateway connection attempts have timeouts; an utterance's entire turn is
bounded to 120 seconds. Do not increase workers blindly: tickets are process-local
and require sticky routing or a shared ticket/session store before horizontal
scaling. Existing Atlas billing checks retain their current rollout policy.

## Accounting and privacy

Completed STT requests record returned `usage.cost`; completed TTS requests record
an **estimate** from the provider's current per-character catalog price and the
actual cleaned spoken text. Sources are `blackbird-call-transcribe`,
`blackbird-call-speak-estimated`, and `blackbird-call` for the Atlas answer itself.
Ledger writes for completed speech results survive user interruption. Interrupted
provider requests can still incur upstream charges before cancellation; reconciliation
of such incomplete requests and final invoice reconciliation are not implemented.

The new call code does not store microphone recordings or generated MP3 files.
Completed text exchanges are saved to the user's existing Atlas thread. A model
response interrupted before completion is not committed as a completed exchange.
Gateway session events contain routing/status metadata, not socket frames. Error
diagnostics contain call ID and exception type, not transcripts or credentials.
Audio is sent to OpenRouter/ElevenLabs; their retention policies still apply, so
this is not a claim of provider-side zero retention.

## Validation and release checklist

Automated coverage includes gateway binary/control relay, CSRF, ticket single use,
origin denial, duplicate calls, revoked access, invalid audio, interruption,
completed-cost preservation, client ownership, stale frames, VAD and WAV encoding.

On 2026-10-09 a synthetic Russian phrase was genuinely synthesized through
OpenRouter Eleven v4 Turbo and transcribed through Scribe v2, with an exact text
round trip. One sample measured 0.75 s TTS and 0.76 s STT (not full call latency).

Before release, validate on Windows with a real microphone: permission denied,
Bluetooth/output changes, loudspeaker echo, barge-in, lock/logout, expiry/ban,
network loss and a second device. Deploy both the Atlas runtime and web gateway
before shipping the desktop build. The older server has neither the ticket route
nor the socket relay. This feature is not activated by frontend-only hot update.

Local visual preview (demo only, no microphone/network call):
`/src/renderer/index.html?visual=atlas-call&skip-launch=1`.

Provider reference: https://openrouter.ai/blog/announcements/elevenlabs-on-openrouter/
