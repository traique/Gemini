"""Encrypted persistence for the personal Zalo session."""
import json
from core import crypto, database as db

_KEY = "zalo:session:v1"

async def load_session() -> dict | None:
    raw = crypto.decrypt(await db.get_setting(_KEY))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None

async def save_session(value: dict) -> None:
    await db.set_setting(_KEY, crypto.encrypt(json.dumps(value, separators=(",", ":"))))

async def clear_session() -> None:
    await db.set_setting(_KEY, "")
