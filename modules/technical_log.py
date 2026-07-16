"""Discord adapter for deduplicated technical events."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import traceback
from typing import Any

import discord

from modules.control_center_runtime import resolve_registered_channel


_last_sent: dict[tuple[int, str], float] = {}


async def log_technical_event(
    bot: Any,
    guild: discord.Guild,
    *,
    title: str,
    details: str,
    level: str = "error",
    dedupe_key: str | None = None,
    cooldown_seconds: int = 300,
    mention_everyone: bool = False,
) -> bool:
    del bot  # Reserved for future gateway-based delivery.
    key = (guild.id, dedupe_key or title)
    now = asyncio.get_running_loop().time()
    previous = _last_sent.get(key)
    if previous is not None and now - previous < cooldown_seconds:
        return True
    try:
        channel = await resolve_registered_channel(guild, "tech_log")
        if channel is None:
            return False
        colors = {
            "warning": discord.Color.orange(),
            "info": discord.Color.blue(),
            "error": discord.Color.red(),
        }
        icons = {"warning": "🟠", "info": "🔵", "error": "🔴"}
        embed = discord.Embed(
            title=f"{icons.get(level, '🔴')} {title}",
            description=str(details)[:3900],
            color=colors.get(level, discord.Color.red()),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="T-Mod • технический журнал")
        await channel.send(
            content="@everyone" if mention_everyone else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=mention_everyone,
                users=False,
                roles=False,
                replied_user=False,
            ),
        )
        _last_sent[key] = now
        return True
    except Exception:
        traceback.print_exc()
        return False
