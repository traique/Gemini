"""
Wrapper quanh gemini-webapi (thư viện reverse-engineered, dùng cookie
tài khoản Gemini Pro thay vì API key chính thức).

⚠️ Lưu ý: __Secure-1PSID là session token toàn quyền của tài khoản Google.
Không log giá trị này ra console/file log.
"""
import asyncio
import logging
from typing import Optional

from gemini_webapi import ChatSession, GeminiClient

import config

logger = logging.getLogger(__name__)

_client: Optional[GeminiClient] = None
_init_lock = asyncio.Lock()

# Chỉ có 1 tài khoản Gemini Pro / 1 session -> serialize mọi lệnh gọi
# để tránh 2 request cùng lúc làm rối session hoặc bị Google nghi ngờ.
call_lock = asyncio.Lock()

# Phiên chat đa lượt dùng cho tin nhắn "chat tự nhiên" (không cần lệnh /...).
# Bot chỉ phục vụ 1 user (ALLOWED_USER_ID) nên dùng 1 session global là đủ,
# không cần phân theo user_id. Sống trong RAM - mất khi process restart
# (Render free tier ngủ/restart thường xuyên) - đó là giới hạn đã biết,
# không phải lỗi.
_chat_session: Optional[ChatSession] = None


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
                close_delay=600,  # tự đóng session sau 10 phút không dùng
                auto_refresh=True,
            )
            logger.info("GeminiClient đã sẵn sàng.")
        return _client


async def ask(prompt: str, model: Optional[str] = None):
    """
    Gửi 1 prompt single-turn tới Gemini, trả về gemini_webapi.ModelOutput.
    Dùng call_lock để đảm bảo chỉ 1 request chạy tại một thời điểm.

    Nếu lần gọi đầu lỗi (vd session bị auto-close, cookie hết hạn theo
    auto_refresh không thành công - thư viện này là reverse-engineered nên
    không đảm bảo hành vi 100%), reset client và thử lại đúng 1 lần trước
    khi để lỗi bay lên cho caller xử lý.
    """
    kwargs = {}
    if model:
        kwargs["model"] = model

    async with call_lock:
        client = await get_client()
        try:
            return await client.generate_content(prompt, **kwargs)
        except Exception:
            logger.warning(
                "Gọi Gemini lỗi lần 1, reset client và thử lại 1 lần.", exc_info=True
            )
            await reset_client()
            client = await get_client()
            return await client.generate_content(prompt, **kwargs)


async def get_flash_model():
    """
    Tìm model "Flash" trong danh sách model thật của tài khoản (qua
    client.list_models(), chỉ có sau khi init() thành công).

    Lý do cần hàm này: quan sát thực tế cho thấy quota tạo ảnh/ngày của
    model Pro/Thinking thấp hơn nhiều so với Flash trên CÙNG một tài khoản.
    Khi không truyền `model`, gemini_webapi dùng Model.UNSPECIFIED và Google
    có thể route vào Pro -> dễ bị "hết hạn mức" trong khi Flash vẫn dùng
    được bình thường (đã verify bằng cách thử lại đúng prompt trên web UI
    với model Flash). Dùng tên model động (model_name) thay vì hard-code
    string, để không bị vỡ nếu Google đổi tên/định dạng model lần nữa
    (giống lần đổi sang họ Gemini 3.x trước đây).

    Trả về `AvailableModel` nếu tìm thấy, hoặc `None` nếu không (caller nên
    fallback về model mặc định khi đó).
    """
    client = await get_client()
    models = client.list_models() or []
    for m in models:
        name = (getattr(m, "model_name", "") or "").lower()
        if "flash" in name and "thinking" not in name:
            return m
    return None


async def ask_image(prompt: str):
    """
    Biến thể của ask() dành riêng cho tạo ảnh: luôn ưu tiên model Flash vì
    quota tạo ảnh/ngày cao hơn model mặc định (Pro). Nếu không tra được
    model Flash (vd tài khoản không có, hoặc list_models() rỗng), tự động
    fallback về model mặc định như ask() bình thường.
    """
    flash_model = await get_flash_model()
    return await ask(prompt, model=flash_model)


async def reset_client() -> None:
    """Dùng khi cần ép tạo lại client (vd: sau khi đổi cookie mới)."""
    global _client, _chat_session
    async with _init_lock:
        _client = None
        # Chat session cũ gắn với client cũ - reset luôn để tránh lỗi mơ hồ
        # khi đổi cookie nhưng session vẫn dùng metadata cũ.
        _chat_session = None


async def chat(prompt: str):
    """
    Gửi 1 tin nhắn "chat tự nhiên" (đa lượt) tới Gemini - dùng cho mọi tin
    nhắn người dùng gõ KHÔNG kèm lệnh /..., để Gemini nhớ được ngữ cảnh các
    lượt trước (khác với ask(), vốn luôn single-turn, dùng cho /anh /video
    /content vì những lệnh đó cần kết quả độc lập, không phụ thuộc lịch sử
    chat).

    Cùng cơ chế serialize + retry-1-lần-khi-lỗi như ask().
    """
    global _chat_session

    async with call_lock:
        client = await get_client()
        if _chat_session is None:
            _chat_session = client.start_chat()
        try:
            return await _chat_session.send_message(prompt)
        except Exception:
            logger.warning(
                "Gọi Gemini (chat) lỗi lần 1, reset client+session và thử lại 1 lần.",
                exc_info=True,
            )
            await reset_client()
            client = await get_client()
            _chat_session = client.start_chat()
            return await _chat_session.send_message(prompt)


async def reset_chat() -> None:
    """Xoá ngữ cảnh chat tự nhiên hiện tại, bắt đầu hội thoại mới (giữ
    nguyên client/cookie - khác reset_client() là reset cả phiên đăng nhập)."""
    global _chat_session
    _chat_session = None
