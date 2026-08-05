"""Định dạng số và làm sạch báo cáo phân tích cổ phiếu.

Tách khỏi stock/analysis.py để tầng logic thuần hàm (không I/O, không LLM) có
thể unit test trực tiếp. Đây là chốt cuối chặn các lỗi TRÌNH BÀY đã lọt ra tin
nhắn thật ngày 05/08/2026 và có thể khiến người đọc hiểu sai mức độ rủi ro:

- Mốc hỗ trợ/kháng cự nêu ra mà không kèm khoảng cách % so với giá hiện tại,
  nên mốc cách 25% trông ngang hàng với mốc cách 2%.
- MACD histogram in bằng đồng tuyệt đối (vd +24.44 trên cổ phiếu 14.000đ chỉ
  là 0,17% giá) đọc như một tín hiệu rất mạnh.
- ADX in trơ số, không nói xu hướng đang nghiêng lên hay nghiêng xuống, nên
  ADX 43,7 của một mã đang giảm bị diễn giải thành "xu hướng mạnh" tích cực.
- Đoạn "đây chỉ là tham khảo" bị in hai lần ở cuối cùng một báo cáo.
- Số trộn lẫn dấu chấm/phẩy thập phân trong cùng một tin nhắn.
- Tin Google News lấy theo chuỗi "<mã> cổ phiếu" nhưng không kiểm tra mã có
  trong tiêu đề, nên tin của mã khác bị gán cho mã đang phân tích - và tệ hơn,
  sentiment của tin đó chảy vào news_impact của tầng policy.
"""

import math
import re
from datetime import timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Ngưỡng "mốc giá còn ý nghĩa giao dịch". Xa hơn mức này thì mốc chỉ còn giá
# trị tham chiếu dài hạn, không dùng làm điểm vào/ra trong vài phiên tới.
NEAR_LEVEL_MAX_PCT = 7.0


def fmt_price(value: float | None) -> str:
    """70370 -> "70.370". Chuẩn VN: dấu chấm phân cách nghìn."""
    if value is None:
        return "N/A"
    return f"{value:,.0f}".replace(",", ".")


def fmt_number(value: float | None, decimals: int = 2) -> str:
    """1234.56 -> "1.234,56". Dấu chấm nghìn, dấu PHẨY thập phân."""
    if value is None:
        return "N/A"
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def fmt_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{fmt_number(value, decimals)}%"


