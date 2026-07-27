from __future__ import annotations

import asyncio
import re
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands

from persistence import outbox_repository as _outbox_storage
from persistence import tvrs_repository as _tvrs_storage
from modules.consensus_runtime import (
    active_sessions as _active_sessions,
)
from modules.delivery_runtime import register_delivery_handler, wake_delivery_worker
from modules.public_panel_runtime import register_public_panel_provider
from modules.tvrs_config import (
    TVRS_ADMIN_COMMAND_DESCRIPTION,
    TVRS_ADMIN_COMMAND_NAME,
    TVRS_COMMAND_DESCRIPTION,
    TVRS_COMMAND_NAME,
    TVRS_CONSENSUS_VOICE_CHANNEL_ID,
    TVRS_MATERIALS_CHANNEL_ID,
    TVRS_SETBILL_COMMAND_DESCRIPTION,
    TVRS_SETBILL_COMMAND_NAME,
    TVRS_STICKY_COMMAND_DESCRIPTION,
    TVRS_STICKY_COMMAND_NAME,
)
from modules.tvrs_formatting import (
    format_bill_number,
)
from modules.tvrs_navigation_runtime import register_tvrs_hub_handler
from modules.tvrs_delivery import (
    TVRS_CONTROL_DM_TOPIC,
    TVRS_CONTROL_NOTICE_TOPIC,
    TVRS_NOTICE_DELETE_TOPIC,
    TVRS_DISCUSSION_INVITE_TOPIC,
    TVRS_PHASE_ANNOUNCEMENT_TOPIC,
    TVRS_BILL_PUBLICATION_TOPIC,
    TVRS_RETRY_BILL_TOPIC,
    TVRS_RESULT_TOPIC,
    TVRS_SESSION_SUMMARY_TOPIC,
    make_retry_bill_delivery_handler,
    make_result_delivery_handler,
    make_session_summary_delivery_handler,
)

from modules.tvrs_presentation import (
    _open_tvrs_hub_impl,
    build_public_universality_embed,
    build_universality_embed,
    is_chair,
)
from modules.tvrs_hub_views import TVRSPublicPanelView, TVRSUniversalityView
from modules.tvrs_discussion import check_realtime_quorum, forward_discussion_message
from modules.tvrs_control import (
    deliver_consensus_control_dm,
    deliver_consensus_control_notice,
    deliver_consensus_discussion_invite,
    deliver_consensus_phase_announcement,
)
from modules.tvrs_transient_notices import deliver_consensus_notice_deletion
from modules.tvrs_recovery import ensure_sticky_message, schedule_sticky_refresh

