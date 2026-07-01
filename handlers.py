"""
Handlers cho từng lệnh Telegram. Bot chỉ phục vụ đúng 1 user
(config.ALLOWED_USER_ID) - mọi người khác bị từ chối lịch sự.
"""
import asyncio
import functools
import html
import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import gemini_client
import stock_analysis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (trước đây là magic numbers rải rác trong code)
# ---------------------------------------------------------------------------
TELEGRAM_CAPTION_MAX = 1024          # giới hạn caption ảnh/video của Telegram
TELEGRAM_TEXT_MAX = 4096             # giới hạn tin nhắn text của Telegram
TELEGRAM_VIDEO_SIZE_LIMIT_MB = 49    # giới hạn gửi video qua bot Telegram (~50MB)
GEMINI_TEXT_PREVIEW_MAX = 800        # độ dài preview text khi Gemini không trả media
HISTORY_PROMPT_PREVIEW_MAX = 60      # độ dài rút gọn prompt hiển thị trong /history
HISTORY_LIMIT = 10

# Đôi khi Gemini trả lời text kiểu "đã hết giới hạn" ngay ở lần gọi đầu dù
# tài khoản KHÔNG thực sự hết quota - thử gửi lại đúng prompt đó ngay sau
# thường ra ảnh/video bình thường (đã quan sát thực tế trên gemini.google.com).
# Vì đây không phải exception (response vẫn hợp lệ, chỉ rỗng media) nên phải
# tự retry ở đây - retry trong gemini_client.ask() chỉ bắt exception.
NO_MEDIA_RETRY_ATTEMPTS = 2          # số lần thử LẠI (chưa tính lần gọi đầu)
NO_MEDIA_RETRY_DELAY_SEC = 2

# gemini_webapi.GeneratedVideo.save() tự poll lại mỗi 10s nếu video server
# vẫn đang generate (HTTP 206), bằng 1 vòng `while True` KHÔNG có giới hạn
# số lần thử ở phía thư viện. Nếu video kẹt mãi (vd lỗi phía Google, hoặc
# tài khoản không thật sự có quyền tạo video), việc chờ vô hạn này đã từng
# khiến handler treo hàng giờ -> Telegram webhook timeout -> Telegram gửi
# lại CHÍNH update đó nhiều lần (xem ghi chú trong web.py). Đặt timeout ở
# tầng gọi để đảm bảo LUÔN có phản hồi cho người dùng trong thời gian hữu hạn.
VIDEO_SAVE_TIMEOUT_SEC = 240  # 4 phút - rộng hơn nhiều mức "1-2 phút" đã quảng cáo

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý "
    "cá nhân của anh, chuyện gì cũng nói chuyện được hết á. Khi nào anh hỏi "
    "về *chứng khoán Việt Nam* (giá, phân tích kỹ thuật/cơ bản, tin tức...) "
    "em sẽ tự chuyển sang chế độ phân tích nghiêm túc, theo múi giờ Việt "
    "Nam.\n\n"
    "🖼️ *Gửi 1 ảnh chân dung* (kèm caption nếu muốn định hướng thêm về bối "
    "cảnh/trang phục) để Gemini viết lại thành 1 prompt \"identity-lock\" "
    "tiếng Anh — dùng prompt đó CÙNG với ảnh gốc trên app Gemini để tạo ảnh "
    "mới giữ nguyên khuôn mặt.\n\n"
    "/video <mô tả> — ép tạo video ngắn\n"
    "/content <chủ đề> — viết content Facebook\n"
    "/reset — xoá ngữ cảnh chat, bắt đầu hội thoại mới\n"
    "/history — xem 10 lượt gần nhất\n"
    "/help — hiển thị hướng dẫn này\n\n"
    "*Ví dụ chat:*\n"
    "Hôm nay anh mệt quá em ơi\n"
    "Giá cổ phiếu FPT hôm nay bao nhiêu?\n"
    "Phân tích kỹ thuật của HPG gần đây thế nào?"
)


