import unittest

from localization import safe_command_description, safe_command_name


class LocalizationFallbackTests(unittest.TestCase):
    def test_missing_command_localization_uses_clean_fallback(self) -> None:
        self.assertEqual(safe_command_name("missing.commands.name", "working_name"), "working_name")
        self.assertEqual(
            safe_command_description("missing.commands.description", "Working description"),
            "Working description",
        )


if __name__ == "__main__":
    unittest.main()
