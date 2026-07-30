import asyncio
import html
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.ext import ContextTypes

import messages
from ai import cookie_client, orchestrator
from core import config, database as db
from handlers import common
from services import memory_service
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

HISTORY_PROMPT_PREVIEW_MAX = 60
HISTORY_LIMIT = 10

# ---------------------------------------------------------------------------
# Identity lock constants — dùng chung cho /prompt và media_handler.py
# ---------------------------------------------------------------------------
KEEP_FACE_KEYWORDS = ["giữ mặt", "giữ khuôn mặt", "mặt tôi", "mặt anh", "mặt em"]
GIRL_KEYWORDS = ["cô gái 20", "gái 20"]

IDENTITY_LOCK_REFERENCE = "[Identity Lock: Strictly maintain the exact facial features, skin tone, age, ethnicity, and facial proportions of the person in the reference image. Preserve natural skin texture and visible pores; DO NOT smooth or airbrush the face]"
IDENTITY_LOCK_GIRL = """[IDENTITY LOCK (ABSOLUTE CONSISTENCY)
The subject is the exact same 20-year-old Vietnamese woman in every generation. Preserve her facial identity with zero variation.
Heart-shaped face with a smooth jawline, large round doe eyes, natural eyelashes, delicate nose, natural soft lips. Authentic Vietnamese beauty.
CRUCIAL FOR REALISM: Unretouched natural skin texture, visible facial pores, subtle natural skin variations, realistic specular highlights on skin. ABSOLUTELY NO airbrushing, NO flawless porcelain skin, NO plastic smoothing.
Maintain identical facial structure, facial proportions, eye shape, eyebrow shape, nose, lips, jawline, chin, skin tone, and overall identity across every image.]"""

_IDENTITY_RULE_LOCK = "1. ALWAYS start with the exact identity lock text provided in the prompt structure above."
_IDENTITY_RULE_NONE = "1. DO NOT include an [Identity Lock] line unless the user’s description specifically involves a person’s face. For scenes, objects, or abstract subjects, omit the Identity Lock entirely and start directly with the scene description."

# ---------------------------------------------------------------------------
# Cache ngắn hạn cho /gia
# ---------------------------------------------------------------------------
_PRICE_CACHE: dict[str, tuple[float, str]] = {}
_PRICE_CACHE_TTL_SECONDS = 30 * 60  # 30 phút


def _normalize_product_key(product_name: str) -> str:
    return " ".join(product_name.lower().split())


