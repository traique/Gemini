"""Xử lý ảnh gửi tới bot: phân tích ảnh chân dung -> viết prompt
"identity-lock" tiếng Anh (dùng cùng ảnh gốc trên app Gemini để tạo ảnh mới
giữ nguyên khuôn mặt).

Hỗ trợ 2 cách gửi ảnh:
- Ảnh nén (filters.PHOTO): đi qua photo_msg trực tiếp.
- File/document ảnh (filters.Document.IMAGE): cũng đi qua photo_msg,
  đọc từ update.message.document thay vì update.message.photo.
"""
import logging

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

import messages
from core import config
from ai import orchestrator
from handlers import common
from handlers.commands import (
    IDENTITY_LOCK_GIRL,
    IDENTITY_LOCK_REFERENCE,
    KEEP_FACE_KEYWORDS,
    GIRL_KEYWORDS,
    _IDENTITY_RULE_LOCK,
)
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

# Rule dùng khi không có từ khoá: để Gemini tự quyết dựa vào ảnh
_PHOTO_IDENTITY_RULE_NONE = (
    '1. ONLY start with an "[Identity Lock: ...]" line IF the reference image '
    "clearly contains a person or face. If the image is a landscape, object, "
    "or has no clear human subject, DO NOT include the Identity Lock."
)

IMAGE_ANALYZE_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation tools, specialized in writing "identity-preserving" and HYPER-REALISTIC prompts. The goal is to generate images that look like real, candid, unretouched photographs, avoiding any "AI-generated", plasticky, or overly polished aesthetic.

Look at the attached reference image and write ONE complete, ready-to-use English prompt following EXACTLY this structure and style (this is an example of the expected style/quality - match its level of detail, but invent NEW creative content appropriate to the reference photo):

---
{identity_lock_example}

Raw, candid smartphone photo of the subject from the reference image standing on a wet pedestrian street at night. She is looking slightly off-camera with a natural, unposed expression. Her hair is drenched from the rain, clinging to her neck and shoulders.

She is wearing a thin, wet white button-up shirt that clings to her skin, showing realistic wet fabric textures and natural folds. 

The background is a gritty, authentic urban street at night with heavy rain. Blurred streetlights and car headlights create natural out-of-focus bokeh on the wet asphalt. 

Shot on iPhone 15 Pro Max camera, unedited, unretouched. 35mm lens, f/1.8. 

Harsh, imperfect street lighting mixed with camera flash. Natural skin texture, visible pores, slight skin imperfections, specular highlights on wet skin. Subtle chromatic aberration, noticeable low-light noise and film grain. Authentic, raw, documentary photography style, zero airbrushing. --ar 4:5
---

⚠️ The example above demonstrates FORMAT and PHOTOGRAPHY STYLE ONLY. The scene, setting, lighting, and mood MUST be derived entirely from the reference image, NOT copied from the example.

Rules for what you generate:
{identity_rule}
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

    # Xác định identity lock theo 3 trường hợp (chỉ kích hoạt khi có từ khoá):
    # - giữ mặt / giữ khuôn mặt / mặt tôi / mặt anh / mặt em -> REFERENCE
    # - cô gái 20 / gái 20                                          -> GIRL
    # - không có từ khoá (kể cả no-caption)                         -> AI tự quyết dựa vào ảnh
    caption_lower = caption.lower()
    if any(kw in caption_lower for kw in KEEP_FACE_KEYWORDS):
        identity_lock_example = IDENTITY_LOCK_REFERENCE
        identity_rule = _IDENTITY_RULE_LOCK
    elif any(kw in caption_lower for kw in GIRL_KEYWORDS):
        identity_lock_example = IDENTITY_LOCK_GIRL
        identity_rule = _IDENTITY_RULE_LOCK
    else:
        # Không caption hoặc caption không có từ khoá -> để AI tự phân tích ảnh
        identity_lock_example = "[Identity Lock: None. Let the AI decide the subject's face based on the image]."
        identity_rule = _PHOTO_IDENTITY_RULE_NONE

    # Hỗ trợ cả ảnh gửi dạng nén (photo) lẫn dạng file (document)
    if update.message.photo:
        file_obj = update.message.photo[-1]
    elif update.message.document:
        file_obj = update.message.document
    else:
        await update.message.reply_text("❌ Không đọc được ảnh. Anh thử gửi lại nhé.")
        return

    # ── Pha 1: Tải ảnh từ Telegram ──────────────────────────────────────────
    try:
        await common.download_telegram_photo_with_retry(file_obj, local_path)
    except (TimedOut, NetworkError) as e:
        logger.exception("Lỗi tải ảnh từ Telegram")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(messages.PHOTO_TIMEOUT_ERROR)
        return
    except Exception as e:
        logger.exception("Lỗi không xác định khi tải ảnh")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text("❌ Không tải được ảnh. Anh thử gửi lại nhé.")
        return

    # ── Pha 2: Phân tích bằng Gemini và gửi kết quả ──────────────────────
    try:
        instruction = IMAGE_ANALYZE_INSTRUCTION_BASE.format(
            identity_lock_example=identity_lock_example,
            identity_rule=identity_rule,
        )
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
        # Gửi header riêng, rồi nội dung prompt qua reply_long_text
        # để tránh lỗi Message_too_long khi prompt > 4096 ký tự
        await update.message.reply_text(
            "📝 <b>Prompt gợi ý (dùng cho app Gemini):</b>",
            parse_mode="HTML",
        )
        await common.reply_long_text(update.message, result_text + suffix)
    except Exception as e:
        logger.exception("Lỗi phân tích ảnh (Gemini hoặc gửi kết quả)")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Gemini không phân tích được ảnh lúc này. Anh thử lại sau nhé."
        )
    finally:
        await common.safe_delete(local_path)
