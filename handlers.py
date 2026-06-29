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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (trước đây là magic numbers rải rác trong code)
# ---------------------------------------------------------------------------
TELEGRAM_CAPTION_MAX = 1024          # giới hạn caption ảnh/video của Telegram
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

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để *chat tự nhiên* với Gemini (không cần "
    "lệnh /). Gemini sẽ tự quyết định trả lời text, vẽ ảnh hay tạo video "
    "tuỳ nội dung bạn nhắn, và nhớ được ngữ cảnh các lượt trước.\n\n"
    "/anh <mô tả> — ép tạo ảnh (single-turn, có tự thử lại nếu Gemini báo "
    "hết giới hạn)\n"
    "/video <mô tả> — ép tạo video ngắn\n"
    "/content <chủ đề> — viết content Facebook\n"
    "/reset — xoá ngữ cảnh chat tự nhiên, bắt đầu hội thoại mới\n"
    "/history — xem 10 lượt gần nhất\n"
    "/help — hiển thị hướng dẫn này\n\n"
    "*Ví dụ chat tự nhiên:*\n"
    "Vẽ giúp tôi một chú mèo anime đang uống trà sữa\n"
    "Hôm nay Sài Gòn có mưa không?\n\n"
    "*Ví dụ dùng lệnh /:*\n"
    "/anh Một chú mèo anime dễ thương đang uống trà sữa\n"
    "/video Hoàng hôn trên biển Đà Nẵng\n"
    "/content Review quán cafe ở Sài Gòn"
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
        "Xin chào! Bot tạo ảnh/video/content bằng Gemini Pro qua Telegram.\n"
        "Cứ gõ chuyện bình thường để chat với mình, không cần lệnh /.\n"
        "Gõ /help để xem các lệnh đầy đủ."
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
        response = await _ask_with_media_retry(
            lambda: gemini_client.ask(f"Generate an image: {prompt}"), "images"
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
            await video.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
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
    prompt_id = await db.save_prompt(user_id, "chat", text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
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
                await video.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
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
        await update.message.reply_text(reply_text or "Gemini không phản hồi gì, thử lại nhé.")
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
        await update.message.reply_text(text)
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
    icon_map = {"image": "🖼️", "video": "🎬", "content": "📝", "chat": "💬"}
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
