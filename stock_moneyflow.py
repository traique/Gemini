"""Dòng tiền (Money Flow) - port từ src/lib/server/money-flow.ts.

Ghi chú quan trọng (giữ nguyên tinh thần bản gốc): KHÔNG bịa số "khối ngoại mua/bán
X tỷ VND" - nguồn số liệu khối ngoại THẬT (SSI iBoard) trong bản gốc hiện cũng
CHƯA khả dụng (luôn trả rỗng), nên cả 2 bản đều dùng "volume proxy": suy ra tín
hiệu tích lũy/phân phối từ CMF (Chaikin Money Flow) + volume spike trên chính
dữ liệu OHLCV DNSE - và luôn ghi rõ đây là ước lượng, KHÔNG phải số liệu khối
ngoại thật.

Market breadth (% mã tăng/giảm toàn thị trường) trong bản gốc cần một watchlist
nhiều mã để tính - không áp dụng khi phân tích 1 mã đơn lẻ nên được bỏ qua ở đây.
"""
from dataclasses import dataclass


def calc_cmf(closes: list[float], highs: list[float], lows: list[float], volumes: list[float], period: int = 20) -> float:
    length = min(len(closes), len(highs), len(lows), len(volumes), period)
    if length < 5:
        return 0.0
    start = len(closes) - length
    sum_mfv = sum_vol = 0.0
    for i in range(start, len(closes)):
        h, l, c, v = highs[i], lows[i], closes[i], volumes[i]
        hl = h - l
        if hl > 0 and v > 0:
            mfm = ((c - l) - (h - c)) / hl
            sum_mfv += mfm * v
            sum_vol += v
    return round(sum_mfv / sum_vol, 3) if sum_vol > 0 else 0.0


def detect_volume_spikes(closes: list[float], volumes: list[float]) -> tuple[int, int]:
    if len(closes) < 21 or len(volumes) < 21:
        return 0, 0
    ma20vol = sum(volumes[-21:-1]) / 20
    bullish = bearish = 0
    lookback = min(10, len(closes) - 1)
    for i in range(len(closes) - lookback, len(closes)):
        if volumes[i] > ma20vol * 1.5:
            if closes[i] >= closes[i - 1]:
                bullish += 1
            else:
                bearish += 1
    return bullish, bearish


@dataclass
class ForeignFlow:
    signal: str  # strong_buy | buy | neutral | sell | strong_sell
    cmf: float
    volume_spikes: tuple[int, int]
    note: str


def analyze_money_flow(closes: list[float], highs: list[float], lows: list[float], volumes: list[float]) -> ForeignFlow | None:
    if not closes or not volumes:
        return None
    cmf = calc_cmf(closes, highs, lows, volumes, 20)
    spikes = detect_volume_spikes(closes, volumes)

    if cmf > 0.15:
        signal, desc = "strong_buy", f"Tích lũy mạnh — CMF {cmf} ({spikes[0]} phiên vol spike tăng giá)"
    elif cmf > 0.05:
        signal, desc = "buy", f"Tích lũy nhẹ — CMF {cmf} (volume vào khi giá tăng)"
    elif cmf > -0.05:
        signal, desc = "neutral", f"Trung tính — CMF {cmf} (volume cân bằng)"
    elif cmf > -0.15:
        signal, desc = "sell", f"Phân phối nhẹ — CMF {cmf} ({spikes[1]} phiên vol spike giảm giá)"
    else:
        signal, desc = "strong_sell", f"Phân phối mạnh — CMF {cmf} (xả hàng trên volume cao)"

    return ForeignFlow(signal, cmf, spikes, f"[Ước lượng từ volume — KHÔNG phải số liệu khối ngoại] {desc}")


def calc_obv(closes: list[float], volumes: list[float]) -> float:
    if len(closes) < 2 or len(volumes) < 2:
        return 0.0
    length = min(len(closes), len(volumes))
    obv = 0.0
    for i in range(1, length):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
    return obv


def calc_obv_trend(closes: list[float], volumes: list[float], lookback: int = 10) -> float:
    if len(closes) < lookback + 2:
        return 0.0
    recent_obv = calc_obv(closes, volumes)
    past_obv = calc_obv(closes[: len(closes) - lookback], volumes[: len(volumes) - lookback])
    if past_obv == 0:
        return 0.0
    return round((recent_obv - past_obv) / abs(past_obv) * 100, 1)


def calc_mfi(closes: list[float], volumes: list[float], highs: list[float] | None = None, lows: list[float] | None = None, period: int = 14) -> float:
    if len(closes) < period + 1 or len(volumes) < period + 1:
        return 50.0
    length = min(len(closes), len(volumes))
    has_ohlc = bool(highs) and bool(lows) and len(highs) >= length and len(lows) >= length

    def tp(i: int) -> float:
        return (highs[i] + lows[i] + closes[i]) / 3 if has_ohlc else closes[i]

    pos_flow = neg_flow = 0.0
    for i in range(length - period, length):
        tp_curr, tp_prev = tp(i), tp(i - 1)
        money_flow = tp_curr * (volumes[i] or 0)
        if tp_curr > tp_prev:
            pos_flow += money_flow
        elif tp_curr < tp_prev:
            neg_flow += money_flow

    if pos_flow == 0 and neg_flow == 0:
        return 50.0
    if neg_flow == 0:
        return 100.0
    if pos_flow == 0:
        return 0.0
    mfr = pos_flow / neg_flow
    return round(100 - 100 / (1 + mfr), 1)


def build_money_flow_prompt_section(foreign: ForeignFlow | None, obv_trend: float, mfi: float, symbol: str) -> str:
    lines = [f"[DÒNG TIỀN — {symbol}]"]
    if foreign:
        emoji = "🟢" if "buy" in foreign.signal else ("🔴" if "sell" in foreign.signal else "🟡")
        lines.append(f"{emoji} Tích lũy/Phân phối (ước lượng từ volume — KHÔNG phải khối ngoại): {foreign.note}")
    else:
        lines.append("⚪ Dòng tiền: không có dữ liệu")

    obv_note = (
        f"OBV tăng {obv_trend}% → tích lũy" if obv_trend > 10
        else (f"OBV giảm {obv_trend}% → phân phối" if obv_trend < -10 else f"OBV ổn định ({obv_trend}%)")
    )
    mfi_note = (
        f"MFI {mfi} (overbought)" if mfi > 75
        else (f"MFI {mfi} (oversold — tiền chưa vào hết)" if mfi < 25 else f"MFI {mfi} (trung tính)")
    )
    lines.append(f"📊 Volume flow: {obv_note} | {mfi_note}")
    return "\n".join(lines)
