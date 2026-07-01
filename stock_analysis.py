"""
Orchestrator cho tính năng "phân tích cổ phiếu tự động" - port tinh thần từ
src/app/api/ai/watchlist-scan/route.ts (stock-portfolio), áp dụng cho 1 mã
gõ trong Telegram thay vì cả watchlist.

Khác biệt quan trọng so với bản gốc:
- Bản gốc gửi JSON cho LLM (Groq, có JSON schema validation bằng Zod) rồi ép
  cứng lại tp/sl bằng code trước khi trả về người dùng.
- Ở đây Gemini được gọi qua gemini-webapi (thư viện cookie, KHÔNG có JSON mode
  đảm bảo) nên: mọi con số quan trọng (giá, entry/TP/SL, %, điểm số) đều được
  TÍNH SẴN bằng Python (deterministic, không do AI bịa), rồi mới đưa cho Gemini
  để viết PHẦN DIỄN GIẢI bằng văn phong Lan Anh - Gemini không được tự đặt ra
  con số giá nào khác ngoài các số đã cho.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import stock_earnings as earnings
import stock_indicators as ind
import stock_moneyflow as moneyflow
import stock_providers as providers
import stock_sector as sector
from stock_sector import ALL_KNOWN_SYMBOLS

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# ─── Cache đơn giản trong RAM (giống tinh thần WATCHLIST_AI_CACHE_TTL_MS bản gốc) ──
_CACHE_TTL_SEC = 15 * 60  # 15 phút - ngắn hơn bản gốc (2h) vì đây là tra cứu 1 mã theo yêu cầu tức thời
_cache: dict[str, tuple[float, str]] = {}


def _cache_get(symbol: str) -> str | None:
    entry = _cache.get(symbol)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        _cache.pop(symbol, None)
        return None
    return value


def _cache_set(symbol: str, value: str) -> None:
    _cache[symbol] = (time.time(), value)


# ─── Nhận diện mã cổ phiếu trong tin nhắn tự nhiên ───────────────────────────

def detect_symbol_candidates(text: str) -> list[str]:
    """Tìm các token trong `text` có khả năng là mã cổ phiếu VN.

    Chỉ 2 trường hợp được coi là "ứng viên mạnh" (để tránh gọi mạng xác minh
    tràn lan trên MỌI tin nhắn chat bình thường):
      1. Token khớp với 1 mã trong danh sách đã biết (SECTOR_MAP, ~80 mã lớn).
      2. Token được người dùng gõ TOÀN CHỮ HOA, dài 3-4 ký tự ASCII - dấu hiệu
         rõ ràng đây là ý định gõ mã CK (vd "FPT", "HPG dạo này sao rồi?").
    Các ứng viên này còn phải qua bước xác minh sống (verify_symbol_exists) ở
    analyze_symbol() trước khi thực sự chạy phân tích, để loại nốt trường hợp
    trùng tình cờ (vd viết hoa 1 từ để nhấn mạnh nhưng không phải mã CK).
    """
    import re

    tokens = re.findall(r"\b[A-Za-z]{3,4}\b", text)
    candidates: list[str] = []
    seen = set()
    for tok in tokens:
        upper = tok.upper()
        if upper in seen:
            continue
        is_known = upper in ALL_KNOWN_SYMBOLS or upper == "VNINDEX"
        is_shouted = tok == upper  # người dùng gõ toàn hoa
        if is_known or is_shouted:
            seen.add(upper)
            candidates.append(upper)
    return candidates


async def find_valid_symbols(text: str, limit: int = 3) -> list[str]:
    """Trả về các mã CK THẬT (đã xác minh có giá) tìm thấy trong `text`."""
    candidates = detect_symbol_candidates(text)[:6]  # chặn spam quá nhiều mã 1 lúc
    if not candidates:
        return []
    results = await asyncio.gather(*[providers.verify_symbol_exists(c) for c in candidates])
    valid = [c for c, ok in zip(candidates, results) if ok]
    return valid[:limit]


# ─── Phân biệt "hỏi giá" (chỉ cần số liệu DNSE) vs "yêu cầu phân tích" ───────
# Mặc định khi nhắc tới 1 mã CK trong chat, chỉ trả giá + % thay đổi (nhanh,
# không tốn lượt gọi Gemini). CHỈ chạy pipeline phân tích đầy đủ (kỹ thuật +
# dòng tiền + ngành + BCTC + tin tức + Gemini diễn giải) khi người dùng dùng
# từ ngữ thể hiện rõ ràng muốn PHÂN TÍCH, không chỉ tra giá.
ANALYSIS_KEYWORDS = [
    "phân tích", "phan tich", "kỹ thuật", "ky thuat", "cơ bản", "co ban",
    "đánh giá", "danh gia", "nhận định", "nhan dinh", "khuyến nghị",
    "khuyen nghi", "tư vấn", "tu van", "nên mua", "nen mua", "nên bán",
    "nen ban", "có nên", "co nen", "triển vọng", "trien vong", "review",
    "so sánh", "so sanh", "dự báo", "du bao", "xu hướng", "xu huong",
    "định giá", "dinh gia", "dòng tiền", "dong tien",
]


def wants_full_analysis(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in ANALYSIS_KEYWORDS)


def format_quote_message(q: providers.Quote) -> str:
    """Định dạng tin nhắn tra giá nhanh (KHÔNG phân tích) - toàn số liệu lấy
    trực tiếp từ DNSE, không qua Gemini."""
    if q.change > 0:
        arrow = "🟢▲"
        sign = "+"
    elif q.change < 0:
        arrow = "🔴▼"
        sign = ""
    else:
        arrow = "⚪"
        sign = ""
    return (
        f"📊 {q.symbol}: {_fmt_price(q.price)} VND\n"
        f"{arrow} {sign}{_fmt_price(q.change)} ({sign}{q.change_pct}%) so với phiên trước "
        f"({_fmt_price(q.prev_close)} VND)"
        + (f" — phiên {q.date}" if q.date else "")
    )


async def quick_quote(symbol: str) -> str:
    """Trả về text tra giá nhanh cho 1 mã, gọi trực tiếp DNSE, không dùng Gemini."""
    symbol = symbol.strip().upper()
    q = await providers.fetch_quote(symbol)
    if q is None:
        return f"Em không lấy được giá {symbol} lúc này, anh thử lại sau ít phút nhé."
    return format_quote_message(q)


# ─── Score tổng hợp (port scoreSignal() từ route.ts) ─────────────────────────

def _score_signal(stats: ind.SignalStats, has_news: bool) -> float:
    trend_score = ind.clamp(stats.trend_3m, -20, 20)
    momentum_score = ind.clamp(stats.momentum * 1.5, -15, 15)
    volume_score = ind.clamp(stats.volume_trend * 0.5, -15, 15)
    news_score = 5 if has_news else 0
    vol_penalty = stats.volatility * 0.5
    return round(50 + trend_score + momentum_score + volume_score + news_score - vol_penalty, 2)


@dataclass
class StockContext:
    symbol: str
    price: float
    fetched_at_vn: str
    stats: ind.SignalStats
    action: str
    confidence: str
    base_reason: str
    enhanced: ind.EnhancedIndicators | None
    indicator_summary: str
    support_resistance: ind.SupportResistance | None
    bias_ma: ind.BiasMA | None
    ma_alignment: ind.MAAlignment | None
    trend_score: int | None
    final_score: float
    sector_prompt: str
    money_flow_prompt: str
    earnings_prompt: str
    news: list[providers.NewsHeadline]
    relative_strength: float


async def build_context(symbol: str) -> StockContext | None:
    symbol_series, vnindex_series, news = await asyncio.gather(
        providers.fetch_ohlcv(symbol, days=90),
        providers.fetch_ohlcv("VNINDEX", days=90),
        providers.fetch_news(symbol),
    )
    if not symbol_series.closes:
        return None

    price = symbol_series.price
    news_impact = providers.calc_news_impact(news)
    stats = ind.calc_signal_stats(symbol_series.closes, symbol_series.volumes, price, news_impact)

    def _trend(closes: list[float]) -> float:
        return ((closes[-1] - closes[0]) / closes[0]) * 100 if closes and closes[0] else 0.0

    vnindex_trend = _trend(vnindex_series.closes)
    symbol_trend_full = _trend(symbol_series.closes)
    relative_strength = round(symbol_trend_full - vnindex_trend, 2)

    action, confidence, base_reason = ind.decide_action(
        stats.trend_3m, stats.momentum, stats.volume_trend, news_impact, stats.volatility, stats.rsi14, relative_strength,
    )

    enhanced = None
    indicator_summary = ""
    enhanced_score = 0
    if len(symbol_series.closes) > 20:
        enhanced = ind.build_enhanced_indicators(symbol_series.closes, price, symbol_series.highs, symbol_series.lows)
        enhanced_score = ind.score_enhanced_indicators(enhanced)
        indicator_summary = ind.build_indicator_summary(enhanced, symbol)

    support_resistance = None
    if symbol_series.highs:
        support_resistance = ind.calc_support_resistance(symbol_series.highs, symbol_series.lows, price, 30)

    ma_alignment = None
    bias_ma = None
    trend_score = None
    if len(symbol_series.closes) >= 20:
        ma_alignment = ind.calc_ma_alignment(symbol_series.closes)
        bias_ma = ind.calc_bias_ma(price, ma_alignment.ma5)
        if enhanced:
            trend_score = ind.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram)

    base_score = _score_signal(stats, bool(news))
    if trend_score is not None:
        final_score = round((base_score * 0.4 + trend_score * 0.6) + enhanced_score * 0.5, 1)
    else:
        final_score = round(base_score + enhanced_score, 1)

    # Sector
    sector_prompt = ""
    try:
        sector_keys = sector.get_symbol_sectors(symbol)
        if sector_keys:
            ctx = await sector.build_sector_context(sector_keys)
            sector_prompt = sector.build_sector_prompt_section(ctx, symbol)
    except Exception:  # noqa: BLE001
        logger.warning("sector context lỗi cho %s", symbol, exc_info=True)

    # Money flow
    money_flow_prompt = ""
    try:
        foreign = moneyflow.analyze_money_flow(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes)
        obv_trend = moneyflow.calc_obv_trend(symbol_series.closes, symbol_series.volumes)
        mfi = moneyflow.calc_mfi(symbol_series.closes, symbol_series.volumes, symbol_series.highs, symbol_series.lows)
        money_flow_prompt = moneyflow.build_money_flow_prompt_section(foreign, obv_trend, mfi, symbol)
    except Exception:  # noqa: BLE001
        logger.warning("money flow lỗi cho %s", symbol, exc_info=True)

    # Earnings (ước tính mùa BCTC)
    earnings_prompt = ""
    try:
        cal = earnings.estimate_next_earnings()
        earnings_prompt = earnings.build_earnings_prompt_section(cal, symbol)
    except Exception:  # noqa: BLE001
        logger.warning("earnings lỗi cho %s", symbol, exc_info=True)

    fetched_at_vn = datetime.now(_VN_TZ).strftime("%H:%M ngày %d/%m/%Y")

    return StockContext(
        symbol=symbol, price=price, fetched_at_vn=fetched_at_vn, stats=stats,
        action=action, confidence=confidence, base_reason=base_reason,
        enhanced=enhanced, indicator_summary=indicator_summary,
        support_resistance=support_resistance, bias_ma=bias_ma, ma_alignment=ma_alignment,
        trend_score=trend_score, final_score=final_score,
        sector_prompt=sector_prompt, money_flow_prompt=money_flow_prompt, earnings_prompt=earnings_prompt,
        news=news, relative_strength=relative_strength,
    )


# ─── Prompt cho Gemini (Lan Anh - văn phong) ─────────────────────────────────

def _fmt_price(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def build_prompt(ctx: StockContext) -> str:
    lines: list[str] = []
    lines.append(
        "Bạn là Lan Anh, trợ lý cá nhân, xưng \"em\" gọi người dùng là \"anh\". "
        "Tin nhắn này thuộc CHẾ ĐỘ PHÂN TÍCH CỔ PHIẾU nghiêm túc: bỏ giọng nũng nịu, "
        "trả lời chính xác, đáng tin cậy, dựa HOÀN TOÀN vào DỮ LIỆU bên dưới - "
        "TUYỆT ĐỐI KHÔNG tự bịa thêm bất kỳ con số giá/% nào khác ngoài các số đã cho. "
        "Nếu thiếu dữ liệu ở mục nào, cứ nói rõ là chưa có, đừng đoán."
    )
    lines.append("")
    lines.append(f"=== DỮ LIỆU MÃ {ctx.symbol} (chốt lúc {ctx.fetched_at_vn} giờ VN) ===")
    lines.append(f"Giá hiện tại: {_fmt_price(ctx.price)} VND")
    lines.append(f"Entry đề xuất: {_fmt_price(ctx.price)} | TP: {_fmt_price(ctx.stats.suggested_tp)} | SL: {_fmt_price(ctx.stats.suggested_sl)}")
    lines.append(f"Điểm tổng hợp (0-100 càng cao càng tích cực): {ctx.final_score}")
    lines.append(f"Tín hiệu hệ thống: {ctx.action} (độ tin cậy {ctx.confidence}) — {ctx.base_reason}")
    lines.append(
        f"Technical cơ bản: trend {ctx.stats.trend_3m}% (~3 tháng), momentum {ctx.stats.momentum}, "
        f"volume trend {ctx.stats.volume_trend}%, volatility {ctx.stats.volatility}%, RSI14 {ctx.stats.rsi14}, "
        f"relative strength vs VNINDEX {ctx.relative_strength}%"
    )
    if ctx.indicator_summary:
        lines.append(ctx.indicator_summary)
    if ctx.support_resistance and ctx.support_resistance.support:
        sr = ctx.support_resistance
        lines.append(f"Hỗ trợ/kháng cự (30 phiên): support {_fmt_price(sr.support)} ({sr.dist_to_support}%), resistance {_fmt_price(sr.resistance)} ({sr.dist_to_resistance}%)")
    if ctx.ma_alignment and ctx.ma_alignment.alignment != "unknown":
        lines.append(f"MA alignment: {ctx.ma_alignment.alignment} (MA5={_fmt_price(ctx.ma_alignment.ma5)}, MA10={_fmt_price(ctx.ma_alignment.ma10)}, MA20={_fmt_price(ctx.ma_alignment.ma20)})")
    if ctx.bias_ma:
        lines.append(f"Độ lệch giá vs MA5: {ctx.bias_ma.bias}% ({ctx.bias_ma.status})")
    if ctx.trend_score is not None:
        lines.append(f"Trend score tổng hợp: {ctx.trend_score}/100")
    if ctx.sector_prompt:
        lines.append(ctx.sector_prompt)
    if ctx.money_flow_prompt:
        lines.append(ctx.money_flow_prompt)
    if ctx.earnings_prompt:
        lines.append(ctx.earnings_prompt)
    if ctx.news:
        lines.append(f"[TIN TỨC gần đây — {ctx.symbol}]")
        for n in ctx.news[:5]:
            tag = "🟢" if n.sentiment > 0.2 else ("🔴" if n.sentiment < -0.2 else "⚪")
            lines.append(f"{tag} {n.title} ({n.source})")
    else:
        lines.append(f"[TIN TỨC — {ctx.symbol}]: không tìm thấy tin gần đây.")

    lines.append("")
    lines.append(
        "=== YÊU CẦU OUTPUT ===\n"
        "Viết 1 tin nhắn Telegram bằng tiếng Việt, giọng Lan Anh nhưng nghiêm túc, có emoji vừa phải, gồm:\n"
        "1. Dòng mở đầu nêu nhận định nhanh (dùng ĐÚNG tín hiệu hệ thống đã cho: BUY/HOLD/WATCH/SELL) kèm giá hiện tại.\n"
        "2. 3-5 gạch đầu dòng lý do CÓ SỐ LIỆU CỤ THỂ lấy từ DỮ LIỆU trên (không chung chung).\n"
        "3. Vùng giá tham khảo: Entry / TP / SL - dùng ĐÚNG 3 con số đã cho ở trên, không đổi.\n"
        "4. 1 dòng lưu ý rủi ro (dựa vào volatility/tin xấu/ngành yếu nếu có).\n"
        "5. Câu kết ngắn nhắc đây là tham khảo, không phải khuyến nghị đầu tư tuyệt đối.\n"
        "KHÔNG dùng markdown code block, không lặp lại nguyên văn nhãn tiếng Anh của dữ liệu, viết tự nhiên."
    )
    return "\n".join(lines)


def _fallback_text(ctx: StockContext) -> str:
    """Dùng khi Gemini lỗi/không trả lời được - vẫn có kết quả tối thiểu đáng tin (toàn số liệu tự tính)."""
    action_label = {"BUY": "🟢 MUA", "HOLD": "🟡 GIỮ", "WATCH": "🟡 THEO DÕI", "SELL": "🔴 BÁN/TRÁNH"}.get(ctx.action, ctx.action)
    lines = [
        f"📊 {ctx.symbol} — {_fmt_price(ctx.price)} VND (dữ liệu {ctx.fetched_at_vn} giờ VN)",
        f"Tín hiệu: {action_label} (độ tin cậy {ctx.confidence}) — {ctx.base_reason}",
        f"Entry {_fmt_price(ctx.price)} | TP {_fmt_price(ctx.stats.suggested_tp)} | SL {_fmt_price(ctx.stats.suggested_sl)}",
        f"Điểm tổng hợp: {ctx.final_score}/100 | RSI14 {ctx.stats.rsi14} | Trend ~3M {ctx.stats.trend_3m}%",
        "⚠️ Gemini đang không phản hồi được nên đây là bản rút gọn từ dữ liệu kỹ thuật, chưa có phần diễn giải chi tiết.",
        "(Đây là thông tin tham khảo, không phải khuyến nghị đầu tư.)",
    ]
    return "\n".join(lines)


async def analyze_symbol(symbol: str, *, force_refresh: bool = False) -> str:
    symbol = symbol.strip().upper()
    if not force_refresh:
        cached = _cache_get(symbol)
        if cached:
            return cached

    ctx = await build_context(symbol)
    if ctx is None:
        return f"Em không lấy được dữ liệu giá cho mã {symbol} lúc này, anh thử lại sau ít phút nhé."

    prompt = build_prompt(ctx)

    import gemini_client

    try:
        response = await gemini_client.ask(prompt)
        text = (response.text or "").strip()
        result = text or _fallback_text(ctx)
    except Exception:  # noqa: BLE001
        logger.exception("Gemini lỗi khi phân tích %s", symbol)
        result = _fallback_text(ctx)

    _cache_set(symbol, result)
    return result
