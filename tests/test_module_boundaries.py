import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PersistenceBoundaryTests(unittest.TestCase):
    def test_storage_is_a_small_implementation_free_compatibility_facade(self) -> None:
        path = ROOT / "storage.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        self.assertLessEqual(len(source.splitlines()), 100)
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in tree.body
            ),
            "storage.py must not grow repository implementations again",
        )

    def test_persistence_modules_never_depend_on_legacy_storage_facade(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "persistence").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "storage" for alias in node.names
                ):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and node.module == "storage":
                    offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_runtime_xml_dependency_uses_safe_parser_defaults(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("lxml>=6.1.1,<7", requirements)

    def test_feature_modules_use_bounded_repositories(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "modules").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "storage" for alias in node.names
                ):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and node.module == "storage":
                    offenders.append(path.name)
        self.assertEqual(offenders, [])


class AsyncIoBoundaryTests(unittest.TestCase):
    def test_repository_calls_never_block_async_discord_handlers(self) -> None:
        """SQLite/file repositories must run outside the Discord event loop."""

        offenders: list[str] = []
        repository_aliases = {"storage", "_storage", "_tvrs_storage"}
        worker_helpers = {"to_thread", "run_blocking_cancellation_safe"}
        for path in sorted((ROOT / "modules").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node

            for function in ast.walk(tree):
                if not isinstance(function, ast.AsyncFunctionDef):
                    continue
                for call in ast.walk(function):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id in repository_aliases
                    ):
                        continue
                    current: ast.AST | None = call
                    protected = False
                    while current is not None and current is not function:
                        current = parents.get(current)
                        if not isinstance(current, ast.Call):
                            continue
                        target = current.func
                        helper_name = (
                            target.attr
                            if isinstance(target, ast.Attribute)
                            else target.id
                            if isinstance(target, ast.Name)
                            else ""
                        )
                        if helper_name in worker_helpers:
                            protected = True
                            break
                    if not protected:
                        offenders.append(
                            f"{path.name}:{call.lineno}:{call.func.value.id}."
                            f"{call.func.attr}"
                        )
        self.assertEqual(
            offenders,
            [],
            "repository call can block the async event loop",
        )

class TvrsBoundaryTests(unittest.TestCase):
    EXPECTED_MODULES = {
        "tvrs_presentation.py",
        "tvrs_hub_views.py",
        "tvrs_consensus_views.py",
        "tvrs_discussion.py",
        "tvrs_control.py",
        "tvrs_decision.py",
        "tvrs_recovery.py",
        "tvrs_setup.py",
    }

    def test_tvrs_is_a_small_implementation_free_compatibility_facade(self) -> None:
        path = ROOT / "modules" / "tvrs.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        self.assertLessEqual(len(source.splitlines()), 100)
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in tree.body
            ),
            "modules/tvrs.py must remain a facade",
        )

    def test_tvrs_implementation_is_split_into_stable_responsibilities(self) -> None:
        actual = {
            path.name
            for path in (ROOT / "modules").glob("tvrs_*.py")
            if path.name in self.EXPECTED_MODULES
        }
        self.assertEqual(actual, self.EXPECTED_MODULES)

        oversized = {
            name: len(
                (ROOT / "modules" / name).read_text(encoding="utf-8").splitlines()
            )
            for name in self.EXPECTED_MODULES
            if len((ROOT / "modules" / name).read_text(encoding="utf-8").splitlines())
            > 1000
        }
        self.assertEqual(oversized, {})

    def test_tvrs_modules_do_not_import_the_compatibility_facade(self) -> None:
        offenders: list[str] = []
        for name in self.EXPECTED_MODULES:
            path = ROOT / "modules" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "modules.tvrs":
                    offenders.append(name)
                if isinstance(node, ast.Import) and any(
                    alias.name == "modules.tvrs" for alias in node.names
                ):
                    offenders.append(name)
        self.assertEqual(offenders, [])


class SglArchiveBoundaryTests(unittest.TestCase):
    def test_archive_capture_and_restore_are_separate_bounded_modules(self) -> None:
        names = ("sgl_archive.py", "sgl_archive_restore.py")
        sizes = {
            name: len(
                (ROOT / "modules" / name).read_text(encoding="utf-8").splitlines()
            )
            for name in names
        }
        self.assertEqual(
            {name: size for name, size in sizes.items() if size > 1000},
            {},
        )


class MusicBoundaryTests(unittest.TestCase):
    EXPECTED_MODULES = {
        "music_audio.py",
        "music_config.py",
        "music_domain.py",
        "music_providers.py",
        "music_progress.py",
        "music_public_panel.py",
        "music_public_views.py",
        "music_runtime.py",
        "music_runtime_errors.py",
        "music_setup.py",
        "music_speech.py",
        "music_status.py",
        "music_stt_audio.py",
        "music_stt_orchestrator.py",
        "music_views.py",
        "music_voice_diagnostics.py",
        "music_voice_setup.py",
    }

    def test_music_is_split_into_bounded_responsibilities(self) -> None:
        actual = {path.name for path in (ROOT / "modules").glob("music_*.py")}
        self.assertEqual(actual, self.EXPECTED_MODULES)
        limits = {
            "music_audio.py": 500,
            "music_config.py": 200,
            "music_domain.py": 350,
            "music_providers.py": 350,
            "music_progress.py": 100,
            "music_public_panel.py": 500,
            "music_public_views.py": 400,
            "music_runtime.py": 900,
            "music_runtime_errors.py": 50,
            "music_setup.py": 150,
            "music_speech.py": 350,
            "music_status.py": 200,
            "music_stt_audio.py": 220,
            "music_stt_orchestrator.py": 350,
            "music_views.py": 500,
            "music_voice_diagnostics.py": 100,
            "music_voice_setup.py": 100,
        }
        oversized = {
            name: len(
                (ROOT / "modules" / name).read_text(encoding="utf-8").splitlines()
            )
            for name, limit in limits.items()
            if len((ROOT / "modules" / name).read_text(encoding="utf-8").splitlines())
            > limit
        }
        self.assertEqual(oversized, {})

    def test_music_domain_and_providers_do_not_depend_on_discord(self) -> None:
        offenders: list[str] = []
        for name in (
            "music_config.py",
            "music_domain.py",
            "music_providers.py",
            "music_progress.py",
            "music_speech.py",
            "music_stt_audio.py",
            "music_stt_orchestrator.py",
        ):
            path = ROOT / "modules" / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    alias.name == "discord" or alias.name.startswith("discord.")
                    for alias in node.names
                ):
                    offenders.append(name)
                if isinstance(node, ast.ImportFrom) and str(node.module).startswith(
                    "discord"
                ):
                    offenders.append(name)
        self.assertEqual(offenders, [])


class VoiceControlBoundaryTests(unittest.TestCase):
    EXPECTED_MODULES = {
        "voice_control_audio.py",
        "voice_control_config.py",
        "voice_control_domain.py",
        "voice_control_local.py",
        "voice_control_service.py",
    }

    def test_voice_control_is_a_standalone_reusable_platform(self) -> None:
        actual = {path.name for path in (ROOT / "modules").glob("voice_control_*.py")}
        self.assertEqual(actual, self.EXPECTED_MODULES)
        for name in self.EXPECTED_MODULES:
            source = (ROOT / "modules" / name).read_text(encoding="utf-8")
            self.assertNotIn("import discord", source)
            self.assertNotIn("from discord", source)
            self.assertLessEqual(len(source.splitlines()), 400)


if __name__ == "__main__":
    unittest.main()
