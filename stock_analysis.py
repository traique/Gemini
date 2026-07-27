import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

import stock_backtest as backtest
import stock_features as feat
import stock_fundamentals as fundamentals
import stock_policy as policy
import stock_providers as providers
import stock_sector as sector
import stock_validation as validation
import messages
from core import database as db
from stock_sector import ALL_KNOWN_SYMBOLS

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_CACHE_TTL_SEC = 15 * 60
_CACHE_MAX_ENTRIES = 200
_cache: dict[tuple[str, bool], tuple[float, str]] = {}

def _cache_get(symbol: str, holding: bool) -> str | None:
    entry = _cache.get((symbol, holding))
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        _cache.pop((symbol, holding), None)
        return None
    return value

def _cache_set(symbol: str, holding: bool, value: str) -> None:
    if len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest_key = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest_key, None)
    _cache[(symbol, holding)] = (time.time(), value)

_SYMBOL_TOKEN_RE = re.compile(r"\b[A-Za-z]{3,4}\b")
_UPPERCASE_TOKEN_RE = re.compile(r"\b[A-Z]{3,4}\b")
_INDEX_NAME_RE = re.compile(r"\b(vn[\s\-]?index|vn[\s\-]?30|hnx[\s\-]?index|hnx[\s\-]?30|upcom[\s\-]?index)\b", re.IGNORECASE)

def _normalize_index_name(raw: str) -> str:
    return re.sub(r"[\s\-]", "", raw).upper()

_COMMON_WORD_EXCLUDE = {
    "ANH", "EM", "OI", "GIA", "CHO", "KHI", "NAY", "ROI", "NHE", "NHA",
    "VOI", "LA", "VA", "DO", "CO", "KO", "MA", "THE", "SAO", "VAY", "NAO",
    "LAM", "XEM", "GIO", "DUOC", "MOT", "HAI", "BA", "NAM", "NGAY", "TUAN",
    "OK", "CEO", "CFO", "CTO", "ATM", "PR", "FYI", "ASAP", "VIP", "FAQ",
    "TV", "PC", "AI", "US", "UK", "EU", "OS", "ID", "URL", "PDF", "CV", "OMG",
}
_MAX_UNVERIFIED_PER_MSG = 6

_AMBIGUOUS_KNOWN = {"GAS", "VND", "HAG", "OIL"}
_STOCK_CONTEXT_KEYWORDS = ("cổ phiếu", "co phieu", "mã", "ma ", "phân tích", "phan tich", "cp ", " cp")


def _has_stock_context(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _STOCK_CONTEXT_KEYWORDS)


def detect_symbol_candidates(text: str) -> tuple[list[str], list[str]]:
    tokens = _SYMBOL_TOKEN_RE.findall(text)
    uppercase_tokens = set(_UPPERCASE_TOKEN_RE.findall(text))
    known, unverified = [], []
    seen = set()
    stock_context = _has_stock_context(text)

    for m in _INDEX_NAME_RE.finditer(text):
        norm = _normalize_index_name(m.group(0))
        if norm not in seen:
            seen.add(norm)
            known.append(norm)

    for tok in tokens:
        upper = tok.upper()
        if upper in seen: continue
        seen.add(upper)
        if upper in ALL_KNOWN_SYMBOLS or upper == "VNINDEX":
            if upper in _AMBIGUOUS_KNOWN and not (tok in uppercase_tokens or stock_context):
                # mã trùng từ tiếng Việt/tiếng Anh thông dụng ("gas", "vnd"...)
                # - chỉ nhận khi viết HOA nguyên bản hoặc tin nhắn có keyword
                # chứng khoán, tránh bot trả nhầm giá cổ phiếu cho câu hỏi
                # không liên quan.
                continue
            known.append(upper)
        elif tok in uppercase_tokens and upper not in _COMMON_WORD_EXCLUDE:
            # nhóm unverified (cần verify qua DNSE) chỉ nhận token viết HOA
            # NGUYÊN BẢN trong tin nhắn gốc - token thường (vd "hom", "vang",
            # "ket" từ tiếng Việt không dấu) không được coi là ứng viên mã,
            # tránh tốn request verify và false positive khi trùng mã thật.
            unverified.append(upper)
    return known, unverified

