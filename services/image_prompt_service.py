"""Xây dựng instruction cho luồng "tạo prompt ảnh" (`/prompt` và gửi ảnh),
nhắm riêng tới 2 công cụ người dùng thực sự dùng để vẽ ảnh: Gemini và
ChatGPT. KHÔNG dùng syntax Midjourney (--ar, --v, --style...) hay tag
Stable Diffusion/LoRA/sampler/CFG/seed.

Module này KHÔNG import telegram, ai.orchestrator, core.database, hay
services.telemetry - chỉ build string thuần Python - để handlers/commands.py
và handlers/media_handler.py có thể import mà không tạo circular dependency.
"""
from __future__ import annotations

import html
import re

IDENTITY_KEYWORDS = (
    "giữ mặt",
    "giữ khuôn mặt",
    "giữ gương mặt",
    "mặt tôi",
    "mặt anh",
    "mặt em",
    "khuôn mặt của tôi",
    "khuôn mặt của anh",
    "khuôn mặt của em",
    "sử dụng khuôn mặt",
    "dùng khuôn mặt",
    "dùng mặt tôi",
    "same face",
    "preserve face",
    "use my face",
    "reference face",
    "identity",
    "identity lock",
)

REALISM_KEYWORDS = (
    "chân thật",
    "thật nhất",
    "như ảnh chụp",
    "như chụp",
    "không ai",
    "không giống ai",
    "không bị ai",
    "không phải ai",
    "có hồn",
    "đời thường",
    "tự nhiên",
    "realistic",
    "real photo",
    "candid",
    "photo",
    "photograph",
    "not ai",
    "not ai-generated",
)


def wants_identity_lock(text: str | None) -> bool:
    """Return True when user explicitly asks to preserve face/identity."""
    if not text:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in IDENTITY_KEYWORDS)


def wants_max_realism(text: str | None) -> bool:
    """Return True when user asks for realistic/candid/non-AI photo feeling."""
    if not text:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in REALISM_KEYWORDS)


# ─── Các khối instruction dùng chung ────────────────────────────────────

_PLATFORM_RULES = """The user creates images ONLY with two tools: Gemini (Google's image generation) and ChatGPT (OpenAI's image generation). Write prompts optimized for these two tools only.

Hard rules - do NOT violate any of these:
- Do NOT write Midjourney-style parameters. Do NOT use --ar, --v, --style, --chaos, --stylize, --niji, or any double-dash flag.
- Do NOT write Stable Diffusion / SDXL tags, LoRA names, sampler names (e.g. Euler, DPM++), CFG scale, or seed numbers.
- Do NOT write comma-separated keyword-spam tag lists. Write natural, flowing art-direction sentences instead, the way a human director would describe a photo to a photographer.
- Write both image prompts entirely in English.
- Write all suggestions/explanations in Vietnamese.
- Each prompt should read as ONE cohesive scene description: who/what the subject is and what they are doing, the setting, the composition/framing, the lighting, the color palette, the mood, and the camera/photographic style when relevant.
- If the user's input is short or vague, add reasonable, concrete visual details to make the scene vivid - but never change the core idea the user gave."""

_AVOID_SENTENCE = """End each prompt with a short natural sentence covering things to avoid, phrased naturally (not as a tag list), for example: no text, no logos, no watermarks, no blurry details, no distorted anatomy or hands, no plastic skin, no airbrushing, no AI-generated look."""

_FORBIDDEN_WORDS = """Never use these words/phrases, they make images look fake and overly polished: masterpiece, 8k, ultra-photorealistic, perfect, flawless, editorial, glamour, studio lighting, symmetrical face, cinematic masterpiece, award-winning, hyper detailed, fashion magazine."""

_REALISM_BLOCK = """Prioritize maximum realism. Make the photo feel like an unedited candid photo taken by a real person in a real everyday moment, with natural imperfections and human emotion - not a generated, retouched, or "AI-looking" image.
Weave in natural imperfections where relevant: visible pores, real skin texture, slight facial asymmetry, minor skin imperfections, natural clothing folds, imperfect hair, realistic shadows, subtle grain/noise, mild lens softness, imperfect focus, natural color tones, ordinary/slightly cluttered everyday background, realistic imperfect lighting (window light, street light, neon, phone flash, cafe lighting).
Avoid perfect studio aesthetics, plastic skin, overly clean symmetry, glamour retouching, and poster-like composition."""

