"""KQKD / lịch công bố BCTC - port từ src/lib/server/earnings-analyzer.ts.

Bản gốc có 2 phần: (1) ước tính lịch công bố BCTC theo mùa BCTC Việt Nam - luôn
hoạt động, không cần mạng; (2) lấy số liệu BCTC quý gần nhất thật qua Yahoo
Finance quoteSummary - chính bản gốc ghi chú nguồn này "hiện chưa khả dụng
trên Vercel" / dễ bị chặn. Ở bot này ta chỉ giữ phần (1) - đủ để cảnh báo rủi ro
biến động quanh mùa BCTC (pre-earnings window) mà không phụ thuộc 1 nguồn scrape
dễ vỡ.
"""
from dataclasses import dataclass
from datetime import date, datetime

# Mùa BCTC VN: Q4: 1/3-31/3 · Q1: 15/4-15/5 · Q2: 15/7-15/8 · Q3: 15/10-15/11
_WINDOWS = [
    ("Q4", (3, 1), (3, 31)),
    ("Q1", (4, 15), (5, 15)),
    ("Q2", (7, 15), (8, 15)),
    ("Q3", (10, 15), (11, 15)),
]


@dataclass
class EarningsCalendar:
    next_earnings_date: str
    quarter: str
    days_to_earnings: int
    pre_earnings_alert: bool  # True nếu < 15 ngày tới ngày công bố ước tính


def estimate_next_earnings(today: date | None = None) -> EarningsCalendar:
    today = today or datetime.now().date()
    year = today.year
    for quarter, (sm, sd), (em, ed) in _WINDOWS:
        start = date(year, sm, sd)
        end = date(year, em, ed)
        mid = start + (end - start) / 2
        if today <= mid:
            days_to = (mid - today).days
            return EarningsCalendar(mid.isoformat(), f"{quarter}/{year}", days_to, days_to < 15)
    next_q4 = date(year + 1, 3, 15)
    days_to = (next_q4 - today).days
    return EarningsCalendar(next_q4.isoformat(), f"Q4/{year}", days_to, days_to < 15)


def build_earnings_prompt_section(cal: EarningsCalendar, symbol: str) -> str:
    lines = [f"[KQKD/BCTC — {symbol}]"]
    if cal.pre_earnings_alert:
        lines.append(f"⚠️ Sắp tới mùa công bố BCTC {cal.quarter} (ước tính ~{cal.next_earnings_date}, còn {cal.days_to_earnings} ngày) — biến động giá 2 chiều có thể tăng quanh thời điểm này.")
    else:
        lines.append(f"Kỳ BCTC tiếp theo ước tính {cal.quarter} (~{cal.next_earnings_date}, còn {cal.days_to_earnings} ngày) — chưa trong vùng biến động trước công bố.")
    lines.append("(Đây là mốc ƯỚC TÍNH theo mùa BCTC thông thường của thị trường VN, không phải ngày công bố chính thức của riêng mã này.)")
    return "\n".join(lines)
