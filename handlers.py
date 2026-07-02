"""Handlers cho từng lệnh Telegram. Bot chỉ phục vụ đúng 1 user (config.ALLOWED_USER_ID)."""
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
import stock_providers

logger = logging.getLogger(__name__)

TELEGRAM_CAPTION_MAX = 1024
TELEGRAM_TEXT_MAX = 4096
GEMINI_TEXT_PREVIEW_MAX = 800
HISTORY_PROMPT_PREVIEW_MAX = 60
HISTORY_LIMIT = 10

# Gemini đôi khi trả lời "hết giới hạn" ở lần gọi đầu dù account chưa hết quota thật -
# thử lại đúng prompt đó thường ra ảnh bình thường. Chỉ retry ở /anh, không retry chat.
NO_MEDIA_RETRY_ATTEMPTS = 2
NO_MEDIA_RETRY_DELAY_SEC = 2

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý "
    "cá nhân của anh, chuyện gì cũng nói chuyện được hết á.\n\n"
    "📊 Khi anh nhắc tới 1 *mã cổ phiếu Việt Nam*, mặc định em lấy *giá khớp "
    "lệnh REALTIME + % tăng giảm ngay từ DNSE* trả lời liền, không phân tích "
    "dài dòng. Anh cần "
    "phân tích kỹ thuật/cơ bản thì cứ nói rõ (vd \"phân tích giúp anh mã "
    "FPT\") em sẽ chuyển sang chế độ phân tích nghiêm túc, theo múi giờ Việt "
    "Nam.\n\n"
    "🖼️ *Gửi 1 ảnh chân dung* (kèm caption nếu muốn định hướng thêm về bối "
    "cảnh/trang phục) để Gemini viết lại thành 1 prompt \"identity-lock\" "
    "tiếng Anh — dùng prompt đó CÙNG với ảnh gốc trên app Gemini để tạo ảnh "
    "mới giữ nguyên khuôn mặt.\n\n"
    "/anh <mô tả> — tạo ảnh\n"
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
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _check_access(update):
            return await _deny(update)
        return await handler(update, context)

    return wrapper


def _extract_arg(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip() if context.args else ""


async def _read_file_bytes(path) -> bytes:
    def _read():
        with open(path, "rb") as f:
            return f.read()

    return await asyncio.to_thread(_read)


async def _safe_delete(path) -> None:
    try:
        await asyncio.to_thread(os.remove, path)
    except OSError:
        pass


async def _reply_long_text(message, text: str) -> None:
    """Chia text thành nhiều tin nhắn nếu vượt TELEGRAM_TEXT_MAX, cắt tại ranh giới dòng/từ."""
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
    """Chỉ lưu type của exception vào DB, không lưu str(e) - có thể chứa dữ liệu nhạy cảm."""
    try:
        await db.save_result(prompt_id, result_type, content_text=f"error: {type(error).__name__}")
    except Exception:
        logger.exception("Không ghi được lỗi vào DB cho prompt_id=%s", prompt_id)


@restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào anh, em là Lan Anh, trợ lý cá nhân của anh nè! 💕\n"
        "Anh cứ nhắn chuyện gì cũng được, em nói chuyện với anh bình thường. "
        "Hỏi giá cổ phiếu em trả lời realtime từ DNSE liền, còn khi nào anh "
        "cần phân tích sâu thì cứ nói rõ, em sẽ chuyển sang phân tích nghiêm "
        "túc cho anh.\n"
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


@restricted
async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = _extract_arg(context)
    if not prompt:
        return await update.message.reply_text("Cú pháp: /anh <mô tả ảnh>")
    user_id = update.effective_user.id
    prompt_id = await db.save_prompt(user_id, "image", prompt)
    status = await update.message.reply_text("🎨 Đang tạo ảnh, chờ chút...")
    try:
        # ask_image() ưu tiên model Flash - quota tạo ảnh/ngày cao hơn Pro/Thinking (model mặc định).
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


@restricted
async def chat_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    symbols = await stock_analysis.find_valid_symbols(text)
    if symbols:
        if stock_analysis.wants_full_analysis(text):
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
        else:
            prompt_id = await db.save_prompt(user_id, "stock_price", ",".join(symbols))
            try:
                quotes = await asyncio.gather(*[stock_analysis.quick_quote(s) for s in symbols])
                for quote_text in quotes:
                    await db.save_result(prompt_id, "stock_price", content_text=quote_text)
                    await update.message.reply_text(quote_text)
            except Exception as e:  # noqa: BLE001
                logger.exception("Lỗi lấy giá cổ phiếu")
                await _record_failure(prompt_id, "stock_price", e)
                await update.message.reply_text(
                    "❌ Có lỗi khi lấy giá cổ phiếu. Hãy thử lại sau giây lát."
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
                caption = reply_text[:TELEGRAM_CAPTION_MAX] if i == 0 and reply_text else None
                await update.message.reply_photo(photo=photo_bytes, caption=caption)
                await db.save_result(prompt_id, "image", file_path=str(full_path))
                await _forward_to_gallery(context, photo_path=full_path, caption=text)
                await _safe_delete(full_path)
            return

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
    await gemini_client.reset_chat()
    await update.message.reply_text("🔄 Đã xoá ngữ cảnh hội thoại. Bắt đầu chat mới nhé!")


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
    user_id = update.effective_user.id
    caption = (update.message.caption or "").strip()
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await db.save_prompt(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

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
        await update.message.reply_text(
            f"📝 <b>Prompt gợi ý (dùng cho app Gemini):</b>\n\n<pre>{html.escape(result_text)}</pre>",
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Lỗi phân tích ảnh")
        await _record_failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Có lỗi khi phân tích ảnh. Hãy thử lại sau giây lát."
        )
    finally:
        await _safe_delete(local_path)


@restricted
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=HISTORY_LIMIT)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {"image": "🖼️", "chat": "💬", "promptify": "🔍", "stock_analysis": "📊", "stock_price": "💹"}
    lines = [f"🕘 <b>{HISTORY_LIMIT} lượt gần nhất:</b>\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = (
            prompt[:HISTORY_PROMPT_PREVIEW_MAX] + "…"
            if len(prompt) > HISTORY_PROMPT_PREVIEW_MAX
            else prompt
        )
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        lines.append(
            f"{icon} [{html.escape(command_type)}] {html.escape(short_prompt)} ({date_part})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Lỗi không được xử lý", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "❌ Đã có lỗi không mong muốn xảy ra. Vui lòng thử lại."
            )
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
