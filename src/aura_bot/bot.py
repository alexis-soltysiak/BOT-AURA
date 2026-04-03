from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime

import discord
from discord import app_commands

from aura_bot.config import build_rebuild_start_date, configure_logging, load_settings
from aura_bot.database import AuraDatabase, AuraEntry, TopMessageEntry

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


def medal_for_rank(rank: int) -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    return medals.get(rank, f"`#{rank}`")


class YearSelectorView(discord.ui.View):
    def __init__(self, bot: "AuraBot", guild: discord.Guild | None, guild_id: int) -> None:
        super().__init__(timeout=604800)
        self.bot = bot
        self.guild = guild
        self.guild_id = guild_id
        self.years = self.bot.available_years()
        self.selected_year = self.years[0]
        self.update_buttons()

    def update_buttons(self) -> None:
        self.year_one_button.label = str(self.years[0])
        self.year_two_button.label = str(self.years[1])
        self.year_three_button.label = str(self.years[2])
        self.year_one_button.disabled = self.selected_year == self.years[0]
        self.year_two_button.disabled = self.selected_year == self.years[1]
        self.year_three_button.disabled = self.selected_year == self.years[2]
        self.spacer_button.disabled = True

    async def build_embed(self) -> discord.Embed:
        raise NotImplementedError

    async def refresh_message(self, interaction: discord.Interaction) -> None:
        embed = await self.build_embed()
        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="2026", style=discord.ButtonStyle.primary)
    async def year_one_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.selected_year = self.years[0]
        await self.refresh_message(interaction)

    @discord.ui.button(label="2025", style=discord.ButtonStyle.secondary)
    async def year_two_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.selected_year = self.years[1]
        await self.refresh_message(interaction)

    @discord.ui.button(label="2024", style=discord.ButtonStyle.secondary)
    async def year_three_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.selected_year = self.years[2]
        await self.refresh_message(interaction)

    @discord.ui.button(label="\u200b", style=discord.ButtonStyle.secondary, disabled=True)
    async def spacer_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.success, emoji="🔄")
    async def refresh_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.refresh_message(interaction)


class AuraRankingView(YearSelectorView):
    async def build_embed(self) -> discord.Embed:
        rows = await self.bot.database.top_aura_for_year(
            self.guild_id,
            year=self.selected_year,
            limit=10,
        )
        return self.bot.build_aura_embed(self.guild, rows, year=self.selected_year)


class FakerRankingView(YearSelectorView):
    async def build_embed(self) -> discord.Embed:
        rows = await self.bot.database.top_messages_for_year(
            self.guild_id,
            year=self.selected_year,
            limit=5,
        )
        return self.bot.build_faker_embed(self.guild, rows, year=self.selected_year)


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

    def available_years(self) -> list[int]:
        current_year = datetime.now().year
        return [current_year, current_year - 1, current_year - 2]

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

            year = self.available_years()[0]
            rows = await self.database.top_aura_for_year(interaction.guild_id, year=year, limit=10)
            embed = self.build_aura_embed(interaction.guild, rows, year=year)
            view = AuraRankingView(self, interaction.guild, interaction.guild_id)
            await interaction.response.send_message(embed=embed, view=view)

        @self.tree.command(
            name="faker",
            description="Affiche les 5 messages avec le plus de reactions.",
        )
        async def faker(interaction: discord.Interaction) -> None:
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "Cette commande doit etre utilisee dans un serveur.",
                    ephemeral=True,
                )
                return

            year = self.available_years()[0]
            rows = await self.database.top_messages_for_year(
                interaction.guild_id,
                year=year,
                limit=5,
            )
            embed = self.build_faker_embed(interaction.guild, rows, year=year)
            view = FakerRankingView(self, interaction.guild, interaction.guild_id)
            await interaction.response.send_message(embed=embed, view=view)

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
        self,
        guild: discord.Guild | None,
        rows: list[AuraEntry],
        *,
        year: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Classement AURA {year}",
            description=f"Le top des membres qui ont fait reagir le serveur en {year}.",
            color=discord.Color.gold(),
        )
        self.apply_guild_style(embed, guild)

        if not rows:
            embed.description = (
                f"Le top des membres qui ont fait reagir le serveur en {year}.\n\n"
                "Aucun point pour le moment."
            )
            return embed

        total_points = sum(row.score for row in rows)
        leader = rows[0]
        embed.add_field(
            name="Leader",
            value=f"<@{leader.user_id}>\n**{pluralize_points(leader.score)}**",
            inline=True,
        )
        embed.add_field(
            name="Total",
            value=f"**{total_points} points**",
            inline=True,
        )
        embed.add_field(
            name="Affichage",
            value=f"Top **{len(rows)}**",
            inline=True,
        )
        embed.add_field(
            name="Classement",
            value=self.render_aura_ranking(rows),
            inline=False,
        )
        embed.set_footer(text="1 reaction unique d'une personne = 1 point.")
        return embed

    def build_faker_embed(
        self,
        guild: discord.Guild | None,
        rows: list[TopMessageEntry],
        *,
        year: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"Top Messages {year}",
            description=f"Les messages qui ont le plus fait reagir le serveur en {year}.",
            color=discord.Color.blurple(),
        )
        self.apply_guild_style(embed, guild)

        if not rows:
            embed.description = (
                f"Les messages qui ont le plus fait reagir le serveur en {year}.\n\n"
                "Aucun message classe pour le moment."
            )
            return embed

        total_reactions = sum(row.reaction_points for row in rows)
        embed.add_field(name="Messages", value=f"**{len(rows)}**", inline=True)
        embed.add_field(name="Reactions", value=f"**{total_reactions}**", inline=True)
        embed.add_field(
            name="Record",
            value=f"**{pluralize_reactions(rows[0].reaction_points)}**",
            inline=True,
        )
        embed.add_field(
            name="Top 5",
            value=self.render_faker_ranking(rows),
            inline=False,
        )
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

    def render_aura_ranking(self, rows: list[AuraEntry]) -> str:
        lines = [
            f"{medal_for_rank(rank)} <@{row.user_id}> - **{row.score}**"
            for rank, row in enumerate(rows, start=1)
        ]
        return "\n".join(lines) or "\u200b"

    def render_faker_ranking(self, rows: list[TopMessageEntry]) -> str:
        lines: list[str] = []
        for rank, row in enumerate(rows, start=1):
            jump_url = (
                f"https://discord.com/channels/{row.guild_id}/{row.channel_id}/{row.message_id}"
            )
            lines.append(
                f"{medal_for_rank(rank)} {row.reaction_points} {self.reaction_emoji(rank)} <@{row.author_id}>"
            )
            lines.append(f"[Voir le message]({jump_url})")
        return "\n".join(lines) or "\u200b"

    def reaction_emoji(self, rank: int) -> str:
        if rank == 1:
            return "🔥"
        if rank == 2:
            return "✨"
        if rank == 3:
            return "⚡"
        return "💬"

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
