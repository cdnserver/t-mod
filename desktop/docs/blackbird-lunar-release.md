# BLACKBIRD Lunar Preview · core 1.3.5-p4

Private preview of the Blackbird Client for Technologies of the Fellowship.
The preview retains the shared core version for compatibility with the existing
server minimum-version policy; its releases are separate from T-Mod Desktop.

- Large, side-lit lunar sphere from the NASA LRO mosaic; a custom quiet starfield, no video.
- The startup screen remains until a keyboard key is pressed; pointer movement does not dismiss it.
- Existing Blackbird wordmark, bird emblem and startup sound are preserved.
- Native account/PIN entry and the Atlas/Senate hub, notifications and overlay settings.
- Separate application identity, user-data partition and Windows installation from T-Mod Desktop.

## Build without Actions or Wine

On a native Windows x64 machine with Node.js and pnpm installed, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_blackbird_windows.ps1
```

The script stops at the first failing step and builds an NSIS installer under
`release-blackbird`. It never deploys/restarts the backend or publishes automatically.
This unsigned preview may trigger Windows SmartScreen. A successful build is not
a guarantee of a clean scan or publisher reputation; verify the release checksum.

The closed preview does not receive T-Mod Desktop's public auto-update feed.
Account eligibility is enforced by the existing private-edition backend gate.

Lunar credits and source details: `resources/blackbird/THIRD_PARTY_NOTICES.txt`.