async def find_valid_symbols(text: str, limit: int = 3) -> list[str]:
    known, unverified = detect_symbol_candidates(text)
    result = list(known)
    if len(result) < limit and unverified:
        to_check = [s for s in unverified if s not in result][:_MAX_UNVERIFIED_PER_MSG]
        checks = await asyncio.gather(*[providers.verify_symbol_exists(s) for s in to_check], return_exceptions=True)
        for sym, ok in zip(to_check, checks):
            if ok is True and sym not in result:
                result.append(sym)
                if len(result) >= limit: break
    return result[:limit]

ANALYSIS_KEYWORDS = [
    "phân tích", "phan tich", "kỹ thuật", "ky thuat", "cơ bản", "co ban",
    "đánh giá", "danh gia", "nhận định", "nhan dinh", "khuyến nghị",
    "khuyen nghi", "tư vấn", "tu van", "nên mua", "nen mua", "nên bán",
    "nen ban", "có nên", "co nen", "triển vọng", "trien vong", "review",
    "so sánh", "so sanh", "dự báo", "du bao", "xu hướng", "xu huong",
    "định giá", "dinh gia", "dòng tiền", "dong tien",
    "giờ sao", "gio sao", "xử lý sao", "xu ly sao", "làm sao", "lam sao",
    "cắt lỗ", "cat lo", "chốt lời", "chot loi", "về bờ", "ve bo", 
    "kẹt", "ket", "giữ hay bán", "giu hay ban", "nên giữ", "nen giu"
]

def wants_full_analysis(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in ANALYSIS_KEYWORDS)

PRICE_KEYWORDS = ["giá", "gia", "price", "bao nhiêu", "bao nhieu"]
_PRICE_KEYWORDS_RE = re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in PRICE_KEYWORDS) + r")\b", re.IGNORECASE)
_BARE_SYMBOLS_FILLER_RE = re.compile(r"[,\.\-/&+]+")

def wants_price_quote(text: str, symbols: list[str]) -> bool:
    remaining = text
    for sym in symbols:
        remaining = re.sub(re.escape(sym), "", remaining, flags=re.IGNORECASE)
    remaining = _PRICE_KEYWORDS_RE.sub(" ", remaining)
    remaining = _BARE_SYMBOLS_FILLER_RE.sub(" ", remaining)
    return remaining.strip() == ""

def format_quote_message(q: providers.Quote) -> str:
    arrow, sign = ("🟢▲", "+") if q.change > 0 else ("🔴▼", "") if q.change < 0 else ("⚪", "")
    time_note = f"khớp lệnh realtime lúc {datetime.now(_VN_TZ).strftime('%H:%M ngày %d/%m/%Y')} giờ VN" if q.is_realtime else f"giá đóng cửa phiên gần nhất ({q.date})" if q.date else "giá đóng cửa phiên gần nhất"
    return f"📊 **{q.symbol}**: **{_fmt_price(q.price)} VND** ({time_note})\n{arrow} {sign}{_fmt_price(q.change)} ({sign}{q.change_pct}%) so với phiên trước ({_fmt_price(q.prev_close)} VND)"

async def quick_quote(symbol: str) -> str:
    q = await providers.fetch_quote(symbol.strip().upper())
    if q is None:
        return f"Em không lấy được giá {symbol} lúc này, anh thử lại sau ít phút nhé."
    return format_quote_message(q)

_GROUNDING_MAX_SYMBOLS = 5
async def build_price_grounding(symbols: list[str]) -> str:
    subset = symbols[:_GROUNDING_MAX_SYMBOLS]
    results = await asyncio.gather(*[providers.fetch_quote(s) for s in subset], return_exceptions=True)
    lines = []
    for sym, res in zip(subset, results):
        if isinstance(res, BaseException) or res is None: continue
        time_note = "khớp lệnh realtime" if res.is_realtime else (f"đóng cửa phiên {res.date}" if res.date else "đóng cửa phiên gần nhất")
        sign = "+" if res.change > 0 else ""
        lines.append(f"- {sym}: {_fmt_price(res.price)} VND ({time_note}), {sign}{_fmt_price(res.change)} ({sign}{res.change_pct}%) so với phiên trước ({_fmt_price(res.prev_close)} VND)")
    if not lines: return ""
    now = datetime.now(_VN_TZ)
    return f"[DỮ LIỆU GIÁ THỰC TẾ lúc {now:%H:%M %d/%m/%Y} giờ VN, lấy trực tiếp từ DNSE - đây là số liệu ĐÚNG duy nhất được phép dùng cho các mã dưới đây. TUYỆT ĐỐI KHÔNG tự suy diễn/bịa thêm số liệu nào khác ngoài danh sách này:\n" + "\n".join(lines) + "]"

