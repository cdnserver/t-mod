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
            any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body),
            "storage.py must not grow repository implementations again",
        )

    def test_persistence_modules_never_depend_on_legacy_storage_facade(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "persistence").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(alias.name == "storage" for alias in node.names):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and node.module == "storage":
                    offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_feature_modules_use_bounded_repositories(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "modules").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(alias.name == "storage" for alias in node.names):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and node.module == "storage":
                    offenders.append(path.name)
        self.assertEqual(offenders, [])


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
            name: len((ROOT / "modules" / name).read_text(encoding="utf-8").splitlines())
            for name in self.EXPECTED_MODULES
            if len((ROOT / "modules" / name).read_text(encoding="utf-8").splitlines()) > 1000
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
                if isinstance(node, ast.Import) and any(alias.name == "modules.tvrs" for alias in node.names):
                    offenders.append(name)
        self.assertEqual(offenders, [])


class SglArchiveBoundaryTests(unittest.TestCase):
    def test_archive_capture_and_restore_are_separate_bounded_modules(self) -> None:
        names = ("sgl_archive.py", "sgl_archive_restore.py")
        sizes = {
            name: len((ROOT / "modules" / name).read_text(encoding="utf-8").splitlines())
            for name in names
        }
        self.assertEqual(
            {name: size for name, size in sizes.items() if size > 1000},
            {},
        )


if __name__ == "__main__":
    unittest.main()
