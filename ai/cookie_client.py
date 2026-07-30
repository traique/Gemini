"""Nhánh cookie của provider-chain: wrapper quanh gemini-webapi (thư viện
reverse-engineered, dùng cookie tài khoản Gemini cá nhân).

⚠️ __Secure-1PSID là session token toàn quyền của tài khoản Google. Không log
ra console/file.

__Secure-1PSIDTS tự "rotate" mỗi ~10 phút (gemini-webapi auto_refresh=True),
nhưng giá trị mới chỉ sống trong RAM/tmp - mất khi container restart. Module
này định kỳ lưu giá trị đã rotate vào DB (mã hoá qua core.crypto) để lần khởi
động sau dùng bản mới nhất thay vì cookie tĩnh trong biến môi trường.
"""
import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from gemini_webapi import ChatSession, GeminiClient, set_log_level

import logging_setup
from core import config, crypto
from core import database as db
from ai.provider_state import SETTING_PSIDTS_SEED, send_alert
import messages

logger = logging.getLogger(__name__)

# Chỉ bật debug khi thật sự cần chẩn đoán. Log DEBUG của gemini-webapi có thể
# in cookie rotate dạng plaintext, không nên bật mặc định trên Render.
set_log_level("DEBUG" if config.GEMINI_WEBAPI_DEBUG else "WARNING")

_client: Optional[GeminiClient] = None
_init_lock = asyncio.Lock()

_SETTING_PSIDTS = "gemini_1psidts"
# Giảm từ 300s -> 90s để cửa sổ có thể mất token (nếu process chết ngay sau
# khi token vừa rotate mà chưa kịp lưu) nhỏ lại.
_PSIDTS_PERSIST_INTERVAL = 90
_persist_task: Optional[asyncio.Task] = None

# Cảnh báo sớm hơn: nếu __Secure-1PSIDTS không rotate trong nhiều chu kỳ liên
# tiếp (bình thường gemini-webapi tự rotate mỗi ~10 phút), có thể là dấu hiệu
# phiên sắp hết hạn.
_PSIDTS_STALE_WARN_CYCLES = 8  # ~8 * 90s = 12 phút không đổi
_psidts_last_seen: Optional[str] = None
_psidts_unchanged_cycles = 0
_psidts_stale_alerted = False


def _extract_1psidts(client: GeminiClient) -> Optional[str]:
    try:
        for cookie in client.cookies.jar:
            if cookie.name == "__Secure-1PSIDTS" and cookie.value:
                return cookie.value
    except Exception:
        logger.warning("Không đọc được cookie hiện tại của client.", exc_info=True)
    return None


# gemini-webapi tự cache cookie ra 1 file riêng (mặc định
# {tempdir}/gemini_webapi/.cached_cookies_{1PSID}.json, đổi được qua env
# GEMINI_COOKIE_PATH) và khi init() sẽ ƯU TIÊN đọc file này trước cookie
# mình truyền vào - tạo ra 2 nguồn cookie không biết tới nhau (file của thư
# viện vs DB Fernet của mình). Xoá file này trước mỗi lần init để DB luôn là
# nguồn DUY NHẤT được tin tưởng; thư viện vẫn tự ghi lại file trong lúc
# process đang chạy (vô hại, chỉ không được đọc lại ở lần init kế tiếp).
def _library_cookie_cache_path(secure_1psid: str) -> Path:
    base = os.getenv("GEMINI_COOKIE_PATH") or tempfile.gettempdir()
    return Path(base) / "gemini_webapi" / f".cached_cookies_{secure_1psid}.json"


def _reset_library_cookie_cache() -> None:
    if not config.GEMINI_SECURE_1PSID:
        return
    try:
        _library_cookie_cache_path(config.GEMINI_SECURE_1PSID).unlink(missing_ok=True)
    except Exception:
        logger.warning("Không xoá được cache cookie cũ của gemini-webapi.", exc_info=True)


async def _persist_1psidts_if_changed(client: GeminiClient) -> None:
    new_value = _extract_1psidts(client)
    if not new_value:
        return
    current = crypto.decrypt(await db.get_setting(_SETTING_PSIDTS))
    if new_value != current:
        await db.set_setting(_SETTING_PSIDTS, crypto.encrypt(new_value))
        # Giá trị mới sau rotate chưa nằm trong danh sách redact (vốn chỉ nạp
        # 1 lần lúc khởi động từ env) - đăng ký thêm để log/traceback về sau
        # cũng che được token mới, không chỉ token gốc.
        logging_setup.add_redacted_secret(new_value)
        logger.info("Đã lưu __Secure-1PSIDTS vừa rotate vào DB.")


