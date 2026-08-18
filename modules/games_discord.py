"""Discord controls for playful T-Mod Games interactions."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from persistence import game_repository as games


_ERROR_MESSAGES = {
    "game_missing": "Партия с таким ID не найдена.",
    "game_message_chess_only": "Эта команда работает только в шахматах T-Mod Games.",
    "game_not_active": "Сообщение можно показать только во время активной партии.",
    "game_message_target_invalid": "Выбранный игрок не участвует в этой партии.",
    "game_not_yours": "Отправлять реплики могут участники партии и администраторы.",
    "game_message_target_self": "Выберите соперника, а не себя.",
    "game_message_rate_limited": "Секунду — дайте сопернику пережить предыдущее сообщение.",
    "game_message_invalid": "Текст должен содержать от 1 до 180 символов.",
}


def setup_games_discord(bot: commands.Bot) -> None:
    @bot.tree.command(
        name="chessmsg",
        description="Показать игроку временную реплику поверх шахматной доски",
    )
    @app_commands.guild_only()
    @app_commands.rename(match_id="партия", player="игрок", message="сообщение")
    @app_commands.describe(
        match_id="ID партии из ссылки T-Mod Games",
        player="Игрок этой партии, которому показать реплику",
        message="Текст до 180 символов",
    )
    async def chess_message(
        interaction: discord.Interaction,
        match_id: str,
        player: discord.Member,
        message: app_commands.Range[str, 1, 180],
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        administrator = bool(
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )
        try:
            sent = await asyncio.to_thread(
                games.game_send_chess_message,
                int(interaction.guild_id),
                str(match_id).strip(),
                actor_user_id=int(interaction.user.id),
                recipient_user_id=int(player.id),
                sender_display=str(interaction.user.display_name),
                message=str(message),
                allow_moderator=administrator,
            )
        except games.GameStorageError as exc:
            code = str(exc)
            await interaction.edit_original_response(
                content=_ERROR_MESSAGES.get(code, "Не удалось отправить шахматную реплику.")
            )
            return

        side = "белых" if sent["recipient_side"] == "white" else "чёрных"
        await interaction.edit_original_response(
            content=(
                f"♟️ Реплика отправлена **{player.display_name}** — стороне {side}.\n"
                "Если партия открыта, сообщение сейчас появится поверх доски."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )


__all__ = ["setup_games_discord"]
