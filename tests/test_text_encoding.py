import unittest

from modules.text_encoding import repair_utf8_mojibake


class TextEncodingTests(unittest.TestCase):
    def test_repairs_windows_mojibake_in_bot_status(self) -> None:
        broken = "Товарищество - светлый круг".encode("utf-8").decode("latin1")
        self.assertEqual(
            repair_utf8_mojibake(broken),
            "Товарищество - светлый круг",
        )

    def test_repairs_multiple_windows_transcoding_layers(self) -> None:
        expected = "Товарищество - светлый круг"
        broken = expected
        for _ in range(2):
            broken = broken.encode("utf-8").decode("latin1")
        self.assertEqual(repair_utf8_mojibake(broken), expected)

    def test_preserves_valid_russian_and_plain_text(self) -> None:
        self.assertEqual(repair_utf8_mojibake("Светлый круг"), "Светлый круг")
        self.assertEqual(repair_utf8_mojibake("T-Mod Atlas"), "T-Mod Atlas")


if __name__ == "__main__":
    unittest.main()
