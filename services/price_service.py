"""Pipeline giá cho /gia (Giai đoạn 2+3 của kế hoạch nâng cấp) - tách khỏi
handlers/commands.py để price_cmd chỉ còn là lớp vỏ Telegram, giống cách
stock_handler tách khỏi stock_analysis.

Kiến trúc:
1. Ưu tiên gọi THẲNG api1 -> api2 (official_client.generate_search_json,
   Google Search BẬT + JSON có cấu trúc) - KHÔNG dùng thứ tự PROVIDER_ORDER
   mặc định của chat (cookie trước). Lý do: ai.orchestrator.ask() chỉ truyền
   enable_search cho nhánh API, nhánh cookie bỏ qua tham số này hoàn toàn -
   nếu để cookie chạy trước (mặc định của chat), phần lớn lượt /gia sẽ KHÔNG
   có JSON/grounding metadata chuẩn, khiến toàn bộ pipeline validate-được ở
   dưới gần như không được dùng tới. Ở đây cookie CHỈ là phao cứu sinh cuối
   khi cả 2 API key đều thiếu/cooldown quota.
2. Nếu có JSON hợp lệ: code Python tự validate/dedupe/tính giá rẻ nhất/format
   số VN - không tin lời văn model tự so sánh.
3. Cache theo query_norm (core.database.price_cache), TTL mặc định 25 phút,
   hỗ trợ "/gia <tên> moi" để ép làm mới.
4. Nếu API JSON pipeline thất bại hoàn toàn (thiếu key, lỗi, JSON không hợp
   lệ) -> fallback về orchestrator.ask() với prompt text-based như hành vi
   TRƯỚC khi nâng cấp (khi đó có thể chạy qua cookie), không cache kết quả
   này vì không có gì đảm bảo chất lượt/link.
"""
import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from ai import official_client
from ai.provider_state import provider_state
from core import database as db

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

CACHE_TTL_SECONDS = 25 * 60  # 20-30 phút theo kế hoạch - giá bán lẻ VN không đổi nhanh hơn thế.
_MIN_VALID_PRICE = 10_000
_MAX_VALID_PRICE = 1_000_000_000
_FORCE_REFRESH_TOKENS = {"moi", "mới"}

# Model hay ĐOÁN slug URL sản phẩm theo pattern quen thuộc (vd
# "/dtdd/samsung-galaxy-a57-5g-8gb-256gb") thay vì copy chính xác link thật
# từ kết quả search - đặc biệt sai nhiều với TGDD/CellphoneS vì slug đặt tên
# phức tạp. Đã tự tay xác minh URL TÌM KIẾM (site search) thật của các
# retailer lớn nhất - LUÔN load được, khác với link sản phẩm cụ thể do model
# tự bịa hay 404. Match theo domain nhận diện được từ TÊN SHOP model trả về
# (không phải từ url model trả về, vì url mới là thứ hay sai).
_SHOP_NAME_DOMAIN_HINTS: dict[str, str] = {
    "thegioididong": "thegioididong.com",
    "dienmayxanh": "dienmayxanh.com",
    "cellphones": "cellphones.com.vn",
}
_VERIFIED_SEARCH_URL_TEMPLATES: dict[str, str] = {
    "thegioididong.com": "https://www.thegioididong.com/tim-kiem?key={q}",
    "dienmayxanh.com": "https://www.dienmayxanh.com/search?key={q}",
    "cellphones.com.vn": "https://cellphones.com.vn/catalogsearch/result?q={q}",
}


def _strip_diacritics(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _guess_domain_for_shop(shop_name: str) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]+", "", _strip_diacritics(shop_name).lower())
    for hint, domain in _SHOP_NAME_DOMAIN_HINTS.items():
        if hint in key:
            return domain
    return None


def _resolve_item_link(shop_name: str, product_name: str) -> tuple[str, bool]:
    """Trả (url, is_search_link). Với TGDD/Điện Máy Xanh/CellphoneS - LUÔN
    ép dùng link tìm kiếm thật đã xác minh (bỏ qua hoàn toàn url model tự
    đoán, vì đoán đúng domain không có nghĩa đoán đúng đường dẫn sản phẩm).
    Các shop khác chưa xác minh được URL tìm kiếm chuẩn -> trả rỗng, để
    caller giữ nguyên url model cung cấp (best-effort, không đảm bảo)."""
    domain = _guess_domain_for_shop(shop_name)
    template = _VERIFIED_SEARCH_URL_TEMPLATES.get(domain or "")
    if not template:
        return "", False
    return template.format(q=quote_plus(product_name)), True

