"""Xử lý ảnh gửi tới bot: phân tích ảnh (chủ thể/bối cảnh/bố cục/ánh sáng/
màu sắc/phong cách) rồi viết prompt tiếng Anh cho Gemini và ChatGPT để tái
tạo hoặc biến đổi ảnh. Chỉ bật identity-lock (giữ mặt) khi caption có
keyword yêu cầu rõ ràng - không ép mọi ảnh thành ảnh chân dung."""
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from core import config
from ai import orchestrator
from handlers import common
from services import image_prompt_service
from services.telemetry import telemetry

logger = logging.getLogger(__name__)


@common.restricted
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    caption = (update.message.caption or "").strip()
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await telemetry.start(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    filename = f"promptify_{prompt_id}.jpg"
    local_path = config.MEDIA_DIR / filename

    try:
        tg_photo = update.message.photo[-1]
        await common.download_telegram_photo_with_retry(tg_photo, local_path)

        instruction = image_prompt_service.build_image_to_prompt_instruction(caption)

        response = await orchestrator.analyze_image(instruction, str(local_path))
        result_text = image_prompt_service.response_text(response)

        if not result_text:
            await telemetry.success(prompt_id, "promptify", "(Gemini không trả về nội dung)")
            await update.message.reply_text(
                "Gemini không trả về nội dung phân tích. Thử gửi lại ảnh hoặc ảnh khác nhé."
            )
            return

        await telemetry.success(prompt_id, "promptify", result_text)
        suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""

        formatted = image_prompt_service.format_telegram_html(result_text)
        await update.message.reply_text(
            f"📝 <b>Prompt ảnh cho Gemini/ChatGPT:</b>\n\n{formatted}{suffix}",
            parse_mode="HTML",
        )
    except (TimedOut, NetworkError) as e:
        logger.exception("Lỗi tải ảnh từ Telegram")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            messages.PHOTO_TIMEOUT_ERROR
        )
    except Exception as e:
        logger.exception("Lỗi xử lý ảnh (tải hoặc phân tích)")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Có lỗi khi xử lý ảnh. Hãy thử lại sau giây lát."
        )
    finally:
        await common.safe_delete(local_path)