def fmt_signed_pct(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value > 0 else ""
    return f"{sign}{fmt_number(value, decimals)}%"


def level_distance_pct(price: float | None, level: float | None) -> float | None:
    """Khoảng cách từ GIÁ tới MỐC, tính theo % giá hiện tại.

    Dương = mốc nằm TRÊN giá (kháng cự), âm = mốc nằm DƯỚI giá (hỗ trợ). Lấy
    giá hiện tại làm mẫu số vì đây là con số người đọc dùng để ước lượng lãi/lỗ
    từ vị thế của họ ngay lúc này.
    """
    if not price or price <= 0 or not level or level <= 0:
        return None
    return round((level - price) / price * 100, 2)


def format_level(price: float, level: float, touches: int | None = None) -> str:
    """Một mốc giá LUÔN đi kèm khoảng cách % - không bao giờ in số trơ."""
    details = []
    dist = level_distance_pct(price, level)
    if dist is not None:
        details.append(f"{fmt_signed_pct(dist, 1)} so với giá")
    if touches:
        details.append(f"{touches} lần test")
    text = fmt_price(level)
    if details:
        text += " (" + ", ".join(details) + ")"
    return text


def _nearest(levels: list[tuple[float, int]], price: float, above: bool) -> tuple[float, int] | None:
    side = [lv for lv in levels if (lv[0] > price if above else lv[0] < price)]
    if not side:
        return None
    return min(side, key=lambda lv: abs(lv[0] - price))


def _level_note(dist: float, atr_pct: float | None) -> str:
    """Cảnh báo hai chiều cho một mốc: quá xa để giao dịch, hoặc quá gần so
    với biên động bình thường một phiên (ATR) nên rất dễ bị xuyên bởi nhiễu.
    """
    if abs(dist) > NEAR_LEVEL_MAX_PCT:
        return f" - cách quá xa (>{fmt_number(NEAR_LEVEL_MAX_PCT, 0)}%), KHÔNG dùng làm điểm vào/ra ngắn hạn"
    if atr_pct and abs(dist) < atr_pct:
        return f" - nằm trong biên nhiễu một phiên (ATR {fmt_pct(atr_pct)}), dễ bị xuyên qua mà chưa đổi xu hướng"
    return ""


def nearest_levels_line(
    price: float,
    supports: list[tuple[float, int]],
    resistances: list[tuple[float, int]],
    atr_pct: float | None = None,
) -> str:
    """Dòng bắt buộc nêu MỐC GẦN NHẤT hai phía kèm khoảng cách %.

    find_key_levels chỉ lấy swing pivot trong 60 phiên, nên mốc gần nhất có thể
    cách giá 20-30%. Lúc đó phải nói thẳng là không có mốc nào đủ gần, thay vì
    để người đọc tưởng đó là vùng mua/bán khả thi trong vài phiên tới.
    """
    parts = []
    near_support = _nearest(supports, price, above=False)
    near_resistance = _nearest(resistances, price, above=True)
    if near_support:
        dist = level_distance_pct(price, near_support[0])
        note = _level_note(dist, atr_pct) if dist is not None else ""
        parts.append(f"Hỗ trợ gần nhất {format_level(price, near_support[0], near_support[1])}{note}")
    else:
        parts.append("Hỗ trợ gần nhất: không tìm được mốc swing nào dưới giá hiện tại")
    if near_resistance:
        dist = level_distance_pct(price, near_resistance[0])
        note = _level_note(dist, atr_pct) if dist is not None else ""
        level_text = format_level(price, near_resistance[0], near_resistance[1])
        parts.append(f"Kháng cự gần nhất {level_text}{note}")
    else:
        parts.append("Kháng cự gần nhất: không tìm được mốc swing nào trên giá hiện tại")
    return "MỐC GẦN NHẤT (dùng mốc này khi nói về điểm vào/ra): " + " | ".join(parts)


def macd_strength_line(macd_line: float, histogram: float, price: float | None) -> str:
    """MACD quy về % giá - số tuyệt đối vô nghĩa khi so giữa các mã.

    +24.44 nghe rất mạnh, nhưng trên cổ phiếu 14.000đ nó chỉ là 0,17% giá.
    """
    if not price or price <= 0:
        return f"MACD: line {fmt_number(macd_line)} | hist {fmt_number(histogram)} (chưa quy đổi được theo % giá)"
    hist_pct = histogram / price * 100
    line_pct = macd_line / price * 100
    abs_hist = abs(hist_pct)
    if abs_hist < 0.2:
        strength = "rất yếu, gần như không đáng kể"
    elif abs_hist < 0.5:
        strength = "yếu"
    elif abs_hist < 1.0:
        strength = "trung bình"
    else:
        strength = "mạnh"
    return (
        f"MACD quy theo % giá: line {fmt_signed_pct(line_pct)} | "
        f"histogram {fmt_signed_pct(hist_pct)} giá -> độ mạnh {strength}. "
        f"Bắt buộc mô tả MACD theo % giá này, KHÔNG nêu số tuyệt đối "
        f"({fmt_number(histogram)}) như thể là biên độ lớn."
    )


def adx_direction_line(
    adx: float,
    di_plus: float,
    di_minus: float,
    trending: bool,
    available: bool = True,
) -> str:
    """ADX chỉ đo ĐỘ MẠNH, hướng do +DI/-DI quyết định.

    Thiếu câu này, ADX 43,7 của một mã đang giảm bị diễn giải thành "xu hướng
    tương đối mạnh" theo nghĩa tích cực.
    """
    if not available:
        return "ADX: chưa đủ dữ liệu H/L thật -> KHÔNG được kết luận xu hướng mạnh hay yếu."
    if di_plus > di_minus:
        direction = "nghiêng TĂNG (+DI > -DI)"
    elif di_minus > di_plus:
        direction = "nghiêng GIẢM (-DI > +DI)"
    else:
        direction = "không rõ hướng (+DI = -DI)"
    strength = "có xu hướng rõ" if trending else "sideway, chưa có xu hướng rõ"
    return (
        f"ADX {fmt_number(adx, 1)}: {strength}, {direction} "
        f"(+DI {fmt_number(di_plus, 1)} vs -DI {fmt_number(di_minus, 1)}). "
        f"ADX chỉ đo ĐỘ MẠNH của xu hướng, PHẢI nêu hướng theo +DI/-DI, "
        f"tuyệt đối không mặc định ADX cao là tín hiệu tăng."
    )


def title_mentions_symbol(title: str, symbol: str) -> bool:
    """Tiêu đề có nhắc ĐÚNG mã này không (so khớp theo biên từ, không substring).

    "Cổ phiếu FPT tăng trần" -> True. "Nhóm VN30 đồng loạt tăng trần" -> False,
    dù Google News vẫn trả tin đó cho truy vấn "FPT cổ phiếu".
    """
    if not title or not symbol:
        return False
    pattern = rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])"
    return re.search(pattern, title, re.IGNORECASE) is not None