@dataclass
class StockContext:
    symbol: str
    price: float
    fetched_at_vn: str
    stats: feat.SignalStats
    decision: policy.Decision
    enhanced: feat.EnhancedIndicators | None
    indicator_summary: str
    support_resistance: feat.SupportResistance | None
    key_levels: feat.KeyLevels | None
    ma_alignment: feat.MAAlignment | None
    sector_prompt: str
    fundamentals_prompt: str
    news: list[providers.NewsHeadline]
    relative_strength: float
    liquidity: feat.Liquidity | None
    quality: validation.DataQuality
    realtime_quote_line: str | None = None

async def _safe_sector_prompt(symbol: str) -> str:
    try:
        sector_keys = sector.get_symbol_sectors(symbol)
        if not sector_keys: return ""
        ctx = await sector.build_sector_context(sector_keys)
        return sector.build_sector_prompt_section(ctx, symbol)
    except Exception:
        return ""

async def _safe_fundamentals_prompt(symbol: str) -> str:
    try:
        bundle = await fundamentals.fetch_fundamentals(symbol)
        return fundamentals.build_fundamentals_prompt_section(bundle.valuation, bundle.foreign, symbol, foreign_trend=bundle.foreign_trend, growth=bundle.growth, events=bundle.events, sector_pe_avg=bundle.sector_pe_avg, sector_pe_sample=bundle.sector_pe_sample, sector_pe_label=bundle.sector_pe_label)
    except Exception:
        return ""

def _trend_pct(closes: list[float]) -> float:
    return ((closes[-1] - closes[0]) / closes[0]) * 100 if closes and closes[0] else 0.0

# Cùng bộ từ khoá lọc fact "danh mục" dùng bởi services/tools.py._tool_get_portfolio
# và scheduler.py._build_portfolio_digest - giữ 1 tiêu chí duy nhất cho khái
# niệm "fact nào được coi là thuộc danh mục đầu tư".
_PORTFOLIO_FACT_KEYWORDS = ("danh_muc", "portfolio", "co_phieu")

async def _is_holding_symbol(user_id: int | None, symbol: str) -> bool:
    """Đoán user có đang giữ `symbol` không, dựa trên fact danh mục đã lưu
    trong trí nhớ dài hạn - dùng để stock_policy.evaluate_policy() phân biệt
    HOLD (đang giữ, tín hiệu chưa đủ rõ thì giữ nguyên) với NO_TRADE/SELL
    (đang cân nhắc mở mới, không có gì để bán). Suy đoán có thể sai (chưa
    từng nhắc trong chat, hoặc đã bán nhưng chưa cập nhật trí nhớ) - chấp
    nhận được vì chỉ ảnh hưởng action/label hiển thị, không đổi ngưỡng
    confidence hay dữ liệu đầu vào."""
    if user_id is None:
        return False
    try:
        facts = await db.get_facts(user_id)
    except Exception:
        return False
    symbol_re = re.compile(rf"\b{re.escape(symbol)}\b", re.IGNORECASE)
    return any(
        symbol_re.search(value)
        for key, value in facts
        if any(kw in key for kw in _PORTFOLIO_FACT_KEYWORDS)
    )

