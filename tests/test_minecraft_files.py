import tempfile
import unittest
import zipfile
from pathlib import Path

from modules import minecraft_files as files


class MinecraftFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "minecraft"
        self.root.mkdir()
        self.config = files.MinecraftFilesConfig(
            root=self.root,
            max_upload_bytes=8 * 1024 * 1024,
            max_edit_bytes=1024 * 1024,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_listing_and_path_sandbox_never_escape_server_root(self) -> None:
        (self.root / "plugins").mkdir()
        (self.root / "server.properties").write_text("motd=T-Mod\n", encoding="utf-8")

        listing = files.minecraft_list_directory(config=self.config)
        self.assertEqual(
            [item["name"] for item in listing["entries"]],
            ["plugins", "server.properties"],
        )
        with self.assertRaisesRegex(files.MinecraftFilesError, "Некорректный путь"):
            files.minecraft_list_directory("../outside", config=self.config)
        with self.assertRaisesRegex(files.MinecraftFilesError, "Некорректное имя"):
            files.minecraft_make_directory("", "CON", config=self.config)
        with self.assertRaisesRegex(files.MinecraftFilesError, "Некорректное имя"):
            files.minecraft_make_directory("", "bad:name", config=self.config)

        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        try:
            (self.root / "escape").symlink_to(outside, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(files.MinecraftFilesError, "Символические ссылки"):
            files.minecraft_list_directory("escape", config=self.config)

    def test_editor_redacts_and_preserves_rcon_secret_with_conflict_control(
        self,
    ) -> None:
        properties = self.root / "server.properties"
        properties.write_text(
            "motd=Old\nrcon.password=never-show-this\nmax-players=20\n",
            encoding="utf-8",
        )

        opened = files.minecraft_read_text("server.properties", config=self.config)
        self.assertNotIn("never-show-this", opened["content"])
        self.assertIn("rcon.password=<скрыто T-Mod>", opened["content"])
        saved = files.minecraft_write_text(
            "server.properties",
            opened["content"].replace("motd=Old", "motd=New"),
            expected_etag=opened["etag"],
            config=self.config,
        )
        raw = properties.read_text(encoding="utf-8")
        self.assertIn("motd=New", raw)
        self.assertIn("rcon.password=never-show-this", raw)
        self.assertNotIn("never-show-this", saved["content"])
        with self.assertRaisesRegex(files.MinecraftFilesError, "уже изменился"):
            files.minecraft_write_text(
                "server.properties",
                "motd=Conflict\n",
                expected_etag=opened["etag"],
                config=self.config,
            )
        revisions = list((self.root / ".tmod-backups" / "edits").iterdir())
        self.assertTrue(revisions)

    def test_plugin_upload_toggle_trash_and_restore(self) -> None:
        plugins = self.root / "plugins"
        plugins.mkdir()
        temporary = files.minecraft_prepare_upload(config=self.config)
        temporary.write_bytes(b"plugin-jar")
        uploaded = files.minecraft_finish_upload(
            "plugins",
            "Example.jar",
            temporary,
            len(b"plugin-jar"),
            config=self.config,
        )
        self.assertEqual(uploaded["path"], "plugins/Example.jar")
        self.assertTrue(files.minecraft_list_plugins(config=self.config)[0]["enabled"])

        disabled = files.minecraft_set_plugin_state(
            "plugins/Example.jar",
            False,
            config=self.config,
        )
        self.assertEqual(disabled["path"], "plugins/Example.jar.disabled")
        trashed = files.minecraft_trash_path(disabled["path"], config=self.config)
        self.assertFalse((plugins / "Example.jar.disabled").exists())
        restored = files.minecraft_restore_trash(trashed["id"], config=self.config)
        self.assertEqual(restored["path"], "plugins/Example.jar.disabled")

        wrong_place = files.minecraft_prepare_upload(config=self.config)
        wrong_place.write_bytes(b"jar")
        with self.assertRaisesRegex(files.MinecraftFilesError, "папку plugins"):
            files.minecraft_finish_upload(
                "",
                "Wrong.jar",
                wrong_place,
                3,
                config=self.config,
            )
        wrong_place.unlink(missing_ok=True)

        mismatched = files.minecraft_prepare_upload(config=self.config)
        mismatched.write_bytes(b"four")
        with self.assertRaisesRegex(files.MinecraftFilesError, "Размер загруженного"):
            files.minecraft_finish_upload(
                "plugins",
                "Mismatch.jar",
                mismatched,
                3,
                config=self.config,
            )
        mismatched.unlink(missing_ok=True)

    def test_backup_is_downloadable_but_does_not_disclose_rcon_secret(self) -> None:
        (self.root / "world").mkdir()
        (self.root / "world" / "level.dat").write_bytes(b"world-data")
        properties = self.root / "server.properties"
        properties.write_text(
            "motd=Backup\nrcon.password=super-secret\n",
            encoding="utf-8",
        )
        backup = files.minecraft_create_backup("before-update", config=self.config)
        backup_path = files.minecraft_backup_path(backup["id"], config=self.config)
        with zipfile.ZipFile(backup_path) as archive:
            archived_properties = archive.read("server.properties").decode("utf-8")
            self.assertEqual(archive.read("world/level.dat"), b"world-data")
        self.assertNotIn("super-secret", archived_properties)
        self.assertIn("<скрыто T-Mod>", archived_properties)

        properties.write_text(
            "motd=Changed\nrcon.password=current-secret\n",
            encoding="utf-8",
        )
        restored = files.minecraft_restore_backup(backup["id"], config=self.config)
        self.assertGreaterEqual(restored["restored_files"], 2)
        current = properties.read_text(encoding="utf-8")
        self.assertIn("motd=Backup", current)
        self.assertIn("rcon.password=current-secret", current)

        properties.unlink()
        files.minecraft_restore_backup(backup["id"], config=self.config)
        restored_without_secret = properties.read_text(encoding="utf-8")
        self.assertIn("motd=Backup", restored_without_secret)
        self.assertNotIn("rcon.password", restored_without_secret)
        self.assertNotIn("<скрыто T-Mod>", restored_without_secret)

    def test_log_tail_and_storage_overview(self) -> None:
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "latest.log").write_text(
            "\n".join(f"line-{index}" for index in range(80)),
            encoding="utf-8",
        )
        tail = files.minecraft_tail_log(20, config=self.config)
        self.assertTrue(tail["available"])
        self.assertEqual(tail["lines"][0], "line-60")
        self.assertEqual(tail["lines"][-1], "line-79")


if __name__ == "__main__":
    unittest.main()