async def _cookie_persist_loop() -> None:
    """Định kỳ lưu cookie đã rotate vào DB, để lần restart sau dùng bản mới nhất
    thay vì cookie tĩnh trong biến môi trường."""
    global _psidts_last_seen, _psidts_unchanged_cycles, _psidts_stale_alerted
    while True:
        await asyncio.sleep(_PSIDTS_PERSIST_INTERVAL)
        client = _client
        if client is None:
            return
        try:
            await _persist_1psidts_if_changed(client)
            current = _extract_1psidts(client)
            if current and current == _psidts_last_seen:
                _psidts_unchanged_cycles += 1
                if _psidts_unchanged_cycles == _PSIDTS_STALE_WARN_CYCLES and not _psidts_stale_alerted:
                    _psidts_stale_alerted = True
                    send_alert(messages.COOKIE_STALE_WARNING)
            else:
                _psidts_unchanged_cycles = 0
                _psidts_stale_alerted = False
            if current:
                _psidts_last_seen = current
        except Exception:
            logger.warning("Lỗi khi lưu cookie đã rotate.", exc_info=True)


async def _safe_close(client: Optional[GeminiClient]) -> None:
    """Đóng 1 GeminiClient một cách an toàn (không để lỗi khi đóng làm hỏng
    luồng chính). Tên method `close()` cần khớp với version gemini-webapi
    đang dùng - kiểm tra lại nếu nâng cấp thư viện."""
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        logger.warning("Lỗi khi đóng GeminiClient cũ.", exc_info=True)


async def get_client() -> GeminiClient:
    global _client, _persist_task
    async with _init_lock:
        if _client is not None:
            return _client

        logger.info("Khởi tạo GeminiClient lần đầu...")

        stored_seed = crypto.decrypt(await db.get_setting(SETTING_PSIDTS_SEED))
        stored_psidts = crypto.decrypt(await db.get_setting(_SETTING_PSIDTS))

        if stored_seed == config.GEMINI_SECURE_1PSIDTS and stored_psidts:
            # Env var chưa đổi -> dùng bản đã rotate trong DB (mới hơn).
            psidts = stored_psidts
            logger.info("Dùng __Secure-1PSIDTS đã rotate trong DB.")
        else:
            # Env var mới hơn cache DB -> luôn ưu tiên env var.
            psidts = config.GEMINI_SECURE_1PSIDTS
            if stored_seed is not None:
                logger.info("Phát hiện env var 1PSIDTS mới -> bỏ cache DB cũ, ưu tiên thử cookie ngay.")
            await db.set_setting(SETTING_PSIDTS_SEED, crypto.encrypt(config.GEMINI_SECURE_1PSIDTS or ""))
            await db.set_setting(_SETTING_PSIDTS, crypto.encrypt(config.GEMINI_SECURE_1PSIDTS or ""))

        # Khởi tạo trên biến cục bộ, chỉ gán vào _client sau khi init()
        # thành công, để tránh client chưa init xong bị dùng mãi về sau.
        new_client = GeminiClient(
            config.GEMINI_SECURE_1PSID,
            psidts,
            proxy=config.GEMINI_PROXY,
        )
        _reset_library_cookie_cache()
        try:
            await new_client.init(
                timeout=15,
                auto_close=False,
                auto_refresh=True,
                refresh_interval=config.GEMINI_COOKIE_REFRESH_INTERVAL,
            )
        except BaseException:
            # BaseException để bắt cả CancelledError khi bị huỷ giữa init().
            logger.warning(
                "Khởi tạo GeminiClient lỗi hoặc bị huỷ giữa chừng, đóng client "
                "tạm và giữ _client=None để lần gọi sau thử init lại từ đầu.",
                exc_info=True,
            )
            await _safe_close(new_client)
            raise

        logger.info("GeminiClient đã sẵn sàng.")

        await _persist_1psidts_if_changed(new_client)
        if _persist_task:
            _persist_task.cancel()
        _persist_task = asyncio.create_task(_cookie_persist_loop())

        _client = new_client
        return _client