async def build_context(symbol: str, *, user_id: int | None = None, is_holding: bool | None = None) -> StockContext | None:
    results = await asyncio.gather(
        providers.fetch_ohlcv(symbol, days=90), providers.fetch_ohlcv("VNINDEX", days=90),
        providers.fetch_quote(symbol), providers.fetch_news(symbol),
        _safe_sector_prompt(symbol), _safe_fundamentals_prompt(symbol),
        return_exceptions=True
    )
    for r in results:
        if isinstance(r, BaseException): raise r
    symbol_series, vnindex_series, quote, news, sector_prompt, fundamentals_prompt = results
    if not symbol_series.closes: return None

    quality = validation.validate_ohlcv(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes, symbol_series.dates)
    # analysis_price = close của phiên gần nhất trong CHUỖI OHLCV - toàn bộ
    # feature/policy (Donchian, Bollinger, S/R, session...) phải nhìn CÙNG
    # một thời điểm để không tự mâu thuẫn nhau (P0-3). quote.price là tick
    # realtime, có thể lệch pha với closes[-1] (vd trước giờ mở cửa, hoặc
    # cuối tuần) - chỉ dùng để HIỂN THỊ "giá khớp hiện tại", không đưa vào
    # tính toán stop/target/R:R.
    analysis_price = symbol_series.price
    realtime_quote_line = None
    if quote is not None:
        time_note = "khớp lệnh realtime" if quote.is_realtime else (f"đóng cửa phiên {quote.date}" if quote.date else "đóng cửa phiên gần nhất")
        realtime_quote_line = f"Giá khớp hiện tại: {_fmt_price(quote.price)} VND ({time_note}) - CHỈ tham khảo hiển thị, KHÔNG dùng để tính stop/target/R:R bên dưới."
    news_impact = providers.calc_news_impact(news)
    stats = feat.calc_signal_stats(symbol_series.closes, symbol_series.volumes, analysis_price)
    relative_strength = round(_trend_pct(symbol_series.closes) - _trend_pct(vnindex_series.closes), 2)

    enhanced, indicator_summary = None, ""
    if quality.usable and len(symbol_series.closes) >= 20:
        enhanced = feat.build_enhanced_indicators(symbol_series.closes, analysis_price, symbol_series.highs, symbol_series.lows)
        indicator_summary = feat.build_indicator_summary(enhanced, symbol)

    ma_alignment = feat.calc_ma_alignment(symbol_series.closes) if len(symbol_series.closes) >= 20 else None
    support_resistance = feat.calc_support_resistance(symbol_series.highs, symbol_series.lows, analysis_price, 30) if symbol_series.highs else None
    key_levels = feat.find_key_levels(symbol_series.highs, symbol_series.lows, symbol_series.closes) if symbol_series.highs else feat.KeyLevels([], [])
    liquidity = feat.calc_liquidity(symbol_series.volumes)
    session = feat.calc_session_metrics(symbol_series.closes, symbol_series.highs, symbol_series.lows, symbol_series.volumes)

    trend_score = feat.calc_trend_score(ma_alignment, stats.rsi14, enhanced.macd.histogram) if ma_alignment and ma_alignment.alignment != "unknown" and enhanced else None
    vnindex_multi_tf = feat.calc_multi_timeframe(vnindex_series.closes) if vnindex_series.closes else None
    vnindex_adx = feat.calc_adx(vnindex_series.closes, vnindex_series.highs, vnindex_series.lows) if vnindex_series.closes else None
    vnindex_distribution_days = feat.calc_distribution_days(vnindex_series.closes, vnindex_series.volumes)

    holding = is_holding if is_holding is not None else await _is_holding_symbol(user_id, symbol)
    decision = policy.evaluate_policy(policy.PolicyInputs(price=analysis_price, stats=stats, enhanced=enhanced, ma_alignment=ma_alignment, support_resistance=support_resistance, liquidity=liquidity, session=session, relative_strength=relative_strength, trend_score=trend_score, news_impact=news_impact, quality=quality, vnindex_multi_tf=vnindex_multi_tf, vnindex_adx=vnindex_adx, vnindex_distribution_days=vnindex_distribution_days, key_levels=key_levels, is_holding=holding))
    fetched_at_vn = datetime.now(_VN_TZ).strftime("%H:%M ngày %d/%m/%Y")

    return StockContext(symbol, analysis_price, fetched_at_vn, stats, decision, enhanced, indicator_summary, support_resistance, key_levels, ma_alignment, sector_prompt, fundamentals_prompt, news, relative_strength, liquidity, quality, realtime_quote_line)

