import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsConsensusLauncherTests(unittest.TestCase):
    def test_standard_launcher_configures_and_checks_consensus_panel(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn("configure_direct_web_windows.ps1", launcher)
        self.assertNotIn("configure_cloudflare_tunnel_windows.ps1", launcher)
        self.assertIn("docker compose up -d --remove-orphans", launcher)
        self.assertNotIn("docker stop minecraft", launcher)
        self.assertNotIn("docker rm minecraft", launcher)
        self.assertIn("http://127.0.0.1:8787/api/health", launcher)
        self.assertIn("/api/health?ready=1", launcher)
        self.assertIn("$r.discord_ready", launcher)
        self.assertIn("https://tvr.lat", launcher)
        self.assertNotIn("http://SERVER_LAN_IP:8787", launcher)

    def test_standard_launcher_generates_minecraft_secrets_with_valid_powershell(
        self,
    ) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        secrets_script = (ROOT / "ensure_minecraft_secrets_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("$bytes ^|", launcher)
        self.assertIn("ensure_minecraft_secrets_windows.ps1", launcher)
        self.assertIn("-RconPath", launcher)
        self.assertIn("-SupervisorPath", launcher)
        self.assertIn("MINECRAFT_RCON_SECRET", launcher)
        self.assertIn("MINECRAFT_SUPERVISOR_SECRET", launcher)
        self.assertIn("[BitConverter]::ToString($bytes)", secrets_script)
        self.assertIn("$item.PSIsContainer", secrets_script)
        self.assertIn("Replacing an empty or invalid", secrets_script)
        self.assertIn("Move-Item", secrets_script)
        self.assertIn("ChangedMarkerPath", secrets_script)
        self.assertIn("MINECRAFT_SECRETS_CHANGED", launcher)
        self.assertIn(
            "--force-recreate minecraft minecraft-supervisor",
            launcher,
        )
        self.assertIn("docker compose logs --no-color", launcher)

    def test_cloudflare_configuration_preserves_and_validates_config(self) -> None:
        script = (ROOT / "configure_cloudflare_tunnel_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('PublicHostName = "tmod.rundans.lat"', script)
        self.assertIn('OriginUrl = "http://127.0.0.1:8787"', script)
        self.assertIn("tmod-backup-", script)
        self.assertIn("ingress validate", script)
        self.assertIn("Restart-Service", script)
        self.assertIn('$routeArguments.Add("dns")', script)
        self.assertIn("Other cloudflared ingress routes were preserved", script)
        self.assertIn("Start-Process", script)
        self.assertIn("-Verb RunAs", script)
        self.assertNotIn("PrivateKey", script)

    def test_direct_network_configuration_enforces_container_settings(self) -> None:
        script = (ROOT / "configure_direct_web_windows.ps1").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        caddyfile = (ROOT / "Caddyfile").read_text(encoding="utf-8")
        example = (ROOT / ".env.persistent.example").read_text(encoding="utf-8")

        self.assertIn("CONSENSUS_WEB_PUBLIC_URL", script)
        self.assertIn("T-Mod Direct HTTPS", script)
        self.assertIn("T-Mod ACME HTTP", script)
        self.assertNotIn("PrivateKey", script)
        self.assertIn('"127.0.0.1:8787:8787"', compose)
        self.assertIn('"80:80"', compose)
        self.assertIn('"443:443"', compose)
        self.assertIn('"443:443/udp"', compose)
        self.assertIn('"./web/consensus:/srv/tmod:ro"', compose)
        self.assertIn("protocols h1 h2 h3", caddyfile)
        self.assertIn("handle_path /assets/*", caddyfile)
        self.assertIn("root * /srv/tmod", caddyfile)
        self.assertIn("tmod-caddy", compose)
        self.assertIn("reverse_proxy tmod-discord-bot:8787", caddyfile)
        self.assertIn("response_header_timeout 15s", caddyfile)
        self.assertIn("Permissions-Policy", caddyfile)
        self.assertIn("condition: service_started", compose)
        self.assertIn("start_period: 90s", compose)
        self.assertNotIn("cloudflare", caddyfile.lower())
        self.assertIn(
            "CONSENSUS_WEB_PUBLIC_URL=https://consensus.tvr.lat",
            example,
        )
        self.assertIn("REACTOR_WEB_PUBLIC_URL=https://reactor.tvr.lat", example)
        self.assertIn("PORTAL_WEB_PUBLIC_URL=https://tvr.lat", example)
        self.assertIn("ATLAS_WEB_PUBLIC_URL=https://atlas.tvr.lat", example)
        self.assertIn("OVR_WEB_PUBLIC_URL=https://ovr.tvr.lat", example)
        self.assertIn("reactor.tvr.lat", caddyfile)
        self.assertIn("consensus.tvr.lat", caddyfile)
        self.assertIn("zigmund.tvr.lat", caddyfile)
        self.assertIn("atlas.tvr.lat", caddyfile)
        self.assertIn("ovr.tvr.lat", caddyfile)
        self.assertIn("OVR_WEB_PUBLIC_URL", script)
        self.assertIn("atlas-qdrant", compose)
        self.assertIn("qdrant/qdrant:v1.16.2", compose)
        self.assertIn('"atlas-qdrant-data:/qdrant/storage"', compose)
        self.assertNotIn("SGLDiscordBot/atlas/qdrant:/qdrant/storage", compose)
        self.assertNotIn("mc.tvr.lat", caddyfile)
        self.assertIn("T-Mod Direct HTTPS HTTP3", script)
        self.assertIn("-Protocol UDP", script)
        self.assertIn('"25565:25565/tcp"', compose)
        self.assertIn('MOTD: "T-Mod • mc.tvr.lat • Товарищество"', compose)
        self.assertIn("MINECRAFT_PUBLIC_ADDRESS=mc.tvr.lat", example)
        self.assertNotIn('RCON_PASSWORD: "', compose)
        self.assertIn("RCON_PASSWORD_FILE", compose)
        self.assertIn('OVERRIDE_SERVER_PROPERTIES: "true"', compose)
        self.assertIn("minecraft-supervisor", compose)
        self.assertIn("minecraft-supervisor-token.txt", compose)
        self.assertIn('"/var/run/docker.sock:/var/run/docker.sock"', compose)
        bot_service = compose.split("  tmod-caddy:", 1)[0]
        self.assertNotIn("/var/run/docker.sock", bot_service)
        self.assertNotIn("minecraft-supervisor:\n        condition", bot_service)
        self.assertIn("/api/health", bot_service)
        self.assertIn('user: "0:0"', bot_service)

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER tmod", dockerfile)
        self.assertIn("COPY --chown=tmod:tmod . .", dockerfile)

    def test_web_health_server_starts_before_discord_ready(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        setup_hook = source.index("async def setup_hook")
        early_web = source.index("await ensure_consensus_web_server(self)", setup_hook)
        command_sync = source.index("self.tree.sync", setup_hook)
        self.assertLess(early_web, command_sync)

    def test_discord_commands_use_one_global_scope_without_guild_duplicates(self) -> None:
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        setup_hook = source[source.index("async def setup_hook") : source.index("intents =")]

        self.assertNotIn("copy_global_to", setup_hook)
        self.assertIn("self.tree.clear_commands(guild=guild)", setup_hook)
        self.assertIn("await self.tree.sync(guild=guild)", setup_hook)
        self.assertIn("global_synced = await self.tree.sync()", setup_hook)

    def test_standard_launcher_verifies_minecraft_rcon_after_startup(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")

        self.assertIn('call :stage "11" "Minecraft RCON verification"', launcher)
        self.assertIn("call :check_minecraft_rcon", launcher)
        self.assertIn("docker exec minecraft rcon-cli list", launcher)
        self.assertIn("Minecraft RCON secret accepted", launcher)
        self.assertIn("server.properties are not synchronized", launcher)

    def test_transactional_updater_tests_backs_up_and_rolls_back(self) -> None:
        desktop = (ROOT / "start_tmod_windows.bat").read_text(encoding="utf-8")
        runtime = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        updater = (ROOT / "safe_update_windows.ps1").read_text(encoding="utf-8")
        guard = (ROOT / "launch_tmod_guarded_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("safe_update_windows.ps1", desktop)
        self.assertIn("launch_tmod_guarded_windows.ps1", desktop)
        self.assertIn("UpdateTimeoutSeconds", guard)
        self.assertIn("FallbackTimeoutSeconds", guard)
        self.assertIn("taskkill.exe", guard)
        self.assertIn("Starting the installed release", guard)
        self.assertIn('TMOD_SKIP_BUILD = "0"', guard)
        self.assertIn("TMOD_SKIP_BUILD", runtime)
        self.assertIn("TMOD_TRANSACTIONAL_UPDATE", runtime)
        self.assertIn("tmod_db_guard.py backup --kind pre-update", updater)
        self.assertIn("worktree add --detach", updater)
        self.assertIn("unittest discover", updater)
        self.assertIn("db-validation-", updater)
        self.assertIn("storage.init_db()", updater)
        self.assertIn("PRAGMA integrity_check", updater)
        self.assertIn("caddy validate", updater)
        self.assertIn("merge --ff-only", updater)
        self.assertIn("credential.interactive=never", updater)
        self.assertIn("http.lowSpeedTime=20", updater)
        self.assertIn("GitTimeoutSeconds", updater)
        self.assertIn("BackupTimeoutSeconds", updater)
        self.assertIn("WaitForExit", updater)
        self.assertIn("taskkill.exe", updater)
        self.assertIn("--timeout-seconds $BackupTimeoutSeconds", updater)
        self.assertIn("unrecognized arguments:.*timeout-seconds", updater)
        self.assertIn("-SkipBuild ([bool]$RollbackImage)", updater)
        self.assertIn("rollback-", updater)
        self.assertIn("Restore-CodeRevision", updater)
        self.assertIn('State "rolled_back"', updater)
        self.assertIn('State "blocked_local_changes"', updater)
        self.assertNotIn("stash push", updater)
        self.assertNotIn("git reset --hard", updater)
        self.assertNotIn("stash push", desktop)
        self.assertNotIn("checkout -B", desktop)

    def test_git_watcher_only_runs_safe_update_for_a_new_clean_release(self) -> None:
        desktop = (ROOT / "start_tmod_windows.bat").read_text(encoding="utf-8")
        runtime = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        desktop_installer = (ROOT / "install_desktop_launcher_windows.bat").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "configure_auto_update_windows.ps1").read_text(
            encoding="utf-8"
        )
        watcher = (ROOT / "watch_tmod_updates_windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("configure_auto_update_windows.ps1", runtime)
        self.assertIn("configure_auto_update_windows.ps1", desktop_installer)
        self.assertIn("Desktop launcher refreshed", runtime)
        self.assertIn("credential.interactive=never", watcher)
        self.assertIn("-IntervalMinutes 2", runtime)
        self.assertIn("schtasks.exe", installer)
        self.assertIn("/SC MINUTE", installer)
        self.assertIn("/MO $IntervalMinutes", installer)
        self.assertIn("ls-remote --exit-code", watcher)
        self.assertIn("status --porcelain", watcher)
        self.assertIn("safe_update_windows.ps1", watcher)
        self.assertIn("launch_tmod_guarded_windows.ps1", watcher)
        self.assertIn('state -eq "rolled_back"', watcher)
        self.assertIn("Local\\TModAutoUpdateWatcher", watcher)
        self.assertIn("Get-RemoteCommitBounded", watcher)
        self.assertIn("GitTimeoutSeconds", watcher)
        self.assertIn("WaitForExit", watcher)
        self.assertIn("taskkill.exe", watcher)
        self.assertNotIn("git reset --hard", watcher)
        self.assertNotIn("stash push", watcher)

    def test_retired_browser_stream_is_removed_during_startup(self) -> None:
        launcher = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        admin_backend = (ROOT / "modules/consensus_admin_web.py").read_text(
            encoding="utf-8"
        )
        admin_html = (ROOT / "web/consensus/admin.html").read_text(encoding="utf-8")
        admin_js = (ROOT / "web/consensus/admin.js").read_text(encoding="utf-8")

        self.assertIn("docker stop discord-browser-stream", launcher)
        self.assertIn("docker rm discord-browser-stream", launcher)
        self.assertIn("docker image rm tmod-browser-stream:latest", launcher)
        self.assertIn('del /Q "%PERSISTENT_DIR%\\browser-stream.env"', launcher)
        self.assertIn('rmdir /S /Q "%PERSISTENT_DIR%\\browser-stream"', launcher)
        self.assertIn("--remove-orphans", launcher)
        self.assertNotIn("COMPOSE_PROFILES=browser-stream", launcher)
        self.assertNotIn("configure_browser_stream_windows.ps1", launcher)
        self.assertNotIn("  discord-browser-stream:", compose)
        self.assertNotIn("setup_browser_stream", main)
        self.assertNotIn("browser_stream", admin_backend)
        self.assertNotIn("browser-stream-form", admin_html)
        self.assertNotIn('data-media-target="browser"', admin_html)
        self.assertNotIn("data.browser_stream", admin_js)
        self.assertFalse((ROOT / "modules/browser_stream.py").exists())
        self.assertFalse((ROOT / "discord-browser-stream").exists())


if __name__ == "__main__":
    unittest.main()
