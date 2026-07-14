import discord
from discord.ext import commands

DISCORD_BUREAU_LINK = "https://discord.gg/buPmaEgZ9B"
DISCORD_TVRS_LINK = "https://discord.gg/5vAKXdX5sw"
ALIASES = {".l", ".л", ".link", ".линк", ".links", ".ссылка", ".ссылки"}


async def send_links_ephemeral(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(f"Ссылка на дискорд с доступом к бюро: {DISCORD_BUREAU_LINK}", ephemeral=True)
    await interaction.followup.send(f"Ссылка на заявку в товарищество: {DISCORD_TVRS_LINK}", ephemeral=True)


async def send_links_dm_or_channel(message: discord.Message) -> None:
    try:
        await message.author.send(f"Ссылка на дискорд с доступом к бюро: {DISCORD_BUREAU_LINK}")
        await message.author.send(f"Ссылка на заявку в товарищество: {DISCORD_TVRS_LINK}")
        try:
            await message.add_reaction("✅")
        except discord.DiscordException:
            pass
    except discord.DiscordException:
        # Prefix commands cannot be truly ephemeral. If DMs are closed, keep it short and temporary.
        try:
            await message.reply(f"Ссылка на дискорд с доступом к бюро: {DISCORD_BUREAU_LINK}", delete_after=60, mention_author=True)
            await message.channel.send(f"Ссылка на заявку в товарищество: {DISCORD_TVRS_LINK}", delete_after=60)
        except discord.DiscordException:
            pass


def setup_links(bot: commands.Bot) -> None:
    @bot.tree.command(name="l", description="Получить ссылки Товарищества")
    async def l(interaction: discord.Interaction) -> None:
        await send_links_ephemeral(interaction)

    @bot.tree.command(name="link", description="Получить ссылки Товарищества")
    async def link(interaction: discord.Interaction) -> None:
        await send_links_ephemeral(interaction)

    @bot.tree.command(name="л", description="Получить ссылки Товарищества")
    async def l_ru(interaction: discord.Interaction) -> None:
        await send_links_ephemeral(interaction)

    @bot.tree.command(name="линк", description="Получить ссылки Товарищества")
    async def link_ru(interaction: discord.Interaction) -> None:
        await send_links_ephemeral(interaction)

    @bot.listen("on_message")
    async def link_prefix_listener(message: discord.Message) -> None:
        if message.author.bot:
            return
        content = (message.content or "").strip().lower()
        if content in ALIASES:
            await send_links_dm_or_channel(message)