def relevant_news_impact(items: list[tuple[str, float]], symbol: str) -> float:
    """news_impact CHỈ tính trên tin có nhắc đúng mã.

    providers.calc_news_impact lấy trung bình sentiment của MỌI tin Google News
    trả về, kể cả tin của mã khác. Điểm đó là một đầu vào của tầng policy, nên
    tin không liên quan đang tác động trực tiếp tới khuyến nghị mua/bán.
    """
    relevant = [s for title, s in items if title_mentions_symbol(title, symbol)]
    if not relevant:
        return 0.0
    avg = sum(relevant) / len(relevant)
    return max(-2.0, min(2.0, avg * math.log(len(relevant) + 1)))


def fmt_news_date(raw: str) -> str:
    """pubDate dạng RFC822 -> "05/08/2026". Trả "" nếu không parse được."""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_VN_TZ).strftime("%d/%m/%Y")


_SELF_INTRO_RE = re.compile(
    r"^\s*(?:anh\s*ơi[,!.\s]*)?em\s+lan\s+anh\s+(?:đây|xin\s+chào)[^.!?\n]*[.!?]\s*",
    re.IGNORECASE,
)
_PET_NAME_RE = re.compile(r"\banh\s+yêu\b", re.IGNORECASE)
_TASK_DONE_RE = re.compile(
    r"^\s*nhiệm vụ[^.!?\n]*(?:xong|hoàn thành)[^.!?\n]*[.!?]\s*$",
    re.IGNORECASE,
)
_DISCLAIMER_HINTS = (
    "chỉ là tham khảo",
    "mang tính tham khảo",
    "để tham khảo",
    "không phải khuyến nghị",
    "không phải là khuyến nghị",
    "khuyến nghị đầu tư",
    "tự chịu trách nhiệm",
    "quyết định cuối cùng",
)
# Đoạn dài thì gần như chắc chắn là nội dung phân tích có lồng câu nhắc nhở,
# không phải đoạn disclaimer thuần - không được xoá kẻo mất nội dung thật.
_DISCLAIMER_MAX_LEN = 400


def _is_disclaimer(paragraph: str) -> bool:
    if len(paragraph) > _DISCLAIMER_MAX_LEN:
        return False
    lower = paragraph.lower()
    return any(hint in lower for hint in _DISCLAIMER_HINTS)


def clean_analysis_output(text: str) -> str:
    """Chốt cuối trước khi gửi báo cáo cho người dùng.

    Bỏ câu tự giới thiệu, bỏ danh xưng thân mật quá đà trong báo cáo tiền thật,
    bỏ câu "nhiệm vụ của em xong rồi", và giữ ĐÚNG MỘT đoạn disclaimer. Cả
    prompt và hàm này đều canh việc chỉ có một disclaimer: prompt là lớp chính,
    hàm này là lớp chặn tất định vì LLM sinh ra không ổn định (cùng một prompt,
    FPT bị lặp hai lần còn CII thì không).
    """
    if not text:
        return text
    cleaned = _SELF_INTRO_RE.sub("", text.strip(), count=1)
    cleaned = _PET_NAME_RE.sub("anh", cleaned)
    separator = "\n\n" if "\n\n" in cleaned else "\n"
    if separator == "\n\n":
        blocks = re.split(r"\n\s*\n", cleaned)
    else:
        blocks = cleaned.split("\n")
    result: list[str] = []
    seen: set[str] = set()
    disclaimer_kept = False
    for block in blocks:
        para = block.strip()
        if not para:
            continue
        if _TASK_DONE_RE.match(para):
            continue
        key = re.sub(r"\W+", "", para.lower())
        if key and key in seen:
            continue
        if _is_disclaimer(para):
            if disclaimer_kept:
                continue
            disclaimer_kept = True
        if key:
            seen.add(key)
        result.append(para)
    return separator.join(result).strip()
