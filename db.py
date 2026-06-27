"""
Lưu trữ bằng Supabase Postgres - bền hơn SQLite vì Render free tier xoá
ổ đĩa local mỗi khi service ngủ/restart, còn Supabase free Postgres thì
không tự hết hạn theo thời gian (khác với Render free Postgres ~30 ngày).
"""
from typing import Optional

import asyncpg

import config

_pool: Optional[asyncpg.pool.Pool] = None


async def get_pool() -> asyncpg.pool.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=30,
            ssl="require",  # Supabase bắt buộc TLS
            # PgBouncer/Supavisor (pooler của Supabase) ở chế độ transaction
            # không giữ được prepared statement giữa các câu lệnh -> phải
            # tắt statement cache của asyncpg để tránh lỗi
            # "prepared statement already exists" / "does not exist".
            statement_cache_size=0,
        )
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                id SERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                command_type TEXT NOT NULL,   -- 'image' | 'video' | 'content'
                prompt TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id SERIAL PRIMARY KEY,
                prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
                result_type TEXT NOT NULL,
                content_text TEXT,
                file_path TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def save_prompt(telegram_user_id: int, command_type: str, prompt: str) -> int:
    pool = await get_pool()
    row = await pool.fetchrow(
        "INSERT INTO prompts (telegram_user_id, command_type, prompt) "
        "VALUES ($1, $2, $3) RETURNING id",
        telegram_user_id,
        command_type,
        prompt,
    )
    return row["id"]


async def save_result(
    prompt_id: int,
    result_type: str,
    content_text: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO results (prompt_id, result_type, content_text, file_path) "
        "VALUES ($1, $2, $3, $4)",
        prompt_id,
        result_type,
        content_text,
        file_path,
    )


async def get_history(telegram_user_id: int, limit: int = 10):
    """Trả về list (command_type, prompt, created_at_iso, result_types_str)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT p.command_type, p.prompt, p.created_at,
               STRING_AGG(DISTINCT r.result_type, ',') AS result_types
        FROM prompts p
        LEFT JOIN results r ON r.prompt_id = p.id
        WHERE p.telegram_user_id = $1
        GROUP BY p.id
        ORDER BY p.id DESC
        LIMIT $2
        """,
        telegram_user_id,
        limit,
    )
    return [
        (r["command_type"], r["prompt"], r["created_at"].isoformat(), r["result_types"] or "")
        for r in rows
    ]


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
