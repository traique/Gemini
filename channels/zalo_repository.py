"""Supabase persistence for dynamically managed Zalo groups."""
import asyncio
import os

from core import database as db

_schema_lock = asyncio.Lock()
_schema_ready = False


def _retention_days() -> int:
    try:
        return max(1, min(365, int(os.getenv("ZALO_GROUP_RETENTION_DAYS", "30"))))
    except ValueError:
        return 30


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_groups (
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (account_id, group_id),
                    UNIQUE (account_id, alias)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS zalo_group_messages (
                    id BIGSERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (account_id, group_id, message_id),
                    FOREIGN KEY (account_id, group_id)
                        REFERENCES zalo_groups(account_id, group_id)
                        ON DELETE CASCADE
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_zalo_group_messages_window
                ON zalo_group_messages (account_id, group_id, sent_at DESC)
                """
            )
        _schema_ready = True


async def list_groups(account_id: str) -> list[tuple[str, str]]:
    await ensure_schema()
    pool = await db.get_pool()
    rows = await pool.fetch(
        "SELECT group_id, alias FROM zalo_groups WHERE account_id = $1 ORDER BY alias",
        account_id,
    )
    return [(row["group_id"], row["alias"]) for row in rows]


async def add_group(account_id: str, group_id: str, alias: str) -> None:
    await ensure_schema()
    pool = await db.get_pool()
    await pool.execute(
        """
        INSERT INTO zalo_groups (account_id, group_id, alias)
        VALUES ($1, $2, $3)
        ON CONFLICT (account_id, group_id)
        DO UPDATE SET alias = EXCLUDED.alias, updated_at = now()
        """,
        account_id,
        group_id,
        alias.lower(),
    )


async def remove_group(account_id: str, target: str) -> bool:
    await ensure_schema()
    pool = await db.get_pool()
    result = await pool.execute(
        "DELETE FROM zalo_groups WHERE account_id = $1 AND (group_id = $2 OR alias = $3)",
        account_id,
        target,
        target.lower(),
    )
    return result != "DELETE 0"


async def save_group_message(
    *,
    account_id: str,
    group_id: str,
    message_id: str,
    sender_id: str,
    sender_name: str,
    text: str,
    sent_at_ms: int,
) -> bool:
    await ensure_schema()
    pool = await db.get_pool()
    result = await pool.execute(
        """
        INSERT INTO zalo_group_messages (
            account_id, group_id, message_id, sender_id, sender_name, content, sent_at
        )
        SELECT $1, $2, $3, $4, $5, $6, to_timestamp($7::double precision / 1000.0)
        WHERE EXISTS (
            SELECT 1 FROM zalo_groups WHERE account_id = $1 AND group_id = $2
        )
        ON CONFLICT (account_id, group_id, message_id) DO NOTHING
        """,
        account_id,
        group_id,
        message_id,
        sender_id,
        sender_name[:500],
        text,
        sent_at_ms,
    )
    return result == "INSERT 0 1"


async def cleanup_old_messages(account_id: str) -> None:
    await ensure_schema()
    pool = await db.get_pool()
    await pool.execute(
        """
        DELETE FROM zalo_group_messages
        WHERE account_id = $1
          AND sent_at < now() - ($2::text || ' days')::interval
        """,
        account_id,
        _retention_days(),
    )
