# Blackbird 1.3.5-p5 · Windows x64 private preview

- Seven-second publisher scene with real preloading, a black pause, then the lunar launch screen.
- One-time per-account setup, preferred name, accessibility and notification preferences.
- Revised Atlas / Senate hub, without redundant branding or a left decorative rail.
- Full-page settings: account, appearance, lock, notifications, Atlas Overlay, updates and connection.
- Discord profile avatar and account menu; durable avatar projection for the standalone backend.
- Hardened login transaction, transient failure retries and fresh server session confirmation.
- Adaptive custom / Windows notifications, a searchable notification centre and read acknowledgements.

Private Blackbird package version is 1.3.5-p5. The public T-Mod package manifest and
release channel are unchanged. Windows x64 installer only, native Windows build;
no GitHub Actions and no Wine. Private previews currently update manually and
require the existing owner allowlist. The installer is unsigned; a checksum
verifies integrity, not publisher reputation or an antivirus verdict.

Server avatar projection requires deploying the accompanying backend commit
and a Discord member snapshot refresh. This release operation does not restart
or update the running server services. Browser previews are not authenticated
Windows end-to-end tests.