PRICE_JSON_SYSTEM = """Bạn là hệ thống tra cứu giá, KHÔNG phải đang trò chuyện với người dùng. Dùng Google Search để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam (vd Thế Giới Di Động, FPT Shop, CellphoneS, Điện Máy Xanh, Nguyễn Kim...).

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng nếu người dùng nêu rõ trong tên sản phẩm; nếu KHÔNG nêu, mặc định lấy bản thấp nhất/rẻ nhất và ghi chú rõ điều này trong "notes".
2. Không tự bịa giá hay link - chỉ liệt kê nơi bán bạn thực sự tìm thấy giá qua tra cứu.
3. Nếu không tìm được giá nào đáng tin, trả "results": [] và giải thích ngắn gọn trong "notes".

CHỈ trả về DUY NHẤT 1 object JSON hợp lệ (không markdown, không code fence, không thêm chữ nào khác trước/sau), đúng khuôn mẫu:
{{"product": "tên sản phẩm", "results": [{{"shop": "tên shop", "price_vnd": 18990000, "variant": "256GB Titan", "promo": "khuyến mãi ngắn nếu có, hoặc chuỗi rỗng", "url": "link sản phẩm nếu có, hoặc chuỗi rỗng"}}], "notes": "ghi chú ngắn, ví dụ đã mặc định phiên bản nào, sản phẩm hết hàng ở đâu..."}}"""

# Fallback text-based (Giai đoạn 1 - list thay vì bảng Markdown) - dùng khi
# pipeline JSON ở trên không chạy được (thiếu API key/lỗi), qua provider-chain
# thường (có thể rơi vào cookie) như hành vi trước khi nâng cấp.
TEXT_FALLBACK_SYSTEM = """Bạn là trợ lý Lan Anh. Nhiệm vụ của bạn là sử dụng công cụ Google Search để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam.

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng nếu người dùng nêu rõ; nếu KHÔNG nêu, mặc định lấy bản thấp nhất và ghi chú rõ điều này trong kết quả.
2. BẮT BUỘC phải trích xuất URL (đường link) gốc của trang sản phẩm để người dùng bấm vào xem.
3. Không tự bịa giá. Nếu hệ thống báo hết hàng hoặc không có giá, hãy ghi chú rõ.
4. TUYỆT ĐỐI KHÔNG dùng bảng Markdown (Telegram không render được bảng) - trình bày mỗi nơi bán 1 dòng theo đúng định dạng sau:

**{product_name}** — giá cập nhật mới nhất

Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:

🏪 [Tên shop] — **[Giá]đ**
   [Màu/khuyến mãi ngắn] · [Link trực tiếp đến sản phẩm]

(lặp lại 1 dòng như trên cho mỗi nơi bán tìm được)

🔥 **Chỗ rẻ nhất em thấy:**
👉 **[Tên shop rẻ nhất]**: [Giá rẻ nhất]đ cho [Màu/phiên bản].

*(Lưu ý nhỏ: Giá này em tra cứu online ngay lúc này, có thể thay đổi tùy tồn kho từng chi nhánh hoặc flash sale anh nhé).*"""


class PriceServiceError(Exception):
    """Raise khi cả pipeline JSON lẫn fallback text-based đều thất bại -
    price_cmd/tools bắt lỗi này để hiển thị thông báo lỗi cho user."""


def _parse_force_refresh(raw_product_name: str) -> tuple[str, bool]:
    """'iPhone 16 Pro moi' -> ('iPhone 16 Pro', True). Không phân biệt hoa
    thường, chỉ nhận từ khoá ở CUỐI câu để tránh cắt nhầm tên sản phẩm có
    chứa chữ 'moi'/'mới' ở giữa (hiếm nhưng vẫn nên tránh)."""
    tokens = (raw_product_name or "").strip().split()
    if tokens and tokens[-1].lower() in _FORCE_REFRESH_TOKENS:
        return " ".join(tokens[:-1]).strip(), True
    return (raw_product_name or "").strip(), False


