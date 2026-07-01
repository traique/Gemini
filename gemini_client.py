"""
Wrapper quanh gemini-webapi (thư viện reverse-engineered, dùng cookie
tài khoản Gemini Pro thay vì API key chính thức).

⚠️ Lưu ý: __Secure-1PSID là session token toàn quyền của tài khoản Google.
Không log giá trị này ra console/file log.
"""
import asyncio
import hashlib
import logging
from typing import Optional

from gemini_webapi import ChatSession, GeminiClient

import config
import db

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

# Keys trong bảng settings (xem db.py) để persist Gem id của chat tự nhiên
# qua các lần restart, tránh tạo Gem trùng lặp trên tài khoản Gemini.
_SETTING_GEM_ID = "chat_gem_id"
_SETTING_GEM_SKILL_HASH = "chat_gem_skill_hash"

# Hash nội dung skill đã dùng để tạo _chat_session hiện tại (trong RAM) -
# giúp phát hiện skill vừa bị đổi (chat_skill.txt sửa) MÀ CHƯA restart
# process, để tự tạo lại session với Gem mới ngay lần chat kế tiếp thay vì
# phải đợi deploy lại mới nhận skill mới.
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


async def _get_or_create_chat_gem() -> tuple[Optional[str], str]:
    """
    Đảm bảo có 1 Gem (system prompt) trên tài khoản Gemini khớp với nội dung
    hiện tại của chat_skill.txt, và trả về (gem_id, skill_hash).

    - Nếu chat_skill.txt rỗng/không tồn tại -> trả (None, "") -> chat() sẽ
      không gắn Gem, hoạt động như chat thường không giới hạn phạm vi.
    - Nếu đã có gem_id lưu trong DB và hash khớp nội dung file hiện tại ->
      dùng lại gem_id đó (không gọi thêm request nào lên Gemini).
    - Nếu hash lệch (skill vừa được sửa) hoặc chưa từng tạo -> tạo/update
      Gem trên Gemini rồi lưu lại gem_id + hash mới vào DB.
    """
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
        # Skill đã đổi nội dung - update Gem hiện có thay vì tạo Gem mới,
        # để tránh tích tụ rác trong danh sách Gem trên tài khoản Gemini.
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


async def chat(prompt: str):
    """
    Gửi 1 tin nhắn "chat tự nhiên" (đa lượt) tới Gemini - dùng cho mọi tin
    nhắn người dùng gõ KHÔNG kèm lệnh /..., để Gemini nhớ được ngữ cảnh các
    lượt trước (khác với ask(), vốn luôn single-turn, dùng cho /anh /video
    /content vì những lệnh đó cần kết quả độc lập, không phụ thuộc lịch sử
    chat).

    Mỗi session được gắn 1 Gem (system prompt) đọc từ chat_skill.txt, để
    Gemini chỉ trả lời trong phạm vi bạn định nghĩa trước - xem
    _get_or_create_chat_gem(). Nếu file skill rỗng, chat chạy không giới hạn
    phạm vi như trước đây.

    Cùng cơ chế serialize + retry-1-lần-khi-lỗi như ask().
    """
    global _chat_session, _chat_session_skill_hash

    async with call_lock:
        client = await get_client()
        gem_id, skill_hash = await _get_or_create_chat_gem()

        # Tạo lại session nếu: chưa có session, HOẶC skill vừa đổi nội dung
        # kể từ lần tạo session gần nhất (không cần đợi restart process).
        if _chat_session is None or _chat_session_skill_hash != skill_hash:
            _chat_session = client.start_chat(gem=gem_id) if gem_id else client.start_chat()
            _chat_session_skill_hash = skill_hash

        try:
            return await _chat_session.send_message(prompt)
        except Exception:
            logger.warning(
                "Gọi Gemini (chat) lỗi lần 1, reset client+session và thử lại 1 lần.",
                exc_info=True,
            )
            await reset_client()
            client = await get_client()
            gem_id, skill_hash = await _get_or_create_chat_gem()
            _chat_session = client.start_chat(gem=gem_id) if gem_id else client.start_chat()
            _chat_session_skill_hash = skill_hash
            return await _chat_session.send_message(prompt)


async def reset_chat() -> None:
    """Xoá ngữ cảnh chat tự nhiên hiện tại, bắt đầu hội thoại mới (giữ
    nguyên client/cookie - khác reset_client() là reset cả phiên đăng nhập).
    Lần chat kế tiếp sẽ tự kiểm tra lại chat_skill.txt và gắn Gem tương ứng."""
    global _chat_session, _chat_session_skill_hash
    _chat_session = None
    _chat_session_skill_hash = None


async def analyze_image(instruction: str, image_path: str):
    """
    Gửi 1 ảnh (single-turn, KHÔNG dùng chung chat session với chat tự
    nhiên) kèm instruction tới Gemini để phân tích - dùng cho việc "gửi ảnh
    -> Gemini viết lại thành prompt tạo ảnh" thay vì tự tạo ảnh (tính năng
    tạo ảnh của gemini-webapi đang bị chặn theo vị trí server, xem ghi chú
    trong config.py về GEMINI_PROXY; NHƯNG phân tích ảnh (vision, không tạo
    ảnh mới) là tính năng khác, không bị ảnh hưởng bởi hạn chế đó).

    image_path: đường dẫn file ảnh THẬT trên đĩa (không dùng bytes thô) -
    generate_content() của thư viện chỉ giữ đúng tên file + phần mở rộng
    (quyết định content-type khi upload) khi truyền str/Path; truyền bytes
    thô sẽ bị đặt tên ngẫu nhiên với đuôi .txt mặc định, khiến Google nhận
    sai kiểu file.

    Cùng cơ chế serialize + retry-1-lần-khi-lỗi như ask().
    """
    async with call_lock:
        client = await get_client()
        try:
            return await client.generate_content(instruction, files=[image_path])
        except Exception:
            logger.warning(
                "Gọi Gemini (analyze_image) lỗi lần 1, reset client và thử lại 1 lần.",
                exc_info=True,
            )
            await reset_client()
            client = await get_client()
            return await client.generate_content(instruction, files=[image_path])