def _get_cached_price(product_name: str) -> str | None:
    key = _normalize_product_key(product_name)
    cached = _PRICE_CACHE.get(key)
    if not cached:
        return None
    saved_at, text = cached
    age = time.time() - saved_at
    if age > _PRICE_CACHE_TTL_SECONDS:
        _PRICE_CACHE.pop(key, None)
        return None
    minutes = max(1, int(age // 60))
    return f"{text}\n\n*(⏱️ Kết quả tra cứu cách đây {minutes} phút, lấy từ cache anh nhé)*"


def _set_cached_price(product_name: str, text: str) -> None:
    _PRICE_CACHE[_normalize_product_key(product_name)] = (time.time(), text)


_MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")
_LINK_CHECK_TIMEOUT = 6.0


async def _check_url(client: httpx.AsyncClient, url: str) -> bool | None:
    """True = link sống. False = CHẮC CHẮN hỏng (404/410/không kết nối
    được). None = không chắc (403 bị chặn bot, timeout, lỗi 5xx tạm thời...)
    - những trường hợp này KHÔNG gắn cảnh báo để tránh báo nhầm link tốt
    thành hỏng chỉ vì trang có chặn request tự động."""
    try:
        resp = await client.head(url, timeout=_LINK_CHECK_TIMEOUT, follow_redirects=True)
        if resp.status_code in (403, 405):
            resp = await client.get(url, timeout=_LINK_CHECK_TIMEOUT, follow_redirects=True)
        if resp.status_code < 400:
            return True
        if resp.status_code in (404, 410):
            return False
        return None
    except httpx.ConnectError:
        return False
    except Exception:
        return None


async def _verify_links(text: str) -> str:
    matches = list(_MD_LINK_RE.finditer(text))
    if not matches:
        return text

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LanAnhBot/1.0)"}
        async with httpx.AsyncClient(headers=headers) as client:
            results = await asyncio.gather(
                *(_check_url(client, m.group(2)) for m in matches),
                return_exceptions=False,
            )
    except Exception:
        logger.exception("Lỗi khi verify link giá sản phẩm, giữ nguyên text gốc")
        return text

    out: list[str] = []
    last_end = 0
    for match, is_alive in zip(matches, results):
        out.append(text[last_end:match.start()])
        label, url = match.group(1), match.group(2)
        if is_alive is False:
            out.append(f"[⚠️ {label} (link có thể đã đổi/hết hàng)]({url})")
        else:
            out.append(match.group(0))
        last_end = match.end()
    out.append(text[last_end:])
    return "".join(out)

HELP_TEXT = (
    "📖 *Các lệnh hỗ trợ:*\n\n"
    "💬 Gõ tin nhắn bình thường để trò chuyện với em - Lan Anh - như trợ lý cá nhân.\n\n"
    "📊 Khi anh nhắc tới 1 *mã cổ phiếu Việt Nam*, mặc định em lấy giá khớp lệnh REALTIME.\n"
    "Cần phân tích sâu thì cứ nói rõ (vd \"phân tích giúp anh mã FPT\").\n\n"
    "🖼️ *Gửi 1 ảnh chân dung* để Gemini viết lại prompt giữ nguyên khuôn mặt.\n\n"
    "/prompt — viết prompt tạo ảnh từ mô tả cơ bản\n"
    "/gia — Tìm và so sánh giá sản phẩm\n"
    "/reset — xoá ngữ cảnh chat\n"
    "/history — xem 10 lượt gần nhất\n"
    "/memory — xem trí nhớ dài hạn\n"
    "/forget — xoá trí nhớ dài hạn\n"
    "/notes — xem ghi chú đã lưu\n"
    "/model — xem/đổi model chat\n"
    "/status — xem trạng thái provider\n"
    "/usecookie — ép thử lại cookie ngay\n"
    "/help — hiển thị hướng dẫn này"
)

TEXT_PROMPT_INSTRUCTION_BASE = """You are an expert prompt engineer for AI image generation tools, specialized in writing "identity-preserving" and HYPER-REALISTIC prompts. The goal is to generate images that look like real, candid, unretouched photographs, avoiding any "AI-generated", plasticky, or overly polished aesthetic.

Based on the user's basic description, write ONE complete, ready-to-use English prompt following EXACTLY this structure and style:

---
{identity_lock}

Raw, candid smartphone photo of the woman standing on a wet pedestrian street at night. She is looking slightly off-camera with a natural, unposed expression. Her hair is drenched from the rain, clinging to her neck and shoulders. 

She is wearing a thin, wet white button-up shirt that clings to her skin, showing realistic wet fabric textures and natural folds. 

The background is a gritty, authentic urban street at night with heavy rain. Blurred streetlights and car headlights create natural out-of-focus bokeh on the wet asphalt. 

Shot on iPhone 15 Pro Max camera, unedited, unretouched. 35mm lens, f/1.8. 

Harsh, imperfect street lighting mixed with camera flash. Natural skin texture, visible pores, slight skin imperfections, specular highlights on wet skin. Subtle chromatic aberration, noticeable low-light noise and film grain. Authentic, raw, documentary photography style, zero airbrushing. --ar 4:5
---

⚠️ The example above demonstrates FORMAT and PHOTOGRAPHY STYLE ONLY. The scene, setting, lighting, and mood MUST be derived entirely from the user's description below, NOT copied from the example.

Rules for what you generate:
{identity_rule}
2. ACCURATELY describe the outfit, pose, and vibe based on the user's description.
3. FORBIDDEN WORDS: NEVER use terms like "masterpiece", "8k", "ultra-photorealistic", "perfect", "flawless", "editorial", or "studio lighting".
4. MANDATORY WORDS: ALWAYS include photography terms that add realism and imperfection, such as "candid", "unretouched", "raw photo", "natural skin texture", "visible pores", "film grain", "amateur lighting", or specific camera models (e.g., "Shot on Kodak Portra 400", "Polaroid", "iPhone snapshot").
5. Output ONLY the final prompt as plain text, no markdown headers, no preamble.

User's basic description: {user_desc}"""

PRICE_SEARCH_SYSTEM = """Bạn là trợ lý Lan Anh. Nhiệm vụ của bạn là sử dụng công cụ Google Search để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam.

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng.
2. BẮT BUỘC phải trích xuất URL (đường link) gốc của trang sản phẩm để người dùng bấm vào xem.
3. Không tự bịa giá. Nếu hệ thống báo hết hàng hoặc không có giá, hãy ghi chú rõ.
4. BẮT BUỘC dùng công cụ Google Search TRƯỚC, rồi mới trả lời - không được trả lời dựa trên trí nhớ/kiến thức đã học sẵn của bạn. Kiến thức nội bộ của bạn có thể đã LỖI THỜI (sản phẩm mới ra mắt sau thời điểm bạn được huấn luyện). Nếu kết quả tìm kiếm cho thấy sản phẩm đã có bán/có giá, PHẢI tin theo kết quả tìm kiếm dù điều đó trái với những gì bạn "nhớ". Chỉ được kết luận "chưa ra mắt" hoặc "chưa có giá" khi kết quả tìm kiếm thực sự không tìm thấy thông tin nào về sản phẩm này.

Trình bày kết quả theo ĐÚNG định dạng list (KHÔNG dùng bảng markdown vì Telegram không hiển thị được bảng) và văn phong sau:

**{product_name}** — giá cập nhật mới nhất

Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:

🏨 **[Tên shop 1]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

🏨 **[Tên shop 2]** — **[Giá]đ**
[Màu sắc/Khuyến mãi ngắn gọn]. [Xem sản phẩm]([Link trực tiếp đến sản phẩm])

(lặp lại 1 khối như trên cho mỗi shop tìm được, tối đa 5 shop)

🔥 **Chỗ rẻ nhất em thấy:**
👉 **[Tên shop rẻ nhất]**: [Giá rẻ nhất]đ cho [Màu/phiên bản].

*(Lưu ý nhỏ: Giá này em tra cứu online ngay lúc này, có thể thay đổi tùy tồn kho từng chi nhánh hoặc flash sale anh nhé).*

QUAN TRỌNG VỀ LINK: mỗi link BẮT BUỘC viết đúng cú pháp markdown [Chữ hiển thị](https://url-that-page), không được dán URL trần, không được để URL trong ngoặc đơn kèm mô tả."""

@common.restricted
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Chào anh, em Lan Anh nè - trợ lý cá nhân của anh đây! 💕\n"
        "Gõ /help xem đầy đủ lệnh nha anh."
    )

