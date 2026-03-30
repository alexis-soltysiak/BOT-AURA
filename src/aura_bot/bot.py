from __future__ import annotations

from datetime import datetime
import asyncio
import logging
from collections.abc import Iterable

import discord
from discord import app_commands

from aura_bot.config import build_rebuild_start_date, configure_logging, load_settings
from aura_bot.database import AuraAverageEntry, AuraDatabase, AuraEntry, TopMessageEntry

LOGGER = logging.getLogger("aura_bot")


def emoji_to_key(emoji: str | discord.Emoji | discord.PartialEmoji) -> str:
    if isinstance(emoji, str):
        return emoji
    return str(emoji)


def pluralize_points(value: int) -> str:
    suffix = "point" if value == 1 else "points"
    return f"{value} {suffix}"


def pluralize_reactions(value: int) -> str:
    suffix = "reaction unique" if value == 1 else "reactions uniques"
    return f"{value} {suffix}"


class AuraRankingView(discord.ui.View):
    def __init__(self, bot: "AuraBot", guild: discord.Guild | None, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.guild_id = guild_id
        self.mode = "total"
        self.update_buttons()

    def update_buttons(self) -> None:
        self.total_button.disabled = self.mode == "total"
        self.average_button.disabled = self.mode == "average"

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        if self.mode == "total":
            rows = await self.bot.database.top_aura(self.guild_id, limit=10)
            embed = self.bot.build_aura_embed(self.guild, rows)
        else:
            rows = await self.bot.database.top_aura_average(self.guild_id, limit=10)
            embed = self.bot.build_aura_average_embed(self.guild, rows)
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Classement total", style=discord.ButtonStyle.primary)
    async def total_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.mode = "total"
        await self.refresh_message(interaction)

    @discord.ui.button(label="Aura / message", style=discord.ButtonStyle.secondary)
    async def average_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.mode = "average"
        await self.refresh_message(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.success, emoji="🔄")
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.refresh_message(interaction)


class AuraBot(discord.Client):
    def __init__(
        self,
        database: AuraDatabase,
        sync_guild_id: int | None,
        aura_rebuild_allowed_user_id: int | None,
        rebuild_pause_every: int,
        rebuild_pause_seconds: float,
        rebuild_progress_every: int,
    ) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = False

        super().__init__(intents=intents)
        self.database = database
        self.tree = app_commands.CommandTree(self)
        self.sync_guild_id = sync_guild_id
        self.aura_rebuild_allowed_user_id = aura_rebuild_allowed_user_id
        self.rebuild_pause_every = max(1, rebuild_pause_every)
        self.rebuild_pause_seconds = max(0.0, rebuild_pause_seconds)
        self.rebuild_progress_every = max(1, rebuild_progress_every)

    async def setup_hook(self) -> None:
        await self.database.connect()
        self.register_commands()

        if self.sync_guild_id is not None:
            guild = discord.Object(id=self.sync_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                LOGGER.info(
                    "Synced %s guild command(s) to guild %s",
                    len(synced),
                    self.sync_guild_id,
                )
                return
            except discord.Forbidden:
                LOGGER.warning(
                    "Missing access to guild %s for command sync. Falling back to global sync.",
                    self.sync_guild_id,
                )

        synced = await self.tree.sync()
        LOGGER.info("Synced %s global command(s)", len(synced))

    async def close(self) -> None:
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        LOGGER.info("Connected as %s (%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        await self.database.ensure_message(
            message_id=message.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id is None:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return
        member = payload.member
        if member is not None and member.bot:
            return
        if payload.user_id == payload.message_author_id:
            return
        if payload.message_author_id is None:
            message = await self.fetch_message_from_payload(payload)
            if message is None or message.author.bot or payload.user_id == message.author.id:
                return
            author_id = message.author.id
            channel_id = message.channel.id
        else:
            author_id = payload.message_author_id
            channel_id = payload.channel_id

        await self.database.add_reaction(
            message_id=payload.message_id,
            guild_id=payload.guild_id,
            channel_id=channel_id,
            author_id=author_id,
            reactor_id=payload.user_id,
            emoji_key=emoji_to_key(payload.emoji),
        )

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id is None:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return
        message = await self.fetch_message_from_payload(payload)
        if message is not None and (message.author.bot or payload.user_id == message.author.id):
            return

        await self.database.remove_reaction(
            message_id=payload.message_id,
            reactor_id=payload.user_id,
            emoji_key=emoji_to_key(payload.emoji),
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.database.remove_message(payload.message_id)

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        await self.database.clear_message_reactions(payload.message_id)

    async def on_raw_reaction_clear_emoji(
        self, payload: discord.RawReactionClearEmojiEvent
    ) -> None:
        await self.database.clear_emoji_from_message(
            payload.message_id,
            emoji_to_key(payload.emoji),
        )

    async def fetch_message_from_payload(
        self, payload: discord.RawReactionActionEvent
    ) -> discord.Message | None:
        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return None

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None

        try:
            return await channel.fetch_message(payload.message_id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None

    def register_commands(self) -> None:
        @self.tree.command(name="aura", description="Affiche le classement des auras du serveur.")
        async def aura(interaction: discord.Interaction) -> None:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "Cette commande doit etre utilisee dans un serveur.",
                    ephemeral=True,
                )
                return

            rows = await self.database.top_aura(interaction.guild_id, limit=10)
            embed = self.build_aura_embed(interaction.guild, rows)
            view = AuraRankingView(self, interaction.guild, interaction.guild_id)
            await interaction.response.send_message(embed=embed, view=view)

        @self.tree.command(name="faker", description="Affiche les 3 messages avec le plus de reactions.")
        async def faker(interaction: discord.Interaction) -> None:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "Cette commande doit etre utilisee dans un serveur.",
                    ephemeral=True,
                )
                return

            rows = await self.database.top_messages(interaction.guild_id, limit=3)
            embed = self.build_faker_embed(interaction.guild, rows)
            await interaction.response.send_message(embed=embed)

        @self.tree.command(
            name="aura_rebuild",
            description="Rescan complet du serveur pour recalculer les points AURA.",
        )
        @app_commands.describe(
            day="Jour de debut du scan",
            month="Mois de debut du scan",
            year="Annee de debut du scan",
        )
        async def aura_rebuild(
            interaction: discord.Interaction,
            day: app_commands.Range[int, 1, 31] | None = None,
            month: app_commands.Range[int, 1, 12] | None = None,
            year: app_commands.Range[int, 2000, 2100] | None = None,
        ) -> None:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "Cette commande doit etre utilisee dans un serveur.",
                    ephemeral=True,
                )
                return

            if (
                self.aura_rebuild_allowed_user_id is not None
                and interaction.user.id != self.aura_rebuild_allowed_user_id
            ):
                await interaction.response.send_message(
                    "Tu n'es pas autorise a utiliser cette commande.",
                    ephemeral=True,
                )
                return

            try:
                start_date = build_rebuild_start_date(day=day, month=month, year=year)
            except ValueError:
                await interaction.response.send_message(
                    "Pour filtrer la date, renseigne `day`, `month` et `year` ensemble.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            messages_scanned, scored_messages = await self.rebuild_guild_scores(
                interaction.guild,
                start_date=start_date,
            )
            filter_line = (
                f"A partir du {start_date.strftime('%d/%m/%Y')}\n"
                if start_date is not None
                else ""
            )
            await interaction.followup.send(
                (
                    "Reconstruction terminee.\n"
                    f"{filter_line}"
                    f"Messages analyses : {messages_scanned}\n"
                    f"Messages avec points : {scored_messages}"
                ),
                ephemeral=True,
            )

    def build_aura_embed(
        self, guild: discord.Guild | None, rows: list[AuraEntry]
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Classement AURA",
            description="Le top des membres qui font reagir le serveur.",
            color=discord.Color.gold(),
        )
        self.apply_guild_style(embed, guild)

        if not rows:
            embed.description = "Le top des membres qui font reagir le serveur.\n\n✨ Aucun point pour le moment."
            return embed

        total_points = sum(row.score for row in rows)
        leader = rows[0]
        embed.add_field(
            name="👑 Leader",
            value=f"<@{leader.user_id}>\n**{pluralize_points(leader.score)}**",
            inline=True,
        )
        embed.add_field(
            name="🔥 Total",
            value=f"**{total_points} points**",
            inline=True,
        )
        embed.add_field(
            name="📊 Affichage",
            value=f"Top **{len(rows)}**",
            inline=True,
        )

        embed.add_field(
            name="🏆 Podium",
            value=self.render_podium(rows[:3]),
            inline=False,
        )

        remaining = rows[3:10]
        if remaining:
            embed.add_field(
                name="✨ Suite du classement",
                value=self.render_rank_list(remaining, start_rank=4),
                inline=False,
            )

        embed.set_footer(text="1 reaction unique d'une personne = 1 point.")
        return embed

    def build_aura_average_embed(
        self, guild: discord.Guild | None, rows: list[AuraAverageEntry]
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Classement Aura / Message",
            description="Le top des membres avec le plus de reactions en moyenne par message.",
            color=discord.Color.orange(),
        )
        self.apply_guild_style(embed, guild)

        if not rows:
            embed.description = (
                "Le top des membres avec le plus de reactions en moyenne par message.\n\n"
                "✨ Aucun message analyse pour le moment."
            )
            return embed

        leader = rows[0]
        embed.add_field(
            name="👑 Leader moyen",
            value=f"<@{leader.user_id}>\n**{leader.average_score:.2f}** / message",
            inline=True,
        )
        embed.add_field(
            name="📝 Messages",
            value=f"**{leader.message_count}**",
            inline=True,
        )
        embed.add_field(
            name="🔥 Total reactions",
            value=f"**{leader.score}**",
            inline=True,
        )

        embed.add_field(
            name="🏆 Podium",
            value=self.render_average_podium(rows[:3]),
            inline=False,
        )

        remaining = rows[3:10]
        if remaining:
            embed.add_field(
                name="✨ Suite du classement",
                value=self.render_average_rank_list(remaining, start_rank=4),
                inline=False,
            )

        embed.set_footer(text="Moyenne calculee sur tous les messages suivis du membre.")
        return embed

    def build_faker_embed(
        self, guild: discord.Guild | None, rows: list[TopMessageEntry]
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Top Messages",
            description="Les messages qui ont le plus fait reagir le serveur.",
            color=discord.Color.blurple(),
        )
        self.apply_guild_style(embed, guild)

        if not rows:
            embed.description = "Les messages qui ont le plus fait reagir le serveur.\n\n💤 Aucun message classe pour le moment."
            return embed

        total_reactions = sum(row.reaction_points for row in rows)
        embed.add_field(name="💬 Messages", value=f"**{len(rows)}**", inline=True)
        embed.add_field(
            name="⚡ Reactions",
            value=f"**{total_reactions}**",
            inline=True,
        )
        embed.add_field(
            name="🥇 Record",
            value=f"**{pluralize_reactions(rows[0].reaction_points)}**",
            inline=True,
        )

        ranking_names = ["🥇 Premier", "🥈 Deuxieme", "🥉 Troisieme"]
        for index, row in enumerate(rows):
            jump_url = f"https://discord.com/channels/{row.guild_id}/{row.channel_id}/{row.message_id}"
            channel_mention = f"<#{row.channel_id}>"
            value = (
                f"👤 <@{row.author_id}>\n"
                f"📍 {channel_mention}\n"
                f"✨ `{pluralize_reactions(row.reaction_points)}`\n"
                f"🔗 [Voir le message]({jump_url})"
            )
            embed.add_field(name=ranking_names[index], value=value, inline=False)

        embed.set_footer(text="Chaque personne ne compte qu'une fois par message.")
        return embed

    def apply_guild_style(self, embed: discord.Embed, guild: discord.Guild | None) -> None:
        if guild is None:
            return
        if guild.icon is not None:
            embed.set_author(name=guild.name, icon_url=guild.icon.url)
            embed.set_thumbnail(url=guild.icon.url)
        else:
            embed.set_author(name=guild.name)

    def render_podium(self, rows: list[AuraEntry]) -> str:
        icons = ["🥇", "🥈", "🥉"]
        lines: list[str] = []
        for index, row in enumerate(rows):
            lines.append(f"{icons[index]} <@{row.user_id}> • **{row.score}**")
        return "\n".join(lines) or "\u200b"

    def render_rank_list(self, rows: list[AuraEntry], start_rank: int) -> str:
        lines = [
            f"`#{index}` <@{row.user_id}> • **{row.score}**"
            for index, row in enumerate(rows, start=start_rank)
        ]
        return "\n".join(lines) or "\u200b"

    def render_average_podium(self, rows: list[AuraAverageEntry]) -> str:
        icons = ["🥇", "🥈", "🥉"]
        lines: list[str] = []
        for index, row in enumerate(rows):
            lines.append(
                f"{icons[index]} <@{row.user_id}> • **{row.average_score:.2f}**/msg • {row.score} total"
            )
        return "\n".join(lines) or "\u200b"

    def render_average_rank_list(
        self, rows: list[AuraAverageEntry], start_rank: int
    ) -> str:
        lines = [
            f"`#{index}` <@{row.user_id}> • **{row.average_score:.2f}**/msg • {row.message_count} msg"
            for index, row in enumerate(rows, start=start_rank)
        ]
        return "\n".join(lines) or "\u200b"

    async def rebuild_guild_scores(
        self,
        guild: discord.Guild,
        *,
        start_date: datetime | None = None,
    ) -> tuple[int, int]:
        await self.database.clear_guild(guild.id)

        LOGGER.info(
            "Starting AURA rebuild for guild %s (%s)%s",
            guild.name,
            guild.id,
            f" from {start_date.date().isoformat()}" if start_date is not None else "",
        )
        messages_scanned = 0
        scored_messages = 0
        for channel in self.iter_rebuild_channels(guild):
            channel_scanned = 0
            LOGGER.info("Rebuild scanning channel #%s (%s)", channel.name, channel.id)
            try:
                async for message in channel.history(
                    limit=None,
                    oldest_first=True,
                    after=start_date,
                ):
                    messages_scanned += 1
                    channel_scanned += 1
                    reaction_pairs = await self.extract_reaction_pairs(message)
                    points = await self.database.store_message_snapshot(
                        message_id=message.id,
                        guild_id=guild.id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        reaction_pairs=reaction_pairs,
                    )
                    if points > 0:
                        scored_messages += 1
                    if messages_scanned % self.rebuild_progress_every == 0:
                        LOGGER.info(
                            "Rebuild progress | scanned=%s | scored=%s | channel=%s",
                            messages_scanned,
                            scored_messages,
                            channel.name,
                        )
                    if messages_scanned % self.rebuild_pause_every == 0:
                        await asyncio.sleep(self.rebuild_pause_seconds)
            except discord.Forbidden as error:
                LOGGER.warning(
                    "Skipping channel #%s (%s) during rebuild because of permissions: %s",
                    channel.name,
                    channel.id,
                    error,
                )
                continue
            except discord.HTTPException as error:
                LOGGER.warning(
                    "Skipping channel #%s (%s) during rebuild because of HTTP error %s: %s",
                    channel.name,
                    channel.id,
                    error.status,
                    error,
                )
                continue

            LOGGER.info(
                "Finished channel #%s (%s) | scanned=%s | total_scanned=%s",
                channel.name,
                channel.id,
                channel_scanned,
                messages_scanned,
            )

        LOGGER.info(
            "Completed AURA rebuild for guild %s (%s) | scanned=%s | scored=%s",
            guild.name,
            guild.id,
            messages_scanned,
            scored_messages,
        )
        return messages_scanned, scored_messages

    def iter_rebuild_channels(
        self, guild: discord.Guild
    ) -> Iterable[discord.TextChannel | discord.Thread]:
        yielded_ids: set[int] = set()
        for channel in guild.text_channels:
            yielded_ids.add(channel.id)
            yield channel
        for thread in guild.threads:
            if thread.id not in yielded_ids:
                yield thread

    async def extract_reaction_pairs(self, message: discord.Message) -> list[tuple[int, str]]:
        reaction_pairs: list[tuple[int, str]] = []
        for reaction in message.reactions:
            try:
                async for user in reaction.users():
                    if user.bot or user.id == message.author.id:
                        continue
                    reaction_pairs.append((user.id, emoji_to_key(reaction.emoji)))
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not fetch users for reaction on message %s", message.id)
        return reaction_pairs


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)

    database = AuraDatabase(settings.database_path)
    bot = AuraBot(
        database=database,
        sync_guild_id=settings.command_sync_guild_id,
        aura_rebuild_allowed_user_id=settings.aura_rebuild_allowed_user_id,
        rebuild_pause_every=settings.rebuild_pause_every,
        rebuild_pause_seconds=settings.rebuild_pause_seconds,
        rebuild_progress_every=settings.rebuild_progress_every,
    )
    bot.run(settings.discord_token, log_handler=None)
