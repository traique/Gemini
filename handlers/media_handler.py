"""Xử lý ảnh gửi tới bot: phân tích ảnh -> viết prompt tiếng Anh dán sang
app Gemini.

Có 3 DẠNG, mỗi dạng LẤY KHUÔN MẶT TỪ MỘT NGUỒN KHÁC NHAU, nên prompt
xuất ra phải tả chủ thể theo một cách khác nhau. Trước đây cả 3 dùng chung
một câu "the subject from the reference image" và không tả mặt gì cả - đó là
lý do ảnh tạo ra khác hẳn ảnh mẫu:

1. Không caption (hoặc caption không có từ khoá) -> TẢ LẠI ĐÚNG NGƯỜI
   TRONG ẢNH thành chữ. Prompt này được dán sang Gemini dưới dạng chữ thuần
   tuý, KHÔNG đính kèm ảnh. Vì vậy câu "the subject from the reference image"
   là câu RỖNG - không có ảnh nào để trỏ tới, model sẽ tự bịa ra một khuôn
   mặt bất kỳ.
2. "cô gái 20" -> TẢ LẠI KHUÔN MẶT TRONG IDENTITY_LOCK_GIRL thành chữ để
   đồng nhất nhân vật qua nhiều ảnh. Ảnh gốc chỉ cho dáng, đồ, bối cảnh -
   TUYỆT ĐỐI không tả mặt người trong ảnh, vì tả vào là đánh nhau với khoá.
3. "mặt tôi" -> user ĐÍNH KÈM ẢNH cùng prompt trên app Gemini. Lúc này câu
   "attached reference image" mới có nghĩa. Ngược lại, KHÔNG được bịa chi
   tiết khuôn mặt vì sẽ chọi với ảnh thật đính kèm.

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

# ---------------------------------------------------------------------------
# Rule 1 - có chen dòng [Identity Lock] hay không
# ---------------------------------------------------------------------------
# Dạng 1 (không từ khoá, gồm cả ảnh không caption): không có dòng lock.
_PHOTO_IDENTITY_RULE_NONE = (
    "1. DO NOT include an \"[Identity Lock: ...]\" line at all, under any "
    "circumstances. Start the prompt directly with the scene description, "
    "even if the reference image clearly contains a person or a face."
)

# ---------------------------------------------------------------------------
# Cách tả chủ thể ngay trong prompt mẫu (đây là đòn bẩy chính: Gemini bắt
# chước y nguyên cách gọi chủ thể của ví dụ)
# ---------------------------------------------------------------------------
# Dạng 1: tả người TRONG ẢNH bằng chữ.
_SUBJECT_PHRASE_DESCRIBED = (
    "a woman in her early 20s with long dark brown hair parted in the middle, "
    "an oval face with a soft jawline, almond-shaped dark brown eyes, softly "
    "arched thin eyebrows, a small straight nose, full natural lips and fair "
    "warm-toned skin,"
)
# Dạng 2: tả khuôn mặt ĐÃ BỊ KHOÁ bằng chữ (lấy từ IDENTITY_LOCK_GIRL),
# không phải người trong ảnh.
_SUBJECT_PHRASE_GIRL = (
    "the same 20-year-old Vietnamese woman defined in the Identity Lock above "
    "- heart-shaped face with a smooth jawline, large round doe eyes with "
    "natural eyelashes, a delicate nose and natural soft lips,"
)
# Dạng 3: có ảnh thật đính kèm trên app Gemini -> không tả mặt.
_SUBJECT_PHRASE_REFERENCE = "the subject from the attached reference image"

# ---------------------------------------------------------------------------
# Rule 2 - khuôn mặt lấy từ đâu và tả như thế nào
# ---------------------------------------------------------------------------
_PHOTO_SUBJECT_RULE_DESCRIBED = (
    "2. CRITICAL - this prompt will be pasted as PLAIN TEXT with NO image "
    "attached. Therefore you must NEVER write \"the subject from the reference "
    "image\", \"the person in the photo\", or any phrase pointing at an image: "
    "with nothing attached such a phrase is empty and the generator will "
    "invent a random face. Instead REPLACE the subject with a dense written "
    "description of the person you actually see in the reference image, so "
    "that the prompt reproduces them on its own. You MUST state all of: "
    "approximate age, ethnicity or facial character, face shape, eye shape and "
    "eye colour, eyebrow shape, nose shape, lip shape, jawline and chin, skin "
    "tone and skin texture, and hair colour, length, texture and parting. Put "
    "this description in the very first sentence, exactly like the example."
)
_PHOTO_SUBJECT_RULE_GIRL = (
    "2. CRITICAL - the face is FIXED by the Identity Lock above and must be "
    "identical in every generation. You MUST also restate that locked face as "
    "a written description inside the first sentence of the prompt (face "
    "shape, jawline, eye shape, eyelashes, nose, lips, age, ethnicity), copied "
    "faithfully from the Identity Lock block, exactly like the example. DO NOT "
    "describe the face of the person in the reference image, and DO NOT copy "
    "their age, ethnicity, hair colour, eye colour or any facial feature. Take "
    "ONLY the pose, framing, outfit, accessories, setting, lighting and mood "
    "from the reference image."
)
_PHOTO_SUBJECT_RULE_REFERENCE = (
    "2. CRITICAL - the user will attach the reference photo together with this "
    "prompt, so the identity is carried by that attachment. Refer to the "
    "subject as \"the subject from the attached reference image\" and state "
    "that her face must match the attached photo exactly. DO NOT invent "
    "concrete facial features (eye colour, face shape, nose or lip shape, hair "
    "colour): inventing them fights the attached photo and changes the face. "
    "Describe only pose, expression, outfit, setting and lighting."
)

IMAGE_ANALYZE_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation tools, specialized in writing "identity-preserving" and HYPER-REALISTIC prompts. The goal is to generate images that look like real, candid, unretouched photographs, avoiding any "AI-generated", plasticky, or overly polished aesthetic.

Look at the attached reference image and write ONE complete, ready-to-use English prompt following EXACTLY this structure and style (this is a NEUTRAL example showing the expected format, level of detail and how to name the subject - match its structure, but invent NEW content taken from the reference photo):

---
{identity_lock_block}Raw, candid smartphone photo of {subject_phrase} standing on a quiet residential sidewalk in the late afternoon. She is looking slightly off-camera with a natural, unposed expression, one hand resting on the strap of her shoulder bag.

She is wearing a plain oversized grey cotton t-shirt and simple straight-leg jeans, with natural fabric folds and everyday creases.

The background is an ordinary street with a low garden wall and parked cars, softly out of focus.

Shot on iPhone 15 Pro Max camera, unedited, unretouched. 35mm lens, f/1.8.

Soft, warm late-afternoon daylight. Natural skin texture, visible pores, slight skin imperfections. Subtle chromatic aberration, fine film grain. Authentic, raw, documentary photography style, zero airbrushing.
---

⚠️ The example above demonstrates FORMAT, PHOTOGRAPHY STYLE and HOW TO NAME THE SUBJECT only. The scene, setting, outfit, pose, lighting and mood MUST be derived entirely from the reference image, NOT copied from the example. Do NOT reuse the sidewalk, the grey t-shirt, the jeans or the late-afternoon light unless the reference image actually shows them.

Rules for what you generate:
{identity_rule}
{subject_rule}
3. ACCURATELY describe the outfit, accessories, pose, framing and vibe of the reference image. If it's a sensual/wet look, describe it accurately using anatomical and clothing terms without being explicitly pornographic.
4. LIGHTING AND GRAIN MUST MATCH THE ACTUAL SCENE of the reference image. Only write "low-light noise" for a genuine night or dim indoor scene; for daylight, overcast or bright indoor scenes write the correct light and use "fine film grain" or "subtle sensor noise" instead. Contradictory lighting terms make the generator drift away from the reference.
5. DO NOT append any tool-specific flags or parameters such as "--ar 4:5", "--v 6", "--style raw" or "::". These belong to other tools and are meaningless here; if the aspect ratio matters, describe the framing in plain words (e.g. "vertical portrait framing").
6. FORBIDDEN WORDS: NEVER use terms like "masterpiece", "8k", "ultra-photorealistic", "perfect", "flawless", "editorial", or "studio lighting". These cause the image to look fake.
7. MANDATORY WORDS: ALWAYS include photography terms that add realism and imperfection, such as "candid", "unretouched", "raw photo", "natural skin texture", "visible pores", "film grain", "amateur lighting", or specific camera models (e.g., "Shot on Kodak Portra 400", "Polaroid", "iPhone snapshot").
8. Output ONLY the final prompt as plain text, no markdown headers, no preamble."""


