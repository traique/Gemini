"""Chỉ báo kỹ thuật thuần toán học trên close[]/high[]/low[]/volume[] - port từ repo stock-portfolio."""
from dataclasses import dataclass, field


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─── RSI (Wilder) ────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
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


def calc_bollinger(closes: list[float], price: float) -> BollingerResult:
    period = 20
    if len(closes) < period:
        return BollingerResult(price * 1.05, price, price * 0.95, 5.0, 50.0, False)
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
        round(clamp(pct_b, 0, 100), 1), width < 5,
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


def calc_adx(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, period: int = 14) -> ADXResult:
    if len(closes) < period * 2:
        return ADXResult(20.0, 15.0, 15.0, False)

    has_real = bool(highs) and bool(lows) and len(highs) == len(closes) and len(lows) == len(closes)
    H = highs if has_real else [closes[0]] + [max(c, closes[i - 1]) for i, c in enumerate(closes[1:], 1)]
    L = lows if has_real else [closes[0]] + [min(c, closes[i - 1]) for i, c in enumerate(closes[1:], 1)]

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
    adx = sum(valid_dx[-period:]) / period if len(valid_dx) >= period else 20.0
    di_plus = di_plus_series[-1] if di_plus_series else 15.0
    di_minus = di_minus_series[-1] if di_minus_series else 15.0

    return ADXResult(round(adx, 1), round(di_plus, 1), round(di_minus, 1), adx > 25)


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


def calc_trend_score(ma_align: MAAlignment, rsi14: float, macd_histogram: float) -> int:
    score = 50
    if ma_align.alignment == "bullish":
        score += 20
    elif ma_align.alignment == "bearish":
        score -= 20
    elif ma_align.ma5 > ma_align.ma10 or ma_align.ma10 > ma_align.ma20:
        score += 5
    else:
        score -= 5

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

    if ind.adx.trending:
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
    bb_note = " 🔥 SQUEEZE (sắp breakout)" if bb.squeeze else ""
    lines.append(f"BB: %B={bb.pct_b}% | width={bb.width}%{bb_note}")
    cross_parts = []
    if cross.golden_cross:
        cross_parts.append("⭐ GOLDEN CROSS")
    if cross.death_cross:
        cross_parts.append("💀 DEATH CROSS")
    cross_parts.append("above SMA20" if cross.above_sma20 else "below SMA20")
    cross_parts.append("above SMA50" if cross.above_sma50 else "below SMA50")
    lines.append("SMA: " + " | ".join(cross_parts))
    lines.append(f"ADX: {adx.adx} ({'xu hướng mạnh' if adx.trending else 'sideway'}) | +DI {adx.di_plus} vs -DI {adx.di_minus}")
    return "\n".join(lines)


# ─── Decision (score-based) - port từ decideAction() trong ai/technical.ts ──

@dataclass
class SignalStats:
    trend_3m: float
    volatility: float
    momentum: float
    volume_trend: float
    rsi14: float
    suggested_tp: float
    suggested_sl: float


def round_price(v: float) -> float:
    return round(v / 10) * 10


def calc_signal_stats(closes: list[float], volumes: list[float], price: float, news_impact: float) -> SignalStats:
    if not closes or price <= 0:
        return SignalStats(0, 2, 0, 0, 50, round_price(price * 1.05), round_price(price * 0.97))

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

    risk = clamp(volatility, 3, 8)
    news_boost = clamp(news_impact, -1, 1)
    rsi_adj = -0.3 if rsi14 > 70 else (0.3 if rsi14 < 30 else 0)
    reward_mult = clamp(1.5 + news_boost * 0.5 + rsi_adj, 0.8, 2.5) if trend_3m >= 0 else 1.0
    suggested_tp = round_price(price * (1 + (risk * reward_mult) / 100))
    suggested_sl = round_price(price * (1 - risk / 100))

    return SignalStats(
        round(trend_3m, 2), round(volatility, 2), round(momentum, 2), round(volume_trend, 2),
        round(rsi14, 1), suggested_tp, suggested_sl,
    )


def decide_action(trend_3m: float, momentum: float, volume_trend: float, news_impact: float, volatility: float, rsi14: float, relative_strength: float) -> tuple[str, str, str]:
    """Trả về (action, confidence, reason) - port từ decideAction()."""
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
    if rsi14 < 30:
        score += 1
    elif rsi14 > 70:
        score -= 1
    if relative_strength > 5:
        score += 1
    elif relative_strength < -5:
        score -= 1

    if score >= 4:
        rsi_note = ", RSI cao — cân nhắc chờ điều chỉnh nhẹ" if rsi14 > 65 else ""
        return "BUY", "HIGH", f"Xu hướng tăng mạnh, momentum và khối lượng xác nhận, outperform VNINDEX{rsi_note}"
    if score >= 2:
        return "BUY", "MEDIUM", ("Xu hướng tăng hình thành, RSI vùng oversold — cơ hội bắt đáy" if rsi14 < 35 else "Xu hướng tăng đang hình thành, chờ thêm xác nhận khối lượng")
    if score in (0, 1):
        return "HOLD", "MEDIUM", "Tín hiệu trung tính, vị thế hiện tại ổn — theo dõi thêm"
    if score == -1:
        return "WATCH", "LOW", ("Tín hiệu yếu, underperform VNINDEX — chưa nên vào mới" if relative_strength < -5 else "Tín hiệu yếu, chưa rõ xu hướng — chờ xác nhận")
    if score <= -4:
        return "SELL", "HIGH", f"Xu hướng giảm mạnh{', RSI oversold — nếu gồng thì đặt SL chặt' if rsi14 < 35 else ', momentum và dòng tiền đều xác nhận'}"
    return "SELL", "MEDIUM", ("Xu hướng yếu, RSI chưa về vùng hỗ trợ — cân nhắc cắt lỗ một phần" if rsi14 > 60 else "Xu hướng yếu, cân nhắc cắt lỗ hoặc chờ tín hiệu đảo chiều")