@common.restricted
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

@common.restricted
async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.INVALID_COMMAND)

@common.restricted
async def prompt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_desc = common.extract_arg(context)
    if not user_desc:
        await update.message.reply_text("Anh nhập mô tả muốn tạo prompt nhé. Ví dụ: /prompt cô gái đứng trước nhà")
        return

    user_id = update.effective_user.id
    prompt_id = await telemetry.start(user_id, "prompt_generator", user_desc)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Xác định identity lock theo từ khoá:
    # - giữ mặt / giữ khuôn mặt / mặt tôi / mặt anh / mặt em -> REFERENCE
    # - cô gái 20 / gái 20                                          -> GIRL
    # - không có từ khoá                                            -> không inject lock
    desc_lower = user_desc.lower()
    if any(kw in desc_lower for kw in KEEP_FACE_KEYWORDS):
        identity_lock = IDENTITY_LOCK_REFERENCE
        identity_rule = _IDENTITY_RULE_LOCK
    elif any(kw in desc_lower for kw in GIRL_KEYWORDS):
        identity_lock = IDENTITY_LOCK_GIRL
        identity_rule = _IDENTITY_RULE_LOCK
    else:
        identity_lock = "[Identity Lock: None]"
        identity_rule = _IDENTITY_RULE_NONE

    instruction = TEXT_PROMPT_INSTRUCTION_BASE.format(
        identity_lock=identity_lock,
        identity_rule=identity_rule,
        user_desc=user_desc,
    )

    try:
        response = await orchestrator.ask(instruction)
        result_text = (response.text or "").strip()

        if not result_text:
            await telemetry.success(prompt_id, "prompt_generator", "(Gemini không trả về nội dung)")
            await update.message.reply_text("Gemini không trả lời được, anh thử lại nha.")
            return

        await telemetry.success(prompt_id, "prompt_generator", result_text)
        suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""
        await update.message.reply_text("📝 <b>Prompt gợi ý:</b>", parse_mode="HTML")
        await common.reply_long_text(update.message, result_text + suffix)
    except Exception as e:
        logger.exception("Lỗi tạo prompt")
        await telemetry.failure(prompt_id, "prompt_generator", e)
        await update.message.reply_text("❌ Có lỗi khi tạo prompt. Hãy thử lại sau giây lát.")

