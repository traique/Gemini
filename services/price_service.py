"""Pipeline giá cho /gia và cho câu hỏi giá tự nhiên (product price finder).

Kiến trúc giữ nguyên phần đã chứng minh hoạt động tốt từ trước: gọi thẳng
api1 -> api2 (official_client.generate_search_json, Google Search BẬT) thay
vì đi qua provider-chain mặc định (cookie trước) - orchestrator.ask() chỉ
truyền enable_search cho nhánh API, nhánh cookie bỏ qua hoàn toàn tham số
này. Cache theo query_norm (core.database.price_cache), TTL mặc định 25
phút, hỗ trợ "/gia <tên> moi" để ép làm mới. Nếu pipeline JSON thất bại hoàn
toàn (thiếu key/lỗi/JSON hỏng) -> fallback text-based qua provider-chain
thường, không cache (không đảm bảo chất lượng).

Sau khi rank xong, các link ứng viên top đầu được XÁC THỰC THẬT bằng HTTP
HEAD/GET (module httpx, timeout ngắn) trước khi đưa vào tin nhắn trả về -
suy đoán "trông giống link hợp lệ" từ hình dạng URL không đáng tin (Gemini
vẫn có thể trả link 404/trang danh mục dù có vẻ là link sản phẩm cụ thể).
Link không phản hồi bị loại; với vài retailer lớn đã xác minh sẵn URL tìm
kiếm thật thì thay vào đó thay vì bỏ hẳn (vẫn ghi rõ đó là link tìm kiếm).
"""
import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import httpx

from ai import official_client
from ai.provider_state import provider_state
from core import database as db
from stock_sector import ALL_KNOWN_SYMBOLS

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

CACHE_TTL_SECONDS = 25 * 60
_MIN_VALID_PRICE = 10_000
_MAX_VALID_PRICE = 1_000_000_000
# Số không kèm đơn vị (không "đ"/"triệu"/...) chỉ được nhận trong khoảng hẹp
# hơn hẳn, để không ăn nhầm "16" (iPhone 16), "2025", "256" (GB)...
_BARE_MIN_VALID_PRICE = 100_000
_BARE_MAX_VALID_PRICE = 500_000_000
_MIN_CONFIDENCE = 55.0
_DISPLAY_LIMIT = 5  # tổng số cửa hàng hiển thị trong tin nhắn (1 nổi bật + phần còn lại)

_LINK_CHECK_TIMEOUT = httpx.Timeout(4.0, connect=3.0)
_LINK_CHECK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
_LINK_CHECK_CANDIDATE_BUFFER = 3

SOUTHERN_PRIORITY_RETAILERS = [
    "dienmaycholon.com",
    "hoanghamobile.com",
    "cellphones.com.vn",
    "didongviet.vn",
    "thegioididong.com",
    "dienmayxanh.com",
    "nguyenkim.com",
    "fptshop.com.vn",
    "viettelstore.vn",
    "gearvn.com",
    "phongvu.vn",
    "memoryzone.com.vn",
    "tinhocngoisao.com",
    "mediamart.vn",
    "tiki.vn",
    "lazada.vn",
    "shopee.vn",
]

RETAILER_WEIGHTS = {
    "dienmaycholon.com": 25,
    "hoanghamobile.com": 24,
    "cellphones.com.vn": 23,
    "didongviet.vn": 23,
    "thegioididong.com": 22,
    "dienmayxanh.com": 22,
    "nguyenkim.com": 21,
    "fptshop.com.vn": 20,
    "viettelstore.vn": 18,
    "gearvn.com": 18,
    "phongvu.vn": 18,
    "memoryzone.com.vn": 16,
    "tinhocngoisao.com": 16,
    "mediamart.vn": 14,
    "tiki.vn": 8,
    "lazada.vn": 6,
    "shopee.vn": 5,
}

PRODUCT_CATEGORY_KEYWORDS = {
    "phone": [
        "iphone", "samsung galaxy", "oppo", "xiaomi", "vivo", "realme",
        "điện thoại", "dien thoai", "smartphone",
    ],
    "laptop": [
        "laptop", "thinkpad", "macbook", "asus", "lenovo", "dell", "hp",
        "acer", "msi", "surface",
    ],
    "appliance": [
        "máy lạnh", "may lanh", "điều hòa", "dieu hoa", "tủ lạnh", "tu lanh",
        "máy giặt", "may giat", "tivi", "tv", "nồi cơm", "noi com",
        "lò vi sóng", "lo vi song", "máy lọc nước", "may loc nuoc",
    ],
    "audio": [
        "tai nghe", "loa", "sony wh", "airpods", "marshall", "jbl",
    ],
    "pc_parts": [
        "cpu", "gpu", "card màn hình", "card man hinh", "ram", "ssd",
        "mainboard", "nguồn máy tính", "psu",
    ],
}

