# Blackbird 1.3.5-p6 · Windows x64 private preview

## Published release

- Source repository: https://github.com/cdnserver/t-mod
- Feature commit: `bc7075c` (workspaces, account security, settings, observatory idle).
- Final source commit: `e86c2aa45e2e55c553ef5dc06ea2c99482ba92ba`.
- Installer repository: https://github.com/cdnserver/blackbird-releases
- Release: https://github.com/cdnserver/blackbird-releases/releases/tag/blackbird-v1.3.5-p6
- Windows x64 installer: `BLACKBIRD-Private-Setup-1.3.5-p6.exe`.
- Installer size: 123689802 bytes.
- SHA-256: `456fcce8cf993774ba1c248d59e08bbeec73eb6de5770a09556caabccd1e0e23`.

Source changes belong to `t-mod`; the private installer repository stores release
artifacts and release records, not a second copy of the application source.
Comparing its release tags therefore does not show the application source diff.

## Included

- Separate Atlas and Senate workspaces with dedicated navigation and transitions.
- Full-page account security and billing settings; checkout stays in the browser.
- PIN/password login, TOTP, Discord/Telegram codes and one-use recovery codes.
- Own Blackbird observatory idle screen and native background lock-state tracking.
- Horizontal/vertical control bar and three publisher intro styles.
- Refined publisher mark, setup branding and lunar edge rendering.

## Verification and limits

Native Windows build, without Wine or GitHub Actions. Type checking, 106 client
tests and packaged main/preload syntax checks passed. The ASAR metadata and
included feature markers were verified; local and GitHub installer hashes match.
38 account/authentication tests and 64 web-panel tests passed on isolated sources.

The installer is unsigned. A checksum establishes integrity, not an antivirus
verdict or publisher reputation. Private previews currently update manually.

This release did not update or restart production services. Account security
requires the accompanying server schema and a persistent
`TMOD_ACCOUNT_SECURITY_KEY`; account settings may be unavailable on an older
server. Do not enable 2FA before updating the server and supported clients.
Real message delivery and authenticated Windows end-to-end checks remain
deployment checks. See [account security deployment notes](../../docs/blackbird-account-security.md).