def setup_tvrs(bot: commands.Bot, remember_command_activity: Callable[[discord.Interaction, str, str], None]) -> None:
    register_public_panel_provider(build_public_universality_embed, TVRSPublicPanelView)
    register_tvrs_hub_handler(_open_tvrs_hub_impl)
    register_delivery_handler(TVRS_RESULT_TOPIC, make_result_delivery_handler(bot))
    register_delivery_handler(TVRS_BILL_PUBLICATION_TOPIC, make_retry_bill_delivery_handler(bot))
    register_delivery_handler(TVRS_RETRY_BILL_TOPIC, make_retry_bill_delivery_handler(bot))
    register_delivery_handler(
        TVRS_CONTROL_DM_TOPIC,
        lambda message: deliver_consensus_control_dm(message, bot),
    )
    register_delivery_handler(
        TVRS_CONTROL_NOTICE_TOPIC,
        lambda message: deliver_consensus_control_notice(message, bot),
    )
    register_delivery_handler(
        TVRS_NOTICE_DELETE_TOPIC,
        lambda message: deliver_consensus_notice_deletion(message, bot),
    )
    register_delivery_handler(
        TVRS_PHASE_ANNOUNCEMENT_TOPIC,
        lambda message: deliver_consensus_phase_announcement(message, bot),
    )
    register_delivery_handler(
        TVRS_DISCUSSION_INVITE_TOPIC,
        lambda message: deliver_consensus_discussion_invite(message, bot),
    )
    register_delivery_handler(TVRS_SESSION_SUMMARY_TOPIC, make_session_summary_delivery_handler(bot))

    @bot.listen("on_message")
    async def tvrs_on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            await forward_discussion_message(bot, message)
            return
        if message.channel.id != TVRS_MATERIALS_CHANNEL_ID:
            return
        active = _active_sessions.get(message.guild.id)
        if active and not active.finished:
            return
        schedule_sticky_refresh(bot, message.guild)

    @bot.listen("on_voice_state_update")
    async def tvrs_voice_quorum_listener(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot or member.guild is None:
            return
        if (getattr(before.channel, "id", None) != TVRS_CONSENSUS_VOICE_CHANNEL_ID and getattr(after.channel, "id", None) != TVRS_CONSENSUS_VOICE_CHANNEL_ID):
            return
        session = _active_sessions.get(member.guild.id)
        if session and not session.finished:
            await check_realtime_quorum(bot, member.guild, session)

    @bot.tree.command(name=TVRS_COMMAND_NAME, description=TVRS_COMMAND_DESCRIPTION)
    async def tvrs(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        remember_command_activity(interaction, "command_tvrs", "/tvrs")
        embed = await asyncio.to_thread(build_universality_embed, interaction.guild, interaction.user.id)
        await interaction.followup.send(
            embed=embed,
            view=TVRSUniversalityView(interaction.user.id),
            ephemeral=True,
        )

    @bot.tree.command(name=TVRS_SETBILL_COMMAND_NAME, description=TVRS_SETBILL_COMMAND_DESCRIPTION)
    @app_commands.describe(last_accepted="Последний принятый номер. Например: 8, чтобы следующий был 009")
    async def tvrs_setbill(interaction: discord.Interaction, last_accepted: app_commands.Range[int, 0, 9999]) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_setbill", "/tvrs_setbill")
        next_number = await asyncio.to_thread(
            _tvrs_storage.tvrs_set_last_accepted_bill_number,
            interaction.guild.id,
            int(last_accepted),
        )
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.followup.send(f"Последний принятый законопроект: `{format_bill_number(int(last_accepted))}`. Следующий номер: `{format_bill_number(next_number)}`.", ephemeral=True)



    @bot.tree.command(name=TVRS_ADMIN_COMMAND_NAME, description=TVRS_ADMIN_COMMAND_DESCRIPTION)
    @app_commands.describe(target="bill/result/plenary/delivery", action="view/delete/edit/retry", identifier="Номер проекта, результата, консенсуса или доставки", confirm="Для удаления напишите DELETE", field="Поле для edit", value="Новое значение")
    async def tvrs_admin(interaction: discord.Interaction, target: str, action: str, identifier: str, confirm: str | None = None, field: str | None = None, value: str | None = None) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_admin", "/tvrs_admin")
        target = target.strip().lower()
        action = action.strip().lower()
        if target in {"bill", "закон", "project", "проект"}:
            try:
                num = int(re.sub(r"\D", "", identifier) or "0")
            except ValueError:
                num = 0
            if action == "view":
                bill = await asyncio.to_thread(_tvrs_storage.tvrs_get_bill_by_number, interaction.guild.id, num)
                if not bill:
                    await interaction.followup.send(f"Законопроект `{identifier}` не найден.", ephemeral=True)
                    return
                await interaction.followup.send(f"`{format_bill_number(bill['bill_number'])}` • **{bill.get('title','')}**\nСтатус: `{bill.get('status')}`\nID: `{bill.get('id')}`", ephemeral=True)
                return
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                try:
                    bill = await asyncio.to_thread(
                        _tvrs_storage.tvrs_delete_bill_by_number,
                        interaction.guild.id,
                        num,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    if str(exc) == "bill_locked_by_active_consensus":
                        await interaction.followup.send(
                            "Этот законопроект участвует в активном консенсусе и защищён от удаления.",
                            ephemeral=True,
                        )
                    elif str(exc) == "tvrs_delivery_pending":
                        await interaction.followup.send(
                            "Для этого законопроекта ещё выполняется публикация или уведомление. Повторите удаление после доставки.",
                            ephemeral=True,
                        )
                    else:
                        await interaction.followup.send(f"Удаление отклонено: `{str(exc)[:200]}`", ephemeral=True)
                    return
                await interaction.followup.send((f"Законопроект `{format_bill_number(num)}` удален." if bill else "Законопроект не найден."), ephemeral=True)
                await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
                return
            if action == "edit":
                if not field or value is None:
                    await interaction.followup.send("Для edit нужны `field` и `value`. Поля: title, summary, materials, status, result_summary.", ephemeral=True)
                    return
                try:
                    bill = await asyncio.to_thread(
                        _tvrs_storage.tvrs_update_bill_field,
                        interaction.guild.id,
                        num,
                        field.strip(),
                        value,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except Exception as exc:
                    await interaction.followup.send(f"Ошибка изменения: `{str(exc)[:300]}`", ephemeral=True)
                    return
                await interaction.followup.send((f"Законопроект `{format_bill_number(num)}` обновлен." if bill else "Законопроект не найден."), ephemeral=True)
                return
        if target in {"result", "итог"}:
            rid = int(re.sub(r"\D", "", identifier) or "0")
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                try:
                    row = await asyncio.to_thread(
                        _tvrs_storage.tvrs_delete_live_result,
                        rid,
                        interaction.guild.id,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        (
                            "Для этого итога ещё выполняется доставка. Повторите удаление после неё."
                            if str(exc) == "tvrs_delivery_pending"
                            else "Итог относится к активному консенсусу и пока защищён от удаления."
                        ),
                        ephemeral=True,
                    )
                    return
                await interaction.followup.send((f"Итог голосования ID `{rid}` удален." if row else "Итог не найден."), ephemeral=True)
                return
        if target in {"plenary", "consensus", "консенсус"}:
            pn = int(re.sub(r"\D", "", identifier) or "0")
            if action == "delete":
                if confirm != "DELETE":
                    await interaction.followup.send("Для удаления укажите `confirm: DELETE`.", ephemeral=True)
                    return
                try:
                    count = await asyncio.to_thread(
                        _tvrs_storage.tvrs_delete_plenary_results,
                        interaction.guild.id,
                        pn,
                        interaction.user.id,
                        getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except ValueError as exc:
                    await interaction.followup.send(
                        (
                            "Финальная сводка этого консенсуса ещё доставляется. Повторите удаление позже."
                            if str(exc) == "tvrs_delivery_pending"
                            else "Итоги активного консенсуса пока защищены от удаления."
                        ),
                        ephemeral=True,
                    )
                    return
                await interaction.followup.send(f"Удалено итогов консенсуса №`{pn}`: `{count}`.", ephemeral=True)
                return
        if target in {"delivery", "outbox", "доставка"}:
            delivery_id = int(re.sub(r"\D", "", identifier) or "0")
            if action in {"retry", "repeat", "повторить"}:
                requeued = await asyncio.to_thread(
                    _outbox_storage.delivery_outbox_requeue_dead,
                    delivery_id,
                    guild_id=interaction.guild.id,
                )
                if requeued:
                    wake_delivery_worker()
                await interaction.followup.send(
                    (
                        f"Доставка ID `{delivery_id}` возвращена в очередь."
                        if requeued
                        else "Остановленная доставка с таким ID не найдена."
                    ),
                    ephemeral=True,
                )
                return
        await interaction.followup.send("Неизвестная команда администратора. Используйте target: bill/result/plenary/delivery и action: view/delete/edit/retry.", ephemeral=True)

    @bot.tree.command(name=TVRS_STICKY_COMMAND_NAME, description=TVRS_STICKY_COMMAND_DESCRIPTION)
    async def tvrs_sticky(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Команда работает только на сервере Discord.", ephemeral=True)
            return
        if not is_chair(interaction.user):
            await interaction.response.send_message("Команда доступна только председателю.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        remember_command_activity(interaction, "command_tvrs_sticky", "/tvrs_sticky")
        await ensure_sticky_message(interaction.client, interaction.guild, force_repost=True)
        await interaction.followup.send(f"Сообщение подачи законопроектов обновлено в <#{TVRS_MATERIALS_CHANNEL_ID}>.", ephemeral=True)

__all__ = ['setup_tvrs']
