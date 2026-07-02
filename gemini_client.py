"""Wrapper quanh gemini-webapi (thư viện reverse-engineered, dùng cookie tài khoản Gemini).

⚠️ __Secure-1PSID là session token toàn quyền của tài khoản Google. Không log ra console/file.
"""
import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from gemini_webapi import ChatSession, GeminiClient

import config
import db

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_VN_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def _now_vn_context() -> str:
    now = datetime.now(_VN_TZ)
    weekday = _VN_WEEKDAYS[now.weekday()]
    return f"[Thời điểm hiện tại: {now:%H:%M} ngày {now:%d/%m/%Y} ({weekday}), giờ Việt Nam]"


_client: Optional[GeminiClient] = None
_init_lock = asyncio.Lock()

# Chỉ 1 tài khoản / 1 session -> serialize mọi request.
call_lock = asyncio.Lock()

_chat_session: Optional[ChatSession] = None

_SETTING_GEM_ID = "chat_gem_id"
_SETTING_GEM_SKILL_HASH = "chat_gem_skill_hash"
_chat_session_skill_hash: Optional[str] = None


async def get_client() -> GeminiClient:
    global _client
    async with _init_lock:
        if _client is None:
            logger.info("Khởi tạo GeminiClient lần đầu...")
            _client = GeminiClient(
                config.GEMINI_SECURE_1PSID,
                config.GEMINI_SECURE_1PSIDTS,
                proxy=config.GEMINI_PROXY,
            )
            await _client.init(
                timeout=60,
                auto_close=True,
                close_delay=600,
                auto_refresh=True,
            )
            logger.info("GeminiClient đã sẵn sàng.")
        return _client


async def _call_with_retry(call_fn):
    """Gọi call_fn() 1 lần; nếu lỗi thì reset client và thử lại đúng 1 lần."""
    try:
        return await call_fn()
    except Exception:
        logger.warning("Gọi Gemini lỗi lần 1, reset và thử lại 1 lần.", exc_info=True)
        await reset_client()
        return await call_fn()


async def ask(prompt: str, model: Optional[str] = None):
    kwargs = {"model": model} if model else {}

    async def _call():
        client = await get_client()
        return await client.generate_content(prompt, **kwargs)

    async with call_lock:
        return await _call_with_retry(_call)


async def get_flash_model():
    """Quota tạo ảnh/ngày của Flash cao hơn Pro/Thinking trên cùng tài khoản."""
    client = await get_client()
    models = client.list_models() or []
    for m in models:
        name = (getattr(m, "model_name", "") or "").lower()
        if "flash" in name and "thinking" not in name:
            return m
    return None


async def ask_image(prompt: str):
    flash_model = await get_flash_model()
    return await ask(prompt, model=flash_model)


async def reset_client() -> None:
    global _client, _chat_session
    async with _init_lock:
        _client = None
        _chat_session = None


async def _get_or_create_chat_gem() -> tuple[Optional[str], str]:
    skill_text = config.load_chat_skill()
    if not skill_text:
        return None, ""

    skill_hash = hashlib.sha256(skill_text.encode("utf-8")).hexdigest()

    stored_gem_id = await db.get_setting(_SETTING_GEM_ID)
    stored_hash = await db.get_setting(_SETTING_GEM_SKILL_HASH)
    if stored_gem_id and stored_hash == skill_hash:
        return stored_gem_id, skill_hash

    client = await get_client()
    if stored_gem_id:
        logger.info("Nội dung chat_skill.txt đã đổi, cập nhật lại Gem hiện có...")
        gem = await client.update_gem(
            stored_gem_id,
            name="Bot Telegram - Chat Skill",
            prompt=skill_text,
            description="Tự động tạo/cập nhật bởi bot Telegram từ chat_skill.txt",
        )
    else:
        logger.info("Chưa có Gem cho chat tự nhiên, tạo mới từ chat_skill.txt...")
        gem = await client.create_gem(
            name="Bot Telegram - Chat Skill",
            prompt=skill_text,
            description="Tự động tạo/cập nhật bởi bot Telegram từ chat_skill.txt",
        )

    await db.set_setting(_SETTING_GEM_ID, gem.id)
    await db.set_setting(_SETTING_GEM_SKILL_HASH, skill_hash)
    return gem.id, skill_hash


async def _ensure_chat_session() -> None:
    global _chat_session, _chat_session_skill_hash
    client = await get_client()
    gem_id, skill_hash = await _get_or_create_chat_gem()
    if _chat_session is None or _chat_session_skill_hash != skill_hash:
        _chat_session = client.start_chat(gem=gem_id) if gem_id else client.start_chat()
        _chat_session_skill_hash = skill_hash


async def chat(prompt: str):
    prompt_with_time = f"{_now_vn_context()}\n{prompt}"

    async def _call():
        await _ensure_chat_session()
        return await _chat_session.send_message(prompt_with_time)

    async with call_lock:
        return await _call_with_retry(_call)


async def reset_chat() -> None:
    global _chat_session, _chat_session_skill_hash
    _chat_session = None
    _chat_session_skill_hash = None


async def analyze_image(instruction: str, image_path: str):
    async def _call():
        client = await get_client()
        return await client.generate_content(instruction, files=[image_path])

    async with call_lock:
        return await _call_with_retry(_call)
