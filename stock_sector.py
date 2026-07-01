"""Phân tích ngành (Sector Rotation) - port từ src/lib/sector-map.ts + sector-analyzer.ts.

Không cần API trả phí: tính performance ngành hoàn toàn từ OHLCV lịch sử qua
DNSE (stock_providers.fetch_ohlcv) + SECTOR_MAP phân loại thủ công.
"""
import asyncio
from dataclasses import dataclass

import stock_providers as providers

SECTOR_MAP: dict[str, dict] = {
    "banking":    {"label": "Ngân hàng",                 "symbols": ["VCB", "BID", "CTG", "TCB", "MBB", "ACB", "VPB", "HDB", "STB", "EIB", "TPB", "SHB"]},
    "steel":      {"label": "Thép",                       "symbols": ["HPG", "HSG", "NKG", "GEX"]},
    "realestate": {"label": "Bất động sản",               "symbols": ["VIC", "VHM", "NVL", "KDH", "DXG", "PDR", "NLG", "DIG", "VRE", "KBC", "BCM", "HDC"]},
    "oilgas":     {"label": "Dầu khí",                    "symbols": ["GAS", "PLX", "PVD", "PVT", "PVS", "BSR", "OIL", "PLC"]},
    "technology": {"label": "Công nghệ",                  "symbols": ["FPT", "CMG", "VGI", "CTR"]},
    "securities": {"label": "Chứng khoán",                "symbols": ["SSI", "VCI", "HCM", "VND", "VIX", "SHS", "MBS", "BVS"]},
    "retail":     {"label": "Bán lẻ",                     "symbols": ["MWG", "FRT", "PNJ", "DGW"]},
    "food":       {"label": "Thực phẩm & Đồ uống",        "symbols": ["VNM", "SAB", "MSN", "DBC", "HAG", "QNS", "MCH"]},
    "industrial": {"label": "Khu công nghiệp & Xây dựng", "symbols": ["GEX", "CTD", "VCG", "REE", "CII", "KBC", "BCM", "SIP"]},
    "utilities":  {"label": "Điện & Tiện ích",            "symbols": ["POW", "REE", "GAS", "PLC"]},
    "logistics":  {"label": "Vận tải & Logistics",        "symbols": ["GMD", "PVT", "ACV", "VJC"]},
}

ALL_KNOWN_SYMBOLS: set[str] = {s for meta in SECTOR_MAP.values() for s in meta["symbols"]}


def get_symbol_sectors(symbol: str) -> list[str]:
    s = symbol.strip().upper()
    return [key for key, meta in SECTOR_MAP.items() if s in meta["symbols"]]


def get_primary_sector_label(symbol: str) -> str:
    keys = get_symbol_sectors(symbol)
    return SECTOR_MAP[keys[0]]["label"] if keys else "Khác"


@dataclass
class SectorPerformance:
    key: str
    label: str
    trend_1m: float
    trend_3m: float
    vs_vnindex_1m: float
    momentum: str  # hot | warm | cold | dump
    top_movers: list[str]


def _trend_pct(closes: list[float], lookback: int) -> float:
    if len(closes) < lookback + 1:
        return 0.0
    past = closes[-1 - min(lookback, len(closes) - 1)]
    curr = closes[-1]
    return round(((curr - past) / past) * 100, 2) if past > 0 else 0.0


async def _analyze_sector(key: str, meta: dict, vnindex_1m: float) -> SectorPerformance | None:
    sample = meta["symbols"][:4]
    results = await asyncio.gather(*[providers.fetch_ohlcv(sym, days=90) for sym in sample], return_exceptions=True)

    valid = []
    for sym, series in zip(sample, results):
        if isinstance(series, Exception) or not series.closes:
            continue
        t1m = _trend_pct(series.closes, 22)
        t3m = _trend_pct(series.closes, 65)
        if t1m != 0:
            valid.append((sym, t1m, t3m))

    if not valid:
        return None

    avg_1m = sum(v[1] for v in valid) / len(valid)
    avg_3m = sum(v[2] for v in valid) / len(valid)
    vs_vnindex = round(avg_1m - vnindex_1m, 2)

    if avg_1m > 5:
        momentum = "hot"
    elif avg_1m > 1:
        momentum = "warm"
    elif avg_1m > -3:
        momentum = "cold"
    else:
        momentum = "dump"

    top_movers = [v[0] for v in sorted(valid, key=lambda x: abs(x[1]), reverse=True)[:2]]

    return SectorPerformance(key, meta["label"], round(avg_1m, 2), round(avg_3m, 2), vs_vnindex, momentum, top_movers)


@dataclass
class SectorContext:
    sectors: list[SectorPerformance]
    strong_sectors: list[str]
    risky_sectors: list[str]
    rotation_signal: str


async def build_sector_context(sector_keys: list[str]) -> SectorContext | None:
    if not sector_keys:
        return None
    vn_series = await providers.fetch_ohlcv("VNINDEX", days=90)
    vnindex_1m = _trend_pct(vn_series.closes, 22) if vn_series.closes else 0.0

    results = await asyncio.gather(
        *[_analyze_sector(key, SECTOR_MAP[key], vnindex_1m) for key in sector_keys if key in SECTOR_MAP]
    )
    sectors = [s for s in results if s is not None]
    if not sectors:
        return None

    strong = [s.label for s in sectors if s.momentum == "hot" or s.vs_vnindex_1m > 3]
    risky = [s.label for s in sectors if s.momentum == "dump" or s.vs_vnindex_1m < -3]

    if strong:
        rotation = f"Dòng tiền đang vào: {', '.join(strong)}"
    elif risky:
        rotation = f"Dòng tiền đang rút khỏi: {', '.join(risky)}"
    else:
        rotation = "Dòng tiền chưa rõ xu hướng luân chuyển ngành rõ rệt"

    return SectorContext(sectors, strong, risky, rotation)


def build_sector_prompt_section(ctx: SectorContext | None, symbol: str) -> str:
    if not ctx:
        return ""
    sectors_of_symbol = get_symbol_sectors(symbol)
    if not sectors_of_symbol:
        return ""
    lines = [f"[NGÀNH — {symbol}]"]
    for key in sectors_of_symbol:
        sp = next((s for s in ctx.sectors if s.key == key), None)
        if not sp:
            continue
        emoji = {"hot": "🔥", "warm": "🟢", "cold": "🟡", "dump": "🔴"}[sp.momentum]
        lines.append(
            f"{emoji} Ngành {sp.label}: {'+' if sp.trend_1m > 0 else ''}{sp.trend_1m}% (1M), "
            f"{'outperform' if sp.vs_vnindex_1m > 0 else 'underperform'} VNINDEX {'+' if sp.vs_vnindex_1m > 0 else ''}{sp.vs_vnindex_1m}%. "
            f"Mã tiêu biểu: {', '.join(sp.top_movers)}."
        )
    lines.append(f"Tín hiệu luân chuyển: {ctx.rotation_signal}")
    return "\n".join(lines)
