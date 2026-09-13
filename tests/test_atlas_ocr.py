import subprocess
import unittest
from unittest.mock import patch

from modules.atlas_ocr import (
    AtlasOcrConfig,
    AtlasOcrError,
    AtlasOcrUnavailable,
    atlas_ocr_attachment,
)


class AtlasOcrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AtlasOcrConfig(
            max_input_bytes=1024 * 1024,
            max_output_chars=500,
            timeout_seconds=20,
            max_pdf_pages=3,
            languages="rus+eng",
        )

    def test_image_uses_local_tesseract_and_returns_bounded_text(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tesseract"], 0, stdout="Статья 16. Проверенный текст акта.".encode(), stderr=b""
        )
        with patch("modules.atlas_ocr._run", return_value=completed) as run:
            result = atlas_ocr_attachment(
                b"\x89PNG\r\n\x1a\nimage",
                mime_type="image/png",
                filename="court-act.png",
                config=self.config,
            )

        self.assertEqual(result.engine, "tesseract")
        self.assertEqual(result.pages, 1)
        self.assertIn("Статья 16", result.text)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["tesseract", command[1], "stdout"])
        self.assertIn("rus+eng", command)

    def test_pdf_prefers_existing_text_layer_without_ocr(self) -> None:
        completed = subprocess.CompletedProcess(
            ["pdftotext"], 0, stdout="Глава 16. Текст из исходного PDF.".encode(), stderr=b""
        )
        with patch("modules.atlas_ocr._run", return_value=completed) as run:
            result = atlas_ocr_attachment(
                b"%PDF-1.7\nsource",
                mime_type="application/pdf",
                filename="code.pdf",
                config=self.config,
            )

        self.assertEqual(result.engine, "pdftotext")
        self.assertIn("Глава 16", result.text)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "pdftotext")

    def test_unsupported_attachment_never_starts_a_process(self) -> None:
        with patch("modules.atlas_ocr._run") as run:
            with self.assertRaisesRegex(AtlasOcrUnavailable, "изображения и PDF"):
                atlas_ocr_attachment(
                    b"PK\x03\x04archive",
                    mime_type="application/zip",
                    filename="files.zip",
                    config=self.config,
                )
        run.assert_not_called()

    def test_timeout_is_retryable_and_does_not_hide_the_source(self) -> None:
        with patch(
            "modules.atlas_ocr.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["tesseract"], 20),
        ):
            with self.assertRaises(AtlasOcrError) as captured:
                atlas_ocr_attachment(
                    b"\xff\xd8\xffimage",
                    mime_type="image/jpeg",
                    filename="act.jpg",
                    config=self.config,
                )
        self.assertEqual(captured.exception.code, "atlas_ocr_timeout")
        self.assertTrue(captured.exception.retryable)

    def test_result_is_capped_before_it_can_enter_review(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tesseract"], 0, stdout=("А" * 900).encode(), stderr=b""
        )
        with patch("modules.atlas_ocr._run", return_value=completed):
            result = atlas_ocr_attachment(
                b"\x89PNG\r\n\x1a\nimage",
                mime_type="image/png",
                config=self.config,
            )
        self.assertLessEqual(len(result.text), 560)
        self.assertIn("безопасным лимитом", result.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
