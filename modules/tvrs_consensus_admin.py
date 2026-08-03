"""Chair-only recovery controls for a live consensus.

Normal voting stays in the participant and host views.  This module contains
only exceptional, audited actions and therefore never appears to ordinary
participants.
"""

from __future__ import annotations

import discord

from modules.async_safety import run_blocking_cancellation_safe
from modules.consensus_core import ConsensusStateError, LiveConsensusSession
from modules.consensus_health import assess_consensus_health
from modules.consensus_recovery_plan import (
    ConsensusOperationalState,
    build_consensus_recovery_plan,
)
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
    coordinator as _consensus,
    registry as _consensus_registry,
    session_lock as consensus_session_lock,
)
from modules.consensus_service import ConsensusActor
from modules.delivery_runtime import wake_delivery_worker
from modules.operations_runtime import wake_operations_worker
from modules.tvrs_formatting import clean_stage_name, clip_text, format_bill_number


def _session_by_key(session_key: str) -> LiveConsensusSession | None:
    return next(
        (
            item
            for item in _active_sessions.values()
            if item.session_key == str(session_key) and not item.finished
        ),
        None,
    )


def build_consensus_admin_embed(
    session: LiveConsensusSession,
    operational: ConsensusOperationalState | None = None,
) -> discord.Embed:
    report = assess_consensus_health(session)
    plan = build_consensus_recovery_plan(session, report, operational)
    plan_has_warning = any(item.priority == "warning" for item in plan.actions)
    color = 0xD67F7F if plan.critical else (0xD6B46A if plan_has_warning else 0x7FD17F)
    embed = discord.Embed(
        title=f"🛟 Восстановление консенсуса №{session.plenary_number}",
        description=(
            "Аварийный пульт председателя. Каждое изменение сохраняется в журнале; "
            "обычное голосование выполняется через личный пульт."
        ),
        color=color,
    )
    embed.add_field(name="Этап", value=f"**{clean_stage_name(session.stage)}**", inline=True)
    embed.add_field(name="Ведущий", value=f"<@{session.leader_id}>", inline=True)
    embed.add_field(name="Ревизия", value=f"`{session.revision}`", inline=True)
    if session.current_bill:
        embed.add_field(
            name="Текущий проект",
            value=(
                f"№`{format_bill_number(int(session.current_bill.get('bill_number') or 0))}` • "
                f"**{clip_text(str(session.current_bill.get('title') or ''), 180)}**"
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Текущий проект", value="Нет проекта в работе.", inline=False)
    if not report.issues:
        diagnostic = "✅ Состояние целостно. Аварийные действия не требуются."
    else:
        icons = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
        diagnostic = "\n".join(
            f"{icons[item.severity]} **{item.message}**\n↳ {item.recovery}"
            for item in report.issues[:6]
        )
    embed.add_field(name="Диагностика", value=clip_text(diagnostic, 1000), inline=False)
    if operational is not None:
        counts = dict(operational.delivery_counts)
        delivery_line = ", ".join(
            f"{name}: `{count}`" for name, count in sorted(counts.items()) if count
        ) or "нет задач"
        live_lines = [
            f"Ведущий на сервере: {'✅' if operational.leader_present else '❌'}",
            f"Ведущий в голосовом: {'✅' if operational.leader_in_voice else '❌'}",
            f"Кворум: {'✅' if operational.quorum_ready else '⏸️'} {operational.quorum_reason or ''}",
            f"Доставки: {delivery_line}",
        ]
        if operational.discussion_channel_available is not None:
            live_lines.append(
                "Канал дискуссии: "
                + ("✅ доступен" if operational.discussion_channel_available else "❌ отсутствует")
            )
        if operational.timer_task_running is not None:
            live_lines.append(
                "Таймер процесса: "
                + ("✅ работает" if operational.timer_task_running else "❌ требует восстановления")
            )
        if operational.missing_control_user_ids:
            live_lines.append(
                f"Личные панели требуют ремонта: `{len(operational.missing_control_user_ids)}`"
            )
        if operational.dead_delivery_errors:
            live_lines.append(
                "Последняя ошибка доставки: `"
                + clip_text(operational.dead_delivery_errors[0], 180).replace("`", "'")
                + "`"
            )
        embed.add_field(
            name="Живое состояние",
            value=clip_text("\n".join(live_lines), 1000),
            inline=False,
        )
    plan_lines = [
        f"{'🤖' if item.automatic else '👤'} {item.message}"
        for item in plan.actions[:6]
    ]
    embed.add_field(
        name="План восстановления",
        value=clip_text("\n".join(plan_lines), 1000),
        inline=False,
    )
    embed.add_field(
        name="Гарантии",
        value=(
            "• закрытие без итогов отменяет номер заседания;\n"
            "• текущий незавершённый проект возвращается в очередь;\n"
            "• устный итог помечается отдельно и не имитирует цифровые голоса;\n"
            "• этап фиксации нельзя оборвать — сначала восстанавливается транзакция."
        ),
        inline=False,
    )
    embed.set_footer(text="TVRS • аварийные действия доступны председателям и администраторам")
    return embed


class _ChairRecoveryView(discord.ui.View):
    def __init__(self, requester_id: int, session_key: str, *, timeout: float = 900) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = int(requester_id)
        self.session_key = str(session_key)

    def session(self) -> LiveConsensusSession | None:
        return _session_by_key(self.session_key)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        from modules.tvrs_presentation import is_chair

        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Этот аварийный пульт открыт для другого пользователя.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Пульт работает только на сервере Discord.", ephemeral=True)
            return False
        if not is_chair(interaction.user):
            await interaction.response.send_message("Аварийное управление доступно только председателю.", ephemeral=True)
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        message = (
            "Восстановление не завершено. Состояние сохранено; повторите действие чуть позже "
            "или безопасно закройте заседание."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.DiscordException:
            pass
        if interaction.guild is not None:
            from modules.technical_log import log_technical_event

            try:
                await log_technical_event(
                    interaction.client,
                    interaction.guild,
                    title="Ошибка аварийного управления консенсусом",
                    details=(
                        f"Сессия: `{self.session_key[:120]}`\n"
                        f"Действие: `{getattr(item, 'label', None) or type(item).__name__}`\n"
                        f"Ошибка: `{type(error).__name__}: {str(error)[:700]}`"
                    ),
                    dedupe_key=f"consensus-admin-error:{self.session_key}:{type(error).__name__}",
                    cooldown_seconds=60,
                )
            except Exception:
                pass


class OralResolutionModal(discord.ui.Modal):
    def __init__(self, requester_id: int, session_key: str, status: str) -> None:
        label = "принят" if status == "accepted" else "отклонён"
        super().__init__(title=f"Устный итог: проект {label}")
        self.requester_id = int(requester_id)
        self.session_key = str(session_key)
        self.status = str(status)
        self.note = discord.ui.TextInput(
            label="Основание или краткий протокол",
            placeholder="Кто участвовал и какое решение было принято",
            min_length=3,
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.confirmation = discord.ui.TextInput(
            label="Подтверждение",
            placeholder="Введите УСТНО",
            min_length=5,
            max_length=10,
        )
        self.add_item(self.note)
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_decision import record_oral_result
        from modules.tvrs_presentation import is_chair

        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это подтверждение открыто для другого пользователя.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_chair(interaction.user):
            await interaction.response.send_message("Право председателя больше не подтверждается.", ephemeral=True)
            return
        if str(self.confirmation.value).strip().upper() != "УСТНО":
            await interaction.response.send_message("Решение не записано: введите `УСТНО` без кавычек.", ephemeral=True)
            return
        session = _session_by_key(self.session_key)
        if session is None or session.current_bill is None:
            await interaction.response.send_message("Проект уже закрыт или сессия завершена.", ephemeral=True)
            return
        bill_id = int(session.current_bill.get("id") or 0)
        await interaction.response.defer(ephemeral=True)
        try:
            await record_oral_result(
                interaction.client,
                interaction.guild,
                session,
                ConsensusActor(interaction.user.id, interaction.user.display_name),
                status=self.status,
                note=str(self.note.value),
                expected_bill_id=bill_id,
            )
        except ConsensusStateError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "Устное решение зафиксировано отдельно от цифрового голосования. Теперь можно продолжить или завершить заседание.",
            ephemeral=True,
        )


class AdministrativeCloseModal(discord.ui.Modal):
    def __init__(self, requester_id: int, session_key: str) -> None:
        super().__init__(title="Безопасное закрытие консенсуса")
        self.requester_id = int(requester_id)
        self.session_key = str(session_key)
        self.reason = discord.ui.TextInput(
            label="Причина закрытия",
            placeholder="Например: заседание проведено устно",
            min_length=3,
            max_length=1000,
            style=discord.TextStyle.paragraph,
        )
        self.confirmation = discord.ui.TextInput(
            label="Подтверждение",
            placeholder="Введите ЗАКРЫТЬ",
            min_length=7,
            max_length=12,
        )
        self.add_item(self.reason)
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from modules.tvrs_decision import finish_session
        from modules.tvrs_presentation import is_chair

        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Это подтверждение открыто для другого пользователя.", ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not is_chair(interaction.user):
            await interaction.response.send_message("Право председателя больше не подтверждается.", ephemeral=True)
            return
        if str(self.confirmation.value).strip().upper() != "ЗАКРЫТЬ":
            await interaction.response.send_message("Сессия не закрыта: введите `ЗАКРЫТЬ` без кавычек.", ephemeral=True)
            return
        session = _session_by_key(self.session_key)
        if session is None:
            await interaction.response.send_message("Сессия уже завершена.", ephemeral=True)
            return
        if session.stage == "finalizing":
            await interaction.response.send_message(
                "Сейчас идёт атомарная фиксация результата. Сначала нажмите «Восстановить», чтобы не получить половинчатый итог.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        cancelled = not bool(session.results)
        await finish_session(
            interaction.client,
            interaction.guild,
            session,
            interaction.channel,
            actor=ConsensusActor(interaction.user.id, interaction.user.display_name),
            cancelled=cancelled,
            reason=str(self.reason.value),
        )
        action = "отменена" if cancelled else "административно завершена"
        await interaction.followup.send(
            f"Сессия {action}. Незавершённый проект, если он был, возвращён в очередь.",
            ephemeral=True,
        )


class TVRSConsensusAdminView(_ChairRecoveryView):
    def __init__(self, requester_id: int, session_key: str) -> None:
        super().__init__(requester_id, session_key)
        session = self.session()
        participant = session.participants.get(int(requester_id)) if session else None
        can_takeover = bool(
            session
            and int(session.leader_id) != int(requester_id)
            and participant
            and participant.confirmed
            and participant.kind == "chair"
        )
        takeover = discord.ui.Button(
            label="Принять ведение",
            emoji="🎛️",
            style=discord.ButtonStyle.primary,
            disabled=not can_takeover,
            row=0,
        )
        takeover.callback = self.takeover
        self.add_item(takeover)
        repair = discord.ui.Button(label="Восстановить", emoji="🩺", style=discord.ButtonStyle.secondary, row=0)
        repair.callback = self.repair
        self.add_item(repair)
        refresh = discord.ui.Button(label="Обновить", emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
        refresh.callback = self.refresh
        self.add_item(refresh)

        can_oral = bool(
            session
            and session.current_bill
            and session.stage in {"voting", "paused", "discussion_type", "discussion"}
            and not (
                session.stage == "paused"
                and session.previous_stage == "presentation"
            )
        )
        accepted = discord.ui.Button(
            label="Устно: принят",
            emoji="✅",
            style=discord.ButtonStyle.success,
            disabled=not can_oral,
            row=1,
        )
        accepted.callback = self.oral_accepted
        self.add_item(accepted)
        rejected = discord.ui.Button(
            label="Устно: отклонён",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            disabled=not can_oral,
            row=1,
        )
        rejected.callback = self.oral_rejected
        self.add_item(rejected)
        end_discussion_button = discord.ui.Button(
            label="Завершить дискуссию",
            emoji="🗣️",
            style=discord.ButtonStyle.secondary,
            disabled=not bool(session and session.stage in {"discussion_type", "discussion"}),
            row=2,
        )
        end_discussion_button.callback = self.end_discussion
        self.add_item(end_discussion_button)
        close = discord.ui.Button(label="Безопасно закрыть", emoji="🛑", style=discord.ButtonStyle.danger, row=2)
        close.callback = self.close
        self.add_item(close)

    async def takeover(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        from modules.tvrs_config import TVRS_CONSENSUS_VOICE_CHANNEL_ID

        session = self.session()
        if session is None:
            await interaction.response.send_message("Сессия уже завершена.", ephemeral=True)
            return
        if getattr(getattr(interaction.user, "voice", None), "channel", None) is None or int(
            getattr(interaction.user.voice.channel, "id", 0) or 0  # type: ignore[union-attr]
        ) != int(TVRS_CONSENSUS_VOICE_CHANNEL_ID):
            await interaction.response.send_message(
                "Чтобы принять ведение, войдите в голосовой канал консенсуса. Для закрытия или устного итога это не требуется.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        previous_host = session.host_message_obj
        async with consensus_session_lock(session.guild_id):
            if _consensus_registry.find(session.session_key) is not session:
                changed = False
            else:
                changed = await run_blocking_cancellation_safe(
                    _consensus.transfer_leadership,
                    session,
                    new_leader_id=interaction.user.id,
                    new_leader_display=interaction.user.display_name,
                    actor=ConsensusActor(interaction.user.id, interaction.user.display_name),
                )
        if not changed:
            await interaction.followup.send("Ведение уже передано или состояние успело измениться.", ephemeral=True)
            return
        if previous_host is not None and previous_host is not interaction.message and hasattr(previous_host, "edit"):
            try:
                await previous_host.edit(view=None)
            except discord.DiscordException:
                pass
        wake_operations_worker()
        await self.repair_after_defer(interaction, session)
        operational = await self.operational_state(interaction, session)
        await interaction.edit_original_response(
            embed=build_consensus_admin_embed(session, operational),
            view=TVRSConsensusAdminView(interaction.user.id, session.session_key),
        )

    async def operational_state(
        self,
        interaction: discord.Interaction,
        session: LiveConsensusSession,
    ) -> ConsensusOperationalState | None:
        from modules.consensus_operational import collect_consensus_operational_state

        if interaction.guild is None:
            return None
        try:
            return await collect_consensus_operational_state(interaction.guild, session)
        except Exception:
            return None

    async def repair_after_defer(
        self,
        interaction: discord.Interaction,
        session: LiveConsensusSession,
    ) -> dict[str, object]:
        from modules.tvrs_consensus_portal import ensure_public_consensus_card
        from modules.tvrs_recovery import recover_consensus_session

        repaired = await recover_consensus_session(
            interaction.client,
            interaction.guild,
            session,
            verify_discord_messages=True,
            retry_permanent_failures=True,
        )
        await ensure_public_consensus_card(interaction.client, interaction.guild, session)
        wake_delivery_worker()
        return repaired

    async def repair(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session()
        if session is None:
            await interaction.response.send_message("Сессия уже завершена.", ephemeral=True)
            return
        await interaction.response.defer()
        repaired = await self.repair_after_defer(interaction, session)
        operational = await self.operational_state(interaction, session)
        await interaction.edit_original_response(
            embed=build_consensus_admin_embed(session, operational),
            view=TVRSConsensusAdminView(interaction.user.id, session.session_key),
        )
        repaired_count = (
            len(tuple(repaired.get("repaired") or ()))
            + int(repaired.get("revived") or 0)
            + int(repaired.get("queued") or 0)
            + int(bool(repaired.get("liveness_changed")))
        )
        await interaction.followup.send(
            f"Проверка завершена. Выполнено или поставлено задач восстановления: `{repaired_count}`.",
            ephemeral=True,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        session = self.session()
        if session is None:
            await interaction.response.edit_message(content="Сессия уже завершена.", embed=None, view=None)
            return
        operational = await self.operational_state(interaction, session)
        await interaction.response.edit_message(
            content=None,
            embed=build_consensus_admin_embed(session, operational),
            view=TVRSConsensusAdminView(interaction.user.id, session.session_key),
        )

    async def end_discussion(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        session = self.session()
        if session is None or session.stage not in {"discussion_type", "discussion"}:
            await interaction.response.send_message("Активная дискуссия уже завершена.", ephemeral=True)
            return
        from modules.tvrs_discussion import end_discussion

        await interaction.response.defer()
        await end_discussion(
            interaction.client,
            interaction.guild,
            session,
            actor=ConsensusActor(interaction.user.id, interaction.user.display_name),
        )
        await self.repair_after_defer(interaction, session)
        operational = await self.operational_state(interaction, session)
        await interaction.edit_original_response(
            embed=build_consensus_admin_embed(session, operational),
            view=TVRSConsensusAdminView(interaction.user.id, session.session_key),
        )
        await interaction.followup.send(
            "Дискуссия завершена. Кворум перепроверен, личные панели приведены к текущему этапу.",
            ephemeral=True,
        )

    async def oral_accepted(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OralResolutionModal(interaction.user.id, self.session_key, "accepted"))

    async def oral_rejected(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OralResolutionModal(interaction.user.id, self.session_key, "rejected"))

    async def close(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AdministrativeCloseModal(interaction.user.id, self.session_key))


async def open_consensus_admin_panel(interaction: discord.Interaction, session: LiveConsensusSession) -> None:
    operational = None
    if interaction.guild is not None:
        from modules.consensus_operational import collect_consensus_operational_state

        try:
            operational = await collect_consensus_operational_state(interaction.guild, session)
        except Exception:
            operational = None
    await interaction.response.send_message(
        embed=build_consensus_admin_embed(session, operational),
        view=TVRSConsensusAdminView(interaction.user.id, session.session_key),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


__all__ = [
    "AdministrativeCloseModal",
    "OralResolutionModal",
    "TVRSConsensusAdminView",
    "build_consensus_admin_embed",
    "open_consensus_admin_panel",
]