async def reset_client() -> None:
    global _client, _persist_task
    global _chat_session, _chat_session_skill_hash
    global _psidts_last_seen, _psidts_unchanged_cycles, _psidts_stale_alerted
    async with _init_lock:
        old_client = _client
        if _persist_task:
            _persist_task.cancel()
            _persist_task = None
        _client = None
        _chat_session = None
        _chat_session_skill_hash = None
        _psidts_last_seen = None
        _psidts_unchanged_cycles = 0
        _psidts_stale_alerted = False

    # Đóng client cũ NGOÀI _init_lock (để không giữ lock trong lúc await
    # I/O đóng client, chặn get_client() của các request khác chờ oan).
    # QUAN TRỌNG: client cũ được tạo với auto_close=False, auto_refresh=True
    # -> nếu chỉ gán _client = None mà không đóng hẳn, vòng auto_refresh nền
    # của nó vẫn chạy, tiếp tục tự rotate __Secure-1PSIDTS và vô hiệu hoá
    # token của client MỚI được tạo sau đó (death spiral: client mới vừa
    # init xong đã bị client cũ âm thầm rotate cookie ngầm bên dưới).
    await _safe_close(old_client)


# ─── ChatSession (persona) - chỉ dùng cho nhánh cookie của chat() ──────────
_chat_session: Optional[ChatSession] = None
_chat_session_skill_hash: Optional[str] = None

_SETTING_GEM_ID = "chat_gem_id"
_SETTING_GEM_SKILL_HASH = "chat_gem_skill_hash"
_SETTING_PREFERRED_MODEL = "preferred_model_name"


def clear_chat_session() -> None:
    """Xoá ChatSession phía cookie. ai.orchestrator.reset_chat() gọi hàm này
    dưới call_lock; core.database.clear_chat(user_id) xoá thêm cửa sổ trượt
    phía API - 2 việc tách biệt vì mỗi nhánh giữ lịch sử theo cách khác nhau."""
    global _chat_session, _chat_session_skill_hash
    _chat_session = None
    _chat_session_skill_hash = None


async def get_preferred_model_name() -> Optional[str]:
    value = await db.get_setting(_SETTING_PREFERRED_MODEL)
    return value or None


async def set_preferred_model_name(name: Optional[str]) -> None:
    await db.set_setting(_SETTING_PREFERRED_MODEL, name or "")


async def list_models() -> list:
    client = await get_client()
    return client.list_models() or []


async def find_model(query: str):
    query_lower = query.strip().lower()
    for m in await list_models():
        name = (getattr(m, "model_name", "") or "").lower()
        if query_lower == name or query_lower in name:
            return m
    return None


async def get_flash_model():
    """Chat với Gem (tài khoản free, không có Gemini Advanced) dễ bị Google
    xếp Pro vào hàng đợi vô thời hạn -> chỉ định rõ Flash cho ổn định."""
    client = await get_client()
    models = client.list_models() or []
    for m in models:
        name = (getattr(m, "model_name", "") or "").lower()
        if "flash" in name and "thinking" not in name:
            return m
    return None


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
        logger.info("Nội dung chat_skill.yaml đã đổi, cập nhật lại Gem hiện có...")
        gem = await client.update_gem(
            stored_gem_id,
            name="Bot Telegram - Chat Skill",
            prompt=skill_text,
            description="Tự động tạo/cập nhật bởi bot Telegram từ chat_skill.yaml",
        )
    else:
        logger.info("Chưa có Gem cho chat tự nhiên, tạo mới từ chat_skill.yaml...")
        gem = await client.create_gem(
            name="Bot Telegram - Chat Skill",
            prompt=skill_text,
            description="Tự động tạo/cập nhật bởi bot Telegram từ chat_skill.yaml",
        )

    await db.set_setting(_SETTING_GEM_ID, gem.id)
    await db.set_setting(_SETTING_GEM_SKILL_HASH, skill_hash)
    return gem.id, skill_hash


async def ensure_chat_session() -> bool:
    """Đảm bảo có ChatSession hợp lệ. Trả về True nếu vừa tạo phiên mới
    (để ai.orchestrator.chat() biết khi nào cần chèn memory/grounding vào
    lượt đầu)."""
    global _chat_session, _chat_session_skill_hash
    client = await get_client()
    gem_id, skill_hash = await _get_or_create_chat_gem()
    is_new = _chat_session is None or _chat_session_skill_hash != skill_hash
    if is_new:
        # Chỉ định rõ Flash: tài khoản free dễ bị Google xếp Pro vào hàng
        # đợi vô thời hạn khi có Gem đính kèm, gây treo chat.
        flash_model = await get_flash_model()
        override_name = await get_preferred_model_name()
        chosen_model = await find_model(override_name) if override_name else None
        kwargs = {"model": chosen_model or flash_model} if (chosen_model or flash_model) else {}
        if gem_id:
            kwargs["gem"] = gem_id
        _chat_session = client.start_chat(**kwargs)
        _chat_session_skill_hash = skill_hash
    return is_new


def get_chat_session() -> Optional[ChatSession]:
    return _chat_session