@common.restricted
async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    product_name = common.extract_arg(context)
    if not product_name:
        await update.message.reply_text("Anh nhập tên sản phẩm muốn tìm giá nhé. Ví dụ: /gia iPhone 16 Pro")
        return

    user_id = update.effective_user.id

    cached_text = _get_cached_price(product_name)
    if cached_text is not None:
        await telemetry.start(user_id, "price_search", product_name)
        await update.message.reply_text(f"⚡ Có kết quả gần đây cho \"{product_name}\", gửi anh liền nè:")
        await common.reply_long_text(update.message, cached_text)
        return

    prompt_id = await telemetry.start(user_id, "price_search", product_name)

    status = await update.message.reply_text(f"🔍 Đang dạo siêu thị tìm giá {product_name} cho anh...")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    instruction = PRICE_SEARCH_SYSTEM.format(product_name=product_name)

    try:
        response = await orchestrator.ask(instruction, enable_search=True, require_real_search=True)
        result_text = (response.text or "").strip()

        if not result_text:
            await telemetry.success(prompt_id, "price_search", "(Gemini không trả về nội dung)")
            await status.edit_text("Em không tìm được giá lúc này, anh thử lại sau nha.")
            return

        await telemetry.success(prompt_id, "price_search", result_text)
        result_text = await _verify_links(result_text)
        _set_cached_price(product_name, result_text)
        suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""

        await common.reply_long_text_edit_first(status, result_text + suffix)
    except Exception as e:
        logger.exception("Lỗi tìm giá sản phẩm")
        await telemetry.failure(prompt_id, "price_search", e)
        await status.edit_text("❌ Có lỗi khi cào dữ liệu giá. Anh thử lại sau nhé.")

@common.restricted
async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    notes = await db.get_notes(user_id, limit=10)
    if not notes:
        await update.message.reply_text("📝 Chưa có ghi chú nào.")
        return
    lines = ["📝 *Ghi chú gần đây:*"]
    for content, created_at in notes:
        lines.append(f"• {content} _(đặt lúc {created_at.strftime('%H:%M %d/%m')})_")
    await common.reply_long_text(update.message, "\n".join(lines))

@common.restricted
async def reset_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await orchestrator.reset_chat()
    await db.clear_chat(user_id)
    await update.message.reply_text("🔄 Đã xoá ngữ cảnh hội thoại. Bắt đầu chat mới nhé!")

@common.restricted
async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    facts = await db.get_facts(user_id)
    summary = await db.get_summary(user_id)

    if not facts and not summary:
        await update.message.reply_text("🧠 Em chưa nhớ gì dài hạn về anh cả.")
        return

    lines = ["🧠 *Trí nhớ dài hạn về anh:*"]
    if summary:
        lines.append(f"\n_Tóm tắt:_ {summary}")
    if facts:
        lines.append("\n*Các thông tin đã biết:*")
        for key, value in facts:
            lines.append(f"• `{key}`: {value}")
    lines.append("\nGõ /forget nếu anh muốn em xoá hết trí nhớ này.")
    await common.reply_long_text(update.message, "\n".join(lines))

@common.restricted
async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await memory_service.clear_memory(user_id)
    await update.message.reply_text("🗑️ Đã xoá toàn bộ trí nhớ dài hạn.")

@common.restricted
async def usecookie_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Đang thử lại cookie Gemini, chờ chút...")
    ok, detail = await orchestrator.try_cookie_now()
    if ok:
        await update.message.reply_text("✅ Cookie hoạt động, đã chuyển về dùng cookie.")
    else:
        await update.message.reply_text(f"❌ Cookie vẫn đang lỗi ({html.escape(detail)}).")

@common.restricted
async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = common.extract_arg(context)
    if not arg:
        current = await cookie_client.get_preferred_model_name()
        try:
            models = await cookie_client.list_models()
        except Exception:
            await update.message.reply_text("❌ Không lấy được danh sách model lúc này.")
            return
        names = sorted({getattr(m, "model_name", "") or str(m) for m in models} - {""})
        lines = [
            f"🧠 Model đang dùng cho chat: <b>{html.escape(current or 'tự động')}</b>",
            "", "Các model khả dụng:"
        ]
        lines += [f"• {html.escape(n)}" for n in names]
        lines.extend(["", "Đổi model: <code>/model tên</code>", "Về mặc định: <code>/model auto</code>"])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if arg.lower() in {"auto", "default", "reset"}:
        await cookie_client.set_preferred_model_name(None)
        await orchestrator.reset_chat()
        await update.message.reply_text("🔄 Đã về chọn model tự động.")
        return

    try:
        model = await cookie_client.find_model(arg)
    except Exception:
        await update.message.reply_text("❌ Không kiểm tra được model lúc này.")
        return

    if model is None:
        await update.message.reply_text(f'Không tìm thấy model khớp "{arg}".')
        return

    model_name = getattr(model, "model_name", arg)
    await cookie_client.set_preferred_model_name(model_name)
    await orchestrator.reset_chat()
    await update.message.reply_text(f"✅ Đã đổi model chat sang: {model_name}")

