# Blackbird account reliability

Bootstrap races both approved hosts in the same Electron session partition.
HTTP 200 is not proof of success: identity, service and notification JSON fields
must be valid before the response can win. HTML/proxy pages and malformed JSON
fall through to the other mirror; their failure is classified as a protocol
problem, not incorrect credentials. Each fetch has a seven-second deadline.
A slow healthy mirror is allowed to finish before a faster 401/403 is accepted.
If no mirror succeeds, explicit access denials still take effect.

Confirmed projections can keep an existing account visible during an outage,
but never confirm a new login. Login clears cached identity and must receive a
fresh online projection. Background refresh is deferred during credential entry;
older completions cannot overwrite the new identity. Main-process revision checks
cover payload reads and optional overlay initialization. Concurrent login requests
are rejected, not queued with different credentials. Logout cancels a pending
login and has a five-second network deadline before local cookie cleanup.

Transient failures retry within the existing attempt. A failed network projection
does not multiply three more full bootstrap waves. PINs and credentials are never
included in diagnostic messages. Transport failures, protocol errors and actual
access denial are distinct outcomes. Neither UI nor cached access overrides server
authorization.

Verification: executable mirror/payload tests, login-state rendering tests, type
checking and production JS build. Live Electron cookie propagation, native Windows
resume, and cross-service login still need a Windows integration run before release.
Network/server outages remain possible; this does not promise permanent connectivity.