def _fmt_price(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:,.0f}".replace(",", ".")

def _confidence_label(c: float) -> str:
    if c >= policy.CONFIDENCE_BUY_MIN: return "CAO"
    if c >= policy.CONFIDENCE_WATCH_MIN: return "TRUNG BÌNH"
    return "THẤP"

_ACTION_LABEL_VI = {"BUY": "🟢 MUA", "HOLD": "🟡 GIỮ", "WATCH": "🟡 THEO DÕI", "SELL": "🔴 BÁN/TRÁNH", "NO_TRADE": "⚪ ĐỨNG NGOÀI (NO_TRADE)"}
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=False)
_STOCK_PROMPT_TEMPLATE = _jinja_env.get_template("stock_analysis_prompt.j2")

def build_prompt(ctx: StockContext) -> str:
    d = ctx.decision
    sr = ctx.support_resistance
    support_resistance_line = f"Hỗ trợ/kháng cự (30 phiên): support {_fmt_price(sr.support)} ({sr.dist_to_support}%), resistance {_fmt_price(sr.resistance)} ({sr.dist_to_resistance}%)" if sr and sr.support else None
    ma = ctx.ma_alignment
    ma_alignment_line = f"MA alignment: {ma.alignment} (MA5={_fmt_price(ma.ma5)}, MA10={_fmt_price(ma.ma10)}, MA20={_fmt_price(ma.ma20)})" if ma and ma.alignment != "unknown" else None
    liq = ctx.liquidity
    liquidity_line = f"Thanh khoản: KL phiên gần nhất {liq.current_volume:,} so với TB 20 phiên {liq.avg_volume_20:,} ({liq.liquidity_ratio_pct}%)".replace(",", ".") if liq else None
    news = [{"tag": "🟢" if n.sentiment > 0.2 else ("🔴" if n.sentiment < -0.2 else "⚪"), "title": n.title, "source": n.source} for n in ctx.news[:5]]

    kl = ctx.key_levels
    key_levels_line = None
    if kl and (kl.supports or kl.resistances):
        parts = []
        if kl.supports:
            parts.append("Support: " + ", ".join(f"{_fmt_price(lv.price)} ({lv.touches} lần test)" for lv in kl.supports[:3]))
        if kl.resistances:
            parts.append("Resistance: " + ", ".join(f"{_fmt_price(lv.price)} ({lv.touches} lần test)" for lv in kl.resistances[:3]))
        key_levels_line = "Vùng giá quan trọng (swing pivot, 60 phiên): " + " | ".join(parts)

    trade_plan = None
    if d.trade_plan:
        tp = d.trade_plan
        trade_plan = {
            "entry_low": _fmt_price(tp.entry_low), "entry_high": _fmt_price(tp.entry_high),
            "stop": _fmt_price(tp.stop), "target1": _fmt_price(tp.target1),
            "target2": _fmt_price(tp.target2) if tp.target2 is not None else None,
            "position_size_pct": tp.position_size_pct, "plan_note": tp.plan_note,
        }
    scenarios = [{"name": s.name, "trigger": s.trigger, "action": s.action} for s in d.scenarios]
    backtest_stats_line = backtest.format_setup_stats_line(d.setup_type)

    return _STOCK_PROMPT_TEMPLATE.render(
        symbol=ctx.symbol, fetched_at_vn=ctx.fetched_at_vn, price=_fmt_price(ctx.price), action=d.action,
        target_price=_fmt_price(d.target_price) if d.target_price is not None else "",
        stop_price=_fmt_price(d.stop_price) if d.stop_price is not None else "", rr_ratio=d.rr_ratio,
        confidence=d.confidence, confidence_label=_confidence_label(d.confidence), setup_type=d.setup_type,
        market_regime=d.market_regime, risk_level=d.risk_level, data_quality=d.data_quality,
        reasons_text="; ".join(d.reasons[:6]) if d.reasons else "", invalidation_reason=d.invalidation_reason,
        trend_3m=ctx.stats.trend_3m, momentum=ctx.stats.momentum, volume_trend=ctx.stats.volume_trend,
        volatility=ctx.stats.volatility, rsi14=ctx.stats.rsi14 if ctx.stats.rsi14 is not None else "chưa đủ dữ liệu",
        relative_strength=ctx.relative_strength, indicator_summary=ctx.indicator_summary,
        support_resistance_line=support_resistance_line, ma_alignment_line=ma_alignment_line,
        liquidity_line=liquidity_line, liquidity_thin_warning=liq.is_thin if liq else False,
        sector_prompt=ctx.sector_prompt, fundamentals_prompt=ctx.fundamentals_prompt, news=news,
        realtime_quote_line=ctx.realtime_quote_line, key_levels_line=key_levels_line,
        trade_plan=trade_plan, scenarios=scenarios, backtest_stats_line=backtest_stats_line,
    )