def _fmt_epoch_vn(ts: float) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m")

async def _noop_ai_status() -> tuple[bool, str]:
    return False, "Chưa cấu hình"


_STATUS_CACHE_TTL_SECONDS = 90
_status_cache: tuple[float, str] | None = None


async def _build_status_text() -> str:
    """Ping thật cả 3 provider rồi dựng text trạng thái (HTML parse_mode)."""
    (cookie_ok, cookie_detail), api1_status, api2_status = await asyncio.gather(
        orchestrator.check_cookie_status(),
        orchestrator.check_ai_studio_status(1) if config.GOOGLE_AI_STUDIO_API_KEY_1 else _noop_ai_status(),
        orchestrator.check_ai_studio_status(2) if config.GOOGLE_AI_STUDIO_API_KEY_2 else _noop_ai_status(),
    )

    state = orchestrator.get_provider_state_snapshot()
    now = time.time()

    active_map = {"cookie": "Cookie", "api1": "API 1", "api2": "API 2"}
    active_line = f"🔀 Provider đang dùng: <b>{html.escape(active_map.get(state['active_provider'], state['active_provider']))}</b>"

    cookie_line = "✅ Cookie Gemini: OK" if cookie_ok else f"❌ Cookie Gemini: lỗi ({html.escape(cookie_detail)})"
    if state["cookie_dead_since"]:
        cookie_line += f"\n   ⥄ chết lúc {_fmt_epoch_vn(state['cookie_dead_since'])}"

    def _api_line(idx, key, status, exhausted_until):
        if not key: return f"⚪ API {idx}: chưa cấu hình"
        ok, detail = status
        line = f"✅ API {idx}: OK" if ok else f"❌ API {idx}: lỗi ({html.escape(detail)})"
        if exhausted_until > now:
            line += f"\n   ⥄ cooldown tới {_fmt_epoch_vn(exhausted_until)}"
        return line

    api1_line = _api_line(1, config.GOOGLE_AI_STUDIO_API_KEY_1, api1_status, state["api1_exhausted_until"])
    api2_line = _api_line(2, config.GOOGLE_AI_STUDIO_API_KEY_2, api2_status, state["api2_exhausted_until"])

    preferred = await cookie_client.get_preferred_model_name()
    model_line = f"🧠 Model chat: {html.escape(preferred or 'tự động')} (API: {html.escape(config.GOOGLE_AI_STUDIO_MODEL)})"
    order_line = "🔢 PROVIDER_ORDER: " + " → ".join(config.PROVIDER_ORDER)

    lines = ["📡 <b>Trạng thái bot</b>", "", active_line, order_line, "", cookie_line, api1_line, api2_line, "", model_line]
    return "\n".join(lines)


@common.restricted
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _status_cache

    now = time.time()
    if _status_cache is not None and now - _status_cache[0] <= _STATUS_CACHE_TTL_SECONDS:
        checked_at, cached_text = _status_cache
        await update.message.reply_text(
            f"{cached_text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)} "
            f"({int(now - checked_at)}s trước) - gõ lại sau {_STATUS_CACHE_TTL_SECONDS}s để ping mới.</i>",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text("🔎 Đang kiểm tra provider-chain...")
    text = await _build_status_text()
    checked_at = time.time()
    _status_cache = (checked_at, text)
    await update.message.reply_text(
        f"{text}\n\n<i>⏱️ Kiểm tra lúc {_fmt_epoch_vn(checked_at)}</i>",
        parse_mode="HTML",
    )

@common.restricted
async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = await db.get_history(user_id, limit=HISTORY_LIMIT)
    if not rows:
        return await update.message.reply_text("Chưa có lịch sử nào.")
    icon_map = {"image": "🖼️", "chat": "💬", "promptify": "🔍", "stock_analysis": "📊", "stock_price": "💹", "prompt_generator": "✏️", "price_search": "🛒"}
    lines = [f"🕙 <b>{HISTORY_LIMIT} lượt gần nhất:</b>\n"]
    for command_type, prompt, created_at, _result_types in rows:
        short_prompt = prompt[:HISTORY_PROMPT_PREVIEW_MAX] + "…" if len(prompt) > HISTORY_PROMPT_PREVIEW_MAX else prompt
        icon = icon_map.get(command_type, "•")
        date_part = created_at[:16].replace("T", " ")
        lines.append(f"{icon} [{html.escape(command_type)}] {html.escape(short_prompt)} ({date_part})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Lỗi không được xử lý", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Đã có lỗi không mong muốn xảy ra. Vui lòng thử lại.")
        except Exception:
            logger.exception("Không gửi được thông báo lỗi cho user")
