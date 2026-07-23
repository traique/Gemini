"""Xử lý ảnh gửi tới bot: phân tích ảnh chân dung -> viết prompt
"identity-lock" tiếng Anh (dùng cùng ảnh gốc trên app Gemini để tạo ảnh mới
giữ nguyên khuôn mặt)."""
import html
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from core import config
from ai import orchestrator
from handlers import common
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

IMAGE_ANALYZE_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation tools, specialized in writing "identity-preserving" and HYPER-REALISTIC prompts. The goal is to generate images that look like real, candid, unretouched photographs, avoiding any "AI-generated", plasticky, or overly polished aesthetic.

Look at the attached reference image and write ONE complete, ready-to-use English prompt following EXACTLY this structure and style (this is an example of the expected style/quality - match its level of detail, but invent NEW creative content appropriate to the reference photo):

---
[Identity Lock: Strictly maintain the exact facial features, skin tone, age, ethnicity, and facial proportions of the person in the reference image. Preserve natural skin texture and visible pores; DO NOT smooth or airbrush the face].

Raw, candid smartphone photo of the woman from the reference image standing on a wet pedestrian street at night. She is looking slightly off-camera with a natural, unposed expression. Her hair is drenched from the rain, clinging to her neck and shoulders. 

She is wearing a thin, wet white button-up shirt that clings to her skin, showing realistic wet fabric textures and natural folds. 

The background is a gritty, authentic urban street at night with heavy rain. Blurred streetlights and car headlights create natural out-of-focus bokeh on the wet asphalt. 

Shot on iPhone 15 Pro Max camera, unedited, unretouched. 35mm lens, f/1.8. 

Harsh, imperfect street lighting mixed with camera flash. Natural skin texture, visible pores, slight skin imperfections, specular highlights on wet skin. Subtle chromatic aberration, noticeable low-light noise and film grain. Authentic, raw, documentary photography style, zero airbrushing. --ar 4:5
---

Rules for what you generate:
1. ALWAYS start with an "[Identity Lock: ...]" line.
2. ACCURATELY describe the outfit, pose, and vibe of the reference image. If it's a sensual/wet look, describe it accurately using anatomical and clothing terms without being explicitly pornographic.
3. FORBIDDEN WORDS: NEVER use terms like "masterpiece", "8k", "ultra-photorealistic", "perfect", "flawless", "editorial", or "studio lighting". These cause the image to look fake.
4. MANDATORY WORDS: ALWAYS include photography terms that add realism and imperfection, such as "candid", "unretouched", "raw photo", "natural skin texture", "visible pores", "film grain", "amateur lighting", or specific camera models (e.g., "Shot on Kodak Portra 400", "Polaroid", "iPhone snapshot").
5. Output ONLY the final prompt as plain text, no markdown headers, no preamble."""


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

        instruction = IMAGE_ANALYZE_INSTRUCTION_BASE
        if caption:
            instruction += f"\n\nAdditional user instruction: {caption}"

        response = await orchestrator.analyze_image(instruction, str(local_path))
        result_text = (response.text or "").strip()

        if not result_text:
            await telemetry.success(prompt_id, "promptify", "(Gemini không trả về nội dung)")
            await update.message.reply_text(
                "Gemini không trả về nội dung phân tích. Thử gửi lại ảnh hoặc ảnh khác nhé."
            )
            return

        await telemetry.success(prompt_id, "promptify", result_text)
        suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""
        await update.message.reply_text(
            f"📝 <b>Prompt gợi ý (dùng cho app Gemini):</b>\n\n<pre>{html.escape(result_text)}</pre>{suffix}",
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
