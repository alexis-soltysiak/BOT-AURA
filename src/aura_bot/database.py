from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(slots=True)
class AuraEntry:
    user_id: int
    score: int


@dataclass(slots=True)
class AuraAverageEntry:
    user_id: int
    score: int
    message_count: int
    average_score: float


@dataclass(slots=True)
class TopMessageEntry:
    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    reaction_points: int


class AuraDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._connection.execute("PRAGMA journal_mode = WAL")
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                reaction_points INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS reaction_events (
                message_id INTEGER NOT NULL,
                reactor_id INTEGER NOT NULL,
                emoji_key TEXT NOT NULL,
                PRIMARY KEY (message_id, reactor_id, emoji_key),
                FOREIGN KEY (message_id) REFERENCES messages (message_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS unique_reactors (
                message_id INTEGER NOT NULL,
                reactor_id INTEGER NOT NULL,
                PRIMARY KEY (message_id, reactor_id),
                FOREIGN KEY (message_id) REFERENCES messages (message_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS aura_scores (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not connected.")
        return self._connection

    async def fetchone(self, query: str, params: tuple[object, ...]) -> aiosqlite.Row | None:
        async with self.connection.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple[object, ...]) -> list[aiosqlite.Row]:
        async with self.connection.execute(query, params) as cursor:
            return await cursor.fetchall()

    async def ensure_message(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO messages (message_id, guild_id, channel_id, author_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                channel_id = excluded.channel_id,
                author_id = excluded.author_id
            """,
            (message_id, guild_id, channel_id, author_id),
        )
        await self.connection.commit()

    async def clear_guild(self, guild_id: int) -> None:
        await self.connection.execute(
            "DELETE FROM messages WHERE guild_id = ?",
            (guild_id,),
        )
        await self.connection.execute(
            "DELETE FROM aura_scores WHERE guild_id = ?",
            (guild_id,),
        )
        await self.connection.commit()

    async def store_message_snapshot(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        reaction_pairs: list[tuple[int, str]],
    ) -> int:
        unique_reactors = {reactor_id for reactor_id, _ in reaction_pairs}
        reaction_points = len(unique_reactors)

        await self.connection.execute(
            """
            INSERT INTO messages (message_id, guild_id, channel_id, author_id, reaction_points)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                channel_id = excluded.channel_id,
                author_id = excluded.author_id,
                reaction_points = excluded.reaction_points
            """,
            (message_id, guild_id, channel_id, author_id, reaction_points),
        )

        if reaction_pairs:
            await self.connection.executemany(
                """
                INSERT OR IGNORE INTO reaction_events (message_id, reactor_id, emoji_key)
                VALUES (?, ?, ?)
                """,
                [(message_id, reactor_id, emoji_key) for reactor_id, emoji_key in reaction_pairs],
            )
            await self.connection.executemany(
                """
                INSERT OR IGNORE INTO unique_reactors (message_id, reactor_id)
                VALUES (?, ?)
                """,
                [(message_id, reactor_id) for reactor_id in unique_reactors],
            )

        if reaction_points > 0:
            await self.connection.execute(
                """
                INSERT INTO aura_scores (guild_id, user_id, score)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    score = score + excluded.score
                """,
                (guild_id, author_id, reaction_points),
            )

        await self.connection.commit()
        return reaction_points

    async def add_reaction(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        reactor_id: int,
        emoji_key: str,
    ) -> bool:
        await self.ensure_message(
            message_id=message_id,
            guild_id=guild_id,
            channel_id=channel_id,
            author_id=author_id,
        )

        cursor = await self.connection.execute(
            """
            INSERT OR IGNORE INTO reaction_events (message_id, reactor_id, emoji_key)
            VALUES (?, ?, ?)
            """,
            (message_id, reactor_id, emoji_key),
        )
        inserted_reaction = cursor.rowcount > 0

        if not inserted_reaction:
            await self.connection.commit()
            return False

        cursor = await self.connection.execute(
            """
            INSERT OR IGNORE INTO unique_reactors (message_id, reactor_id)
            VALUES (?, ?)
            """,
            (message_id, reactor_id),
        )
        inserted_unique_reactor = cursor.rowcount > 0

        if inserted_unique_reactor:
            await self.connection.execute(
                """
                UPDATE messages
                SET reaction_points = reaction_points + 1
                WHERE message_id = ?
                """,
                (message_id,),
            )
            await self.connection.execute(
                """
                INSERT INTO aura_scores (guild_id, user_id, score)
                VALUES (?, ?, 1)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    score = score + 1
                """,
                (guild_id, author_id),
            )

        await self.connection.commit()
        return inserted_unique_reactor

    async def remove_reaction(
        self,
        *,
        message_id: int,
        reactor_id: int,
        emoji_key: str,
    ) -> bool:
        row = await self.fetchone(
            """
            SELECT message_id, guild_id, author_id
            FROM messages
            WHERE message_id = ?
            """,
            (message_id,),
        )
        if row is None:
            return False

        cursor = await self.connection.execute(
            """
            DELETE FROM reaction_events
            WHERE message_id = ? AND reactor_id = ? AND emoji_key = ?
            """,
            (message_id, reactor_id, emoji_key),
        )
        deleted_reaction = cursor.rowcount > 0
        if not deleted_reaction:
            await self.connection.commit()
            return False

        remaining = await self.fetchone(
            """
            SELECT 1
            FROM reaction_events
            WHERE message_id = ? AND reactor_id = ?
            LIMIT 1
            """,
            (message_id, reactor_id),
        )
        if remaining is None:
            await self.connection.execute(
                """
                DELETE FROM unique_reactors
                WHERE message_id = ? AND reactor_id = ?
                """,
                (message_id, reactor_id),
            )
            await self.connection.execute(
                """
                UPDATE messages
                SET reaction_points = CASE
                    WHEN reaction_points > 0 THEN reaction_points - 1
                    ELSE 0
                END
                WHERE message_id = ?
                """,
                (message_id,),
            )
            await self.connection.execute(
                """
                UPDATE aura_scores
                SET score = CASE
                    WHEN score > 0 THEN score - 1
                    ELSE 0
                END
                WHERE guild_id = ? AND user_id = ?
                """,
                (row["guild_id"], row["author_id"]),
            )
            await self.connection.execute(
                """
                DELETE FROM aura_scores
                WHERE guild_id = ? AND user_id = ? AND score <= 0
                """,
                (row["guild_id"], row["author_id"]),
            )

        await self.connection.commit()
        return remaining is None

    async def remove_message(self, message_id: int) -> None:
        row = await self.fetchone(
            """
            SELECT guild_id, author_id, reaction_points
            FROM messages
            WHERE message_id = ?
            """,
            (message_id,),
        )
        if row is None:
            return

        if row["reaction_points"] > 0:
            await self.connection.execute(
                """
                UPDATE aura_scores
                SET score = CASE
                    WHEN score >= ? THEN score - ?
                    ELSE 0
                END
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    row["reaction_points"],
                    row["reaction_points"],
                    row["guild_id"],
                    row["author_id"],
                ),
            )
            await self.connection.execute(
                """
                DELETE FROM aura_scores
                WHERE guild_id = ? AND user_id = ? AND score <= 0
                """,
                (row["guild_id"], row["author_id"]),
            )

        await self.connection.execute(
            "DELETE FROM messages WHERE message_id = ?",
            (message_id,),
        )
        await self.connection.commit()

    async def clear_message_reactions(self, message_id: int) -> None:
        row = await self.fetchone(
            """
            SELECT guild_id, author_id, reaction_points
            FROM messages
            WHERE message_id = ?
            """,
            (message_id,),
        )
        if row is None or row["reaction_points"] <= 0:
            return

        await self.connection.execute(
            "DELETE FROM reaction_events WHERE message_id = ?",
            (message_id,),
        )
        await self.connection.execute(
            "DELETE FROM unique_reactors WHERE message_id = ?",
            (message_id,),
        )
        await self.connection.execute(
            """
            UPDATE messages
            SET reaction_points = 0
            WHERE message_id = ?
            """,
            (message_id,),
        )
        await self.connection.execute(
            """
            UPDATE aura_scores
            SET score = CASE
                WHEN score >= ? THEN score - ?
                ELSE 0
            END
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                row["reaction_points"],
                row["reaction_points"],
                row["guild_id"],
                row["author_id"],
            ),
        )
        await self.connection.execute(
            """
            DELETE FROM aura_scores
            WHERE guild_id = ? AND user_id = ? AND score <= 0
            """,
            (row["guild_id"], row["author_id"]),
        )
        await self.connection.commit()

    async def clear_emoji_from_message(self, message_id: int, emoji_key: str) -> None:
        row = await self.fetchone(
            """
            SELECT guild_id, author_id
            FROM messages
            WHERE message_id = ?
            """,
            (message_id,),
        )
        if row is None:
            return

        affected_rows = await self.fetchall(
            """
            SELECT DISTINCT reactor_id
            FROM reaction_events
            WHERE message_id = ? AND emoji_key = ?
            """,
            (message_id, emoji_key),
        )
        if not affected_rows:
            return

        await self.connection.execute(
            """
            DELETE FROM reaction_events
            WHERE message_id = ? AND emoji_key = ?
            """,
            (message_id, emoji_key),
        )

        lost_unique_reactors = 0
        for affected_row in affected_rows:
            reactor_id = affected_row["reactor_id"]
            remaining = await self.fetchone(
                """
                SELECT 1
                FROM reaction_events
                WHERE message_id = ? AND reactor_id = ?
                LIMIT 1
                """,
                (message_id, reactor_id),
            )
            if remaining is None:
                lost_unique_reactors += 1
                await self.connection.execute(
                    """
                    DELETE FROM unique_reactors
                    WHERE message_id = ? AND reactor_id = ?
                    """,
                    (message_id, reactor_id),
                )

        if lost_unique_reactors > 0:
            await self.connection.execute(
                """
                UPDATE messages
                SET reaction_points = CASE
                    WHEN reaction_points >= ? THEN reaction_points - ?
                    ELSE 0
                END
                WHERE message_id = ?
                """,
                (lost_unique_reactors, lost_unique_reactors, message_id),
            )
            await self.connection.execute(
                """
                UPDATE aura_scores
                SET score = CASE
                    WHEN score >= ? THEN score - ?
                    ELSE 0
                END
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    lost_unique_reactors,
                    lost_unique_reactors,
                    row["guild_id"],
                    row["author_id"],
                ),
            )
            await self.connection.execute(
                """
                DELETE FROM aura_scores
                WHERE guild_id = ? AND user_id = ? AND score <= 0
                """,
                (row["guild_id"], row["author_id"]),
            )

        await self.connection.commit()

    async def top_aura(self, guild_id: int, limit: int = 10) -> list[AuraEntry]:
        rows = await self.fetchall(
            """
            SELECT user_id, score
            FROM aura_scores
            WHERE guild_id = ?
            ORDER BY score DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [AuraEntry(user_id=row["user_id"], score=row["score"]) for row in rows]

    async def top_aura_average(self, guild_id: int, limit: int = 10) -> list[AuraAverageEntry]:
        rows = await self.fetchall(
            """
            SELECT
                author_id AS user_id,
                SUM(reaction_points) AS score,
                COUNT(*) AS message_count,
                CAST(SUM(reaction_points) AS REAL) / COUNT(*) AS average_score
            FROM messages
            WHERE guild_id = ?
            GROUP BY author_id
            HAVING COUNT(*) > 0
            ORDER BY average_score DESC, score DESC, message_count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [
            AuraAverageEntry(
                user_id=row["user_id"],
                score=row["score"],
                message_count=row["message_count"],
                average_score=row["average_score"],
            )
            for row in rows
        ]

    async def top_messages(self, guild_id: int, limit: int = 3) -> list[TopMessageEntry]:
        rows = await self.fetchall(
            """
            SELECT message_id, guild_id, channel_id, author_id, reaction_points
            FROM messages
            WHERE guild_id = ? AND reaction_points > 0
            ORDER BY reaction_points DESC, message_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [
            TopMessageEntry(
                message_id=row["message_id"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                author_id=row["author_id"],
                reaction_points=row["reaction_points"],
            )
            for row in rows
        ]