def _normalize_query(product_name: str) -> str:
    text = (product_name or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def _coerce_price(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _build_payload(product_name: str, data: dict, grounding_sources: list[tuple[str, str]]) -> dict:
    """Code TỰ validate/dedupe/lọc giá thay vì tin lời model - đúng nguyên
    tắc của Giai đoạn 2."""
    product = str(data.get("product") or product_name).strip() or product_name
    raw_items = data.get("results")
    items: list[dict] = []
    seen_shops: set[str] = set()

    if isinstance(raw_items, list):
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            shop = str(entry.get("shop") or "").strip()
            if not shop:
                continue
            shop_key = shop.lower()
            if shop_key in seen_shops:
                continue  # dedupe theo shop
            price = _coerce_price(entry.get("price_vnd"))
            if price is None or not (_MIN_VALID_PRICE <= price <= _MAX_VALID_PRICE):
                continue  # loại record thiếu giá / giá phi lý
            seen_shops.add(shop_key)

            # Ưu tiên link tìm kiếm thật đã xác minh (TGDD/DMX/CellphoneS) -
            # bỏ qua url model tự đoán vì slug sản phẩm rất hay sai dù domain
            # đúng. Với shop khác (chưa xác minh URL search chuẩn), giữ
            # nguyên url model đưa ra (best-effort, không đảm bảo).
            verified_url, is_search_link = _resolve_item_link(shop, product)
            url = verified_url or str(entry.get("url") or "").strip()

            items.append(
                {
                    "shop": shop,
                    "price_vnd": price,
                    "variant": str(entry.get("variant") or "").strip(),
                    "promo": str(entry.get("promo") or "").strip(),
                    "url": url,
                    "is_search_link": is_search_link,
                }
            )

    return {
        "product": product,
        "items": items,
        "notes": str(data.get("notes") or "").strip(),
        "grounding_sources": [list(s) for s in grounding_sources],
    }


def _render_from_payload(payload: dict, cached_at: Optional[datetime] = None) -> str:
    product = payload.get("product") or "sản phẩm"
    items = payload.get("items") or []
    notes = payload.get("notes") or ""
    grounding_sources = payload.get("grounding_sources") or []

    if not items:
        lines = [f"**{product}** — em không tìm được giá nào đáng tin cậy lúc này."]
        if notes:
            lines.append(f"*{notes}*")
        else:
            lines.append("*Anh thử lại sau hoặc nêu rõ tên/phiên bản sản phẩm giúp em nha.*")
        return "\n".join(lines)

    # Code tự tính min() - chính xác 100%, không để model tự chọn "rẻ nhất".
    cheapest = min(items, key=lambda it: it["price_vnd"])

    lines = [
        f"**{product}** — giá cập nhật mới nhất",
        "",
        "Dạ em lượn một vòng các đại lý lớn để khảo giá cho anh rồi đây nha:",
        "",
    ]
    for it in items:
        lines.append(f"🏪 {it['shop']} — **{_format_vnd(it['price_vnd'])}**")
        detail_bits = [x for x in (it.get("variant"), it.get("promo")) if x]
        detail = " · ".join(detail_bits)
        tail = []
        if detail:
            tail.append(detail)
        if it.get("url"):
            # Link tìm kiếm đã xác minh (TGDD/DMX/CellphoneS) LUÔN vào được,
            # ghi rõ để user hiểu đây là trang kết quả tìm kiếm chứ không
            # phải thẳng trang sản phẩm; link khác là model tự đoán, có thể
            # thi thoảng sai/404 - không ghi gì thêm để giữ gọn.
            label = "🔎 Tìm trên trang" if it.get("is_search_link") else "Link"
            tail.append(f"[{label}]({it['url']})")
        if tail:
            lines.append("   " + " · ".join(tail))

    lines.append("")
    lines.append(
        f"🔥 **Chỗ rẻ nhất em thấy:**\n"
        f"👉 **{cheapest['shop']}**: {_format_vnd(cheapest['price_vnd'])} cho "
        f"{cheapest.get('variant') or product}."
    )

    if notes:
        lines.append("")
        lines.append(f"*Lưu ý: {notes}*")

    if grounding_sources:
        lines.append("")
        lines.append("🔎 *Nguồn tham khảo (Google Search):*")
        shown = 0
        for src in grounding_sources:
            if not isinstance(src, (list, tuple)) or len(src) != 2:
                continue
            title, uri = src
            if not uri:
                continue
            lines.append(f"• [{title or uri}]({uri})")
            shown += 1
            if shown >= 5:
                break

    if cached_at is not None:
        vn_time = cached_at.astimezone(_VN_TZ).strftime("%H:%M")
        refresh_minutes = CACHE_TTL_SECONDS // 60
        lines.append("")
        lines.append(
            f"♻️ _Giá em tra lúc {vn_time}, gõ “/gia {product} moi” nếu anh muốn em tra lại "
            f"ngay (không thì tự làm mới sau tối đa {refresh_minutes} phút)._"
        )

    return "\n".join(lines)


async def _fetch_via_official_api(product_name: str) -> Optional[dict]:
    """Thử api1 -> api2 (bỏ qua nếu thiếu key hoặc đang cooldown quota, tôn
    trọng provider_state dùng chung toàn bot). Trả payload đã validate nếu
    thành công, None nếu cả 2 đều không dùng được (caller tự fallback text)."""
    await provider_state.ensure_loaded()
    prompt = PRICE_JSON_SYSTEM.format(product_name=product_name)

    for idx in (1, 2):
        if not official_client.api_key_for(idx):
            continue
        if provider_state.api_in_cooldown(idx):
            continue
        try:
            result = await official_client.generate_search_json(idx, prompt)
        except Exception as exc:
            if official_client.is_quota_exhausted_error(exc):
                await provider_state.mark_api_exhausted(idx)
                logger.info("price_service: api%s hết quota, thử key kế tiếp nếu có.", idx)
                continue
            logger.warning("price_service: api%s lỗi (không phải hết quota).", idx, exc_info=True)
            continue

        if not result.data or not isinstance(result.data.get("results"), list):
            logger.warning(
                "price_service: api%s trả JSON không hợp lệ/lệch định dạng, thử key kế tiếp.", idx
            )
            continue

        return _build_payload(product_name, result.data, result.grounding_sources)

    return None


async def _fetch_text_fallback(product_name: str) -> str:
    """Đường cứu cánh cuối - y hệt hành vi /gia TRƯỚC khi nâng cấp (qua
    provider-chain thường, có thể rơi vào cookie), không có JSON/validate."""
    from ai import orchestrator  # import trễ, tránh vòng import với ai.official_client

    instruction = TEXT_FALLBACK_SYSTEM.format(product_name=product_name)
    response = await orchestrator.ask(instruction, enable_search=True)
    text = (response.text or "").strip()
    if not text:
        raise PriceServiceError("Gemini không trả về nội dung ở fallback text-based.")
    suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""
    return text + suffix


async def fetch_price_message(raw_product_name: str) -> str:
    """Entry point chính cho handlers/commands.py::price_cmd và
    services/tools.py::_tool_search_price. Trả về text ĐÃ SẴN SÀNG gửi qua
    tg_format (markdown-lite, list không phải bảng). Raise PriceServiceError
    nếu không lấy được giá bằng bất kỳ cách nào."""
    product_name, force_refresh = _parse_force_refresh(raw_product_name)
    if not product_name:
        raise PriceServiceError("Thiếu tên sản phẩm.")

    query_norm = _normalize_query(product_name)

    if not force_refresh:
        try:
            cached = await db.get_price_cache(query_norm, CACHE_TTL_SECONDS)
        except Exception:
            logger.warning("price_service: lỗi đọc cache, bỏ qua và fetch mới.", exc_info=True)
            cached = None
        if cached is not None:
            payload_json, created_at = cached
            try:
                payload = json.loads(payload_json)
                return _render_from_payload(payload, cached_at=created_at)
            except Exception:
                logger.warning("price_service: cache lỗi định dạng, fetch mới.", exc_info=True)

    payload = await _fetch_via_official_api(product_name)
    if payload is not None:
        try:
            await db.set_price_cache(query_norm, json.dumps(payload, ensure_ascii=False))
        except Exception:
            # Cache là tối ưu hoá, không phải yêu cầu bắt buộc - lỗi ghi cache
            # không được làm hỏng việc trả kết quả cho user.
            logger.warning("price_service: lỗi ghi cache, vẫn trả kết quả bình thường.", exc_info=True)
        return _render_from_payload(payload)

    # Cả api1 lẫn api2 đều không dùng được (thiếu key/cooldown/lỗi/JSON hỏng)
    # -> fallback cuối, không cache vì không có gì đảm bảo chất lượng.
    return await _fetch_text_fallback(product_name)
