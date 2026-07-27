"""Chỉ báo kỹ thuật thuần toán học trên close[]/high[]/low[]/volume[] - port từ repo stock-portfolio."""
from dataclasses import dataclass, field


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─── RSI (Wilder) ────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Trả None khi chưa đủ dữ liệu - KHÔNG bịa giá trị 50 (trung tính) trông
    như một chỉ báo thật, vì nó sẽ chảy vào scoring như thể có dữ liệu."""
    if len(closes) < period + 1:
        return None
    avg_gain = avg_loss = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            avg_gain += diff
        else:
            avg_loss += abs(diff)
    avg_gain /= period
    avg_loss /= period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff >= 0 else 0
        loss = abs(diff) if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


# ─── Momentum (hồi quy tuyến tính) ───────────────────────────────────────────

def calc_momentum_slope(closes: list[float], period: int = 10) -> float:
    s = closes[-period:]
    n = len(s)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(s) / n
    num = sum((i - x_mean) * (s[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return (num / den / s[-1]) * 100 if den and s[-1] else 0.0


# ─── SMA / EMA ───────────────────────────────────────────────────────────────

def calc_sma(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    sl = closes[-period:]
    return sum(sl) / period


def _ema_series(closes: list[float], period: int) -> list[float]:
    if not closes:
        return []
    alpha = 2 / (period + 1)
    if len(closes) < period:
        seed = sum(closes) / len(closes)
        return [seed] * len(closes)
    result = [0.0] * len(closes)
    ema = sum(closes[:period]) / period
    for i in range(period):
        result[i] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * alpha + ema * (1 - alpha)
        result[i] = ema
    return result


def calc_ema(closes: list[float], period: int) -> float:
    series = _ema_series(closes, period)
    return series[-1] if series else 0.0


# ─── MACD(12,26,9) ───────────────────────────────────────────────────────────

@dataclass
class MACDResult:
    macd_line: float = 0.0
    signal_line: float = 0.0
    histogram: float = 0.0
    crossover: str = "none"  # bullish | bearish | none


def calc_macd(closes: list[float]) -> MACDResult:
    if len(closes) < 26:
        return MACDResult()
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_series = [a - b for a, b in zip(ema12, ema26)]
    valid_macd = macd_series[26:]
    signal_series = _ema_series(valid_macd, 9)

    macd_line = macd_series[-1] if macd_series else 0.0
    signal_line = signal_series[-1] if signal_series else 0.0
    histogram = macd_line - signal_line

    crossover = "none"
    if len(signal_series) >= 2:
        prev_hist = macd_series[-2] - signal_series[-2]
        if prev_hist < 0 and histogram > 0:
            crossover = "bullish"
        if prev_hist > 0 and histogram < 0:
            crossover = "bearish"

    return MACDResult(round(macd_line, 2), round(signal_line, 2), round(histogram, 2), crossover)


# ─── Bollinger Bands(20, 2σ) ─────────────────────────────────────────────────

@dataclass
class BollingerResult:
    upper: float
    middle: float
    lower: float
    width: float
    pct_b: float
    squeeze: bool
    available: bool = True


def calc_bollinger(closes: list[float], price: float) -> BollingerResult:
    """Khi lịch sử ngắn hơn period, KHÔNG bịa dải theo %price - trả về
    available=False để tầng scoring/hiển thị biết đây không phải dữ liệu thật."""
    period = 20
    if len(closes) < period:
        return BollingerResult(price, price, price, 0.0, 50.0, False, available=False)
    sl = closes[-period:]
    middle = sum(sl) / period
    variance = sum((v - middle) ** 2 for v in sl) / period
    std_dev = variance ** 0.5
    upper = middle + 2 * std_dev
    lower = middle - 2 * std_dev
    width = ((upper - lower) / middle) * 100 if middle > 0 else 5.0
    pct_b = ((price - lower) / (upper - lower)) * 100 if upper != lower else 50.0
    return BollingerResult(
        round(upper), round(middle), round(lower), round(width, 2),
        round(clamp(pct_b, 0, 100), 1), width < 5, available=True,
    )


# ─── Multi-timeframe trend ───────────────────────────────────────────────────

@dataclass
class MultiTimeframe:
    trend_1w: float
    trend_1m: float
    trend_3m: float
    alignment: str  # bullish | bearish | mixed


def calc_multi_timeframe(closes: list[float]) -> MultiTimeframe:
    def pct(n: int) -> float:
        if len(closes) < n + 1:
            return 0.0
        past = closes[-1 - n]
        curr = closes[-1]
        return ((curr - past) / past) * 100 if past > 0 else 0.0

    t1w, t1m, t3m = pct(5), pct(22), pct(min(len(closes) - 1, 65) if closes else 0)
    vals = [t1w, t1m, t3m]
    if all(v > 0 for v in vals):
        alignment = "bullish"
    elif all(v < 0 for v in vals):
        alignment = "bearish"
    else:
        alignment = "mixed"
    return MultiTimeframe(round(t1w, 2), round(t1m, 2), round(t3m, 2), alignment)


# ─── SMA cross (golden/death) ────────────────────────────────────────────────

@dataclass
class CrossSignal:
    golden_cross: bool
    death_cross: bool
    above_sma20: bool
    above_sma50: bool


def calc_cross_signal(closes: list[float]) -> CrossSignal:
    price = closes[-1] if closes else 0.0
    if len(closes) < 52:
        return CrossSignal(False, False, False, False)
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)
    window = 3
    golden = death = False
    for i in range(len(closes) - window, len(closes) - 1):
        s20p = calc_sma(closes[: i + 1], 20)
        s50p = calc_sma(closes[: i + 1], 50)
        s20c = calc_sma(closes[: i + 2], 20)
        s50c = calc_sma(closes[: i + 2], 50)
        if s20p <= s50p and s20c > s50c:
            golden = True
        if s20p >= s50p and s20c < s50c:
            death = True
    return CrossSignal(golden, death, price > sma20, price > sma50)


# ─── ADX(14) ──────────────────────────────────────────────────────────────────

@dataclass
class ADXResult:
    adx: float
    di_plus: float
    di_minus: float
    trending: bool
    available: bool = True


def calc_adx(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, period: int = 14) -> ADXResult:
    """Chỉ tính ADX khi có high/low THẬT (không tự tổng hợp H/L từ close - số
    bịa trông như chỉ báo thật sẽ chảy vào scoring một cách âm thầm). Nếu
    thiếu dữ liệu (không đủ phiên hoặc không có H/L thật) -> available=False,
    và code gọi (score_enhanced_indicators) phải loại ADX khỏi điểm số."""
    has_real = bool(highs) and bool(lows) and len(highs) == len(closes) and len(lows) == len(closes)
    if len(closes) < period * 2 or not has_real:
        return ADXResult(0.0, 0.0, 0.0, False, available=False)

    H, L = highs, lows
    trs, dm_plus, dm_minus = [], [], []
    for i in range(1, len(closes)):
        high, low, prev_close = H[i], L[i], closes[i - 1]
        prev_high, prev_low = H[i - 1], L[i - 1]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up_move = high - prev_high
        down_move = prev_low - low
        dm_plus.append(up_move if (up_move > down_move and up_move > 0) else 0)
        dm_minus.append(down_move if (down_move > up_move and down_move > 0) else 0)

    def smooth(arr: list[float]) -> list[float]:
        res = [0.0] * len(arr)
        if len(arr) < period:
            return res
        res[period - 1] = sum(arr[:period])
        for i in range(period, len(arr)):
            res[i] = res[i - 1] - res[i - 1] / period + arr[i]
        return res

    atr = smooth(trs)
    sdm_plus = smooth(dm_plus)
    sdm_minus = smooth(dm_minus)

    di_plus_series = [(v / atr[i] * 100) if atr[i] > 0 else 0 for i, v in enumerate(sdm_plus)]
    di_minus_series = [(v / atr[i] * 100) if atr[i] > 0 else 0 for i, v in enumerate(sdm_minus)]
    dx_series = []
    for i, v in enumerate(di_plus_series):
        s = v + di_minus_series[i]
        dx_series.append((abs(v - di_minus_series[i]) / s * 100) if s > 0 else 0)

    valid_dx = dx_series[period - 1:]
    if len(valid_dx) < period:
        return ADXResult(0.0, 0.0, 0.0, False, available=False)
    adx = sum(valid_dx[-period:]) / period
    di_plus = di_plus_series[-1] if di_plus_series else 0.0
    di_minus = di_minus_series[-1] if di_minus_series else 0.0

    return ADXResult(round(adx, 1), round(di_plus, 1), round(di_minus, 1), adx > 25, available=True)


# ─── Support/Resistance, Bias MA, MA alignment, trend score (mozy lessons) ──

@dataclass
class SupportResistance:
    support: float | None
    resistance: float | None
    dist_to_support: float
    dist_to_resistance: float


def calc_support_resistance(highs: list[float], lows: list[float], price: float, lookback: int = 30) -> SupportResistance:
    length = min(len(highs), len(lows), lookback)
    if length == 0:
        return SupportResistance(None, None, 0.0, 0.0)
    recent_highs = highs[-length:]
    recent_lows = lows[-length:]
    resistance = max(recent_highs)
    support = min(recent_lows)
    dist_support = round((price - support) / support * 100, 1) if support > 0 else 0.0
    dist_resistance = round((price - resistance) / resistance * 100, 1) if resistance > 0 else 0.0
    return SupportResistance(round(support), round(resistance), dist_support, dist_resistance)


@dataclass
class BiasMA:
    bias: float
    status: str  # nguy_hiem | canh_giac | an_toan | chiet_khau | qua_ban


def calc_bias_ma(price: float, ma: float) -> BiasMA:
    if ma <= 0:
        return BiasMA(0.0, "an_toan")
    bias = round((price - ma) / ma * 100, 2)
    if bias > 8:
        status = "nguy_hiem"
    elif bias > 5:
        status = "canh_giac"
    elif bias < -8:
        status = "qua_ban"
    elif bias < -5:
        status = "chiet_khau"
    else:
        status = "an_toan"
    return BiasMA(bias, status)


@dataclass
class MAAlignment:
    ma5: float
    ma10: float
    ma20: float
    alignment: str  # bullish | bearish | mixed | unknown
    is_bullish: bool | None


def calc_ma_alignment(closes: list[float]) -> MAAlignment:
    if len(closes) < 20:
        return MAAlignment(0, 0, 0, "unknown", None)
    ma5, ma10, ma20 = calc_sma(closes, 5), calc_sma(closes, 10), calc_sma(closes, 20)
    if ma5 > ma10 > ma20:
        alignment, is_bullish = "bullish", True
    elif ma5 < ma10 < ma20:
        alignment, is_bullish = "bearish", False
    else:
        alignment, is_bullish = "mixed", None
    return MAAlignment(round(ma5), round(ma10), round(ma20), alignment, is_bullish)


def calc_trend_score(ma_align: MAAlignment, rsi14: float | None, macd_histogram: float) -> int:
    score = 50
    if ma_align.alignment == "bullish":
        score += 20
    elif ma_align.alignment == "bearish":
        score -= 20
    elif ma_align.ma5 > ma_align.ma10 or ma_align.ma10 > ma_align.ma20:
        score += 5
    else:
        score -= 5

    if rsi14 is not None:
        if rsi14 > 70:
            score -= 8
        elif rsi14 < 30:
            score += 8
        elif rsi14 > 55:
            score += 5
        elif rsi14 < 45:
            score -= 5

    score += 5 if macd_histogram > 0 else -5
    return int(max(0, min(100, round(score))))


# ─── Enhanced indicators bundle + scoring + summary text ────────────────────

@dataclass
class EnhancedIndicators:
    macd: MACDResult
    bollinger: BollingerResult
    multi_tf: MultiTimeframe
    cross: CrossSignal
    adx: ADXResult
    sma20: float
    sma50: float
    ema9: float


def build_enhanced_indicators(closes: list[float], price: float, highs: list[float] | None = None, lows: list[float] | None = None) -> EnhancedIndicators:
    return EnhancedIndicators(
        macd=calc_macd(closes),
        bollinger=calc_bollinger(closes, price),
        multi_tf=calc_multi_timeframe(closes),
        cross=calc_cross_signal(closes),
        adx=calc_adx(closes, highs, lows),
        sma20=round(calc_sma(closes, 20)),
        sma50=round(calc_sma(closes, 50)),
        ema9=round(calc_ema(closes, 9)),
    )


def score_enhanced_indicators(ind: EnhancedIndicators) -> int:
    score = 0
    if ind.macd.crossover == "bullish":
        score += 2
    elif ind.macd.crossover == "bearish":
        score -= 2
    elif ind.macd.histogram > 0:
        score += 1
    elif ind.macd.histogram < 0:
        score -= 1

    # Bollinger chỉ tính điểm khi có đủ dữ liệu thật (không phải dải bịa).
    if ind.bollinger.available:
        if ind.bollinger.pct_b < 10:
            score += 1
        if ind.bollinger.pct_b > 90:
            score -= 1
        if ind.bollinger.squeeze and ind.macd.histogram > 0:
            score += 1

    if ind.multi_tf.alignment == "bullish":
        score += 1
    if ind.multi_tf.alignment == "bearish":
        score -= 1

    if ind.cross.golden_cross:
        score += 1
    if ind.cross.death_cross:
        score -= 1

    # ADX chỉ tính điểm khi có dữ liệu H/L thật (không phải giá trị mặc định bịa).
    if ind.adx.available and ind.adx.trending:
        score += 1 if ind.adx.di_plus > ind.adx.di_minus else -1

    return score


def build_indicator_summary(ind: EnhancedIndicators, symbol: str) -> str:
    tf, macd, bb, cross, adx = ind.multi_tf, ind.macd, ind.bollinger, ind.cross, ind.adx
    lines = []
    lines.append(
        f"Multi-TF [{symbol}]: 1W {'+' if tf.trend_1w > 0 else ''}{tf.trend_1w}% | "
        f"1M {'+' if tf.trend_1m > 0 else ''}{tf.trend_1m}% | "
        f"3M {'+' if tf.trend_3m > 0 else ''}{tf.trend_3m}% -> {tf.alignment.upper()}"
    )
    cross_note = f" ⚡ {macd.crossover.upper()} CROSSOVER" if macd.crossover != "none" else ""
    lines.append(f"MACD: line {'+' if macd.macd_line > 0 else ''}{macd.macd_line} | hist {'+' if macd.histogram > 0 else ''}{macd.histogram}{cross_note}")
    if bb.available:
        bb_note = " 🔥 SQUEEZE (sắp breakout)" if bb.squeeze else ""
        lines.append(f"BB: %B={bb.pct_b}% | width={bb.width}%{bb_note}")
    else:
        lines.append("BB: chưa đủ dữ liệu (cần tối thiểu 20 phiên)")
    cross_parts = []
    if cross.golden_cross:
        cross_parts.append("⭐ GOLDEN CROSS")
    if cross.death_cross:
        cross_parts.append("💀 DEATH CROSS")
    cross_parts.append("above SMA20" if cross.above_sma20 else "below SMA20")
    cross_parts.append("above SMA50" if cross.above_sma50 else "below SMA50")
    lines.append("SMA: " + " | ".join(cross_parts))
    if adx.available:
        lines.append(f"ADX: {adx.adx} ({'xu hướng mạnh' if adx.trending else 'sideway'}) | +DI {adx.di_plus} vs -DI {adx.di_minus}")
    else:
        lines.append("ADX: chưa đủ dữ liệu H/L thật")
    return "\n".join(lines)


# ─── Thanh khoản: volume hiện tại vs trung bình 20 phiên ────────────────────

@dataclass
class Liquidity:
    avg_volume_20: float
    current_volume: float
    liquidity_ratio_pct: float  # current vs avg20, 100 = bằng trung bình
    is_thin: bool  # thanh khoản quá thấp - khuyến nghị mua/bán gần như vô nghĩa vì khó vào/ra


def calc_liquidity(volumes: list[float], min_avg_volume: float = 100_000) -> Liquidity | None:
    """So khối lượng khớp phiên gần nhất với trung bình 20 phiên.

    Đây là cảnh báo broker luôn nêu đầu tiên: mã thanh khoản thấp thì
    "mua/bán" gần như vô nghĩa vì không đủ đối ứng để vào/ra ở khối lượng
    đáng kể. `min_avg_volume` là ngưỡng tuyệt đối (cổ phiếu/phiên) để gắn cờ
    is_thin - 100k là ước lượng thô cho midcap/penny, có thể chỉnh theo khẩu vị.
    """
    if not volumes:
        return None
    window = volumes[-20:] if len(volumes) >= 20 else volumes
    if not window:
        return None
    avg20 = sum(window) / len(window)
    current = volumes[-1]
    ratio = (current / avg20) * 100 if avg20 > 0 else 0.0
    return Liquidity(
        avg_volume_20=round(avg20),
        current_volume=round(current),
        liquidity_ratio_pct=round(ratio, 1),
        is_thin=avg20 < min_avg_volume,
    )


# ─── Decision (score-based) - port từ decideAction() trong ai/technical.ts ──

@dataclass
class SignalStats:
    trend_3m: float
    volatility: float
    momentum: float
    volume_trend: float
    rsi14: float | None


def round_price(v: float) -> float:
    return round(v / 10) * 10


def calc_signal_stats(closes: list[float], volumes: list[float], price: float, news_impact: float) -> SignalStats:
    if not closes or price <= 0:
        return SignalStats(0, 2, 0, 0, None)

    first, last = closes[0], closes[-1]
    trend_3m = ((last - first) / first) * 100 if first else 0
    momentum = calc_momentum_slope(closes)
    rsi14 = calc_rsi(closes)

    volume_trend = 0.0
    if len(volumes) >= 5:
        avg_vol = sum(volumes) / len(volumes)
        recent_vol = sum(volumes[-5:]) / 5
        volume_trend = ((recent_vol - avg_vol) / avg_vol) * 100 if avg_vol > 0 else 0

    returns = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        volatility = (max(variance, 0) ** 0.5) * 100 * (5 ** 0.5)
    else:
        volatility = 2.0

    return SignalStats(
        round(trend_3m, 2), round(volatility, 2), round(momentum, 2), round(volume_trend, 2),
        round(rsi14, 1) if rsi14 is not None else None,
    )




# ─── Intraday/session risk guardrails ────────────────────────────────────────

@dataclass
class SessionRisk:
    daily_change_pct: float
    close_position_pct: float  # 0 = đóng sát đáy phiên, 100 = đóng sát đỉnh phiên
    volume_ratio_pct: float
    is_selloff: bool
    is_near_floor_like: bool
    is_distribution: bool
    is_close_near_low: bool
    hard_no_buy: bool
    penalty: int
    reasons: list[str]


def calc_session_risk(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    enhanced: EnhancedIndicators | None = None,
) -> SessionRisk:
    """Nhận diện các phiên rủi ro cao để CHẶN BUY sai.

    Bản cũ coi volume tăng là tích cực trong mọi hoàn cảnh. Với cổ phiếu Việt Nam,
    volume bùng nổ trong phiên giảm mạnh/đóng sát đáy thường là phân phối hoặc
    force-sell, không phải tín hiệu gom hàng. Hàm này tạo guardrail cứng để các
    phiên kiểu GEX gần sàn không bị nâng lên BUY chỉ vì 3M còn dương hoặc RSI thấp.
    """
    reasons: list[str] = []
    penalty = 0

    if len(closes) < 2:
        return SessionRisk(0.0, 50.0, 100.0, False, False, False, False, False, 0, reasons)

    price = closes[-1]
    prev_close = closes[-2]
    daily_change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    high = highs[-1] if highs else price
    low = lows[-1] if lows else price
    if high > low:
        close_position_pct = (price - low) / (high - low) * 100
    else:
        close_position_pct = 50.0

    avg20 = 0.0
    volume_ratio_pct = 100.0
    if volumes:
        prior = volumes[-21:-1] if len(volumes) >= 21 else volumes[:-1]
        if prior:
            avg20 = sum(prior) / len(prior)
            volume_ratio_pct = volumes[-1] / avg20 * 100 if avg20 > 0 else 100.0

    is_selloff = daily_change_pct <= -4.0
    # Không biết sàn HOSE/HNX/UPCOM ở tầng này, dùng ngưỡng bảo thủ: <= -6% coi như gần sàn/đạp mạnh.
    is_near_floor_like = daily_change_pct <= -6.0
    is_close_near_low = close_position_pct <= 25.0
    is_distribution = daily_change_pct <= -2.5 and volume_ratio_pct >= 150.0

    if is_near_floor_like:
        penalty -= 5
        reasons.append(f"giảm rất mạnh {daily_change_pct:.2f}% trong phiên (gần sàn/đạp mạnh)")
    elif is_selloff:
        penalty -= 3
        reasons.append(f"giảm mạnh {daily_change_pct:.2f}% trong phiên")

    if is_distribution:
        penalty -= 4
        reasons.append(f"volume {volume_ratio_pct:.1f}% so với trung bình 20 phiên trong phiên giảm — dấu hiệu phân phối")

    if is_close_near_low and daily_change_pct < 0:
        penalty -= 2
        reasons.append(f"đóng cửa sát đáy phiên (vị trí close {close_position_pct:.1f}% trong biên độ ngày)")

    if enhanced is not None:
        below_sma20 = not enhanced.cross.above_sma20
        below_sma50 = not enhanced.cross.above_sma50
        macd_bad = enhanced.macd.crossover == "bearish" or enhanced.macd.histogram < 0
        adx_weak = enhanced.adx.available and enhanced.adx.adx < 15
        di_bad = enhanced.adx.available and enhanced.adx.di_minus > enhanced.adx.di_plus

        if below_sma20 and below_sma50:
            penalty -= 2
            reasons.append("giá nằm dưới cả SMA20 và SMA50")
        if macd_bad:
            penalty -= 2
            reasons.append("MACD đang âm/bearish")
        if adx_weak:
            penalty -= 1
            reasons.append("ADX yếu, chưa có xu hướng tăng đáng tin cậy")
        if di_bad:
            penalty -= 1
            reasons.append("-DI lớn hơn +DI, lực bán đang chiếm ưu thế")

    hard_no_buy = (
        is_near_floor_like
        or (is_selloff and is_distribution)
        or (is_distribution and is_close_near_low)
        or (enhanced is not None and (not enhanced.cross.above_sma20) and (not enhanced.cross.above_sma50)
            and (enhanced.macd.histogram < 0 or enhanced.macd.crossover == "bearish"))
    )

    return SessionRisk(
        daily_change_pct=round(daily_change_pct, 2),
        close_position_pct=round(close_position_pct, 1),
        volume_ratio_pct=round(volume_ratio_pct, 1),
        is_selloff=is_selloff,
        is_near_floor_like=is_near_floor_like,
        is_distribution=is_distribution,
        is_close_near_low=is_close_near_low,
        hard_no_buy=hard_no_buy,
        penalty=penalty,
        reasons=reasons,
    )


def apply_session_risk_guardrails(action: str, confidence: str, reason: str, risk: SessionRisk | None) -> tuple[str, str, str]:
    """Hạ/khóa tín hiệu mua khi phiên hiện tại có dấu hiệu phân phối/breakdown."""
    if risk is None:
        return action, confidence, reason

    if risk.hard_no_buy and action == "BUY":
        short = "; ".join(risk.reasons[:3])
        return "WATCH", "LOW", f"Không mở mua mới: {short}. Chờ cân bằng lại/phiên xác nhận hồi phục."

    if risk.penalty <= -7 and action == "HOLD":
        short = "; ".join(risk.reasons[:2])
        return "WATCH", "LOW", f"Rủi ro ngắn hạn cao: {short}. Ưu tiên quan sát, chưa giải ngân mới."

    if risk.penalty <= -9 and action in ("WATCH", "HOLD"):
        short = "; ".join(risk.reasons[:2])
        return "SELL", "MEDIUM", f"Tín hiệu kỹ thuật xấu đi rõ: {short}. Nếu đang giữ cần siết quản trị rủi ro."

    return action, confidence, reason


def build_session_risk_summary(risk: SessionRisk | None) -> str:
    if risk is None:
        return ""
    parts = [
        f"Biến động phiên gần nhất: {risk.daily_change_pct}%",
        f"vị trí đóng cửa trong biên độ ngày: {risk.close_position_pct}%",
        f"volume vs TB20: {risk.volume_ratio_pct}%",
    ]
    flags = []
    if risk.is_near_floor_like:
        flags.append("GẦN SÀN/ĐẠP MẠNH")
    elif risk.is_selloff:
        flags.append("GIẢM MẠNH")
    if risk.is_distribution:
        flags.append("PHÂN PHỐI")
    if risk.is_close_near_low:
        flags.append("ĐÓNG SÁT ĐÁY")
    if risk.hard_no_buy:
        flags.append("KHÓA BUY")
    if flags:
        parts.append("Cờ rủi ro: " + ", ".join(flags))
    if risk.reasons:
        parts.append("Lý do guardrail: " + "; ".join(risk.reasons))
    return "Session risk: " + " | ".join(parts)


# ─── Vùng giá theo action (BUY khác SELL khác WATCH/HOLD) ────────────────────

@dataclass
class PriceTargets:
    mode: str  # "buy" | "exit" | "watch" - QUYẾT ĐỊNH cách diễn giải ở tầng prompt/text
    price: float
    target: float
    invalidation: float


def calc_price_targets(
    price: float, volatility: float, trend_3m: float, rsi14: float | None, news_impact: float, action: str,
) -> PriceTargets:
    """Vùng giá PHỤ THUỘC action - KHÔNG dùng chung 1 công thức cho mọi tín hiệu.

    - BUY: target = TP phía trên, invalidation = SL phía dưới (đúng logic mua).
    - SELL: KHÔNG phải vùng mua. target = mốc giá nếu xu hướng giảm tiếp tục (dưới giá
      hiện tại, dành cho ai đang cân nhắc chốt lời/cắt lỗ nếu đang nắm giữ), invalidation
      = mốc phía trên mà nếu giá vượt lên thì tín hiệu SELL coi như bị vô hiệu.
    - HOLD/WATCH: chưa đủ rõ xu hướng để đề xuất vùng giá giao dịch cụ thể.
    """
    if price <= 0:
        return PriceTargets("watch", price, price, price)

    risk = clamp(volatility, 3, 8)
    news_boost = clamp(news_impact, -1, 1)
    if rsi14 is None:
        rsi_adj = 0
    else:
        rsi_adj = -0.3 if rsi14 > 70 else (0.3 if rsi14 < 30 else 0)
    reward_mult = clamp(1.5 + news_boost * 0.5 + rsi_adj, 0.8, 2.5) if trend_3m >= 0 else 1.0

    if action == "BUY":
        target = round_price(price * (1 + (risk * reward_mult) / 100))
        invalidation = round_price(price * (1 - risk / 100))
        return PriceTargets("buy", price, target, invalidation)

    if action == "SELL":
        target = round_price(price * (1 - (risk * reward_mult) / 100))
        invalidation = round_price(price * (1 + risk / 100))
        return PriceTargets("exit", price, target, invalidation)

    return PriceTargets("watch", price, price, price)


def decide_action(trend_3m: float, momentum: float, volume_trend: float, news_impact: float, volatility: float, rsi14: float | None, relative_strength: float) -> tuple[str, str, str]:
    """Trả về (action, confidence, reason) - port từ decideAction().
    rsi14 có thể là None (chưa đủ dữ liệu) -> mọi so sánh RSI được bỏ qua
    thay vì ngầm coi như 50 (trung tính), tránh lệch điểm/lý do một cách âm thầm."""
    score = 0
    if trend_3m > 5:
        score += 2
    elif trend_3m < -5:
        score -= 2
    if momentum > 0.2:
        score += 2
    elif momentum < -0.2:
        score -= 2
    if volume_trend > 10:
        score += 1
    if news_impact > 0.5:
        score += 1
    elif news_impact < -0.5:
        score -= 1
    if volatility > 15:
        score -= 1
    if rsi14 is not None:
        if rsi14 < 30:
            score += 1
        elif rsi14 > 70:
            score -= 1
    if relative_strength > 5:
        score += 1
    elif relative_strength < -5:
        score -= 1

    rsi_high = rsi14 is not None and rsi14 > 65
    rsi_low = rsi14 is not None and rsi14 < 35
    rsi_mid_high = rsi14 is not None and rsi14 > 60

    if score >= 4:
        rsi_note = ", RSI cao — cân nhắc chờ điều chỉnh nhẹ" if rsi_high else ""
        return "BUY", "HIGH", f"Xu hướng tăng mạnh, momentum và khối lượng xác nhận, outperform VNINDEX{rsi_note}"
    if score >= 2:
        return "BUY", "MEDIUM", ("Xu hướng tăng hình thành, RSI vùng oversold — cơ hội bắt đáy" if rsi_low else "Xu hướng tăng đang hình thành, chờ thêm xác nhận khối lượng")
    if score in (0, 1):
        return "HOLD", "MEDIUM", "Tín hiệu trung tính, vị thế hiện tại ổn — theo dõi thêm"
    if score in (-1, -2):
        return "WATCH", "LOW", ("Tín hiệu yếu, underperform VNINDEX — chưa nên vào mới" if relative_strength < -5 else "Tín hiệu yếu, chưa rõ xu hướng — chờ xác nhận")
    if score <= -4:
        return "SELL", "HIGH", f"Xu hướng giảm mạnh{', RSI oversold — nếu gồng thì đặt SL chặt' if rsi_low else ', momentum và dòng tiền đều xác nhận'}"
    return "SELL", "MEDIUM", ("Xu hướng yếu, RSI chưa về vùng hỗ trợ — cân nhắc cắt lỗ một phần" if rsi_mid_high else "Xu hướng yếu, cân nhắc cắt lỗ hoặc chờ tín hiệu đảo chiều")