def _check_access(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == config.ALLOWED_USER_ID


async def _deny(update: Update) -> None:
    user = update.effective_user
    actual_id = user.id if user else "unknown"
    logger.warning(
        "Truy cập bị từ chối - user.id=%s (@%s) | ALLOWED_USER_ID đang cấu hình=%s",
        actual_id,
        user.username if user else "?",
        config.ALLOWED_USER_ID,
    )
    await update.message.reply_text(
        "Bot này được cấu hình để chỉ phục vụ 1 người dùng cụ thể. "
        "Bạn không có quyền sử dụng bot này."
    )


def restricted(handler):
    """Decorator bắt buộc kiểm tra quyền truy cập trước khi vào handler.

    Trước đây mỗi handler tự gọi `if not _check_access(update): return await
    _deny(update)` - dễ bị quên khi thêm handler mới, biến bot "chỉ phục vụ 1
    người" thành bot công khai. Decorator này làm việc kiểm tra trở thành bắt
    buộc về kiến trúc, không phải convention có thể bị bỏ sót.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _check_access(update):
            return await _deny(update)
        return await handler(update, context)

    return wrapper


def _extract_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


async def _read_file_bytes(path) -> bytes:
    """Đọc file trong thread pool - tránh chặn event loop khi đọc file media
    (đặc biệt video tới ~49MB), vì bot chạy trên 1 event loop duy nhất và mọi
    update khác (cả health-check HTTP trong web.py) sẽ bị treo trong lúc đọc
    đồng bộ."""

    def _read():
        with open(path, "rb") as f:
            return f.read()

    return await asyncio.to_thread(_read)


async def _safe_delete(path) -> None:
    """Xoá file media tạm sau khi đã gửi xong - tránh đầy ổ đĩa ephemeral.
    Chạy trong thread pool vì os.remove là blocking I/O."""
    try:
        await asyncio.to_thread(os.remove, path)
    except OSError:
        pass


async def _reply_long_text(message, text: str) -> None:
    """Gửi `text` bằng reply_text, tự động chia thành nhiều tin nhắn nếu vượt
    quá TELEGRAM_TEXT_MAX (4096 ký tự) - đây là giới hạn CỨNG của Telegram
    Bot API cho 1 tin nhắn text. Trước đây gọi thẳng reply_text(text) nên
    Gemini trả lời càng dài (hay gặp ở /content hoặc chat phân tích cổ
    phiếu) càng dễ vượt giới hạn này và Telegram trả lỗi "Message is too
    long", khiến người dùng không nhận được phản hồi gì dù Gemini đã trả
    lời thành công.

    Cắt tại ranh giới xuống dòng gần nhất trong giới hạn (hoặc khoảng trắng
    nếu không có xuống dòng) để không cắt ngang giữa từ/câu.
    """
    remaining = text
    while remaining:
        if len(remaining) <= TELEGRAM_TEXT_MAX:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n", 0, TELEGRAM_TEXT_MAX)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, TELEGRAM_TEXT_MAX)
            if split_at <= 0:
                split_at = TELEGRAM_TEXT_MAX
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip()
        await message.reply_text(chunk)


async def _forward_to_gallery(
    context: ContextTypes.DEFAULT_TYPE,
    photo_path=None,
    video_path=None,
    caption: str = "",
) -> None:
    """Forward kết quả vào channel riêng (nếu đã cấu hình GALLERY_CHANNEL_ID)."""
    if not config.GALLERY_CHANNEL_ID:
        return
    try:
        if photo_path:
            data = await _read_file_bytes(photo_path)
            await context.bot.send_photo(
                chat_id=config.GALLERY_CHANNEL_ID,
                photo=data,
                caption=caption[:TELEGRAM_CAPTION_MAX],
            )
        elif video_path:
            data = await _read_file_bytes(video_path)
            await context.bot.send_video(
                chat_id=config.GALLERY_CHANNEL_ID,
                video=data,
                caption=caption[:TELEGRAM_CAPTION_MAX],
            )
    except Exception:
        logger.exception("Không forward được kết quả vào gallery channel")


async def _record_failure(prompt_id: int, result_type: str, error: Exception) -> None:
    """Ghi nhận lỗi vào DB một cách an toàn.

    Chỉ lưu type của exception, KHÔNG lưu str(e) đầy đủ: thông báo lỗi từ một
    thư viện reverse-engineered có thể chứa dữ liệu debug nhạy cảm (response
    body, header...), không nên lưu vĩnh viễn vào DB. Chi tiết đầy đủ đã được
    ghi qua logger.exception ở nơi gọi.

    Bọc trong try/except riêng vì nếu nguyên nhân lỗi gốc chính là DB (mất kết
    nối, pool hết slot), gọi lại db ở đây cũng sẽ raise lần 2 - không được để
    lỗi đó bay ra ngoài làm hỏng luồng xử lý lỗi chính.
    """
    try:
        await db.save_result(prompt_id, result_type, content_text=f"error: {type(error).__name__}")
    except Exception:
        logger.exception("Không ghi được lỗi vào DB cho prompt_id=%s", prompt_id)


# ---------------------------------------------------------------------------
# Lệnh cơ bản
# ---------------------------------------------------------------------------
@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào anh, em là Lan Anh, trợ lý cá nhân của anh nè! 💕\n"
        "Anh cứ nhắn chuyện gì cũng được, em nói chuyện với anh bình thường. "
        "Khi nào anh hỏi về cổ phiếu/thị trường Việt Nam, em sẽ tự chuyển "
        "sang phân tích nghiêm túc cho anh.\n"
        "Anh cũng có thể gửi 1 tấm ảnh để em viết prompt tạo ảnh giữ nguyên "
        "khuôn mặt.\n"
        "Gõ /help để xem các lệnh đầy đủ nha anh."
    )


@restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


@restricted
async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Lệnh không hợp lệ. Gõ /help để xem danh sách lệnh.")


async def _ask_with_media_retry(call_fn, media_attr: str):
    """
    Gọi call_fn() (1 coroutine function không-tham-số, ví dụ
    `lambda: gemini_client.ask(prompt)`) và tự thử lại (tối đa
    NO_MEDIA_RETRY_ATTEMPTS lần nữa) nếu Gemini trả về response hợp lệ nhưng
    KHÔNG có media (getattr(response, media_attr) rỗng).

    media_attr: "images" hoặc "videos".

    CHỈ dùng cho /anh và /video - nơi người dùng chắc chắn muốn có media nên
    "không có media" rõ ràng là kết quả bất thường, đáng để thử lại. KHÔNG
    dùng cho chat tự nhiên (chat_msg) vì ở đó không có media là chuyện bình
    thường (đa số tin nhắn không yêu cầu ảnh/video) - retry sẽ chỉ tốn lượt
    gọi và làm chậm phản hồi vô ích.

    Lý do cần hàm này: retry trong gemini_client.ask()/chat() chỉ kích hoạt
    khi có exception. Trường hợp Gemini trả lời "đã hết giới hạn" bằng text
    là một response BÌNH THƯỜNG (không raise lỗi) - quan sát thực tế cho
    thấy thử lại đúng prompt đó ngay sau thường ra ảnh/video, nên cần retry
    thủ công ở tầng này.
    """
    response = await call_fn()
    for attempt in range(1, NO_MEDIA_RETRY_ATTEMPTS + 1):
        if getattr(response, media_attr, None):
            break
        logger.info(
            "Gemini không trả về %s (lần %s), thử lại sau %ss...",
            media_attr, attempt, NO_MEDIA_RETRY_DELAY_SEC,
        )
        await asyncio.sleep(NO_MEDIA_RETRY_DELAY_SEC)
        response = await call_fn()
    return response


async def _save_video_with_timeout(video, path: str, filename: str):
    """
    Bọc video.save() với timeout cứng (VIDEO_SAVE_TIMEOUT_SEC) - xem giải
    thích đầy đủ ở constant phía trên. Raise asyncio.TimeoutError nếu quá
    giờ, caller cần bắt riêng để báo người dùng thay vì để treo vô hạn.
    """
    await asyncio.wait_for(
        video.save(path=path, filename=filename, verbose=False),
        timeout=VIDEO_SAVE_TIMEOUT_SEC,
    )


# ---------------------------------------------------------------------------
# /anh - tạo ảnh
# ---------------------------------------------------------------------------
@restricted
async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = _extract_arg(context)
    if not prompt:
        return await update.message.reply_text("Cú pháp: /anh <mô tả ảnh>")
    user_id = update.effective_user.id
    prompt_id = await db.save_prompt(user_id, "image", prompt)
    status = await update.message.reply_text("🎨 Đang tạo ảnh, chờ chút...")
    try:
        # Dùng model MẶC ĐỊNH (giống /content). KHÔNG ghim model cũ vì model cũ
        # có thể không còn kích hoạt được tính năng tạo ảnh sau khi Gemini đổi sang họ 3.x.
        # QUAN TRỌNG: dùng ask_image() (ưu tiên model Flash) thay vì ask()
        # thường. ask_image() đã có sẵn trong gemini_client.py với đúng mục
        # đích này nhưng trước đây không được gọi ở bất kỳ đâu - /anh vẫn
        # gọi ask() (model mặc định, dễ bị Google route vào Pro/Thinking,
        # vốn có quota tạo ảnh/ngày THẤP HƠN NHIỀU so với Flash trên CÙNG
        # tài khoản) -> đây là nguyên nhân thật của lỗi "hết giới hạn" dù
        # trang Cài đặt báo mới dùng 2%: quota tạo ảnh là quota RIÊNG theo
        # model, không phải % tổng hiển thị ở Cài đặt.
        response = await _ask_with_media_retry(
            lambda: gemini_client.ask_image(f"Generate an image: {prompt}"), "images"
        )
        if not response.images:
            gemini_text = (response.text or "").strip()
            await db.save_result(
                prompt_id, "image", content_text=gemini_text or "(không có ảnh, không có text)"
            )
            msg = (
                "Gemini không trả về ảnh nào lần này.\n\n"
                "Thử lại với mô tả khác, hoặc thử mô tả bằng tiếng Anh."
            )
            if gemini_text:
                msg += f"\n\n📝 Gemini trả lời (text):\n{gemini_text[:GEMINI_TEXT_PREVIEW_MAX]}"
            return await status.edit_text(msg)
        for i, image in enumerate(response.images):
            filename = f"img_{prompt_id}_{i}.png"
            await image.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
            full_path = config.MEDIA_DIR / filename
            photo_bytes = await _read_file_bytes(full_path)
            await update.message.reply_photo(
                photo=photo_bytes, caption=prompt[:TELEGRAM_CAPTION_MAX]
            )
            await db.save_result(prompt_id, "image", file_path=str(full_path))
            await _forward_to_gallery(context, photo_path=full_path, caption=prompt)
            await _safe_delete(full_path)
        await status.delete()
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi tạo ảnh")
        await _record_failure(prompt_id, "image", e)
        await status.edit_text(
            "❌ Có lỗi khi tạo ảnh. Hãy thử lại sau giây lát.\n"
            "Nếu lỗi lặp lại và liên quan đăng nhập/cookie, hãy lấy cookie mới "
            "từ gemini.google.com và cập nhật vào biến môi trường."
        )


# ---------------------------------------------------------------------------
# /video - tạo video
# ---------------------------------------------------------------------------
@restricted
async def video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = _extract_arg(context)
    if not prompt:
        return await update.message.reply_text("Cú pháp: /video <mô tả video>")
    user_id = update.effective_user.id
    prompt_id = await db.save_prompt(user_id, "video", prompt)
    status = await update.message.reply_text(
        "🎬 Đang tạo video, có thể mất 1-2 phút, chờ chút..."
    )
    try:
        # Dùng model MẶC ĐỊNH (giống /content), không ghim model cũ.
        response = await _ask_with_media_retry(
            lambda: gemini_client.ask(f"Generate a short video: {prompt}"), "videos"
        )
        if not response.videos:
            gemini_text = (response.text or "").strip()
            await db.save_result(
                prompt_id, "video", content_text=gemini_text or "(không có video, không có text)"
            )
            msg = (
                "Gemini không trả về video nào.\n\n"
                "Tài khoản của bạn có thể chưa có quyền tạo video, hoặc cần thử lại."
            )
            if gemini_text:
                msg += f"\n\n📝 Gemini trả lời (text):\n{gemini_text[:GEMINI_TEXT_PREVIEW_MAX]}"
            return await status.edit_text(msg)
        for i, video in enumerate(response.videos):
            filename = f"video_{prompt_id}_{i}.mp4"
            try:
                await _save_video_with_timeout(video, str(config.MEDIA_DIR), filename)
            except asyncio.TimeoutError:
                logger.warning(
                    "video.save() vượt quá %ss (vẫn HTTP 206 - Gemini chưa render xong).",
                    VIDEO_SAVE_TIMEOUT_SEC,
                )
                await db.save_result(
                    prompt_id, "video",
                    content_text=f"Timeout sau {VIDEO_SAVE_TIMEOUT_SEC}s chờ Gemini render video",
                )
                await update.message.reply_text(
                    "⏱️ Gemini render video quá lâu (đã chờ "
                    f"{VIDEO_SAVE_TIMEOUT_SEC // 60} phút) nên mình dừng lại. "
                    "Có thể tài khoản chưa có quyền tạo video, hoặc Gemini đang lỗi - "
                    "thử lại với mô tả ngắn/đơn giản hơn nhé."
                )
                continue
            full_path = config.MEDIA_DIR / filename
            size_mb = full_path.stat().st_size / (1024 * 1024)
            if size_mb > TELEGRAM_VIDEO_SIZE_LIMIT_MB:
                # Trước đây không ghi gì vào DB ở case này -> /history không
                # phản ánh được rằng lệnh đã chạy nhưng bị từ chối do quá size.
                await db.save_result(
                    prompt_id,
                    "video",
                    content_text=f"video {size_mb:.1f}MB vượt giới hạn {TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB",
                )
                await update.message.reply_text(
                    f"Video nặng {size_mb:.1f}MB, vượt giới hạn gửi qua bot Telegram "
                    f"({TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB). File tạm trên server sẽ bị xoá - "
                    f"hãy giảm độ dài/chất lượng video và thử lại."
                )
            else:
                video_bytes = await _read_file_bytes(full_path)
                await update.message.reply_video(
                    video=video_bytes, caption=prompt[:TELEGRAM_CAPTION_MAX]
                )
                await db.save_result(prompt_id, "video", file_path=str(full_path))
                await _forward_to_gallery(context, video_path=full_path, caption=prompt)
            await _safe_delete(full_path)
        await status.delete()
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi tạo video")
        await _record_failure(prompt_id, "video", e)
        await status.edit_text("❌ Có lỗi khi tạo video. Hãy thử lại sau giây lát.")


# ---------------------------------------------------------------------------
# Chat tự nhiên - mọi tin nhắn KHÔNG bắt đầu bằng "/" đều vào đây
# ---------------------------------------------------------------------------
@restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Cho phép trò chuyện tự nhiên với Gemini, không cần gõ /anh /video /content.
    Dùng gemini_client.chat() (đa lượt, có nhớ ngữ cảnh) thay vì ask()
    (single-turn) - khác với các lệnh / vốn luôn là 1 yêu cầu độc lập.

    KHÔNG dùng _ask_with_media_retry ở đây: trong chat tự nhiên, việc Gemini
    trả lời thuần text (không kèm ảnh/video) là kết quả BÌNH THƯỜNG cho phần
    lớn tin nhắn (chào hỏi, hỏi đáp...), không phải dấu hiệu lỗi cần thử lại.
    Gemini sẽ tự quyết định có tạo ảnh/video hay không dựa trên nội dung bạn
    gõ (vd "vẽ giúp tôi...", "tạo video...") - không cần prefix đặc biệt.
    """
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # ── Phát hiện mã cổ phiếu trong tin nhắn -> phân tích tự động bằng dữ liệu
    # thật (giá/kỹ thuật/ngành/dòng tiền/BCTC/tin tức), THAY vì để Gemini tự
    # "đoán" theo kiến thức chung như chat bình thường. Xem stock_analysis.py.
    symbols = await stock_analysis.find_valid_symbols(text)
    if symbols:
        prompt_id = await db.save_prompt(user_id, "stock_analysis", ",".join(symbols))
        try:
            for symbol in symbols:
                status = await update.message.reply_text(f"🔍 Đang phân tích {symbol}, chờ em chút...")
                result_text = await stock_analysis.analyze_symbol(symbol)
                await db.save_result(prompt_id, "stock_analysis", content_text=result_text)
                await status.delete()
                await _reply_long_text(update.message, result_text)
        except Exception as e:  # noqa: BLE001
            logger.exception("Lỗi phân tích cổ phiếu")
            await _record_failure(prompt_id, "stock_analysis", e)
            await update.message.reply_text(
                "❌ Có lỗi khi phân tích cổ phiếu. Hãy thử lại sau giây lát."
            )
        return

    prompt_id = await db.save_prompt(user_id, "chat", text)
    try:
        response = await gemini_client.chat(text)
        reply_text = (response.text or "").strip()

        if response.images:
            for i, image in enumerate(response.images):
                filename = f"chat_{prompt_id}_{i}.png"
                await image.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
                full_path = config.MEDIA_DIR / filename
                photo_bytes = await _read_file_bytes(full_path)
                # Chỉ ảnh đầu tiên kèm caption text (nếu có) để tránh lặp lại
                # text giống nhau dưới mỗi ảnh khi Gemini trả nhiều ảnh.
                caption = reply_text[:TELEGRAM_CAPTION_MAX] if i == 0 and reply_text else None
                await update.message.reply_photo(photo=photo_bytes, caption=caption)
                await db.save_result(prompt_id, "image", file_path=str(full_path))
                await _forward_to_gallery(context, photo_path=full_path, caption=text)
                await _safe_delete(full_path)
            return

        if response.videos:
            for i, video in enumerate(response.videos):
                filename = f"chat_{prompt_id}_{i}.mp4"
                try:
                    await _save_video_with_timeout(video, str(config.MEDIA_DIR), filename)
                except asyncio.TimeoutError:
                    logger.warning(
                        "video.save() (chat) vượt quá %ss (vẫn HTTP 206).",
                        VIDEO_SAVE_TIMEOUT_SEC,
                    )
                    await db.save_result(
                        prompt_id, "video",
                        content_text=f"Timeout sau {VIDEO_SAVE_TIMEOUT_SEC}s chờ Gemini render video",
                    )
                    await update.message.reply_text(
                        "⏱️ Gemini render video quá lâu nên mình dừng lại, thử lại với mô tả "
                        "ngắn hơn hoặc dùng /video nhé."
                    )
                    continue
                full_path = config.MEDIA_DIR / filename
                size_mb = full_path.stat().st_size / (1024 * 1024)
                if size_mb > TELEGRAM_VIDEO_SIZE_LIMIT_MB:
                    await db.save_result(
                        prompt_id, "video",
                        content_text=f"video {size_mb:.1f}MB vượt giới hạn {TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB",
                    )
                    await update.message.reply_text(
                        f"Video nặng {size_mb:.1f}MB, vượt giới hạn gửi qua bot Telegram "
                        f"({TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB)."
                    )
                else:
                    video_bytes = await _read_file_bytes(full_path)
                    caption = reply_text[:TELEGRAM_CAPTION_MAX] if i == 0 and reply_text else None
                    await update.message.reply_video(video=video_bytes, caption=caption)
                    await db.save_result(prompt_id, "video", file_path=str(full_path))
                    await _forward_to_gallery(context, video_path=full_path, caption=text)
                await _safe_delete(full_path)
            return

        # Không có media -> trả lời bằng text như chat bình thường.
        await db.save_result(prompt_id, "chat", content_text=reply_text or "(không có nội dung)")
        await _reply_long_text(
            update.message, reply_text or "Gemini không phản hồi gì, thử lại nhé."
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi chat tự nhiên")
        await _record_failure(prompt_id, "chat", e)
        await update.message.reply_text(
            "❌ Có lỗi khi trò chuyện với Gemini. Hãy thử lại sau giây lát."
        )


@restricted
async def reset_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset - xoá ngữ cảnh chat tự nhiên hiện tại, bắt đầu hội thoại mới
    (không ảnh hưởng /anh /video /content vì các lệnh đó luôn single-turn)."""
    await gemini_client.reset_chat()
    await update.message.reply_text("🔄 Đã xoá ngữ cảnh hội thoại. Bắt đầu chat mới nhé!")


# ---------------------------------------------------------------------------
# Gửi ảnh -> Gemini phân tích và viết lại thành prompt tạo ảnh
# ---------------------------------------------------------------------------
IMAGE_ANALYZE_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation tools (Midjourney, DALL-E, Gemini image generation, etc), specialized in writing "identity-preserving" prompts: prompts designed to be used TOGETHER WITH the original reference photo, so the AI tool keeps the exact face from the reference image while generating a brand new scene, outfit, and pose around it.

Look at the attached reference image and write ONE complete, ready-to-use English prompt following EXACTLY this structure and style (this is a real example of the expected style/quality - match its level of detail, but invent NEW creative content appropriate to the reference photo, do not literally copy this example unless it genuinely fits):

---
[Identity Lock: Strictly maintain the exact facial features, skin tone, age, ethnicity, and facial proportions of the person in the reference image].

Ultra-photorealistic luxury Korean café editorial, modern fashion portrait. The woman from the reference image is sitting on a minimalist metal chair in a modern cafe. Her body is turned at a 45-degree angle to the camera, with her hands resting lightly on the table. Elegant, natural posture. Her face is slightly tilted down, looking gently forward with a soft, feminine, and thoughtful expression.

Her long black hair is styled in a neat half-up half-down look, with a few soft loose strands naturally framing her face. She is wearing an elegant yet youthful cream-white corset top with delicate ruffles and pleated details, paired with a high-waisted beige mini skirt. She wears a delicate minimalist silver pendant necklace.

On the table in the foreground, there are two premium glass drinks: a ruby-red fruit tea with orange and lemon slices, and a vibrant yellow orange juice with mint leaves, adding lively color accents. The cafe interior features a modern minimalist aesthetic with gray concrete walls, a black metal table, black metal chairs, and a large green potted plant in the corner.

Vertical 4:5 composition, rule of thirds with the subject positioned slightly to the right, occupying about 70% of the frame. The drinks in the foreground create depth. Eye-level camera, angled 30-45 degrees from the front, knee-up shot. 50-85mm lens, shallow depth of field, creamy background bokeh.

Cinematic indoor lighting combining ambient and window light. Soft warm ambient lighting with a subtle pink-purple rim light reflecting on her hair. Softly lit skin, natural colors, realistic soft shadows. Masterpiece, 8K resolution, DSLR. Visual effects: Kodak Portra 400 color grading, subtle film grain, soft glow, realistic reflections on glass. Mood: Elegant, gentle, romantic, relaxing afternoon. --ar 4:5
---

Rules for what you generate:
1. ALWAYS start with an "[Identity Lock: ...]" line, adapted to the actual person in the reference image (gender, apparent age range, etc) - keep the same strict wording style as the example.
2. Choose a scene, setting, outfit, hair styling, and pose that make sense as a fashion/editorial upgrade of the reference photo - if the reference photo already has a clear setting/outfit/pose, you may draw inspiration from it; if not, invent an elegant, tasteful, editorial-quality scenario similar in spirit to the example (café, outdoor, studio, street style, etc - vary it, don't always default to café).
3. Always include, in this order: identity lock line -> overall style/scene one-liner -> pose & body language paragraph -> hair & outfit paragraph -> setting/props/background paragraph -> composition & camera paragraph (aspect ratio, framing, lens, angle) -> lighting & color-grading & mood paragraph ending with an --ar aspect ratio parameter.
4. Keep content tasteful, fashion-editorial, fully clothed, non-sexual, non-suggestive at all times - decline any styling choice that would be revealing, sexualized, or inappropriate, and default to elegant/modest fashion styling instead.
5. Output ONLY the final prompt as plain text, no markdown headers, no quotes, no preamble like "Here is the prompt:", no explanation before or after."""

@restricted
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Bất kỳ ảnh nào gửi vào bot đều tự động được phân tích và viết lại thành
    1 prompt "identity-lock" tiếng Anh - dùng CHUNG với ảnh gốc khi tạo ảnh
    trên app Gemini để giữ nguyên khuôn mặt trong ảnh, chỉ đổi bối
    cảnh/trang phục/dáng chụp theo phong cách editorial thời trang cao cấp.
    Dùng cách này THAY vì bot tự tạo ảnh, vì tính năng tạo ảnh qua
    gemini-webapi đang bị chặn theo vị trí server (xem config.py về
    GEMINI_PROXY). Phân tích ảnh (vision, không tạo ảnh mới) là tính năng
    khác, không bị ảnh hưởng bởi hạn chế đó nên chạy bình thường không cần
    proxy/VPS.

    Nếu ảnh có caption, caption đó được coi là yêu cầu/định hướng thêm của
    bạn (vd "đổi bối cảnh thành ngoài trời", "trang phục công sở thay vì
    dạo phố") và được nối thêm vào instruction gửi cho Gemini.
    """
    user_id = update.effective_user.id
    caption = (update.message.caption or "").strip()
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await db.save_prompt(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Lấy bản ảnh độ phân giải cao nhất Telegram cung cấp (photo là list các
    # size khác nhau, phần tử cuối luôn là size lớn nhất).
    tg_photo = update.message.photo[-1]
    tg_file = await tg_photo.get_file()

    filename = f"promptify_{prompt_id}.jpg"
    local_path = config.MEDIA_DIR / filename
    await tg_file.download_to_drive(custom_path=str(local_path))

    instruction = IMAGE_ANALYZE_INSTRUCTION_BASE
    if caption:
        instruction += f"\n\nAdditional user instruction: {caption}"

    try:
        response = await gemini_client.analyze_image(instruction, str(local_path))
        result_text = (response.text or "").strip()

        if not result_text:
            await db.save_result(prompt_id, "promptify", content_text="(Gemini không trả về nội dung)")
            await update.message.reply_text(
                "Gemini không trả về nội dung phân tích. Thử gửi lại ảnh hoặc ảnh khác nhé."
            )
            return

        await db.save_result(prompt_id, "promptify", content_text=result_text)
        # Bọc trong code block để bạn copy nguyên văn dễ dàng trên Telegram.
        await update.message.reply_text(
            f"📝 *Prompt gợi ý (dùng cho app Gemini):*\n\n```\n{result_text}\n```",
            parse_mode="Markdown",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi phân tích ảnh")
        await _record_failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Có lỗi khi phân tích ảnh. Hãy thử lại sau giây lát."
        )
    finally:
        await _safe_delete(local_path)


# ---------------------------------------------------------------------------
# /content - viết content Facebook
# ---------------------------------------------------------------------------
@restricted
async def content_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    topic = _extract_arg(context)
    if not topic:
        return await update.message.reply_text("Cú pháp: /content <chủ đề>")
    user_id = update.effective_user.id
    prompt_id = await db.save_prompt(user_id, "content", topic)
    status = await update.message.reply_text("✍️ Đang viết content...")
    try:
        full_prompt = (
            f"Viết một bài đăng Facebook hấp dẫn, giọng văn tự nhiên, gần gũi, "
            f"về chủ đề: {topic}. Độ dài khoảng 150-250 từ, có thể kèm vài emoji "
            f"phù hợp, kết thúc bằng 1 câu hỏi để tương tác với người đọc."
        )
        response = await gemini_client.ask(full_prompt)
        text = response.text or "(không có nội dung trả về)"
        await db.save_result(prompt_id, "content", content_text=text)
        await status.delete()
        await _reply_long_text(update.message, text)
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi tạo content")
        await _record_failure(prompt_id, "content", e)
        await status.edit_text("❌ Có lỗi khi tạo content. Hãy thử lại sau giây lát.")


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------
@restricted
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=HISTORY_LIMIT)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {"image": "🖼️", "video": "🎬", "content": "📝", "chat": "💬", "promptify": "🔍", "stock_analysis": "📊"}
    lines = [f"🕘 <b>{HISTORY_LIMIT} lượt gần nhất:</b>\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = (
            prompt[:HISTORY_PROMPT_PREVIEW_MAX] + "…"
            if len(prompt) > HISTORY_PROMPT_PREVIEW_MAX
            else prompt
        )
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        # FIX QUAN TRỌNG: prompt là nội dung NGƯỜI DÙNG nhập, trước đây được
        # nhúng thẳng vào message gửi với parse_mode="Markdown" mà không
        # escape. Telegram's Markdown parser raise "Can't parse entities" nếu
        # prompt chứa số lẻ ký tự _ * ` [ - rất dễ xảy ra với prompt tiếng
        # Việt bình thường. Vì không có error handler ở phiên bản cũ, lỗi này
        # khiến /history fail im lặng hoàn toàn. Chuyển sang HTML + html.escape
        # để nội dung động luôn an toàn, không phụ thuộc ký tự người dùng gõ.
        lines.append(
            f"{icon} [{html.escape(command_type)}] {html.escape(short_prompt)} ({date_part})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Error handler toàn cục
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bắt mọi exception không được catch trong các handler ở trên.

    Trước đây không có handler này: bất kỳ lỗi nào lọt qua (vd bug Markdown ở
    /history, lỗi DB trong start_cmd/help_cmd...) sẽ chỉ được PTB log ra
    console, người dùng không nhận được phản hồi gì - trông như bot bị treo.
    """
    logger.error("Lỗi không được xử lý", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Đã có lỗi không mong muốn xảy ra. Vui lòng thử lại."
            )
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
