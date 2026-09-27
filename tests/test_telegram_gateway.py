from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from modules import telegram_gateway
from modules.telegram_gateway import _TelegramPanelApi


class TelegramPanelApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_menu_is_inline_only(self) -> None:
        self.assertIn("inline_keyboard", telegram_gateway._MAIN_KEYBOARD)
        self.assertNotIn("keyboard", telegram_gateway._MAIN_KEYBOARD)
        buttons = [
            button
            for row in telegram_gateway._MAIN_KEYBOARD["inline_keyboard"]
            for button in row
        ]
        self.assertTrue(buttons)
        self.assertTrue(all("callback_data" in button or "url" in button for button in buttons))
        self.assertIn("menu:loginpin", {button.get("callback_data") for button in buttons})

    async def test_login_code_message_has_inline_regenerate_action(self) -> None:
        api = AsyncMock()
        api.send_temporary = AsyncMock()
        user = {"id": 900}

        with (
            patch.object(telegram_gateway, "_GUILD_ID", 77),
            patch.object(telegram_gateway.telegram_storage, "get_link_by_telegram", return_value={"discord_user_id": 42}),
            patch.object(telegram_gateway.auth_storage, "get_web_credential", return_value=object()),
            patch.object(telegram_gateway.ban_storage, "is_globally_banned", return_value=False),
            patch.object(telegram_gateway.telegram_storage, "create_telegram_login_code", return_value={"code": "12345678", "ttl_seconds": 300}),
            patch.object(telegram_gateway, "emit_global_event"),
        ):
            await telegram_gateway._handle_login_code(api, chat_id=901, telegram_user=user)

        api.send_temporary.assert_awaited_once()
        markup = api.send_temporary.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertIn("menu:loginpin", {button.get("callback_data") for button in buttons})
        self.assertTrue(any(
            button.get("url") == "https://tvr.lat/login?next=%2Freactor#telegram-login"
            for button in buttons
        ))
        copy_button = next(button for button in buttons if "copy_text" in button)
        self.assertEqual(copy_button["copy_text"]["text"], "12345678")

    async def test_unlinked_home_has_one_tap_reactor_connection(self) -> None:
        api = AsyncMock()
        user = {"id": 900, "first_name": "Иван"}

        with (
            patch.object(telegram_gateway, "_GUILD_ID", 77),
            patch.object(telegram_gateway.telegram_storage, "get_link_by_telegram", return_value=None),
        ):
            await telegram_gateway._handle_menu(api, chat_id=901, telegram_user=user)

        markup = api.send.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        connect = next(button for button in buttons if button.get("text") == "🔗 Подключить Telegram")
        self.assertEqual(connect["url"], telegram_gateway._REACTOR_CONNECTION_URL)

    async def test_settings_only_offer_login_pin_for_linked_account(self) -> None:
        api = AsyncMock()
        user = {"id": 900}

        with (
            patch.object(telegram_gateway, "_GUILD_ID", 77),
            patch.object(telegram_gateway.telegram_storage, "get_link_by_telegram", return_value=None),
        ):
            await telegram_gateway._handle_settings(api, chat_id=901, telegram_user=user)

        markup = api.send.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertNotIn("menu:loginpin", {button.get("callback_data") for button in buttons})
        self.assertTrue(any(button.get("url") == telegram_gateway._REACTOR_CONNECTION_URL for button in buttons))

    async def test_inline_navigation_edits_current_panel(self) -> None:
        api = AsyncMock()
        panel = _TelegramPanelApi(api, chat_id=123, message_id=456)
        keyboard = {"inline_keyboard": [[{"text": "Назад", "callback_data": "menu:home"}]]}

        await panel.send(123, "👤 Профиль", reply_markup=keyboard)

        api.edit_message_text.assert_awaited_once_with(
            123, 456, "👤 Профиль", reply_markup=keyboard
        )
        api.send.assert_not_awaited()
        api.delete_message.assert_not_awaited()

    async def test_long_panel_edits_first_part_and_sends_remaining_text(self) -> None:
        api = AsyncMock()
        panel = _TelegramPanelApi(api, chat_id=123, message_id=456)
        text = "а" * 4100

        await panel.send(123, text)

        api.edit_message_text.assert_awaited_once()
        first = api.edit_message_text.await_args.args[2]
        self.assertLessEqual(len(first), 3900)
        self.assertEqual(api.send.await_count, 1)
        self.assertEqual(len(api.send.await_args.args[1]), 200)

    async def test_uneditable_legacy_panel_falls_back_to_replacement(self) -> None:
        api = AsyncMock()
        api.edit_message_text.side_effect = RuntimeError(
            "telegram_api_editMessageText:Bad Request: message to edit not found"
        )
        panel = _TelegramPanelApi(api, chat_id=123, message_id=456)

        await panel.send(123, "Главное меню", reply_markup={"inline_keyboard": []})

        api.delete_message.assert_awaited_once_with(123, 456)
        api.send.assert_awaited_once_with(
            123, "Главное меню", reply_markup={"inline_keyboard": []}
        )


if __name__ == "__main__":
    unittest.main()