_IDENTITY_TEXT_BLOCK = """The user wants to preserve the face/identity of the person from a reference image or from prior context in the conversation. Inside BOTH prompts, naturally weave in a short reference-face instruction, phrased like: "use the provided reference image to preserve the person's facial features, face shape, skin tone, natural expression, realistic facial proportions, natural skin texture, visible pores, and subtle asymmetry." Do not over-beautify, airbrush, or otherwise change the face. Only change the requested scene, outfit, background, lighting, or style."""

_NO_IDENTITY_TEXT_BLOCK = """Do not use any identity-lock or "same person every time" language unless the user explicitly asked for it. If the idea involves a person, describe them naturally as part of the scene without inventing a fixed recurring identity."""

_IDENTITY_IMAGE_BLOCK = """The user wants to preserve the face/identity of the person shown in the attached image. Inside BOTH prompts, naturally weave in a short reference-face instruction, phrased like: "use the provided reference image to preserve the person's facial features, face shape, skin tone, natural expression, realistic facial proportions, natural skin texture, visible pores, and subtle asymmetry." Do not claim or imply a real, named public figure's identity - only preserve visual likeness from the reference image. Do not over-beautify, airbrush, or otherwise change the face. Only transform the scene, outfit, background, lighting, or style as requested."""

_NO_IDENTITY_IMAGE_BLOCK = """The image is not necessarily a portrait, and the user did not ask to preserve a face. Do NOT use identity-lock or reference-face language. If a person appears in the image, describe them naturally as part of the scene."""

_SAFETY_IMAGE_BLOCK = """Safety and accuracy rules for the analysis:
- Do not identify any real person by name.
- Do not infer or state sensitive personal attributes about anyone in the photo.
- Describe only what is visibly present in the image, plus reasonable, clearly-artistic interpretation.
- If the user's caption asks for a transformation (e.g. change background, turn into a product/ad photo, change style), prioritize that transformation over literally recreating the original image."""


def _output_format_text(with_analysis: bool) -> str:
    analysis_part = (
        "🖼️ Phân tích ảnh:\n"
        "- Chủ thể:\n"
        "- Bối cảnh:\n"
        "- Bố cục:\n"
        "- Ánh sáng:\n"
        "- Màu sắc:\n"
        "- Phong cách:\n\n"
    ) if with_analysis else ""

    return (
        "Respond using EXACTLY this structure (keep the emoji headers, keep everything under "
        "\"Prompt cho Gemini\" and \"Prompt cho ChatGPT\" in English, everything else in Vietnamese, "
        "no markdown code blocks, no extra commentary outside this structure):\n\n"
        f"{analysis_part}"
        "🎨 Prompt cho Gemini:\n"
        "...\n\n"
        "🤖 Prompt cho ChatGPT:\n"
        "...\n\n"
        "📝 Gợi ý:\n"
        "- Tỷ lệ:\n"
        "- Phong cách:\n"
        "- Có thể chỉnh thêm:\n"
        "..."
    )


def build_text_to_image_instruction(
    user_desc: str,
    *,
    preserve_identity: bool | None = None,
) -> str:
    """Build instruction for /prompt: text -> Gemini + ChatGPT image prompts."""
    if preserve_identity is None:
        preserve_identity = wants_identity_lock(user_desc)
    realism = wants_max_realism(user_desc)

    identity_block = _IDENTITY_TEXT_BLOCK if preserve_identity else _NO_IDENTITY_TEXT_BLOCK
    realism_block = f"\n\n{_REALISM_BLOCK}" if realism else ""

    return (
        "You are an expert AI art director who writes natural, human-sounding image-generation "
        "prompts.\n\n"
        f"{_PLATFORM_RULES}\n\n"
        f"{identity_block}{realism_block}\n\n"
        f"{_FORBIDDEN_WORDS}\n\n"
        f"{_AVOID_SENTENCE}\n\n"
        f"{_output_format_text(with_analysis=False)}\n\n"
        f"User's basic description: {user_desc}"
    )


