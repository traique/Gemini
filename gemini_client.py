"""
Wrapper quanh gemini-webapi (thư viện reverse-engineered, dùng cookie
tài khoản Gemini Pro thay vì API key chính thức).

⚠️ Lưu ý: __Secure-1PSID là session token toàn quyền của tài khoản Google.
Không log giá trị này ra console/file log.
"""
import asyncio
import logging
from typing import Optional

from gemini_webapi import GeminiClient

import config

logger = logging.getLogger(__name__)

_client: Optional[GeminiClient] = None
_init_lock = asyncio.Lock()

# Chỉ có 1 tài khoản Gemini Pro / 1 session -> serialize mọi lệnh gọi
# để tránh 2 request cùng lúc làm rối session hoặc bị Google nghi ngờ.
call_lock = asyncio.Lock()


async def get_client() -> GeminiClient:
    global _client
    async with _init_lock:
        if _client is None:
            logger.info("Khởi tạo GeminiClient lần đầu...")
            _client = GeminiClient(
                config.GEMINI_SECURE_1PSID,
                config.GEMINI_SECURE_1PSIDTS,
            )
            await _client.init(
                timeout=60,
                auto_close=True,
                close_delay=600,  # tự đóng session sau 10 phút không dùng
                auto_refresh=True,
            )
            logger.info("GeminiClient đã sẵn sàng.")
        return _client


async def ask(prompt: str, model: Optional[str] = None):
    """
    Gửi 1 prompt single-turn tới Gemini, trả về gemini_webapi.ModelOutput.
    Dùng call_lock để đảm bảo chỉ 1 request chạy tại một thời điểm.
    """
    client = await get_client()
    kwargs = {}
    if model:
        kwargs["model"] = model

    async with call_lock:
        response = await client.generate_content(prompt, **kwargs)
    return response


async def reset_client() -> None:
    """Dùng khi cần ép tạo lại client (vd: sau khi đổi cookie mới)."""
    global _client
    async with _init_lock:
        _client = None
