from __future__ import annotations

import discord

from modules.consensus_core import LiveConsensusSession
from modules.consensus_runtime import active_sessions as _active_sessions
from modules.tvrs_hub_views import TVRSBaseView


def build_incomplete_roster_warning(session: LiveConsensusSession) -> str:
    unconfirmed = [
        participant
        for participant in session.participants.values()
        if not participant.confirmed
    ]
    lines: list[str] = []
    for participant in unconfirmed[:12]:
        if participant.dm_failed:
            delivery = "ЛС недоступны; резервный пульт доступен на сервере"
        elif participant.dm_message_id:
            delivery = "приглашение доставлено, подтверждения пока нет"
        else:
            delivery = "доставка ещё проверяется; резервный пульт доступен на сервере"
        lines.append(f"• {participant.mention} — {delivery}")
    if len(unconfirmed) > len(lines):
        lines.append(f"• и ещё {len(unconfirmed) - len(lines)} участник(а/ов)")
    roster = "\n".join(lines)
    return (
        "⚠️ **Не все приглашённые подтвердили участие**\n\n"
        f"{roster}\n\n"
        "Если начать сейчас, эти участники не войдут в состав заседания и не смогут "
        "голосовать. Можно подождать подтверждений или явно начать текущим "
        "подтверждённым составом."
    )


class TVRSStartCurrentRosterConfirmView(TVRSBaseView):
    """One-shot approval tied to one exact confirmed participant roster."""

    def __init__(
        self,
        session_key: str,
        leader_id: int,
        *,
        confirmed_user_ids: set[int],
    ) -> None:
        super().__init__(timeout=180)
        self.session_key = str(session_key)
        self.leader_id = int(leader_id)
        self.confirmed_user_ids = frozenset(int(user_id) for user_id in confirmed_user_ids)
        button = discord.ui.Button(
            label="Начать текущим составом",
            style=discord.ButtonStyle.danger,
        )
        button.callback = self.confirm_start  # type: ignore[assignment]
        self.add_item(button)

    def session(self, guild_id: int) -> LiveConsensusSession | None:
        current = _active_sessions.get(int(guild_id))
        return current if current and current.session_key == self.session_key else None

    async def confirm_start(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Подтвердить состав можно только на сервере Discord.",
                ephemeral=True,
            )
            return
        if interaction.user.id != self.leader_id:
            await interaction.response.send_message(
                "Подтвердить текущий состав может только ведущий консенсуса.",
                ephemeral=True,
            )
            return
        session = self.session(guild.id)
        if session is None or session.stage != "registration":
            await interaction.response.send_message(
                "Регистрация уже завершена или эта панель устарела.",
                ephemeral=True,
            )
            return
        current_confirmed = frozenset(
            participant.user_id for participant in session.confirmed_participants()
        )
        if current_confirmed != self.confirmed_user_ids:
            await interaction.response.send_message(
                "Состав успел измениться. Откройте пульт ведущего и проверьте "
                "актуальный список перед запуском.",
                ephemeral=True,
            )
            return
        if not session.quorum_ready():
            await interaction.response.send_message(
                "Подтверждённый кворум больше не набран.",
                ephemeral=True,
            )
            return

        # Lazy imports keep the registration gate independent from the large
        # control/view cycle while preserving one shared lifecycle service.
        from modules import tvrs_consensus_views as consensus_views

        voice_ok, voice_reason = consensus_views.session_voice_quorum_ready(guild, session)
        if not voice_ok:
            await interaction.response.send_message(voice_reason, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await consensus_views.begin_next_bill_vote(
            interaction.client,
            guild,
            session,
            interaction.channel,
            expected_stage="registration",
        )
        editor = getattr(interaction, "edit_original_response", None)
        if callable(editor):
            if session.stage in {"voting", "discussion_type", "discussion"}:
                message = "Голосование запущено текущим подтверждённым составом."
            elif session.stage == "paused":
                message = session.paused_reason or "Запуск приостановлен: проверьте кворум."
            elif session.finished:
                message = "Очередь законопроектов пуста; заседание завершено."
            else:
                message = "Состояние заседания изменилось. Откройте актуальный пульт."
            await editor(content=message, embed=None, view=None)


__all__ = ["TVRSStartCurrentRosterConfirmView", "build_incomplete_roster_warning"]
