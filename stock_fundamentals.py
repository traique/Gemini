"""Định giá cơ bản (P/E, P/B, EPS, ROE...) + dòng tiền khối ngoại THẬT.

Nguồn: thư viện `vnstock` (mã nguồn mở, MIỄN PHÍ, KHÔNG cần đăng ký/API key) -
gom dữ liệu công khai từ VCI/TCBS. Đây là dữ liệu THẬT (không phải ước lượng
như module stock_moneyflow.py/stock_earnings.py đã bị xoá trước đó).

⚠️ QUAN TRỌNG - đọc trước khi tin tưởng module này:
- vnstock là công cụ của bên thứ 3, dựa trên API công khai không tài liệu hoá
  chính thức của VCI/TCBS -> KHÔNG có SLA, có thể lỗi hoặc đổi cấu trúc dữ
  liệu bất kỳ lúc nào mà không báo trước.
- Toàn bộ hàm ở đây match tên cột theo TỪ KHOÁ (substring) thay vì tên cột
  cứng, để bớt nhạy cảm với thay đổi nhỏ giữa các phiên bản vnstock - nhưng
  KHÔNG đảm bảo luôn đúng 100%. Nếu không tìm thấy cột phù hợp, trả về None
  cho trường đó thay vì đoán liều.
- Giấy phép vnstock: dành cho cá nhân/phi thương mại - phù hợp bot 1 user
  này, KHÔNG dùng cho mục đích thương mại nếu chưa xin phép tác giả.
- Gọi vnstock là thao tác ĐỒNG BỘ (blocking, dùng requests) -> luôn chạy qua
  asyncio.to_thread() để không chặn event loop, và luôn có timeout.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_SEC = 15


@dataclass
class Valuation:
    pe: float | None = None
    pb: float | None = None
    eps: float | None = None
    roe: float | None = None
    dividend_yield: float | None = None


@dataclass
class ForeignFlowReal:
    foreign_buy_vol: float | None = None
    foreign_sell_vol: float | None = None
    foreign_net_vol: float | None = None
    foreign_room_pct: float | None = None


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _flatten_columns(columns) -> list[str]:
    flat = []
    for col in columns:
        if isinstance(col, tuple):
            flat.append("_".join(str(c) for c in col if c).strip().lower())
        else:
            flat.append(str(col).strip().lower())
    return flat


def _find_col(flat_columns: list[str], *keywords: str) -> int | None:
    """Trả về index cột đầu tiên chứa TẤT CẢ keyword (không phân biệt hoa/thường)."""
    for i, col in enumerate(flat_columns):
        if all(kw in col for kw in keywords):
            return i
    return None


def _fetch_valuation_sync(symbol: str) -> Valuation | None:
    try:
        from vnstock import Vnstock
    except ImportError:
        logger.warning("Chưa cài thư viện vnstock (pip install vnstock).")
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
    except Exception:
        logger.warning("vnstock: không khởi tạo được cho %s", symbol, exc_info=True)
        return None

    df = None
    for kwargs in ({"period": "quarter"}, {}):
        try:
            df = stock.finance.ratio(**kwargs)
            if df is not None and not df.empty:
                break
        except Exception:
            continue
    if df is None or df.empty:
        return None

    flat_cols = _flatten_columns(df.columns)
    row = df.iloc[0]

    def _val(*keywords: str) -> float | None:
        idx = _find_col(flat_cols, *keywords)
        return _to_float(row.iloc[idx]) if idx is not None else None

    # "pe"/"pb" có thể trùng khớp nhầm vào các cột khác chứa chữ "pe"/"pb" (vd "type"),
    # nên ưu tiên thử "p/e"/"p/b" trước, "pe"/"pb" là fallback.
    return Valuation(
        pe=_val("p/e") or _val("pe"),
        pb=_val("p/b") or _val("pb"),
        eps=_val("eps"),
        roe=_val("roe"),
        dividend_yield=_val("dividend"),
    )


def _fetch_foreign_sync(symbol: str) -> ForeignFlowReal | None:
    try:
        from vnstock import Vnstock
    except ImportError:
        return None

    try:
        stock = Vnstock().stock(symbol=symbol, source="VCI")
        board = stock.trading.price_board(symbols_list=[symbol])
    except Exception:
        logger.warning("vnstock: price_board lỗi cho %s", symbol, exc_info=True)
        return None
    if board is None or board.empty:
        return None

    flat_cols = _flatten_columns(board.columns)
    row = board.iloc[0]

    def _val(*keywords: str) -> float | None:
        idx = _find_col(flat_cols, *keywords)
        return _to_float(row.iloc[idx]) if idx is not None else None

    buy = _val("foreign", "buy", "vol") or _val("foreign", "buy")
    sell = _val("foreign", "sell", "vol") or _val("foreign", "sell")
    room = _val("foreign", "room") or _val("room")
    net = None
    if buy is not None and sell is not None:
        net = round(buy - sell, 2)

    if buy is None and sell is None and room is None:
        return None
    return ForeignFlowReal(foreign_buy_vol=buy, foreign_sell_vol=sell, foreign_net_vol=net, foreign_room_pct=room)


async def fetch_fundamentals(symbol: str) -> tuple[Valuation | None, ForeignFlowReal | None]:
    """Lấy song song định giá + khối ngoại thật. Không bao giờ raise ra ngoài."""
    async def _safe(fn, *args):
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=_FETCH_TIMEOUT_SEC)
        except Exception:
            logger.warning("stock_fundamentals lỗi cho %s (%s)", symbol, fn.__name__, exc_info=True)
            return None

    valuation, foreign = await asyncio.gather(
        _safe(_fetch_valuation_sync, symbol),
        _safe(_fetch_foreign_sync, symbol),
    )
    return valuation, foreign


def _fmt(v: float | None, suffix: str = "") -> str:
    return f"{v:g}{suffix}" if v is not None else "chưa có dữ liệu"


def build_fundamentals_prompt_section(
    valuation: Valuation | None, foreign: ForeignFlowReal | None, symbol: str
) -> str:
    if valuation is None and foreign is None:
        return ""
    lines = [f"[ĐỊNH GIÁ & KHỐI NGOẠI THẬT — {symbol}, nguồn công khai VCI/TCBS qua vnstock]"]
    if valuation:
        lines.append(
            f"P/E: {_fmt(valuation.pe)} | P/B: {_fmt(valuation.pb)} | "
            f"EPS: {_fmt(valuation.eps)} VND | ROE: {_fmt(valuation.roe, '%')} | "
            f"Tỷ suất cổ tức: {_fmt(valuation.dividend_yield, '%')}"
        )
    if foreign:
        lines.append(
            f"Khối ngoại phiên gần nhất — Mua: {_fmt(foreign.foreign_buy_vol)} | "
            f"Bán: {_fmt(foreign.foreign_sell_vol)} | "
            f"Ròng: {_fmt(foreign.foreign_net_vol)} | "
            f"Room ngoại còn lại: {_fmt(foreign.foreign_room_pct, '%')}"
        )
    lines.append(
        "(Lưu ý: dữ liệu lấy qua thư viện bên thứ 3 không chính thức, có thể thiếu/trễ - "
        "nếu số liệu quan trọng cho quyết định lớn, đối chiếu thêm trên app công ty chứng khoán.)"
    )
    return "\n".join(lines)
