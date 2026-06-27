"""
Handlers cho từng lệnh Telegram. Bot chỉ phục vụ đúng 1 user
(config.ALLOWED_USER_ID) - mọi người khác bị từ chối lịch sự.
"""
import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import gemini_client

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "/anh <mô tả> — tạo ảnh\n"
    "/video <mô tả> — tạo video ngắn\n"
    "/content <chủ đề> — viết content Facebook\n"
    "/history — xem 10 lượt gần nhất\n"
    "/help — hiển thị hướng dẫn này\n\n"
    "*Ví dụ:*\n"
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


def _extract_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


def _safe_delete(path) -> None:
    """Xoá file media tạm sau khi đã gửi xong - tránh đầy ổ đĩa ephemeral."""
    try:
        os.remove(path)
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
            with open(photo_path, "rb") as f:
                await context.bot.send_photo(
                    chat_id=config.GALLERY_CHANNEL_ID, photo=f, caption=caption[:1024]
                )
        elif video_path:
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=config.GALLERY_CHANNEL_ID, video=f, caption=caption[:1024]
                )
    except Exception:
        logger.exception("Không forward được kết quả vào gallery channel")


# ---------------------------------------------------------------------------
# Lệnh cơ bản
# ---------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
    await update.message.reply_text(
        "Xin chào! Bot tạo ảnh/video/content bằng Gemini Pro qua Telegram.\n"
        "Gõ /help để xem các lệnh."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
    await update.message.reply_text("Lệnh không hợp lệ. Gõ /help để xem danh sách lệnh.")


# ---------------------------------------------------------------------------
# /anh - tạo ảnh
# ---------------------------------------------------------------------------
async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
    prompt = _extract_arg(context)
    if not prompt:
        return await update.message.reply_text("Cú pháp: /anh <mô tả ảnh>")
    user_id = update.effective_user.id
    prompt_id = await db.save_prompt(user_id, "image", prompt)
    status = await update.message.reply_text("🎨 Đang tạo ảnh, chờ chút...")
    try:
        # Dùng model MẶC ĐỊNH (giống /content). KHÔNG ghim model cũ vì model cũ
        # có thể không còn kích hoạt được tính năng tạo ảnh sau khi Gemini đổi sang họ 3.x.
        response = await gemini_client.ask(f"Generate an image: {prompt}")
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
                msg += f"\n\n📝 Gemini trả lời (text):\n{gemini_text[:800]}"
            return await status.edit_text(msg)
        for i, image in enumerate(response.images):
            filename = f"img_{prompt_id}_{i}.png"
            await image.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
            full_path = config.MEDIA_DIR / filename
            with open(full_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption=prompt[:1024])
            await db.save_result(prompt_id, "image", file_path=str(full_path))
            await _forward_to_gallery(context, photo_path=full_path, caption=prompt)
            _safe_delete(full_path)
        await status.delete()
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi tạo ảnh")
        await db.save_result(prompt_id, "image", content_text=f"error: {e}")
        await status.edit_text(
            "❌ Có lỗi khi tạo ảnh. Hãy thử lại sau giây lát.\n"
            "Nếu lỗi lặp lại và liên quan đăng nhập/cookie, hãy lấy cookie mới "
            "từ gemini.google.com và cập nhật vào biến môi trường."
        )


# ---------------------------------------------------------------------------
# /video - tạo video
# ---------------------------------------------------------------------------
async def video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
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
        response = await gemini_client.ask(f"Generate a short video: {prompt}")
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
                msg += f"\n\n📝 Gemini trả lời (text):\n{gemini_text[:800]}"
            return await status.edit_text(msg)
        for i, video in enumerate(response.videos):
            filename = f"video_{prompt_id}_{i}.mp4"
            await video.save(path=str(config.MEDIA_DIR), filename=filename, verbose=False)
            full_path = config.MEDIA_DIR / filename
            size_mb = full_path.stat().st_size / (1024 * 1024)
            if size_mb > 49:
                await update.message.reply_text(
                    f"Video nặng {size_mb:.1f}MB, vượt giới hạn gửi qua bot Telegram "
                    f"(50MB). File tạm trên server sẽ bị xoá - hãy giảm độ dài/chất "
                    f"lượng video và thử lại."
                )
            else:
                with open(full_path, "rb") as f:
                    await update.message.reply_video(video=f, caption=prompt[:1024])
                await db.save_result(prompt_id, "video", file_path=str(full_path))
                await _forward_to_gallery(context, video_path=full_path, caption=prompt)
            _safe_delete(full_path)
        await status.delete()
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi tạo video")
        await db.save_result(prompt_id, "video", content_text=f"error: {e}")
        await status.edit_text("❌ Có lỗi khi tạo video. Hãy thử lại sau giây lát.")


# ---------------------------------------------------------------------------
# /content - viết content Facebook
# ---------------------------------------------------------------------------
async def content_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
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
        await db.save_result(prompt_id, "content", content_text=f"error: {e}")
        await status.edit_text("❌ Có lỗi khi tạo content. Hãy thử lại sau giây lát.")


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _check_access(update):
        return await _deny(update)
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=10)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {"image": "🖼️", "video": "🎬", "content": "📝"}
    lines = ["🕘 *10 lượt gần nhất:*\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = (prompt[:60] + "…") if len(prompt) > 60 else prompt
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        lines.append(f"{icon} [{command_type}] {short_prompt} ({date_part})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
