import unittest
from dataclasses import replace

from modules.sgl_forum_publish import (
    SGLForumPublishConfig,
    SGLForumPublishError,
    SGLForumPublisher,
    validate_forum_target,
)


class SGLForumPublishGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SGLForumPublishConfig(
            enabled=False,
            selenium_url="http://browser:4444/wd/hub",
            root_url="https://forum.majestic-rp.ru/forums/",
            cookie_file="",
        )

    def test_target_is_canonicalized_on_the_configured_forum_only(self) -> None:
        self.assertEqual(
            validate_forum_target(
                "https://forum.majestic-rp.ru/forums/court.42/?page=2", self.config
            ),
            "https://forum.majestic-rp.ru/forums/court.42/",
        )
        for forbidden in (
            "https://example.com/forums/court.42/",
            "https://forum.majestic-rp.ru/threads/claim.1/",
            "http://forum.majestic-rp.ru/forums/court.42/",
        ):
            with self.subTest(forbidden=forbidden):
                with self.assertRaisesRegex(ValueError, "sgl_forum_target_invalid"):
                    validate_forum_target(forbidden, self.config)

    def test_disabled_publisher_never_opens_or_submits_a_browser_session(self) -> None:
        publisher = SGLForumPublisher(self.config)
        with self.assertRaisesRegex(SGLForumPublishError, "sgl_forum_publish_disabled"):
            publisher.publish(
                target_url="https://forum.majestic-rp.ru/forums/court.42/",
                title="Иск",
                body="Проверенный текст",
            )

    def test_enabled_config_keeps_the_same_forum_boundary(self) -> None:
        enabled = replace(self.config, enabled=True)
        self.assertEqual(
            validate_forum_target("https://forum.majestic-rp.ru/forums/court.42/", enabled),
            "https://forum.majestic-rp.ru/forums/court.42/",
        )


if __name__ == "__main__":
    unittest.main()