def _fallback_text(ctx: StockContext) -> str:
    d = ctx.decision
    action_label = _ACTION_LABEL_VI.get(d.action, d.action)
    if d.action == "BUY": price_line = f"Vùng mua {_fmt_price(ctx.price)} | TP {_fmt_price(d.target_price)} | SL {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "SELL": price_line = f"KHÔNG phải vùng mua | Giá {_fmt_price(ctx.price)} | Nếu đang giữ: cân nhắc chốt/cắt lỗ quanh {_fmt_price(d.target_price)} | Tín hiệu SELL vô hiệu nếu giá vượt {_fmt_price(d.stop_price)}"
    elif d.action == "HOLD" and d.target_price is not None and d.stop_price is not None:
        price_line = f"Đang giữ, tín hiệu vẫn thuận lợi | Giá {_fmt_price(ctx.price)} | Tham khảo chốt lời {_fmt_price(d.target_price)} | Cân nhắc cắt lỗ dưới {_fmt_price(d.stop_price)} | R:R ~{d.rr_ratio}"
    elif d.action == "NO_TRADE": price_line = "Hệ thống chưa đủ edge để đề xuất vùng giá - ưu tiên đứng ngoài quan sát."
    else: price_line = f"Giá {_fmt_price(ctx.price)} | Chưa đủ rõ xu hướng để đề xuất vùng giá cụ thể"
    
    lines = [
        f"📊 **{ctx.symbol}** — **{_fmt_price(ctx.price)} VND** ({ctx.fetched_at_vn})",
        f"Tín hiệu: **{action_label}** (confidence {d.confidence}, setup {d.setup_type}, regime {d.market_regime})",
        price_line,
        f"RSI14 {ctx.stats.rsi14 if ctx.stats.rsi14 is not None else 'chưa đủ dữ liệu'} | Trend ~3M {ctx.stats.trend_3m}% | Risk: {d.risk_level}",
    ]
    if d.reasons: lines.append("Lý do: " + "; ".join(d.reasons[:4]))
    if d.invalidation_reason: lines.append(f"Lưu ý: {d.invalidation_reason}")
    if ctx.realtime_quote_line: lines.append(ctx.realtime_quote_line)
    if ctx.liquidity and ctx.liquidity.is_thin: lines.append("⚠️ Thanh khoản TB20 quá thấp.")
    if ctx.quality.status != "ok": lines.append(f"⚠️ Chất lượng dữ liệu: {ctx.quality.status}")
    lines.append("⚠️ API dự phòng không phản hồi nên đây là bản rút gọn.")
    return "\n".join(lines)

_STALE_NOTE = "\n\n⏱️ _Lưu ý: dữ liệu/thời điểm bên trên là của lần phân tích gần nhất_"

PORTFOLIO_KEYWORDS = ["cơ cấu", "co cau", "danh mục", "danh muc", "tỷ trọng", "ty trong", "giữ hay bán", "nên giữ mã nào"]

def wants_portfolio_analysis(text: str, symbols: list[str]) -> bool:
    if len(symbols) < 2: return False
    lower = text.lower()
    return any(kw in lower for kw in PORTFOLIO_KEYWORDS)

