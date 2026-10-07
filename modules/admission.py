"""Phoenix admission orchestration: OVR, consensus and durable Discord delivery."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from typing import Any

import discord
from discord.ext import commands

from modules.consensus_runtime import active_sessions
from modules.delivery_outbox import DeliveryReceipt, OutboxMessage
from modules.delivery_runtime import register_delivery_handler, wake_delivery_worker
from modules.discord_delivery import raise_classified_discord_error
from modules.links import DISCORD_TVRS_LINK
from modules.technical_log import log_technical_event
from modules.tvrs_config import (
    TVRS_COCHAIR_IDS,
    TVRS_FELLOWSHIP_ROLE_ID,
    TVRS_MATERIALS_CHANNEL_ID,
)
from modules.tvrs_delivery import TVRS_BILL_PUBLICATION_TOPIC
from modules.tvrs_presentation import is_chair
from persistence import activity_repository as meta_storage
from persistence import admission_repository as admission_storage
from persistence import reactor_repository as reactor_storage
from persistence import tvrs_repository as tvrs_storage
from persistence import web_auth_repository as web_auth_storage


ADMISSION_CHANNEL_ID = int(
    os.getenv("ADMISSION_CHANNEL_ID", "1542512677631430666") or 0
)
ADMISSION_LOG_CHANNEL_ID = int(
    os.getenv("ADMISSION_LOG_CHANNEL_ID", "1526606282897887359") or 0
)
ADMISSION_PUBLIC_URL = (
    os.getenv("ADMISSION_PUBLIC_URL", "https://phx.tvr.lat").strip().rstrip("/")
)
ADMISSION_BANNER_URL = os.getenv(
    "ADMISSION_BANNER_URL",
    f"{ADMISSION_PUBLIC_URL}/admission-assets/phoenix-senate-banner.webp",
).strip()
_PANEL_META_PREFIX = "admission:public-panel:v1"
_reconcile_task: asyncio.Task[Any] | None = None


def _is_legacy_admission_panel(message: Any, bot_user_id: int) -> bool:
    """Recognise the pre-split public panel when its stored message id is absent."""

    author_id = int(getattr(getattr(message, "author", None), "id", 0) or 0)
    if author_id != int(bot_user_id):
        return False
    parts = [str(getattr(message, "content", "") or "")]
    for embed in getattr(message, "embeds", ()) or ():
        parts.extend(
            (
                str(getattr(embed, "title", "") or ""),
                str(getattr(embed, "description", "") or ""),
            )
        )
    text = " ".join(parts).casefold()
    return "phoenix" in text and (
        "вступить в сенат" in text
        or "стать сенатор" in text
        or "вступить в товарищество" in text
    )


ADMISSION_QUESTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "mistake",
        "title": "Вы заметили собственную ошибку, о которой пока никто не знает.",
        "options": (
            (
                "own",
                "Сразу сообщу, предложу исправление и возьму ответственность.",
                {"integrity": 3, "responsibility": 3},
            ),
            (
                "fix",
                "Сначала постараюсь исправить самостоятельно, затем сообщу.",
                {"initiative": 2, "responsibility": 1},
            ),
            (
                "wait",
                "Оценю последствия и сообщу, только если ошибка действительно важна.",
                {"prudence": 2, "integrity": -1},
            ),
            (
                "hide",
                "Если последствий нет, не стану привлекать к этому внимание.",
                {"integrity": -3, "responsibility": -2},
            ),
        ),
    },
    {
        "id": "conflict",
        "title": "Два участника вступили в жёсткий спор и просят вас выбрать сторону.",
        "options": (
            (
                "mediate",
                "Соберу факты, дам высказаться обоим и помогу найти решение.",
                {"cooperation": 3, "prudence": 2},
            ),
            (
                "rules",
                "Верну разговор к правилам и предмету спора.",
                {"responsibility": 2, "prudence": 3},
            ),
            (
                "side",
                "Поддержу того, чью позицию считаю правильной.",
                {"initiative": 1, "cooperation": -1},
            ),
            (
                "leave",
                "Не буду вмешиваться — это их личный конфликт.",
                {"initiative": -2, "cooperation": -2},
            ),
        ),
    },
    {
        "id": "criticism",
        "title": "Ваше предложение публично и резко раскритиковали.",
        "options": (
            (
                "clarify",
                "Попрошу конкретику, отделю полезное от эмоций и доработаю идею.",
                {"resilience": 3, "cooperation": 2},
            ),
            (
                "defend",
                "Спокойно защищу позицию фактами и соглашусь с сильными аргументами.",
                {"resilience": 2, "integrity": 2},
            ),
            (
                "counter",
                "Так же жёстко отвечу, чтобы обозначить границы.",
                {"resilience": 1, "cooperation": -2},
            ),
            (
                "withdraw",
                "Сниму предложение и больше не стану его продвигать.",
                {"initiative": -2, "resilience": -2},
            ),
        ),
    },
    {
        "id": "confidentiality",
        "title": "Друг просит переслать закрытую информацию Товарищества «только ему».",
        "options": (
            (
                "refuse",
                "Откажу и объясню, почему доверие важнее личной просьбы.",
                {"integrity": 3, "responsibility": 2},
            ),
            (
                "permission",
                "Уточню у ответственного, можно ли поделиться частью сведений.",
                {"prudence": 3, "cooperation": 1},
            ),
            (
                "summary",
                "Перескажу без деталей, если это не нанесёт вреда.",
                {"prudence": 1, "integrity": -1},
            ),
            (
                "share",
                "Передам: я доверяю этому человеку.",
                {"integrity": -3, "responsibility": -2},
            ),
        ),
    },
    {
        "id": "initiative",
        "title": "Вы видите важную задачу, но ответственного пока нет.",
        "options": (
            (
                "lead",
                "Сформулирую результат, соберу людей и начну работу.",
                {"initiative": 3, "responsibility": 2},
            ),
            (
                "propose",
                "Предложу задачу и возьму конкретную часть после согласования.",
                {"initiative": 2, "cooperation": 2},
            ),
            (
                "signal",
                "Сообщу руководству и дождусь распределения.",
                {"responsibility": 1, "prudence": 2},
            ),
            ("ignore", "Без поручения заниматься этим не буду.", {"initiative": -3}),
        ),
    },
    {
        "id": "pressure",
        "title": "Нужно принять решение быстро, а информации недостаточно.",
        "options": (
            (
                "minimum",
                "Определю минимально необходимые факты и поставлю короткий срок на их сбор.",
                {"prudence": 3, "resilience": 2},
            ),
            (
                "reversible",
                "Выберу обратимое решение и обозначу риски.",
                {"initiative": 2, "prudence": 2},
            ),
            (
                "intuition",
                "Положусь на опыт и интуицию.",
                {"initiative": 1, "prudence": -1},
            ),
            (
                "delay",
                "Откажусь решать, пока не будет полной информации.",
                {"responsibility": -1, "resilience": -1},
            ),
        ),
    },
    {
        "id": "commitment",
        "title": "Вы понимаете, что не успеваете выполнить обещанное в срок.",
        "options": (
            (
                "early",
                "Предупрежу заранее, покажу прогресс и предложу новый реалистичный план.",
                {"responsibility": 3, "integrity": 2},
            ),
            (
                "help",
                "Попрошу помощь и постараюсь сохранить срок.",
                {"cooperation": 3, "responsibility": 2},
            ),
            (
                "rush",
                "Доделаю любой ценой, даже если пострадает качество.",
                {"resilience": 2, "prudence": -2},
            ),
            (
                "late",
                "Сообщу только тогда, когда срок уже будет сорван.",
                {"responsibility": -3, "integrity": -1},
            ),
        ),
    },
    {
        "id": "dissent",
        "title": "Консенсус принял решение, с которым вы принципиально не согласны.",
        "options": (
            (
                "execute",
                "Зафиксирую особое мнение, но буду исполнять принятое решение.",
                {"integrity": 3, "cooperation": 3},
            ),
            (
                "review",
                "Предложу пересмотр, если появились новые факты, и сохраню уважительный тон.",
                {"initiative": 2, "prudence": 3},
            ),
            (
                "campaign",
                "Продолжу убеждать участников отменить решение.",
                {"initiative": 2, "cooperation": -1},
            ),
            (
                "sabotage",
                "Не стану помогать реализации ошибочного решения.",
                {"responsibility": -3, "cooperation": -3},
            ),
        ),
    },
)


TRAIT_LABELS = {
    "integrity": "Принципиальность",
    "responsibility": "Ответственность",
    "cooperation": "Командность",
    "prudence": "Взвешенность",
    "initiative": "Инициативность",
    "resilience": "Устойчивость",
}


def public_questions() -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "title": item["title"],
            "options": [
                {"id": option[0], "label": option[1]} for option in item["options"]
            ],
        }
        for item in ADMISSION_QUESTIONS
    ]


def evaluate_answers(raw: Any) -> tuple[dict[str, str], dict[str, int]]:
    if not isinstance(raw, dict):
        raise ValueError("admission_answers_invalid")
    clean: dict[str, str] = {}
    totals = {key: 0 for key in TRAIT_LABELS}
    counts = {key: 0 for key in TRAIT_LABELS}
    for question in ADMISSION_QUESTIONS:
        question_id = str(question["id"])
        selected = str(raw.get(question_id) or "").strip()
        option = next(
            (
                candidate
                for candidate in question["options"]
                if candidate[0] == selected
            ),
            None,
        )
        if option is None:
            raise ValueError("admission_answers_incomplete")
        clean[question_id] = selected
        for trait, delta in option[2].items():
            totals[trait] += int(delta)
            counts[trait] += 1
    scores = {
        TRAIT_LABELS[key]: max(
            15,
            min(95, round(50 + (totals[key] / max(1, counts[key])) * 14)),
        )
        for key in TRAIT_LABELS
    }
    return clean, scores


def _delivery_embed(payload: dict[str, Any], *, log: bool) -> discord.Embed:
    status = str(payload.get("status") or "ovr_review")
    colors = {
        "chair_review": 0x8FA7D8,
        "ovr_review": 0xD5B56E,
        "ovr_approved": 0x77CFA5,
        "ovr_denied": 0xD7747D,
        "consensus_queued": 0x7EA4E8,
        "membership_approved": 0x83D7B3,
        "membership_denied": 0xD7747D,
    }
    embed = discord.Embed(
        title=str(payload.get("title") or "Phoenix · статус заявки")[:256],
        description=str(payload.get("body") or "")[:3900],
        color=colors.get(status, 0x8896A7),
    )
    references = []
    if payload.get("case_number"):
        references.append(f"ОВР-{int(payload['case_number']):03d}")
    if payload.get("bill_number"):
        references.append(f"законопроект №{int(payload['bill_number']):03d}")
    if references:
        embed.add_field(
            name="Связанные материалы", value=" · ".join(references), inline=False
        )
    if not log:
        embed.add_field(
            name="Следить за заявкой",
            value=f"[Открыть личный кабинет Phoenix]({payload.get('route')})",
            inline=False,
        )
    embed.set_footer(
        text=(
            f"T-Mod · журнал вступления · заявка #{int(payload.get('application_id') or 0)}"
            if log
            else "T-Mod Phoenix · изменения также сохраняются на сайте"
        )
    )
    return embed


async def deliver_admission_event(
    message: OutboxMessage,
    bot: discord.Client,
) -> DeliveryReceipt:
    payload = dict(message.payload)
    kind = str(payload.get("kind") or "")
    guild_id = int(payload.get("guild_id") or 0)
    if guild_id <= 0 or kind not in {"dm", "log"}:
        raise ValueError("admission_delivery_payload_invalid")
    if kind == "dm":
        user_id = int(payload.get("user_id") or 0)
        recipient = bot.get_user(user_id)
        if recipient is None:
            try:
                recipient = await bot.fetch_user(user_id)
            except discord.DiscordException as exc:
                raise_classified_discord_error(exc, missing_is_permanent=True)
        try:
            sent = await recipient.send(
                embed=_delivery_embed(payload, log=False),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as exc:
            raise_classified_discord_error(exc, missing_is_permanent=True)
        return DeliveryReceipt(message_id=int(sent.id))

    channel = bot.get_channel(ADMISSION_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ADMISSION_LOG_CHANNEL_ID)
        except discord.DiscordException as exc:
            raise_classified_discord_error(exc, missing_is_permanent=True)
    if not callable(getattr(channel, "send", None)):
        raise ValueError("admission_log_channel_invalid")
    try:
        sent = await channel.send(
            content=f"<@{int(payload.get('user_id') or 0)}>",
            embed=_delivery_embed(payload, log=True),
            allowed_mentions=discord.AllowedMentions(users=False),
        )
    except discord.DiscordException as exc:
        raise_classified_discord_error(exc, missing_is_permanent=True)
    return DeliveryReceipt(message_id=int(sent.id))


def _bill_copy(application: dict[str, Any]) -> dict[str, str]:
    characters = list(application.get("characters") or [])
    primary = (
        characters[0] if characters else {"nickname": application.get("user_display")}
    )
    candidate = str(
        primary.get("nickname") or application.get("user_display") or "кандидата"
    )
    case_number = int(application.get("ovr_case_number") or 0)
    character_lines = "\n".join(
        f"• {item.get('nickname')} · #{item.get('static_id')}" for item in characters
    )
    return {
        "title": f"О вступлении {candidate} в Сенат Товарищества · Phoenix",
        "summary": (
            f"Принять **{candidate}** в состав IC-Сената Товарищества на "
            "сервере Majestic RP · Phoenix №15. "
            "Кандидат прошёл обязательную проверку ОВР и допущен к "
            "рассмотрению пленарным консенсусом."
        ),
        "materials": (
            f"Основание: положительное решение по делу ОВР-{case_number:03d}.\n"
            f"Персонажи кандидата:\n{character_lines}\n"
            f"Форумный профиль: {application.get('forum_url')}\n\n"
            "Закрытые материалы и мотивировка ОВР остаются в защищённом контуре."
        ),
        "implementation_plan": (
            "После принятия инициативы один из сопредседателей связывается с "
            "кандидатом, завершает IC-процедуру вступления и запускает онбординг сенатора."
        ),
        "leadership_actions": (
            "Связаться с кандидатом; подтвердить основной персонаж; выдать необходимые "
            "роли после завершения IC-процедуры; проконтролировать активацию Реактора."
        ),
    }


async def ensure_membership_bill(
    bot: discord.Client,
    guild: discord.Guild,
    application: dict[str, Any],
) -> bool:
    runtime = active_sessions.get(int(guild.id))
    if runtime is not None and not bool(getattr(runtime, "finished", False)):
        return False
    copy = _bill_copy(application)
    actor_id = int(getattr(bot.user, "id", 0) or 0)
    actor_display = "T-Mod · Phoenix"
    blocks = [
        {
            "id": "contact-candidate",
            "title": "Связаться с принятым кандидатом",
            "description": "Согласовать IC-вступление и дальнейшие действия.",
            "owner": "Сопредседатели",
            "due": "После принятия",
        },
        {
            "id": "complete-admission",
            "title": "Завершить процедуру вступления",
            "description": "Выдать роли только после подтверждения сопредседателем.",
            "owner": "Руководство Товарищества",
            "due": "По итогам связи",
        },
    ]
    bill, _, _ = await asyncio.to_thread(
        tvrs_storage.tvrs_create_bill_with_publication,
        guild_id=int(guild.id),
        channel_id=int(TVRS_MATERIALS_CHANNEL_ID),
        author_id=actor_id,
        author_display=actor_display,
        title=copy["title"],
        summary=copy["summary"],
        materials=copy["materials"],
        decision_category="ordinary",
        implementation_plan=copy["implementation_plan"],
        leadership_actions=copy["leadership_actions"],
        editor_workspace_id=-int(application["id"]),
        execution_blocks_json=json.dumps(blocks, ensure_ascii=False),
        moderated_by_id=actor_id,
        moderated_by_display=actor_display,
        moderated_at=str(application.get("ovr_decided_at") or "") or None,
        delivery_topic=TVRS_BILL_PUBLICATION_TOPIC,
    )
    _, changed = await asyncio.to_thread(
        admission_storage.link_consensus_bill,
        int(application["id"]),
        guild_id=int(guild.id),
        bill_id=int(bill.id),
        bill_number=int(bill.bill_number),
        actor_id=actor_id,
        actor_display=actor_display,
    )
    wake_delivery_worker()
    return changed


async def process_ovr_decision(
    bot: discord.Client,
    guild: discord.Guild,
    *,
    case_id: int,
    approved: bool,
    actor_id: int,
    actor_display: str,
    note: str,
) -> dict[str, Any] | None:
    application, _ = await asyncio.to_thread(
        admission_storage.record_ovr_decision,
        guild_id=int(guild.id),
        case_id=int(case_id),
        approved=bool(approved),
        actor_id=int(actor_id),
        actor_display=str(actor_display),
        note=str(note),
    )
    wake_delivery_worker()
    if application is not None and approved:
        try:
            await ensure_membership_bill(bot, guild, application)
        except ValueError as exc:
            if str(exc) != "bill_submission_locked_by_active_consensus":
                raise
    return application


async def notify_ovr_desks(
    guild: discord.Guild,
    application: dict[str, Any],
) -> int:
    """Create one durable Reactor alert for every active OVR desk."""

    grants = await asyncio.to_thread(
        web_auth_storage.web_section_grants,
        int(guild.id),
    )
    recipient_ids = {
        int(item.get("user_id") or 0)
        for item in grants
        if str(item.get("section") or "") == "ovr"
        and int(item.get("user_id") or 0) > 0
    }
    recipient_ids.update(
        int(member.id)
        for member in getattr(guild, "members", ())
        if bool(getattr(getattr(member, "guild_permissions", None), "administrator", False))
    )
    case_number = int(application.get("ovr_case_number") or 0)
    primary = next(iter(application.get("characters") or ()), {})
    candidate = str(
        primary.get("nickname")
        or application.get("user_display")
        or "Новый кандидат"
    )
    for user_id in sorted(recipient_ids):
        await asyncio.to_thread(
            reactor_storage.reactor_put_notification,
            guild_id=int(guild.id),
            user_id=int(user_id),
            severity="warning",
            kind="ovr_admission",
            title=f"Новая заявка Phoenix · ОВР-{case_number:03d}",
            body=(
                f"{candidate} подал заявку на вступление. "
                "Откройте дело ОВР и распределите проверку."
            ),
            dedupe_key=f"ovr-admission:{int(application['id'])}",
            route=f"/ovr#/case/{int(application.get('ovr_case_id') or 0)}",
            source_key=f"admission:{int(application['id'])}",
        )
    return len(recipient_ids)


async def notify_chair_desks(
    guild: discord.Guild,
    application: dict[str, Any],
) -> int:
    """Create one durable Reactor alert for each chair reviewing Fellowship entry."""

    recipient_ids = {
        int(member.id)
        for member in getattr(guild, "members", ())
        if is_chair(member)
        or bool(
            getattr(getattr(member, "guild_permissions", None), "administrator", False)
        )
    }
    recipient_ids.update(int(item) for item in TVRS_COCHAIR_IDS if int(item) > 0)
    primary = next(iter(application.get("characters") or ()), {})
    candidate = str(
        primary.get("nickname")
        or application.get("user_display")
        or "Новый кандидат"
    )
    for user_id in sorted(recipient_ids):
        await asyncio.to_thread(
            reactor_storage.reactor_put_notification,
            guild_id=int(guild.id),
            user_id=int(user_id),
            severity="info",
            kind="community_admission",
            title="Новая заявка в Товарищество",
            body=(
                f"{candidate} ожидает решения Совета председателей. "
                "ОВР и пленарный консенсус для этой траектории не требуются."
            ),
            dedupe_key=f"community-admission:{int(application['id'])}",
            route="https://phx.tvr.lat/admission#review",
            source_key=f"admission:{int(application['id'])}",
        )
    return len(recipient_ids)


async def ensure_fellowship_role(
    guild: discord.Guild,
    application: dict[str, Any],
) -> bool:
    """Idempotently project an approved Fellowship decision into Discord."""

    role = guild.get_role(int(TVRS_FELLOWSHIP_ROLE_ID))
    if role is None:
        raise RuntimeError("admission_fellowship_role_missing")
    user_id = int(application.get("user_id") or 0)
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.DiscordException as exc:
            raise RuntimeError("admission_member_unavailable") from exc
    if any(int(item.id) == int(role.id) for item in getattr(member, "roles", ())):
        return False
    try:
        await member.add_roles(
            role,
            reason=(
                "T-Mod Phoenix: заявка в Товарищество одобрена "
                "Советом председателей"
            ),
        )
    except discord.DiscordException as exc:
        raise RuntimeError("admission_fellowship_role_delivery_failed") from exc
    return True


async def reconcile_admission_pipeline(
    bot: discord.Client, guild: discord.Guild
) -> int:
    changed = 0
    approved_community = await asyncio.to_thread(
        admission_storage.approved_community_applications,
        int(guild.id),
    )
    for application in approved_community:
        try:
            changed += int(await ensure_fellowship_role(guild, application))
        except RuntimeError as exc:
            await log_technical_event(
                bot,
                guild,
                title="Phoenix · роль Товарищества ожидает выдачи",
                details=(
                    f"Заявка: **{int(application['id'])}**\n"
                    f"Пользователь: `{int(application['user_id'])}`\n"
                    f"Причина: `{str(exc)}`"
                ),
                dedupe_key=f"community-role:{int(application['id'])}",
                cooldown_seconds=600,
                component="admission",
            )
    stranded_decisions = await asyncio.to_thread(
        admission_storage.unreconciled_ovr_decisions,
        int(guild.id),
    )
    system_actor = int(getattr(bot.user, "id", 0) or 0)
    for decision in stranded_decisions:
        actor_id = int(decision.get("assigned_to_id") or system_actor)
        actor_display = str(decision.get("assigned_to_display") or "T-Mod · Recovery")
        _, recorded = await asyncio.to_thread(
            admission_storage.record_ovr_decision,
            guild_id=int(guild.id),
            case_id=int(decision["ovr_case_id"]),
            approved=str(decision.get("status")) == "approved",
            actor_id=actor_id,
            actor_display=actor_display,
            note=str(
                decision.get("decision_reason")
                or "Решение ОВР восстановлено из журнала дела."
            ),
        )
        changed += int(recorded)
    approved = await asyncio.to_thread(
        admission_storage.approved_without_bill,
        int(guild.id),
    )
    for application in approved:
        try:
            changed += int(await ensure_membership_bill(bot, guild, application))
        except ValueError as exc:
            if str(exc) != "bill_submission_locked_by_active_consensus":
                raise
    pending = await asyncio.to_thread(
        admission_storage.pending_consensus_results,
        int(guild.id),
    )
    actor_id = system_actor
    for application in pending:
        result = await asyncio.to_thread(
            tvrs_storage.tvrs_latest_live_result_for_bill,
            int(guild.id),
            int(application["submitted_bill_id"]),
        )
        if result is None:
            continue
        _, recorded = await asyncio.to_thread(
            admission_storage.record_consensus_result,
            int(application["id"]),
            guild_id=int(guild.id),
            result_status=str(result.get("status") or ""),
            actor_id=actor_id,
            actor_display="T-Mod · Consensus",
        )
        changed += int(recorded)
    if changed:
        wake_delivery_worker()
    return changed


class AdmissionPublicView(discord.ui.View):
    def __init__(self, bot_user_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Подать заявку",
                style=discord.ButtonStyle.link,
                url=ADMISSION_PUBLIC_URL,
                emoji="🌐",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Создать аккаунт",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/users/{int(bot_user_id)}",
                emoji="👤",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Вступить в Discord",
                style=discord.ButtonStyle.link,
                url=DISCORD_TVRS_LINK,
                emoji="🔗",
            )
        )


async def ensure_admission_public_panel(
    bot: discord.Client, guild: discord.Guild
) -> None:
    if ADMISSION_CHANNEL_ID <= 0 or bot.user is None:
        return
    channel = guild.get_channel(ADMISSION_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ADMISSION_CHANNEL_ID)
        except discord.DiscordException:
            return
    if not callable(getattr(channel, "send", None)):
        return
    key = f"{_PANEL_META_PREFIX}:{int(guild.id)}"
    raw_id = str(await asyncio.to_thread(meta_storage.get_meta, key) or "")
    message = None
    if raw_id.isdigit() and callable(getattr(channel, "fetch_message", None)):
        try:
            message = await channel.fetch_message(int(raw_id))
        except discord.DiscordException:
            message = None
    if message is None and callable(getattr(channel, "history", None)):
        try:
            async for candidate in channel.history(limit=100):
                if _is_legacy_admission_panel(candidate, int(bot.user.id)):
                    message = candidate
                    break
        except discord.DiscordException:
            # Missing history permission must not prevent a fresh public panel.
            message = None
    embed = discord.Embed(
        title="Вступить в Товарищество · Phoenix №15",
        description=(
            "На единой странице можно выбрать один из двух путей: стать "
            "**участником Товарищества** или получить **мандат Сената "
            "Majestic RP · Phoenix №15**.\n\n"
            "**Как подать заявку**\n"
            "1. Вступите на сервер Товарищества и откройте ЛС с T-Mod.\n"
            "2. Введите `/account`, придумайте логин и PIN, добавьте персонажа.\n"
            "3. Нажмите **«Подать заявку»** и выберите подходящую траекторию.\n\n"
            "После отправки вы сможете следить за каждым этапом на той же странице. "
            "T-Mod лично сообщит обо всех решениях."
        ),
        color=0xD5B56E,
        url=ADMISSION_PUBLIC_URL,
    )
    embed.add_field(
        name="Товарищество",
        value=(
            "Короткую анкету рассматривает Совет председателей. Участник получает "
            "доступ к общению и пространствам, но не голосует и не получает "
            "сенатские преимущества."
        ),
        inline=False,
    )
    embed.add_field(
        name="Сенат Phoenix",
        value=(
            "Расширенная анкета проходит ОВР и пленарный консенсус. Решение ОВР "
            "обязательно и окончательно."
        ),
        inline=False,
    )
    if ADMISSION_BANNER_URL:
        embed.set_image(url=ADMISSION_BANNER_URL)
    embed.set_footer(text="T-Mod · Phoenix №15 · один аккаунт, два пути")
    view = AdmissionPublicView(int(bot.user.id))
    if message is not None:
        await message.edit(embed=embed, view=view)
        await asyncio.to_thread(meta_storage.set_meta_value, key, str(message.id))
        return
    sent = await channel.send(
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await asyncio.to_thread(meta_storage.set_meta_value, key, str(sent.id))


async def _reconcile_loop(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            try:
                await reconcile_admission_pipeline(bot, guild)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exc()
                await log_technical_event(
                    bot,
                    guild,
                    title="Ошибка цепочки вступления Phoenix",
                    details=f"{type(exc).__name__}: {str(exc)[:1200]}",
                    dedupe_key="admission-reconcile",
                    cooldown_seconds=300,
                    exception=exc,
                    component="admission",
                )
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise


def setup_admission(bot: commands.Bot) -> None:
    register_delivery_handler(
        admission_storage.ADMISSION_DELIVERY_TOPIC,
        lambda message: deliver_admission_event(message, bot),
    )

    @bot.listen("on_ready")
    async def admission_ready() -> None:
        global _reconcile_task
        for guild in bot.guilds:
            try:
                await ensure_admission_public_panel(bot, guild)
            except Exception:
                traceback.print_exc()
        if _reconcile_task is None or _reconcile_task.done():
            _reconcile_task = asyncio.create_task(
                _reconcile_loop(bot),
                name="admission-pipeline-reconciler",
            )
        wake_delivery_worker()


__all__ = [
    "ADMISSION_CHANNEL_ID",
    "ADMISSION_LOG_CHANNEL_ID",
    "ADMISSION_PUBLIC_URL",
    "ADMISSION_QUESTIONS",
    "TRAIT_LABELS",
    "ensure_membership_bill",
    "evaluate_answers",
    "ensure_fellowship_role",
    "notify_chair_desks",
    "notify_ovr_desks",
    "process_ovr_decision",
    "public_questions",
    "reconcile_admission_pipeline",
    "setup_admission",
]
