"""Data layer - validate OHLCV trước khi đưa vào feature/policy.

Nguyên tắc: KHÔNG âm thầm coi dữ liệu thiếu/stale/bất thường như dữ liệu
tốt. Mọi vấn đề phải lộ ra thành DataQuality.status + reasons để tầng policy
(Gate B) quyết định NO_TRADE khi cần, thay vì để feature/policy tính toán
trên dữ liệu rác mà không ai biết.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_OUTLIER_DAILY_MOVE_PCT = 35.0
MIN_BARS_HARD_FLOOR = 20
DEFAULT_MIN_BARS = 30
DEFAULT_MAX_STALE_CALENDAR_DAYS = 9


@dataclass
class DataQuality:
    status: str
    reasons: list[str] = field(default_factory=list)
    bars_available: int = 0
    is_stale: bool = False
    has_outlier: bool = False
    has_duplicate_dates: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def usable(self) -> bool:
        return self.status != "bad"


def _parse_date(d: str) -> date | None:
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def validate_ohlcv(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    dates: list[str],
    *,
    min_bars: int = DEFAULT_MIN_BARS,
    max_stale_days: int = DEFAULT_MAX_STALE_CALENDAR_DAYS,
    now: datetime | None = None,
) -> DataQuality:
    reasons: list[str] = []
    if not closes:
        return DataQuality(status="bad", reasons=["không có dữ liệu giá"], bars_available=0)

    n = len(closes)
    is_stale = False
    has_outlier = False
    has_duplicate = False

    if dates:
        parsed = [_parse_date(d) for d in dates]
        valid_dates = [d for d in parsed if d is not None]
        if len(valid_dates) != len(set(valid_dates)):
            has_duplicate = True
            reasons.append("phát hiện ngày trùng lặp trong chuỗi giá")
        if not all(valid_dates[i] <= valid_dates[i + 1] for i in range(len(valid_dates) - 1)):
            reasons.append("chuỗi ngày không tăng dần - không ngầm định provider đã sort")
        last_date = max(valid_dates) if valid_dates else None
        if last_date is not None:
            today = (now or datetime.now(_VN_TZ)).date()
            age_days = (today - last_date).days
            if age_days > max_stale_days:
                is_stale = True
                reasons.append(f"dữ liệu cũ - phiên gần nhất cách đây {age_days} ngày")

    for i in range(1, n):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        move_pct = abs((closes[i] - prev) / prev * 100)
        if move_pct > _OUTLIER_DAILY_MOVE_PCT:
            has_outlier = True
            reasons.append(f"biến động bất thường {move_pct:.1f}% giữa 2 phiên liên tiếp - nghi ngờ lỗi dữ liệu")
            break

    if n < MIN_BARS_HARD_FLOOR:
        reasons.append(f"chỉ có {n} phiên - dưới ngưỡng tối thiểu {MIN_BARS_HARD_FLOOR} để tính chỉ báo")
        return DataQuality(status="bad", reasons=reasons, bars_available=n, is_stale=is_stale, has_outlier=has_outlier, has_duplicate_dates=has_duplicate)
    if has_outlier or has_duplicate:
        return DataQuality(status="bad" if has_duplicate else "degraded", reasons=reasons, bars_available=n, is_stale=is_stale, has_outlier=has_outlier, has_duplicate_dates=has_duplicate)
    if n < min_bars:
        reasons.append(f"chỉ có {n} phiên - dưới mức khuyến nghị {min_bars} để tin cậy cao")
        return DataQuality(status="degraded", reasons=reasons, bars_available=n, is_stale=is_stale, has_outlier=has_outlier, has_duplicate_dates=has_duplicate)
    if is_stale:
        return DataQuality(status="degraded", reasons=reasons, bars_available=n, is_stale=is_stale, has_outlier=has_outlier, has_duplicate_dates=has_duplicate)
    return DataQuality(status="ok", reasons=[], bars_available=n)
