"""Scheduler nền (Bước 6 trong kế hoạch cải tiến - chủ động/proactive).

Dùng lại đúng pattern background task đã có (`ai.orchestrator._cookie_probe_loop`
+ `start_background_tasks()`): 1 asyncio.Task chạy vòng lặp vô hạn, khởi động
từ `bot_app._post_init`.

2 vòng lặp độc lập:
- `_reminder_loop`: quét bảng `reminders` (xem core/database.py, services/tools.py - reminder được
  tạo qua function calling `set_reminder`) mỗi REMINDER_CHECK_INTERVAL_SEC
  giây, gửi các reminder đã tới hạn rồi đánh dấu đã gửi.
- `_daily_digest_loop`: mỗi ngày vào DAILY_DIGEST_HOUR_VN giờ (giờ VN), nếu
  có danh mục đầu tư ghi nhận trong trí nhớ dài hạn (user_facts, xem
  services/memory_service.py), lấy giá realtime từng mã (stock_analysis.quick_quote - CÙNG
  nguồn dữ liệu thật dùng cho tra giá thủ công, không tự bịa số) rồi gửi
  digest. Không gửi gì nếu chưa ghi nhận danh mục nào (tránh spam tin rỗng).

Cả 2 vòng lặp gửi tin qua 1 callback được đăng ký từ bot_app.py (cùng cơ chế
với ai.provider_state.set_alert_callback, nhưng tách riêng module để không lẫn
cảnh báo cookie chết/sống với thông báo chủ động cho người dùng).
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from core import config
from core import database as db

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

_notify_callback: Optional[Callable[[int, str], Awaitable[None]]] = None
_reminder_task: Optional[asyncio.Task] = None
_digest_task: Optional[asyncio.Task] = None


def set_notify_callback(fn: Callable[[int, str], Awaitable[None]]) -> None:
    """Đăng ký callback async(telegram_user_id, text) -> None để gửi tin chủ
    động. bot_app.py gọi 1 lần lúc build_application(), dùng app.bot.send_message
    (chat_id riêng tư 1:1 == telegram_user_id, vì bot chỉ phục vụ 1 user)."""
    global _notify_callback
    _notify_callback = fn


async def _notify(user_id: int, text: str) -> bool:
    if _notify_callback is None:
        logger.warning("scheduler: chưa đăng ký notify_callback, bỏ qua gửi tin.")
        return False
    try:
        await _notify_callback(user_id, text)
        return True
    except Exception:
        logger.warning("scheduler: gửi tin chủ động lỗi.", exc_info=True)
        return False


async def _process_due_reminders(due: list[tuple[int, int, str]]) -> None:
    for reminder_id, user_id, message in due:
        sent_ok = await _notify(user_id, f"⏰ Nhắc việc: {message}")
        if not sent_ok:
            # Chưa mark sent -> lượt quét kế tiếp sẽ thử gửi lại. Có thể
            # gửi trùng nếu lỗi chỉ là tạm thời phía Telegram, nhưng vẫn
            # tốt hơn nhiều so với đánh dấu đã gửi rồi mất vĩnh viễn.
            continue
        try:
            await db.mark_reminder_sent(reminder_id)
        except Exception:
            logger.warning(
                "scheduler: gửi reminder id=%s thành công nhưng mark sent lỗi, "
                "có thể gửi trùng ở lượt quét sau.",
                reminder_id, exc_info=True,
            )


async def _reminder_loop() -> None:
    while True:
        await asyncio.sleep(config.REMINDER_CHECK_INTERVAL_SEC)
        try:
            due = await db.get_due_reminders()
        except Exception:
            logger.warning("scheduler: lỗi khi quét reminders đến hạn.", exc_info=True)
            continue
        await _process_due_reminders(due)


def _seconds_until_next_hour(hour: int) -> float:
    now = datetime.now(_VN_TZ)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _build_portfolio_digest(user_id: int) -> Optional[str]:
    """Trả về text digest, hoặc None nếu chưa ghi nhận danh mục nào (không
    gửi tin rỗng mỗi ngày)."""
    import stock_analysis  # import trễ để tránh vòng import lúc module load

    facts = await db.get_facts(user_id)
    portfolio_text = " ".join(
        v for k, v in facts if any(kw in k for kw in ("danh_muc", "portfolio", "co_phieu"))
    )
    if not portfolio_text.strip():
        return None

    symbols = await stock_analysis.find_valid_symbols(portfolio_text)
    if not symbols:
        return None

    lines = ["📊 *Digest danh mục sáng nay:*"]
    for symbol in symbols:
        try:
            lines.append(await stock_analysis.quick_quote(symbol))
        except Exception:
            logger.warning("scheduler: lỗi lấy giá %s cho digest.", symbol, exc_info=True)
            lines.append(f"{symbol}: ❌ lỗi lấy giá lúc này")
    return "\n".join(lines)


async def _daily_digest_loop(user_id: int) -> None:
    while True:
        await asyncio.sleep(_seconds_until_next_hour(config.DAILY_DIGEST_HOUR_VN))
        try:
            digest = await _build_portfolio_digest(user_id)
            if digest:
                await _notify(user_id, digest)
        except Exception:
            logger.warning("scheduler: lỗi khi tạo/gửi daily digest.", exc_info=True)


def start(allowed_user_id: int) -> None:
    """Gọi 1 lần lúc khởi động bot (bot_app._post_init), giống
    ai.orchestrator.start_background_tasks()."""
    global _reminder_task, _digest_task
    if _reminder_task is None or _reminder_task.done():
        _reminder_task = asyncio.create_task(_reminder_loop())
    if config.ENABLE_DAILY_DIGEST and allowed_user_id:
        if _digest_task is None or _digest_task.done():
            _digest_task = asyncio.create_task(_daily_digest_loop(allowed_user_id))
