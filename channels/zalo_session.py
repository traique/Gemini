"""Encrypted persistence for the personal Zalo session and controller pairing."""
import json
from core import crypto, database as db

_SESSION_KEY = "zalo:session:v1"
_CONTROLLER_KEY = "zalo:controller:v1"

async def load_session() -> dict | None:
    raw = crypto.decrypt(await db.get_setting(_SESSION_KEY))
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None

async def save_session(value: dict) -> None:
    await db.set_setting(_SESSION_KEY, crypto.encrypt(json.dumps(value, separators=(",", ":"))))

async def clear_session() -> None:
    await db.set_setting(_SESSION_KEY, "")

async def load_controller() -> str:
    return (crypto.decrypt(await db.get_setting(_CONTROLLER_KEY)) or "").strip()

async def save_controller(controller_id: str) -> None:
    await db.set_setting(_CONTROLLER_KEY, crypto.encrypt(controller_id.strip()))

async def clear_controller() -> None:
    await db.set_setting(_CONTROLLER_KEY, "")
