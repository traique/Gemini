"""Kiểm tra chuỗi giá lịch sử đã được điều chỉnh sau chia tách/cổ tức chưa.

Vì sao cần module này: OhlcvSeries.is_adjusted tồn tại từ đầu nhưng KHÔNG có
provider nào gán giá trị, nên hệ thống hoàn toàn không biết chuỗi giá đang dùng
là giá đã điều chỉnh hay giá thô. Tệ hơn, fetch_ohlcv failover giữa hai nguồn
(DNSE và vnstock/VCI); hai nguồn này có thể khác chính sách điều chỉnh, nên cùng
một mã có thể đổi hệ quy chiếu giá giữa hai lần phân tích mà không có dấu hiệu.

Nếu chuỗi chưa điều chỉnh, ngày giao dịch không hưởng quyền tạo ra một gap GIẢ
trong dữ liệu: SMA50, kênh Donchian 20 phiên, ATR14 và trend ~3 tháng đều bị méo
trong khi báo cáo vẫn nói như thể số liệu sạch.

Hệ thống không có nguồn dữ liệu corporate action, nên cách xác định duy nhất
đáng tin là so gap với BIÊN ĐỘ giá tối đa một phiên của thị trường VN: HOSE ±7%,
HNX ±10%, UPCOM ±15%, phiên chào sàn/giao dịch trở lại ±20%. Gap vượt xa các mức
đó không thể là biến động giao dịch bình thường.

Module này KHÔNG sửa bất kỳ con số nào của stock/policy.py - chỉ phát hiện gap và
sinh cảnh báo cho phần diễn giải.
"""

from dataclasses import dataclass

from stock import report_format as rfmt

# Ngưỡng nghi vấn: trên biên độ UPCOM (15%) một chút thì gần như không còn cách
# giải thích nào bằng giao dịch thường; để 12% để bắt cả trường hợp gap của phiên
# chào sàn/giao dịch trở lại.
SUSPECT_GAP_PCT = 12.0
# Ngưỡng chắc chắn: vượt xa mọi biên độ, kể cả phiên chào sàn (±20%).
CERTAIN_GAP_PCT = 25.0
_RATIO_TOLERANCE_PCT = 1.5
_MAX_GAPS_IN_NOTE = 2

# (nhãn tiếng Việt, % giảm lý thuyết của giá tham chiếu trong ngày GDKHQ)
_COMMON_RATIOS = (
    ("cổ phiếu thưởng/chia tỷ lệ 1:1", 50.0),
    ("chia tỷ lệ 1:2", 33.33),
    ("cổ phiếu thưởng tỷ lệ 1:3", 25.0),
    ("cổ phiếu thưởng tỷ lệ 1:4", 20.0),
    ("cổ phiếu thưởng tỷ lệ 1:5", 16.67),
    ("cổ phiếu thưởng tỷ lệ 1:6", 14.29),
)


@dataclass
class PriceGap:
    """Một bước nhảy giá close-to-close vượt biên độ giao dịch cho phép."""

    date: str
    prev_close: float
    close: float
    move_pct: float
    level: str
    ratio_hint: str = ""


@dataclass
class PriceAudit:
    """Kết quả soi chuỗi giá: is_adjusted=False nghĩa là CHẮC CHẮN chưa điều chỉnh,
    None nghĩa là không xác định được (không có gap bất thường nào để suy ra)."""

    is_adjusted: bool | None
    gaps: list[PriceGap]
    note: str


def _ratio_hint(move_pct: float) -> str:
    if move_pct >= 0:
        return ""
    drop = abs(move_pct)
    for label, theoretical in _COMMON_RATIOS:
        if abs(drop - theoretical) <= _RATIO_TOLERANCE_PCT:
            return label
    return ""


