"""
Lấy dữ liệu thị trường chứng khoán Việt Nam - port từ repo stock-portfolio
(src/lib/server/providers/dnse-realtime.ts, vci-chart.ts, ai/news.ts).

Tất cả nguồn dùng ở đây đều CÔNG KHAI, KHÔNG CẦN API KEY:
- DNSE Entrade (services.entrade.com.vn) - giá + lịch sử OHLCV, đủ HOSE/HNX/UPCOM/VNINDEX.
- Google News RSS - tin tức theo mã.

Không dùng VCI Edge / Supabase / SSI như bản gốc vì những nguồn đó cần hạ tầng
riêng (Supabase Edge Function) không có trong bot Telegram này.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DNSE_OHLC_BASE = "https://services.entrade.com.vn/chart-api/v2/ohlcs"
PRICE_SCALE = 1000  # DNSE trả giá theo NGHÌN VND cho cổ phiếu -> x1000 = VND thô
REQUEST_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)

_INDEX_SYMBOLS = {"VNINDEX", "VN30", "HNXINDEX", "HNX30", "UPCOMINDEX", "UPINDEX"}


def is_index_symbol(symbol: str) -> bool:
    return symbol.upper().replace("-", "").replace("^", "") in _INDEX_SYMBOLS


def dnse_symbol(symbol: str) -> str:
    s = symbol.upper()
    return "VNINDEX" if s in ("VNINDEX", "^VNINDEX", "VN-INDEX") else s


@dataclass
class OhlcvSeries:
    symbol: str
    closes: list = field(default_factory=list)
    highs: list = field(default_factory=list)
    lows: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    dates: list = field(default_factory=list)

    @property
    def price(self) -> float:
        return self.closes[-1] if self.closes else 0.0


async def fetch_ohlcv(symbol: str, days: int = 90) -> OhlcvSeries:
    """Lấy lịch sử OHLCV 1D cho 1 mã (VND thô). Rỗng nếu lỗi/không có dữ liệu."""
    sym = symbol.strip().upper()
    is_index = is_index_symbol(sym)
    endpoint = f"{DNSE_OHLC_BASE}/{'index' if is_index else 'stock'}"
    to_ts = int(datetime.now(timezone.utc).timestamp())
    from_ts = to_ts - int(days * 1.6 + 10) * 86400

    params = {
        "from": from_ts,
        "to": to_ts,
        "symbol": dnse_symbol(sym),
        "resolution": "1D",
    }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            res = await client.get(
                endpoint, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
        if res.status_code != 200:
            return OhlcvSeries(symbol=sym)
        data = res.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_ohlcv(%s) lỗi: %s", sym, e)
        return OhlcvSeries(symbol=sym)

    t = data.get("t") or []
    o = data.get("o") or []
    h = data.get("h") or []
    l = data.get("l") or []
    c = data.get("c") or []
    v = data.get("v") or []
    if not t:
        return OhlcvSeries(symbol=sym)

    scale = 1 if is_index else PRICE_SCALE
    bars = []
    for i in range(len(t)):
        try:
            close = float(c[i])
        except (IndexError, TypeError, ValueError):
            continue
        if not (close > 0):
            continue
        high = float(h[i]) if i < len(h) and h[i] else close
        low = float(l[i]) if i < len(l) and l[i] else close
        vol = float(v[i]) if i < len(v) and v[i] else 0.0
        bars.append((int(t[i]), high, low, close, vol))

    bars.sort(key=lambda b: b[0])
    bars = bars[-days:]

    closes = [round(b[3] * scale) for b in bars]
    highs = [round(b[1] * scale) for b in bars]
    lows = [round(b[2] * scale) for b in bars]
    volumes = [b[4] for b in bars]
    dates = [datetime.fromtimestamp(b[0], tz=timezone.utc).strftime("%Y-%m-%d") for b in bars]

    return OhlcvSeries(symbol=sym, closes=closes, highs=highs, lows=lows, volumes=volumes, dates=dates)


async def fetch_current_price(symbol: str) -> float:
    """Giá gần nhất (đóng cửa phiên gần nhất/nến gần nhất). 0 nếu không lấy được."""
    series = await fetch_ohlcv(symbol, days=5)
    return series.price


async def verify_symbol_exists(symbol: str) -> bool:
    """Xác minh 1 chuỗi có phải mã cổ phiếu/chỉ số VN thật hay không, bằng cách
    thử fetch giá thật - tránh phải tự maintain danh sách đầy đủ ~1600 mã."""
    price = await fetch_current_price(symbol)
    return price > 0


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    change: float
    change_pct: float
    date: str


async def fetch_quote(symbol: str) -> Quote | None:
    """Giá gần nhất + thay đổi so với phiên liền trước - dùng riêng cho các câu
    hỏi tra giá đơn thuần (KHÔNG phân tích). Chỉ lấy 5 phiên gần nhất (đủ để
    tính chênh lệch phiên-phiên), nhẹ hơn nhiều so với fetch_ohlcv(90 ngày)
    vốn dùng cho phân tích kỹ thuật đầy đủ ở stock_analysis.py."""
    series = await fetch_ohlcv(symbol, days=5)
    if not series.closes:
        return None
    price = series.closes[-1]
    prev_close = series.closes[-2] if len(series.closes) >= 2 else price
    change = round(price - prev_close, 2)
    change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0
    date = series.dates[-1] if series.dates else ""
    return Quote(
        symbol=symbol, price=price, prev_close=prev_close,
        change=change, change_pct=change_pct, date=date,
    )


# ─── Tin tức (Google News RSS) ──────────────────────────────────────────────

NEWS_RECENT_DAYS = 30
NEWS_MAX_ITEMS = 8

NOISE_KEYWORDS = ["cw", "chứng quyền", "cmw"]
NEGATION_WORDS = ["không", "chưa", "chẳng", "chớ", "đừng", "thay vì", "ngoại trừ"]
POS_WORDS = ["tăng", "lãi", "mua", "tích cực", "kỷ lục", "phục hồi", "tăng trưởng", "bứt phá", "vượt", "khởi sắc"]
NEG_WORDS = ["giảm", "lỗ", "bán", "rủi ro", "phạt", "vi phạm", "sụt", "bán tháo", "tụt", "hạ", "yếu"]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def sentiment_score(title: str) -> float:
    t = title.lower()
    words = t.split()
    score = 0
    joined = " ".join(words)
    for pos in POS_WORDS:
        idx = joined.find(pos)
        if idx == -1:
            continue
        before = joined[:idx]
        score += -1 if any(neg in before[-20:] for neg in NEGATION_WORDS) else 1
    for neg_w in NEG_WORDS:
        idx = joined.find(neg_w)
        if idx == -1:
            continue
        before = joined[:idx]
        score += 1 if any(neg in before[-20:] for neg in NEGATION_WORDS) else -1
    return _clamp(score / 3, -1, 1)


@dataclass
class NewsHeadline:
    title: str
    source: str
    pub_date: str
    url: str
    sentiment: float = 0.0


def _extract_tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml, re.IGNORECASE)
    if not m:
        return ""
    return re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", m.group(1)).strip()


async def fetch_news(symbol: str) -> list[NewsHeadline]:
    query = f"{symbol} cổ phiếu"
    url = "https://news.google.com/rss/search"
    try:
        import httpx

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            res = await client.get(
                url,
                params={"q": query, "hl": "vi", "gl": "VN", "ceid": "VN:vi"},
                headers={"User-Agent": USER_AGENT},
            )
        if res.status_code != 200:
            return []
        text = res.text
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_news(%s) lỗi: %s", symbol, e)
        return []

    items = re.findall(r"<item>[\s\S]*?</item>", text, re.IGNORECASE)
    if not items:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_RECENT_DAYS)
    seen = set()
    news: list[NewsHeadline] = []
    for item in items[:NEWS_MAX_ITEMS]:
        raw_title = _extract_tag(item, "title")
        title = raw_title.split(" - ")[0].strip()
        if not title or len(title) <= 5:
            continue
        lower_t = title.lower()
        if any(k in lower_t for k in NOISE_KEYWORDS):
            continue
        key = lower_t.strip()
        if key in seen:
            continue
        pub_date_raw = _extract_tag(item, "pubDate")
        try:
            from email.utils import parsedate_to_datetime

            pub_dt = parsedate_to_datetime(pub_date_raw)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
        except Exception:  # noqa: BLE001
            pass
        seen.add(key)
        source = _extract_tag(item, "source") or (raw_title.split(" - ")[-1].strip() if " - " in raw_title else "")
        link = _extract_tag(item, "link")
        news.append(NewsHeadline(title=title, source=source, pub_date=pub_date_raw, url=link, sentiment=sentiment_score(title)))

    return news


def calc_news_impact(news: list[NewsHeadline]) -> float:
    if not news:
        return 0.0
    import math

    avg = sum(n.sentiment for n in news) / len(news)
    return _clamp(avg * math.log(len(news) + 1), -2, 2)
