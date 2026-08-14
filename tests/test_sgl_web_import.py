import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.import_sgl_web import SGLWebImportError, _extract_product


class SGLWebImportTests(unittest.TestCase):
    def _archive(self, root: Path, files: dict[str, bytes], *, symlink: str | None = None) -> Path:
        archive = root / "bundle.tar"
        with tarfile.open(archive, "w") as bundle:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
            if symlink:
                info = tarfile.TarInfo(symlink)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                bundle.addfile(info)
        return archive

    def test_extracts_only_complete_sgl_product(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._archive(
                root,
                {
                    "web/sgl/index.html": b"<html></html>",
                    "web/sgl/style.css": b"body{}",
                    "web/sgl/app.js": b'"use strict";',
                    "web/sgl/favicon.svg": b"<svg></svg>",
                },
            )
            product = _extract_product(archive, root / "out")
            self.assertEqual((product / "app.js").read_text(), '"use strict";')

    def test_rejects_file_outside_product_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._archive(root, {"modules/sgl_web.py": b"secret backend"})
            with self.assertRaisesRegex(SGLWebImportError, "границу"):
                _extract_product(archive, root / "out")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self._archive(root, {"web/sgl/../../stolen.txt": b"unsafe"})
            with self.assertRaisesRegex(SGLWebImportError, "Небезопасный путь"):
                _extract_product(archive, root / "out")

    def test_rejects_links_and_missing_product_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linked = self._archive(root, {}, symlink="web/sgl/index.html")
            with self.assertRaisesRegex(SGLWebImportError, "Ссылки запрещены"):
                _extract_product(linked, root / "linked")

            incomplete = self._archive(root, {"web/sgl/index.html": b"<html></html>"})
            with self.assertRaisesRegex(SGLWebImportError, "обязательных файлов"):
                _extract_product(incomplete, root / "incomplete")