def detect_price_gaps(
    closes,
    dates=None,
    *,
    suspect_pct: float = SUSPECT_GAP_PCT,
    certain_pct: float = CERTAIN_GAP_PCT,
) -> list[PriceGap]:
    """Liệt kê các bước nhảy close-to-close vượt ngưỡng nghi vấn.

    Chỉ so hai close liền nhau: gap do corporate action luôn nằm giữa hai phiên
    liền kề, và đây là dạng méo dữ liệu duy nhất có thể phát hiện được mà không
    cần nguồn dữ liệu quyền ngoài.
    """
    gaps: list[PriceGap] = []
    if not closes or len(closes) < 2:
        return gaps
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        current = closes[index]
        if not previous or previous <= 0 or not current or current <= 0:
            continue
        move_pct = round((current - previous) / previous * 100, 2)
        magnitude = abs(move_pct)
        if magnitude < suspect_pct:
            continue
        level = "certain" if magnitude >= certain_pct else "suspect"
        date = dates[index] if dates and index < len(dates) else ""
        gaps.append(
            PriceGap(
                date=date,
                prev_close=previous,
                close=current,
                move_pct=move_pct,
                level=level,
                ratio_hint=_ratio_hint(move_pct),
            )
        )
    return gaps


def infer_is_adjusted(closes, dates=None) -> bool | None:
    """Trả False khi chắc chắn chuỗi CHƯA điều chỉnh, None khi không kết luận được.

    Không bao giờ trả True: không có gap bất thường KHÔNG chứng minh chuỗi đã
    điều chỉnh (mã có thể chưa từng chia tách trong cửa sổ dữ liệu). Nói "đã
    điều chỉnh" khi không có bằng chứng cũng nguy hiểm như nói ngược lại.
    """
    for gap in detect_price_gaps(closes, dates):
        if gap.level == "certain":
            return False
    return None


def _describe(gap: PriceGap) -> str:
    when = f"phiên {gap.date}" if gap.date else "một phiên trong chuỗi giá"
    direction = "giảm" if gap.move_pct < 0 else "tăng"
    text = (
        f"{when} {direction} {rfmt.fmt_number(abs(gap.move_pct))}% so với phiên trước "
        f"({rfmt.fmt_price(gap.prev_close)} -> {rfmt.fmt_price(gap.close)} VND)"
    )
    if gap.ratio_hint:
        text = f"{text}, khớp với {gap.ratio_hint}"
    return text


def build_note(symbol: str, source: str, gaps) -> str:
    """Cảnh báo tiếng Việt để chèn vào prompt phân tích. Rỗng khi chuỗi sạch."""
    if not gaps:
        return ""
    certain = [gap for gap in gaps if gap.level == "certain"]
    selected = certain or list(gaps)
    described = "; ".join(_describe(gap) for gap in selected[:_MAX_GAPS_IN_NOTE])
    first_date = next((gap.date for gap in selected if gap.date), "")
    scope = ""
    if first_date:
        scope = (
            f" Mọi mốc giá TRƯỚC ngày {first_date} không còn cùng hệ quy chiếu với giá "
            "hiện tại, không được dùng làm hỗ trợ/kháng cự."
        )
    source_text = source or "không rõ"
    if certain:
        return (
            f"⚠️ DỮ LIỆU GIÁ CHƯA ĐIỀU CHỈNH ({symbol}, nguồn {source_text}): {described}. "
            "Mức này vượt biên độ tối đa một phiên của mọi sàn VN (HOSE 7%, HNX 10%, "
            "UPCOM 15%) nên gần như chắc chắn là ngày giao dịch không hưởng quyền "
            "(chia tách/cổ tức/cổ phiếu thưởng) mà chuỗi giá chưa được điều chỉnh lại. "
            "Hệ quả: SMA50, kênh Donchian 20 phiên, ATR14 và trend ~3 tháng đang bị méo, "
            f"KHÔNG được dùng làm cơ sở kết luận mạnh.{scope} PHẢI nêu rõ hạn chế dữ liệu "
            "này trong phần rủi ro."
        )
    return (
        f"⚠️ NGHI VẤN ĐIỀU CHỈNH GIÁ ({symbol}, nguồn {source_text}): {described}. "
        "Có thể là biến động thật (trần/sàn liên tiếp) nhưng cũng có thể là ngày giao "
        "dịch không hưởng quyền chưa được điều chỉnh. Khi diễn giải SMA50, Donchian 20, "
        "ATR14 và trend ~3 tháng phải nói rõ độ tin cậy bị giảm vì lý do này."
    )


def audit_series(symbol: str, source: str, closes, dates=None) -> PriceAudit:
    gaps = detect_price_gaps(closes, dates)
    is_adjusted = infer_is_adjusted(closes, dates)
    return PriceAudit(is_adjusted=is_adjusted, gaps=gaps, note=build_note(symbol, source, gaps))
