# BLACKBIRD 1.3.6-beta.6 release checkpoint

## Installer

- Native Windows x64 build; no Wine or GitHub Actions.
- Release: https://github.com/cdnserver/blackbird-releases/releases/tag/blackbird-v1.3.6-beta.6
- Client source snapshot: `9293084` (before integration of two fellowship labels from current main).
- Installer: `BLACKBIRD-Private-Setup-1.3.6-beta.6.exe`, 123769193 bytes.
- SHA-256: `459d4d5e9382d99eb551da2ad5564db61be25e27ca588b430cc9aed8ee40c214`.
- Local copy: `~/Downloads/Blackbird-1.3.6-beta.6/`.
- Windows build output: `C:\Users\Admin\Desktop\blackbird-preview-20261008-1945\desktop\release-blackbird`.
- Standard NSIS installer. Experimental custom installer is source-only; not shipped here.
- 251 local client tests including two loopback API integration tests. Windows: 248 passed, three intentionally skipped (two loopback tests and a non-Windows packaging refusal test).

## Backend integration

`36c302a` contains social APIs, attachment handling, link cards, consensus attendance and session protection, integrated with the current account/admission changes. `894eb32` aligns the durable consensus scenario's senator fixture with current fellowship access rules. Real production access checks were not relaxed.

Updating source is not enough: rebuild/recreate the affected backend containers. Restarting old images alone does not load new source. No production containers were restarted by this release task.

## Auto-update activation: intentionally pending

The owner explicitly requested activation **after their backend restart**. Do not advance the feed before that confirmation and a health check.

The authenticated feed is separate from GitHub Releases. Its current persistent manifest was verified as `1.3.6-beta.5` on 2026-10-08. An unauthenticated HTTP 401 is expected; the installed client forwards its account-session cookie to this first-party feed.

After the owner confirms the backend update/restart:

1. Verify backend health and that the running image includes the pushed backend changes.
2. Recheck the staged installer hash above and the SHA-512/size from the newly built `latest.yml`.
3. Copy the beta.6 EXE and blockmap into the configured `TMOD_BLACKBIRD_UPDATE_DIR` (default Windows persistent directory `C:\Users\Admin\Documents\SGLDiscordBot\blackbird-updates`). Keep previous installers for recovery.
4. Replace `latest.yml` last, atomically, after both assets are fully present. Never expose a manifest before its installer is available.
5. Verify the authenticated feed serves beta.6 and that the client detects the version. A GitHub release alone does not activate this feed.

No update signing key, GitHub token, account cookie or production credentials belong in this document or the source archive.
