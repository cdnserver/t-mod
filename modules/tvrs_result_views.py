"""Discord controls used after a bill vote reaches its result boundary."""

from __future__ import annotations

import discord

from modules.consensus_core import ConsensusStateError, LiveConsensusSession
from modules.consensus_runtime import active_sessions
from modules.tvrs_hub_views import TVRSBaseView
from modules.tvrs_presentation import (
    consensus_generation_matches,
    consensus_result_bill_id,
)


class TVRSVetoConfirmView(TVRSBaseView):
    def __init__(self, session_key: str, user_id: int, *, bill_id: int) -> None:
        super().__init__(timeout=120)
        self.session_key = session_key
        self.user_id = int(user_id)
        self.bill_id = int(bill_id)

    @discord.ui.button(label="Подтвердить вето", style=discord.ButtonStyle.danger)
    async def confirm_veto(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Это подтверждение не для вас.",
                ephemeral=True,
            )
            return
        session = next(
            (
                item
                for item in active_sessions.values()
                if item.session_key == self.session_key
            ),
            None,
        )
        if session is None or not consensus_generation_matches(
            session,
            stage="voting",
            bill_id=self.bill_id,
        ):
            await interaction.response.send_message(
                "Сессия не найдена.",
                ephemeral=True,
            )
            return
        guild = interaction.client.get_guild(session.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "Сервер не найден.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            from modules.tvrs_decision import apply_veto

            await apply_veto(
                interaction.client,
                guild,
                session,
                interaction.user,
                expected_bill_id=self.bill_id,
            )
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Право вето применено.", ephemeral=True)


class TVRSAfterResultView(TVRSBaseView):
    def __init__(self, session_key: str) -> None:
        super().__init__(timeout=None)
        self.session_key = session_key
        session = next(
            (
                item
                for item in active_sessions.values()
                if item.session_key == self.session_key
            ),
            None,
        )
        self.result_bill_id = consensus_result_bill_id(session) if session else 0

    def session(self, guild_id: int) -> LiveConsensusSession | None:
        session = active_sessions.get(guild_id)
        return (
            session
            if session and session.session_key == self.session_key
            else None
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Команда работает только на сервере Discord.",
                ephemeral=True,
            )
            return False
        session = self.session(interaction.guild.id)
        if session is None:
            await interaction.response.send_message("Сессия не найдена.", ephemeral=True)
            return False
        if interaction.user.id != session.leader_id:
            await interaction.response.send_message(
                "Управлять этим консенсусом может только ведущий.",
                ephemeral=True,
            )
            return False
        if not consensus_generation_matches(
            session,
            stage="after_result",
            result_bill_id=self.result_bill_id,
        ):
            await interaction.response.send_message(
                "Эта панель относится к уже завершённому этапу.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Следующий проект", style=discord.ButtonStyle.success)
    async def next_bill(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            from modules.tvrs_control import begin_next_bill_vote

            await begin_next_bill_vote(
                interaction.client,
                interaction.guild,
                session,
                interaction.channel,
                expected_stage="after_result",
                expected_result_bill_id=self.result_bill_id,
            )

    @discord.ui.button(
        label="Завершить консенсус",
        style=discord.ButtonStyle.secondary,
    )
    async def finish_all(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        assert interaction.guild is not None
        session = self.session(interaction.guild.id)
        if session:
            await interaction.response.defer()
            from modules.tvrs_decision import finish_session

            await finish_session(
                interaction.client,
                interaction.guild,
                session,
                interaction.channel,
                expected_stage="after_result",
                expected_result_bill_id=self.result_bill_id,
            )


__all__ = ["TVRSAfterResultView", "TVRSVetoConfirmView"]