CATEGORY_RETAILERS = {
    "phone": [
        "hoanghamobile.com", "cellphones.com.vn", "didongviet.vn",
        "thegioididong.com", "fptshop.com.vn", "viettelstore.vn",
        "dienmaycholon.com",
    ],
    "laptop": [
        "phongvu.vn", "gearvn.com", "fptshop.com.vn", "thegioididong.com",
        "cellphones.com.vn", "hacom.vn", "anphatpc.com.vn",
    ],
    "appliance": [
        "dienmaycholon.com", "dienmayxanh.com", "nguyenkim.com",
        "mediamart.vn", "thegioididong.com",
    ],
    "audio": [
        "cellphones.com.vn", "hoanghamobile.com", "thegioididong.com",
        "fptshop.com.vn", "nguyenkim.com",
    ],
    "pc_parts": [
        "gearvn.com", "phongvu.vn", "memoryzone.com.vn", "tinhocngoisao.com",
        "hacom.vn", "anphatpc.com.vn",
    ],
}

NOISE_TERMS = [
    "ốp lưng", "op lung", "cường lực", "cuong luc", "phụ kiện", "phu kien",
    "case", "cover", "review", "đánh giá", "danh gia", "trên tay", "tren tay",
    "so sánh", "so sanh", "tin tức", "tin tuc", "trả góp 0%", "trả góp",
    "tra gop", "đặt cọc", "dat coc", "voucher", "mã giảm giá", "coupon",
]

PRODUCT_PRICE_INTENT_KEYWORDS = [
    "giá", "gia", "bao nhiêu", "bao nhieu", "nhiêu tiền", "nhieu tien",
    "hỏi giá", "hoi gia", "xem giá", "xem gia", "tìm giá", "tim gia",
    "check giá", "check gia", "mua", "bán giá", "ban gia",
]

PRODUCT_HINT_KEYWORDS = [
    "iphone", "samsung", "galaxy", "oppo", "xiaomi", "vivo", "realme",
    "điện thoại", "dien thoai", "laptop", "macbook", "thinkpad",
    "máy lạnh", "may lanh", "điều hòa", "dieu hoa", "tủ lạnh", "tu lanh",
    "máy giặt", "may giat", "tivi", "tv", "tai nghe", "loa", "airpods",
    "ssd", "ram", "cpu", "gpu", "card màn hình", "ps5", "nintendo",
]

REMOVE_PRICE_PHRASES = [
    "giá", "gia", "bao nhiêu", "bao nhieu", "nhiêu tiền", "nhieu tien",
    "hỏi giá", "hoi gia", "xem giá", "xem gia", "tìm giá", "tim gia",
    "check giá", "check gia", "giúp tôi", "giup toi", "giúp anh", "giup anh",
]

_FORCE_REFRESH_TRAILING_WORDS = {"moi", "refresh"}
_FORCE_REFRESH_PHRASES = ["khong cache", "no cache", "bo cache"]

_VARIANT_TIER_TOKENS = ("pro max", "plus", "ultra", "pro", "mini")
_STORAGE_RE = re.compile(r"\b\d{2,4}\s?(?:gb|tb)\b")
_HP_RE = re.compile(r"\b\d(?:\.\d)?\s?hp\b")

_KNOWN_BRANDS = [
    "iphone", "samsung", "oppo", "xiaomi", "vivo", "realme", "apple",
    "panasonic", "lg", "sony", "asus", "lenovo", "dell", "hp", "acer",
    "msi", "jbl", "marshall", "thinkpad", "macbook",
]

_MUST_TERM_STOPWORDS = {"cho", "cua", "voi", "toi", "anh", "em", "giup", "la"}
_MARKETPLACE_DOMAINS = {"tiki.vn", "lazada.vn", "shopee.vn"}
_REVIEW_TERMS = ("review", "danh gia", "tren tay", "so sanh", "tin tuc")
_ACCESSORY_TERMS = ("op lung", "cuong luc", "phu kien", "case", "cover")
_INSTALLMENT_TERMS = ("tra gop", "dat coc", "voucher", "ma giam gia", "coupon")

# Chỉ dùng khi Gemini KHÔNG trả về link nào cho 1 kết quả (rỗng) - lúc đó
# thay bằng link tìm kiếm thật đã tự tay xác minh của vài retailer lớn, để
# vẫn có gì bấm vào được thay vì bỏ trống. Ưu tiên tuyệt đối là link trực
# tiếp Gemini tìm được qua Google Search grounding.
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


class PriceServiceError(Exception):
    """Raise khi cả pipeline JSON lẫn fallback text-based đều thất bại -
    price_cmd/tools/chat_router bắt lỗi này để hiển thị thông báo cho user."""


@dataclass
class PriceQuery:
    raw: str
    clean: str
    force_refresh: bool = False
    category: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    variant_terms: list[str] = field(default_factory=list)
    must_terms: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)


@dataclass
class SearchPlan:
    primary_queries: list[str]
    retailer_queries: list[str]
    broad_queries: list[str]
    negative_terms: list[str]