@common.restricted
async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    caption = (update.message.caption or "").strip()
    prompt_label = caption or "(gửi ảnh, không có caption)"
    prompt_id = await telemetry.start(user_id, "promptify", prompt_label)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    filename = f"promptify_{prompt_id}.jpg"
    local_path = config.MEDIA_DIR / filename

    # Xác định nguồn khuôn mặt theo 3 dạng (xem docstring đầu file):
    # - giữ mặt / mặt tôi ... -> mặt nằm ở ảnh user đính kèm trên app Gemini
    # - cô gái 20 / gái 20   -> mặt nằm ở khối lock cố định, phải tả ra chữ
    # - không từ khoá        -> mặt nằm ở ảnh gốc, phải tả ra chữ
    caption_lower = caption.lower()
    if any(kw in caption_lower for kw in KEEP_FACE_KEYWORDS):
        identity_lock_block = f"{IDENTITY_LOCK_REFERENCE}\n\n"
        identity_rule = _IDENTITY_RULE_LOCK
        subject_phrase = _SUBJECT_PHRASE_REFERENCE
        subject_rule = _PHOTO_SUBJECT_RULE_REFERENCE
        mode_hint = "\n\n📎 Nhớ đính kèm lại ảnh gốc cùng prompt này trên app Gemini nha anh."
    elif any(kw in caption_lower for kw in GIRL_KEYWORDS):
        identity_lock_block = f"{IDENTITY_LOCK_GIRL}\n\n"
        identity_rule = _IDENTITY_RULE_LOCK
        subject_phrase = _SUBJECT_PHRASE_GIRL
        subject_rule = _PHOTO_SUBJECT_RULE_GIRL
        mode_hint = "\n\n🔒 Khoá khuôn mặt cố định - dán prompt KHÔNG kèm ảnh để giữ đúng nhân vật."
    else:
        # Không caption hoặc caption không có từ khoá -> bỏ hẳn dòng Identity Lock,
        # bù lại bằng mô tả khuôn mặt bằng chữ để prompt tự đứng được một mình.
        identity_lock_block = ""
        identity_rule = _PHOTO_IDENTITY_RULE_NONE
        subject_phrase = _SUBJECT_PHRASE_DESCRIBED
        subject_rule = _PHOTO_SUBJECT_RULE_DESCRIBED
        mode_hint = "\n\n🖼️ Prompt tự tả khuôn mặt bằng chữ - dán là dùng được, không cần đính kèm ảnh."

    # Hỗ trợ cả ảnh gửi dạng nén (photo) lẫn dạng file (document)
    if update.message.photo:
        file_obj = update.message.photo[-1]
    elif update.message.document:
        file_obj = update.message.document
    else:
        await update.message.reply_text("❌ Không đọc được ảnh. Anh thử gửi lại nhé.")
        return

    # ── Pha 1: Tải ảnh từ Telegram ──────────────────────────────
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

    # ── Pha 2: Phân tích bằng Gemini và gửi kết quả ──────────────────
    try:
        instruction = IMAGE_ANALYZE_INSTRUCTION_BASE.format(
            identity_lock_block=identity_lock_block,
            subject_phrase=subject_phrase,
            identity_rule=identity_rule,
            subject_rule=subject_rule,
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
        # Header riêng, rồi nội dung prompt gửi trong khối <pre> để Telegram hiện
        # nút Copy. Nhãn "⚙️ API" đặt ở header chứ KHÔNG đặt trong khối prompt,
        # nếu không bấm Copy sẽ chép luôn cả nhãn đó sang app Gemini.
        header = "📝 <b>Prompt gợi ý (dùng cho app Gemini)</b> — chạm vào khối bên dưới để chép:"
        if getattr(response, "used_fallback", False):
            header += "  ⚙️ API"
        header += mode_hint
        await update.message.reply_text(header, parse_mode="HTML")
        await common.reply_code_block(update.message, result_text)
    except Exception as e:
        logger.exception("Lỗi phân tích ảnh (Gemini hoặc gửi kết quả)")
        await telemetry.failure(prompt_id, "promptify", e)
        await update.message.reply_text(
            "❌ Gemini không phân tích được ảnh lúc này. Anh thử lại sau nhé."
        )
    finally:
        await common.safe_delete(local_path)