def build_image_to_prompt_instruction(
    caption: str | None = None,
    *,
    preserve_identity: bool | None = None,
) -> str:
    """Build instruction for image upload: image -> Gemini + ChatGPT prompts."""
    caption = (caption or "").strip()
    if preserve_identity is None:
        preserve_identity = wants_identity_lock(caption)
    realism = wants_max_realism(caption)

    identity_block = _IDENTITY_IMAGE_BLOCK if preserve_identity else _NO_IDENTITY_IMAGE_BLOCK
    realism_block = f"\n\n{_REALISM_BLOCK}" if realism else ""
    caption_line = f"\n\nAdditional user instruction from caption: {caption}" if caption else ""

    return (
        "You are an expert AI art director who writes natural, human-sounding image-generation "
        "prompts.\n\n"
        "Look at the attached image and write prompts that can recreate or transform its visual "
        "idea, composition, mood, and style.\n\n"
        f"{_PLATFORM_RULES}\n\n"
        f"{identity_block}{realism_block}\n\n"
        f"{_SAFETY_IMAGE_BLOCK}\n\n"
        f"{_FORBIDDEN_WORDS}\n\n"
        f"{_AVOID_SENTENCE}\n\n"
        f"{_output_format_text(with_analysis=True)}"
        f"{caption_line}"
    )


def response_text(response) -> str:
    """Extract plain text from cookie/API response object."""
    try:
        text = getattr(response, "text", "") or ""
    except Exception:
        text = ""
    return text.strip()


# ─── Format sang Telegram HTML ─────────────────────────────────────────

# Chấp nhận cả emoji có/không variation selector (🖼️ vs 🖼) vì model không
# phải lúc nào cũng sinh ra đúng byte-for-byte 1 kiểu.
_SECTION_HEADERS = (
    "🖼️ Phân tích ảnh:",
    "🖼 Phân tích ảnh:",
    "🎨 Prompt cho Gemini:",
    "🤖 Prompt cho ChatGPT:",
    "📝 Gợi ý:",
)

# Các section này được bọc trong <pre> để Telegram tự hiện nút "Chép mã"
# riêng cho từng prompt (giống hành vi cũ), thay vì 1 khối <pre> lớn gộp
# chung cả phân tích ảnh lẫn gợi ý.
_CODE_BLOCK_HEADERS = {
    "🎨 Prompt cho Gemini:",
    "🤖 Prompt cho ChatGPT:",
}

_HEADER_SPLIT_RE = re.compile(
    "(" + "|".join(re.escape(h) for h in _SECTION_HEADERS) + ")"
)


def format_telegram_html(result_text: str) -> str:
    """Chuyển output thô của model (theo cấu trúc 🖼️/🎨/🤖/📝) sang Telegram
    HTML: mỗi prompt Gemini/ChatGPT nằm trong <pre>...</pre> riêng để
    Telegram tự hiện nút "Chép mã" cho từng prompt; phần phân tích ảnh và
    gợi ý hiển thị dạng chữ thường có tiêu đề in đậm.

    Nếu model không theo đúng cấu trúc header (không tìm thấy header nào),
    fallback về 1 khối <pre> duy nhất cho toàn bộ nội dung, để vẫn có nút
    chép mã thay vì mất trắng định dạng.
    """
    escaped = html.escape((result_text or "").strip())
    if not escaped:
        return escaped

    parts = _HEADER_SPLIT_RE.split(escaped)
    if len(parts) == 1:
        return f"<pre>{escaped}</pre>"

    blocks: list[str] = []
    preamble = parts[0].strip()
    if preamble:
        blocks.append(preamble)

    i = 1
    while i < len(parts):
        header = parts[i]
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        i += 2

        if not body:
            blocks.append(f"<b>{header}</b>")
        elif header in _CODE_BLOCK_HEADERS:
            blocks.append(f"<b>{header}</b>\n<pre>{body}</pre>")
        else:
            blocks.append(f"<b>{header}</b>\n{body}")

    return "\n\n".join(blocks)