@dataclass
class PriceCandidate:
    title: str
    url: str
    source: str
    snippet: str = ""
    price: Optional[int] = None
    prices: list[int] = field(default_factory=list)
    variant: Optional[str] = None
    is_priority_retailer: bool = False
    is_search_link: bool = False
    is_noise: bool = False
    noise_reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0


# ─── Helper chung ───────────────────────────────────────────────────────

def _strip_diacritics(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def _format_trieu(amount: int) -> str:
    trieu = amount / 1_000_000
    if trieu < 1:
        return _format_vnd(amount)
    text = f"{trieu:.1f}".rstrip("0").rstrip(".")
    return f"{text} triệu"


def _coerce_price(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None
    return None


def _extract_domain(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _guess_domain_for_shop(shop_name: str) -> Optional[str]:
    key = re.sub(r"[^a-z0-9]+", "", _strip_diacritics(shop_name).lower())
    for hint, domain in _SHOP_NAME_DOMAIN_HINTS.items():
        if hint in key:
            return domain
    return None


def _resolve_item_link(shop_name: str, product_name: str, fallback_url: str) -> tuple[str, bool]:
    """Ưu tiên tuyệt đối link trực tiếp mà Gemini tìm được qua Google Search
    grounding (đây là link thật từ kết quả search, không phải model tự bịa
    slug) - chỉ khi KHÔNG có link nào (rỗng) mới thay bằng link tìm kiếm đã
    xác minh cho vài retailer lớn (is_search_link = True), để vẫn có gì đó
    bấm vào được thay vì bỏ trống hoàn toàn."""
    if fallback_url:
        return fallback_url, False

    domain = _guess_domain_for_shop(shop_name)
    template = _VERIFIED_SEARCH_URL_TEMPLATES.get(domain or "")
    if not template:
        return fallback_url, False

    return template.format(q=quote_plus(product_name)), True


# ─── normalize_price_query ──────────────────────────────────────────────

def _detect_force_refresh(text: str) -> tuple[str, bool]:
    lowered = _strip_diacritics(text.lower())
    for phrase in _FORCE_REFRESH_PHRASES:
        idx = lowered.find(phrase)
        if idx != -1:
            return (text[:idx] + text[idx + len(phrase):]).strip(), True

    tokens = text.split()
    if tokens and _strip_diacritics(tokens[-1].lower()) in _FORCE_REFRESH_TRAILING_WORDS:
        return " ".join(tokens[:-1]).strip(), True

    return text.strip(), False


def _contains_keyword(norm_text: str, keyword: str) -> bool:
    """So khớp keyword trong text đã strip dấu + lowercase. Với keyword
    thuần chữ/số (vd "hp", "tv") bắt buộc có ranh giới từ, tránh khớp nhầm
    bên trong số khác - vd "hp" (brand HP) không được khớp vào "1.5hp"
    (mã lực máy lạnh)."""
    kw = _strip_diacritics(keyword.lower())
    if re.fullmatch(r"[a-z0-9]+", kw):
        return re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", norm_text) is not None
    return kw in norm_text


def _detect_category(clean: str) -> Optional[str]:
    norm = _strip_diacritics(clean.lower())
    for category, keywords in PRODUCT_CATEGORY_KEYWORDS.items():
        if any(_contains_keyword(norm, kw) for kw in keywords):
            return category
    return None


def _extract_brand(clean: str) -> Optional[str]:
    norm = _strip_diacritics(clean.lower())
    for brand in _KNOWN_BRANDS:
        if _contains_keyword(norm, brand):
            return brand
    return None


def _detect_variant_tokens(text: str) -> set[str]:
    norm = _strip_diacritics(text.lower())
    found: set[str] = set()
    remaining = norm
    for token in _VARIANT_TIER_TOKENS:
        if token in remaining:
            found.add(token)
            remaining = remaining.replace(token, " ")
    found |= set(_STORAGE_RE.findall(norm))
    found |= set(_HP_RE.findall(norm))
    return found


def _extract_must_terms(clean: str) -> list[str]:
    tokens = _strip_diacritics(clean.lower()).split()
    return [t for t in tokens if len(t) >= 2 and t not in _MUST_TERM_STOPWORDS]


def normalize_price_query(raw: str) -> PriceQuery:
    text, force_refresh = _detect_force_refresh((raw or "").strip())
    clean = re.sub(r"\s+", " ", text).strip()
    return PriceQuery(
        raw=raw or "",
        clean=clean,
        force_refresh=force_refresh,
        category=_detect_category(clean),
        brand=_extract_brand(clean),
        variant_terms=sorted(_detect_variant_tokens(clean)),
        must_terms=_extract_must_terms(clean),
    )


# ─── build_search_plan ──────────────────────────────────────────────────

def build_search_plan(query: PriceQuery) -> SearchPlan:
    q = query.clean
    primary = [f"{q} giá bán", f"{q} chính hãng giá", f"{q} giá rẻ", f"{q} khuyến mãi"]
    retailer_domains = CATEGORY_RETAILERS.get(query.category, SOUTHERN_PRIORITY_RETAILERS[:7])
    retailer_queries = [f"site:{domain} {q}" for domain in retailer_domains]
    broad = [f"mua {q} giá tốt", f"{q} nơi bán rẻ nhất"]
    return SearchPlan(primary, retailer_queries, broad, negative_terms=list(NOISE_TERMS))


def _build_search_prompt(query: PriceQuery, plan: SearchPlan) -> str:
    retailer_lines = "\n".join(f"- {d}" for d in SOUTHERN_PRIORITY_RETAILERS)
    all_queries = plan.primary_queries + plan.retailer_queries + plan.broad_queries
    query_lines = "\n".join(f"- {q}" for q in all_queries)

    if query.variant_terms:
        variant_note = (
            f"Người dùng có nêu rõ phiên bản/biến thể: {', '.join(query.variant_terms)}. "
            "CHỈ lấy kết quả khớp đúng biến thể này."
        )
    else:
        variant_note = (
            "Người dùng KHÔNG nêu rõ phiên bản/dung lượng - lấy bản thấp nhất/phổ biến nhất "
            "và ghi rõ điều này trong \"notes\"."
        )

    return f"""Bạn là hệ thống tra cứu giá bán lẻ Việt Nam cho sản phẩm: "{query.clean}".

Dùng Google Search, tìm lần lượt các truy vấn sau (không chỉ 1 truy vấn):
{query_lines}

Hệ thống bán lẻ ưu tiên (đáng tin, phổ biến ở miền Nam) - ưu tiên nhưng KHÔNG giới hạn chỉ trong danh sách này, vẫn lấy nguồn khác nếu giá tốt và đáng tin:
{retailer_lines}

{variant_note}

YÊU CẦU BẮT BUỘC:
- Liệt kê CÀNG NHIỀU nơi bán khác nhau càng tốt (tối đa 10), không chỉ 1-2 kết quả, để có đủ lựa chọn so sánh giá.
- CHỈ lấy kết quả có giá bán rõ ràng của chính sản phẩm. KHÔNG lấy giá phụ kiện (ốp lưng, cường lực...), KHÔNG lấy bài review/tin tức không có giá bán, KHÔNG lấy số tiền trả góp/tháng hay tiền đặt cọc làm giá bán sản phẩm.
- BẮT BUỘC lấy đúng URL trang sản phẩm thật từ kết quả search (không tự bịa link, không rút gọn về trang chủ).
- Không tự bịa giá hay link - chỉ liệt kê nơi bán thực sự tìm thấy qua tra cứu.
- Nếu không tìm được giá nào đáng tin, trả "results": [].

CHỈ trả về DUY NHẤT 1 object JSON hợp lệ (không markdown, không code fence, không thêm chữ nào khác), đúng khuôn mẫu:
{{"product": "tên sản phẩm", "results": [{{"shop": "tên shop/nguồn bán", "url": "link sản phẩm nếu có", "price_vnd": 18990000, "variant": "biến thể/dung lượng nếu biết", "snippet": "trích đoạn ngắn chứa giá làm bằng chứng"}}], "notes": "ghi chú ngắn nếu có"}}"""


# ─── extract_vnd_prices ─────────────────────────────────────────────────

_MONEY_UNIT_RE = re.compile(r"(\d[\d.,\s]*\d|\d)\s*(?:đ|₫|vnđ|vnd)\b", re.IGNORECASE)
_TRIEU_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu)\b", re.IGNORECASE)
_TR_SHORTHAND_RE = re.compile(r"\b(\d{1,3})\s?tr\s?(\d{1,3})?\b", re.IGNORECASE)
_BARE_GROUPED_RE = re.compile(r"\b\d{1,3}(?:[.,]\d{3}){2,}\b")


def _append_if_valid(bucket: list[int], value: int, lo: int, hi: int) -> None:
    if lo <= value <= hi:
        bucket.append(value)


def extract_vnd_prices(text: str) -> list[int]:
    if not text:
        return []
    prices: list[int] = []

    for m in _MONEY_UNIT_RE.finditer(text):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            _append_if_valid(prices, int(digits), _MIN_VALID_PRICE, _MAX_VALID_PRICE)

    for m in _TRIEU_RE.finditer(text):
        value = float(m.group(1).replace(",", "."))
        _append_if_valid(prices, round(value * 1_000_000), _MIN_VALID_PRICE, _MAX_VALID_PRICE)

    for m in _TR_SHORTHAND_RE.finditer(text):
        base = int(m.group(1)) * 1_000_000
        sub = int(m.group(2)) * 1_000 if m.group(2) else 0
        _append_if_valid(prices, base + sub, _MIN_VALID_PRICE, _MAX_VALID_PRICE)

    for m in _BARE_GROUPED_RE.finditer(text):
        digits = re.sub(r"[^\d]", "", m.group(0))
        _append_if_valid(prices, int(digits), _BARE_MIN_VALID_PRICE, _BARE_MAX_VALID_PRICE)

    return sorted(set(prices))


# ─── noise + scoring ─────────────────────────────────────────────────────

def _looks_unnamed_seller(title: str) -> bool:
    norm = _strip_diacritics(title.lower())
    words = norm.split()
    return len(words) <= 2 and any(g in norm for g in ("tiki", "lazada", "shopee"))


def detect_noise(candidate: PriceCandidate, query: PriceQuery) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    text = _strip_diacritics(f"{candidate.title} {candidate.snippet} {candidate.variant or ''}".lower())
    query_text = _strip_diacritics(query.clean.lower())

    if candidate.price is None:
        reasons.append("no_price")
    if any(term in text for term in _REVIEW_TERMS):
        reasons.append("review_or_news")
    if any(term in text for term in _ACCESSORY_TERMS) and not any(term in query_text for term in _ACCESSORY_TERMS):
        reasons.append("accessory")
    if any(term in text for term in _INSTALLMENT_TERMS):
        reasons.append("installment_or_deposit")
    if candidate.source in _MARKETPLACE_DOMAINS and _looks_unnamed_seller(candidate.title):
        reasons.append("unclear_seller")

    hard_noise = {"no_price", "review_or_news", "accessory"}
    is_noise = bool(hard_noise & set(reasons))
    return is_noise, reasons


def _title_match_score(text: str, must_terms: list[str]) -> float:
    if not must_terms:
        return 0.0
    norm = _strip_diacritics(text.lower())
    hits = sum(1 for term in must_terms if term in norm)
    return round((hits / len(must_terms)) * 25, 1)


def _slug_match_score(url: str, must_terms: list[str]) -> float:
    if not must_terms or not url:
        return 0.0
    norm = _strip_diacritics(url.lower())
    hits = sum(1 for term in must_terms if term in norm)
    return round((hits / len(must_terms)) * 10, 1)


def _variant_match_score(candidate: PriceCandidate, query: PriceQuery) -> tuple[float, bool]:
    query_tokens = set(query.variant_terms)
    if not query_tokens:
        return 0.0, False
    candidate_tokens = _detect_variant_tokens(f"{candidate.title} {candidate.snippet} {candidate.variant or ''}")
    if query_tokens & candidate_tokens:
        return 15.0, False
    if candidate_tokens:
        # Trang chốt rõ 1 biến thể khác hẳn biến thể người dùng yêu cầu.
        return 0.0, True
    return 5.0, False


def _mentions_chinh_hang(candidate: PriceCandidate) -> bool:
    text = _strip_diacritics(f"{candidate.title} {candidate.snippet}".lower())
    return "chinh hang" in text


def score_candidate(candidate: PriceCandidate, query: PriceQuery) -> float:
    match_text = f"{candidate.title} {candidate.snippet} {candidate.variant or ''}"

    score = 0.0
    if candidate.price:
        score += 35
    score += _title_match_score(match_text, query.must_terms)
    score += min(25, RETAILER_WEIGHTS.get(candidate.source, 0))
    variant_score, variant_mismatch = _variant_match_score(candidate, query)
    score += variant_score
    score += _slug_match_score(candidate.url, query.must_terms)
    if _mentions_chinh_hang(candidate):
        score += 5

    if candidate.is_noise:
        score -= 50
    if variant_mismatch:
        score -= 35
    if candidate.price is None:
        score -= 25
    if "review_or_news" in candidate.noise_reasons:
        score -= 20
    if "accessory" in candidate.noise_reasons:
        score -= 20
    if "installment_or_deposit" in candidate.noise_reasons:
        score -= 15
    if "unclear_seller" in candidate.noise_reasons:
        score -= 10

    return round(max(0.0, min(100.0, score)), 1)


# ─── extract / filter / score / rank ────────────────────────────────────

def extract_price_candidates(raw_results: list, query: PriceQuery) -> list[PriceCandidate]:
    candidates: list[PriceCandidate] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("shop") or entry.get("title") or "").strip()
        if not title:
            continue

        raw_url = str(entry.get("url") or "").strip()
        domain = _extract_domain(raw_url) or _guess_domain_for_shop(title) or ""
        snippet = str(entry.get("snippet") or "").strip()
        variant = str(entry.get("variant") or "").strip() or None

        price = _coerce_price(entry.get("price_vnd"))
        parsed_prices = extract_vnd_prices(f"{title} {snippet}")
        if price is None and parsed_prices:
            price = parsed_prices[0]
        if price is not None and not (_MIN_VALID_PRICE <= price <= _MAX_VALID_PRICE):
            price = None

        url, is_search_link = _resolve_item_link(title, query.clean, raw_url)

        candidates.append(PriceCandidate(
            title=title,
            url=url,
            source=domain,
            snippet=snippet,
            price=price,
            prices=parsed_prices,
            variant=variant,
            is_priority_retailer=domain in RETAILER_WEIGHTS,
            is_search_link=is_search_link,
        ))
    return candidates


def filter_noise(candidates: list[PriceCandidate], query: PriceQuery) -> list[PriceCandidate]:
    for candidate in candidates:
        candidate.is_noise, candidate.noise_reasons = detect_noise(candidate, query)
    return candidates


def score_candidates(candidates: list[PriceCandidate], query: PriceQuery) -> list[PriceCandidate]:
    for candidate in candidates:
        candidate.confidence = score_candidate(candidate, query)
    return candidates


def rank_by_price_and_confidence(candidates: list[PriceCandidate]) -> list[PriceCandidate]:
    usable = [c for c in candidates if not c.is_noise and c.price and c.confidence >= _MIN_CONFIDENCE]
    if not usable:
        return []

    deduped: dict[str, PriceCandidate] = {}
    for c in usable:
        key = c.url or f"{c.source}:{c.title}"
        existing = deduped.get(key)
        if existing is None or c.confidence > existing.confidence:
            deduped[key] = c
    result = list(deduped.values())

    prices = sorted(c.price for c in result)
    median_price = prices[len(prices) // 2]
    for c in result:
        if c.price < median_price * 0.65:
            c.confidence -= 20
            c.noise_reasons.append("gia_thap_bat_thuong")

    result = [c for c in result if c.confidence >= _MIN_CONFIDENCE]
    result.sort(key=lambda c: (c.price, -c.confidence, -RETAILER_WEIGHTS.get(c.source, 0)))
    return result


async def _url_is_reachable(client: httpx.AsyncClient, url: str) -> bool:
    if not url:
        return False
    try:
        resp = await client.head(url)
        if resp.status_code in (403, 405) or resp.status_code >= 500:
            resp = await client.get(url)  # 1 số site chặn HEAD nhưng cho GET
        return resp.status_code < 400
    except Exception:
        return False


async def _keep_reachable_links(
    ranked: list[PriceCandidate], query: PriceQuery, limit: int
) -> list[PriceCandidate]:
    """Xác thực THẬT (HTTP request) link của các candidate top đầu trước khi
    đưa vào tin nhắn - suy đoán từ hình dạng URL không đủ tin cậy, Gemini
    vẫn có thể trả link 404/trang danh mục dù trông như link sản phẩm cụ
    thể. Link chết bị loại; riêng vài retailer lớn đã xác minh sẵn URL tìm
    kiếm thì thay vào đó (ghi rõ là link tìm kiếm) thay vì bỏ hẳn kết quả."""
    if not ranked:
        return ranked

    candidates_to_check = ranked[: limit + _LINK_CHECK_CANDIDATE_BUFFER]
    try:
        async with httpx.AsyncClient(
            timeout=_LINK_CHECK_TIMEOUT, follow_redirects=True, headers=_LINK_CHECK_HEADERS
        ) as client:
            flags = await asyncio.gather(
                *(_url_is_reachable(client, c.url) for c in candidates_to_check)
            )
    except Exception:
        logger.warning("price_service: lỗi kiểm tra link, bỏ qua bước xác thực.", exc_info=True)
        return ranked[:limit]

    kept: list[PriceCandidate] = []
    for candidate, ok in zip(candidates_to_check, flags):
        if ok:
            kept.append(candidate)
            continue
        domain = _guess_domain_for_shop(candidate.title)
        template = _VERIFIED_SEARCH_URL_TEMPLATES.get(domain or "")
        if template:
            candidate.url = template.format(q=quote_plus(query.clean))
            candidate.is_search_link = True
            kept.append(candidate)
        # Domain khác không có URL tìm kiếm xác minh sẵn -> bỏ hẳn candidate
        # này, không hiển thị link chết cho người dùng.

    return kept[:limit] if kept else ranked[:limit]


# ─── format output Telegram ─────────────────────────────────────────────

def _price_range_remark(top: list[PriceCandidate]) -> str:
    prices = [c.price for c in top if c.price]
    if not prices:
        return "Chưa đủ dữ liệu giá để so sánh."
    lo, hi = min(prices), max(prices)
    if lo == hi:
        return f"Giá đang ổn định quanh {_format_trieu(lo)}."
    return f"Giá đang gom quanh {_format_trieu(lo)}–{_format_trieu(hi)}."


def _variant_conflict_warning(query: PriceQuery, ranked: list[PriceCandidate]) -> Optional[str]:
    if query.variant_terms:
        return None

    tier_tokens = set(_VARIANT_TIER_TOKENS)
    seen_tiers: set[str] = set()
    seen_storage: set[str] = set()
    seen_hp: set[str] = set()
    for c in ranked[:_DISPLAY_LIMIT]:
        tokens = _detect_variant_tokens(f"{c.title} {c.snippet} {c.variant or ''}")
        seen_tiers |= tokens & tier_tokens
        seen_storage |= {t for t in tokens if t.endswith(("gb", "tb"))}
        seen_hp |= {t for t in tokens if t.endswith("hp")}

    conflicts: set[str] = set()
    if len(seen_tiers) > 1:
        conflicts |= seen_tiers
    if len(seen_storage) > 1:
        conflicts |= seen_storage
    if len(seen_hp) > 1:
        conflicts |= seen_hp
    if not conflicts:
        return None

    return (
        f"⚠️ Em thấy kết quả có thể lẫn {'/'.join(sorted(conflicts))}. "
        "Anh nên ghi rõ dung lượng/phiên bản để em lọc sát hơn."
    )


def _format_no_data_message(display_name: str) -> str:
    return (
        f'Em chưa tìm được giá đủ tin cậy cho "{display_name}".\n\n'
        "Có thể do:\n"
        "- Tên sản phẩm hơi chung.\n"
        "- Kết quả chủ yếu là review/phụ kiện/trả góp.\n"
        "- Các nguồn không hiện giá rõ.\n\n"
        f"Anh thử ghi cụ thể hơn, ví dụ:\n/gia {display_name} 256GB"
    )


def format_telegram_price_message(
    query: PriceQuery, ranked: list[PriceCandidate], cached_at: Optional[datetime] = None
) -> str:
    display_name = query.clean or query.raw
    if not ranked:
        return _format_no_data_message(display_name)

    cheapest = ranked[0]
    alternatives = ranked[1:_DISPLAY_LIMIT]
    top_for_remark = ranked[:_DISPLAY_LIMIT]

    lines = [f"🛒 Giá {display_name}", ""]
    lines.append(f"✅ Rẻ nhất em thấy: {_format_vnd(cheapest.price)}")
    lines.append(f"🏬 {cheapest.title}")
    if cheapest.url:
        if cheapest.is_search_link:
            lines.append(f"🔍 Link tìm kiếm (chưa ra thẳng sản phẩm): {cheapest.url}")
        else:
            lines.append(f"🔗 {cheapest.url}")

    if alternatives:
        lines.append("")
        lines.append("📊 Các lựa chọn khác đáng tham khảo:")
        for i, c in enumerate(alternatives, start=1):
            note = " (link tìm kiếm)" if c.is_search_link else ""
            lines.append(f"{i}. {_format_vnd(c.price)} — {c.title}{note}")

    lines.append("")
    lines.append("💡 Nhận xét nhanh:")
    lines.append(f"- {_price_range_remark(top_for_remark)}")
    lines.append("- Nếu bản dung lượng/màu/bảo hành khác nhau thì giá có thể lệch.")
    lines.append("- Em ưu tiên chỗ bán rẻ nhất có nguồn rõ, không chỉ trong list gợi ý.")

    warning = _variant_conflict_warning(query, ranked)
    if warning:
        lines.append("")
        lines.append(warning)

    lines.append("")
    lines.append("⚠️ Kiểm tra lại tồn kho và phiên bản trước khi mua.")

    ts = (cached_at or datetime.now(_VN_TZ)).astimezone(_VN_TZ)
    lines.append(f"🕒 Cập nhật: {ts.strftime('%H:%M %d/%m/%Y')}")

    return "\n".join(lines)


# ─── thu thập candidate qua Gemini + Google Search grounding ───────────

async def _collect_candidates(query: PriceQuery) -> Optional[tuple[list[PriceCandidate], list[tuple[str, str]]]]:
    await provider_state.ensure_loaded()
    plan = build_search_plan(query)
    prompt = _build_search_prompt(query, plan)

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
            logger.warning("price_service: api%s lỗi khi tìm giá.", idx, exc_info=True)
            continue

        if not result.data or not isinstance(result.data.get("results"), list):
            logger.warning("price_service: api%s trả JSON không hợp lệ, thử key kế tiếp.", idx)
            continue

        candidates = extract_price_candidates(result.data["results"], query)
        return candidates, result.grounding_sources

    return None


# ─── cache ───────────────────────────────────────────────────────────────

def _cache_key(clean_query: str) -> str:
    return re.sub(r"\s+", " ", clean_query.strip().lower())


async def _read_cache(query: PriceQuery) -> Optional[str]:
    try:
        cached = await db.get_price_cache(_cache_key(query.clean), CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("price_service: lỗi đọc cache, bỏ qua và fetch mới.", exc_info=True)
        return None
    if cached is None:
        return None

    payload_json, created_at = cached
    try:
        payload = json.loads(payload_json)
        cached_query = PriceQuery(
            raw=query.raw,
            clean=payload.get("clean", query.clean),
            variant_terms=payload.get("variant_terms", []),
        )
        ranked = [PriceCandidate(**c) for c in payload.get("candidates", [])]
        return format_telegram_price_message(cached_query, ranked, cached_at=created_at)
    except Exception:
        logger.warning("price_service: cache lỗi định dạng, fetch mới.", exc_info=True)
        return None


async def _write_cache(query: PriceQuery, ranked: list[PriceCandidate]) -> None:
    try:
        payload = {
            "clean": query.clean,
            "variant_terms": query.variant_terms,
            "candidates": [asdict(c) for c in ranked],
        }
        await db.set_price_cache(_cache_key(query.clean), json.dumps(payload, ensure_ascii=False))
    except Exception:
        # Cache là tối ưu hoá, lỗi ghi không được làm hỏng việc trả kết quả.
        logger.warning("price_service: lỗi ghi cache, vẫn trả kết quả bình thường.", exc_info=True)


# ─── fallback text-based (cứu cánh cuối khi thiếu API key/lỗi) ─────────

TEXT_FALLBACK_SYSTEM = """Bạn là trợ lý Lan Anh. Nhiệm vụ của bạn là sử dụng công cụ Google Search để tìm giá cập nhật mới nhất cho sản phẩm: "{product_name}" tại các hệ thống bán lẻ uy tín ở Việt Nam, ưu tiên Điện Máy Chợ Lớn, Hoàng Hà Mobile, CellphoneS, Di Động Việt, Thế Giới Di Động, Điện Máy Xanh, Nguyễn Kim, FPT Shop nếu có, nhưng vẫn được lấy nguồn khác nếu giá tốt và đáng tin.

YÊU CẦU QUAN TRỌNG:
1. So khớp CHÍNH XÁC phiên bản/dung lượng nếu người dùng nêu rõ; nếu KHÔNG nêu, mặc định lấy bản thấp nhất và ghi chú rõ điều này trong kết quả.
2. BẮT BUỘC phải trích xuất URL gốc của trang sản phẩm để người dùng bấm vào xem.
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


async def _fetch_text_fallback(product_name: str) -> str:
    from ai import orchestrator  # import trễ, tránh vòng import với ai.official_client

    instruction = TEXT_FALLBACK_SYSTEM.format(product_name=product_name)
    response = await orchestrator.ask(instruction, enable_search=True)
    text = (response.text or "").strip()
    if not text:
        raise PriceServiceError("Gemini không trả về nội dung ở fallback text-based.")
    suffix = "\n\n⚙️ API" if getattr(response, "used_fallback", False) else ""
    return text + suffix


# ─── entry point chính ───────────────────────────────────────────────────

async def fetch_price_message(raw_product_name: str) -> str:
    """Entry point cho handlers/commands.py::price_cmd, chat_router.py (hỏi
    giá tự nhiên), và services/tools.py::_tool_search_price. Raise
    PriceServiceError nếu không lấy được giá bằng bất kỳ cách nào."""
    query = normalize_price_query(raw_product_name)
    if not query.clean:
        raise PriceServiceError("Thiếu tên sản phẩm.")

    if not query.force_refresh:
        cached = await _read_cache(query)
        if cached is not None:
            return cached

    collected = await _collect_candidates(query)
    if collected is not None:
        candidates, _grounding_sources = collected
        candidates = filter_noise(candidates, query)
        candidates = score_candidates(candidates, query)
        ranked = rank_by_price_and_confidence(candidates)
        ranked = await _keep_reachable_links(ranked, query, limit=_DISPLAY_LIMIT)
        await _write_cache(query, ranked)
        return format_telegram_price_message(query, ranked)

    return await _fetch_text_fallback(query.clean)


# ─── nhận diện hỏi giá tự nhiên (không cần /gia) ────────────────────────

_STOCK_ONLY_RE = re.compile(
    r"^\s*(?:giá|gia|xem giá|xem gia|check giá|check gia)?\s*([a-zA-Z]{3,4})\s*"
    r"(?:giá|gia|hôm nay|hom nay|sao rồi|sao roi)?\s*$"
)


def _is_stock_only_query(lowered_text: str) -> bool:
    match = _STOCK_ONLY_RE.match(lowered_text)
    if not match:
        return False
    symbol = match.group(1).upper()
    return symbol in ALL_KNOWN_SYMBOLS or symbol == "VNINDEX"


def is_product_price_query(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    lowered = text.lower()

    matched_intent = [kw for kw in PRODUCT_PRICE_INTENT_KEYWORDS if kw in lowered]
    if not matched_intent:
        return False
    # "giá FPT"/"giá VNM" (đúng 1 mã cổ phiếu hợp lệ, không kèm gì khác) là
    # hỏi giá cổ phiếu, không phải sản phẩm - phải nhường cho stock_handler.
    if _is_stock_only_query(lowered):
        return False

    if any(kw in lowered for kw in PRODUCT_HINT_KEYWORDS):
        return True

    # "mua" một mình quá chung chung cho chat đời thường ("mua vé", "mua đồ
    # ăn", "em muốn mua"...) - chỉ tin khi có product hint đi kèm (đã kiểm ở
    # trên); tới đây nghĩa là không có, nên không tính là hỏi giá sản phẩm.
    if matched_intent == ["mua"]:
        return False

    return True


def extract_product_query_from_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("giá đỡ", "gia do")):
        return text

    cleaned = lowered
    for phrase in sorted(REMOVE_PRICE_PHRASES, key=len, reverse=True):
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or text