async def analyze_portfolio(symbols: list[str], user_text: str, *, user_id: int | None = None) -> str:
    # Dùng chung _is_holding_symbol với analyze_symbol thay vì gắn cứng
    # is_holding=True cho mọi mã - tránh 2 đường (phân tích đơn lẻ vs danh
    # mục) cho action khác nhau với cùng 1 mã khi user hỏi cả 2 kiểu.
    holdings = await asyncio.gather(*[_is_holding_symbol(user_id, sym) for sym in symbols])
    tasks = [build_context(sym, user_id=user_id, is_holding=holding) for sym, holding in zip(symbols, holdings)]
    contexts = await asyncio.gather(*tasks, return_exceptions=True)
    valid_contexts = [ctx for ctx in contexts if not isinstance(ctx, BaseException) and ctx is not None]
    if not valid_contexts:
        return "Em không lấy được dữ liệu của các mã này lúc này, anh thử lại sau xíu nha."

    combined_data = []
    for ctx in valid_contexts:
        d = ctx.decision
        sr_line = f"Hỗ trợ: {_fmt_price(ctx.support_resistance.support)} | Kháng cự: {_fmt_price(ctx.support_resistance.resistance)}" if ctx.support_resistance else "Không rõ"
        trend_line = f"RSI: {ctx.stats.rsi14} | Trend 3M: {ctx.stats.trend_3m}%"
        combined_data.append(f"Mã {ctx.symbol}: Giá {_fmt_price(ctx.price)} | Tín hiệu hệ thống: {d.action} (Độ tin cậy: {d.confidence}) | {trend_line} | {sr_line}")

    data_text = "\n".join(combined_data)
    prompt = (
        f"[DỮ LIỆU KỸ THUẬT DANH MỤC LÚC NÀY]:\n{data_text}\n\n"
        f"[CÂU HỎI TỪ NGƯỜI DÙNG]:\n\"{user_text}\"\n\n"
        f"Lan Anh hãy đóng vai broker chuyên nghiệp tư vấn CƠ CẤU DANH MỤC. "
        f"So sánh sức mạnh các mã, khuyên mã nào nên giữ/gồng lãi, mã nào vi phạm kỹ thuật cần hạ tỷ trọng/cắt lỗ. "
        f"Văn phong: Ngọt ngào, đồng cảm, xưng em/anh tự nhiên, rõ ràng."
    )
    
    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        result = (response.text or "").strip()
        if result and getattr(response, "used_fallback", False): result += "\n\n⚙️ API"
        return result
    except Exception:
        logger.exception("Lỗi khi tổng hợp danh mục")
        return "Em đang gặp chút sự cố khi phân tích danh mục, anh chờ chút thử lại nha."

async def analyze_symbol(symbol: str, user_text: str = "", *, force_refresh: bool = False, user_id: int | None = None) -> str:
    symbol = symbol.strip().upper()
    holding = await _is_holding_symbol(user_id, symbol)
    if not force_refresh and not user_text:
        cached = _cache_get(symbol, holding)
        if cached: return cached + _STALE_NOTE

    try:
        ctx = await build_context(symbol, user_id=user_id, is_holding=holding)
    except Exception:
        logger.exception("Lỗi lấy dữ liệu phân tích %s", symbol)
        ctx = None
    if ctx is None: return messages.STOCK_FETCH_ERROR.format(symbol=symbol)

    prompt = build_prompt(ctx)
    if user_text:
        prompt += (
            f"\n\n[LƯU Ý QUAN TRỌNG TỪ HỆ THỐNG]:\n"
            f"Người dùng vừa hỏi: \"{user_text}\"\n"
            f"Lan Anh hãy phân tích kỹ thuật ở trên, ĐỒNG THỜI phải trả lời trực tiếp "
            f"vào tình huống này của anh ấy (tính mức lời/lỗ, đồng cảm, "
            f"hướng xử lý cụ thể dựa trên Action đã chốt). Nhớ giữ giọng điệu Lan Anh!"
        )

    from ai import orchestrator
    try:
        response = await orchestrator.ask(prompt)
        text = (response.text or "").strip()
        result = text or _fallback_text(ctx)
        if text and getattr(response, "used_fallback", False): result += "\n\n⚙️ API"
    except Exception:
        logger.exception("Gemini lỗi khi phân tích %s", symbol)
        result = _fallback_text(ctx)

    if not user_text:
        _cache_set(symbol, holding, result)
        
    return result
