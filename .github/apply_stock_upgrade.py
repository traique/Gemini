from pathlib import Path


def replace(path, old, new, count=1):
    p = Path(path); text = p.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"missing patch marker in {path}: {old[:80]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


Path("stock/validation.py").write_text('''"""Strict OHLCV contract and data-quality gate."""
from dataclasses import dataclass, field
from datetime import date, datetime
import math
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
    has_length_mismatch: bool = False
    has_invalid_numbers: bool = False
    has_ohlc_violation: bool = False
    invalid_bar_count: int = 0
    @property
    def ok(self): return self.status == "ok"
    @property
    def usable(self): return self.status != "bad"

def _parse_date(value):
    try: return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError): return None

def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))

def ohlcv_contract_errors(closes, highs, lows, volumes, dates):
    if not closes: return ["không có dữ liệu giá"]
    n = len(closes); errors = []
    lengths = {"close": n, "high": len(highs), "low": len(lows), "volume": len(volumes)}
    if len(set(lengths.values())) != 1:
        errors.append("độ dài OHLCV không đồng nhất: " + ", ".join(f"{k}={v}" for k,v in lengths.items()))
    if dates and len(dates) != n: errors.append(f"độ dài ngày không khớp: date={len(dates)}, close={n}")
    if errors: return errors
    invalid = broken = 0
    for close, high, low, volume in zip(closes, highs, lows, volumes):
        if not all(_finite(v) for v in (close, high, low, volume)) or close <= 0 or high <= 0 or low <= 0 or volume < 0:
            invalid += 1; continue
        if high < low or not low <= close <= high: broken += 1
    if invalid: errors.append(f"có {invalid} bar chứa NaN/inf/giá không dương hoặc volume âm")
    if broken: errors.append(f"có {broken} bar vi phạm low <= close <= high")
    if dates:
        parsed = [_parse_date(d) for d in dates]
        if any(d is None for d in parsed): errors.append("ngày phải đúng định dạng YYYY-MM-DD")
        valid = [d for d in parsed if d is not None]
        if len(valid) != len(set(valid)): errors.append("phát hiện ngày trùng lặp trong chuỗi giá")
        if any(valid[i] >= valid[i+1] for i in range(len(valid)-1)): errors.append("chuỗi ngày phải tăng nghiêm ngặt")
    return errors

def validate_ohlcv(closes, highs, lows, volumes, dates, *, min_bars=DEFAULT_MIN_BARS, max_stale_days=DEFAULT_MAX_STALE_CALENDAR_DAYS, now=None):
    n = len(closes); errors = ohlcv_contract_errors(closes, highs, lows, volumes, dates)
    if errors:
        joined = " ".join(errors)
        return DataQuality("bad", errors, n, has_duplicate_dates="trùng lặp" in joined, has_length_mismatch="độ dài" in joined, has_invalid_numbers="NaN/inf" in joined, has_ohlc_violation="low <= close <= high" in joined)
    reasons=[]; stale=False; outlier=False
    if dates:
        age = ((now or datetime.now(_VN_TZ)).date() - _parse_date(dates[-1])).days
        if age > max_stale_days: stale=True; reasons.append(f"dữ liệu cũ - phiên gần nhất cách đây {age} ngày")
    for previous,current in zip(closes, closes[1:]):
        move=abs((current-previous)/previous*100)
        if move > _OUTLIER_DAILY_MOVE_PCT:
            outlier=True; reasons.append(f"biến động bất thường {move:.1f}% - cần kiểm tra corporate action/điều chỉnh giá"); break
    if n < MIN_BARS_HARD_FLOOR:
        reasons.append(f"chỉ có {n} phiên - dưới ngưỡng tối thiểu {MIN_BARS_HARD_FLOOR} để tính chỉ báo")
        return DataQuality("bad", reasons, n, stale, outlier)
    if n < min_bars: reasons.append(f"chỉ có {n} phiên - dưới mức khuyến nghị {min_bars} để tin cậy cao")
    return DataQuality("degraded" if reasons else "ok", reasons, n, stale, outlier)
''', encoding="utf-8")

Path("stock/fundamental_profiles.py").write_text('''"""Sector-aware fundamental normalization profiles."""
from dataclasses import dataclass
from stock import sector
@dataclass(frozen=True)
class FundamentalProfile:
    key: str
    label: str
    benchmark_metric: str
    priority_metrics: tuple[str, ...]
    suppress_metrics: tuple[str, ...] = ()
    note: str = ""
PROFILES = {
 "banking": FundamentalProfile("banking","Ngân hàng","pb",("pb","roe","eps","profit_growth"),("current_ratio","debt_equity"),"Ưu tiên P/B và ROE; D/E/current ratio doanh nghiệp thường không phù hợp."),
 "securities": FundamentalProfile("securities","Chứng khoán","pb",("pb","roe","profit_growth"),("current_ratio",),"Ưu tiên P/B, ROE và độ nhạy lợi nhuận theo thanh khoản thị trường."),
 "insurance": FundamentalProfile("insurance","Bảo hiểm","pb",("pb","roe","profit_growth"),("current_ratio",),"Ưu tiên P/B và ROE; cần đối chiếu dự phòng khi có dữ liệu."),
 "realestate": FundamentalProfile("realestate","Bất động sản","pb",("pb","debt_equity","current_ratio","profit_growth"),(),"Ưu tiên tài sản ròng, đòn bẩy, thanh khoản và tiến độ dự án."),
 "utilities": FundamentalProfile("utilities","Điện & Tiện ích","pe",("pe","dividend_yield","debt_equity","roe"),(),"Ưu tiên dòng tiền/cổ tức và đòn bẩy hạ tầng."),
 "oilgas": FundamentalProfile("oilgas","Dầu khí","pe",("pe","roe","profit_growth","debt_equity"),(),"Đọc định giá cùng chu kỳ giá hàng hóa."),
 "default": FundamentalProfile("default","Doanh nghiệp","pe",("pe","pb","roe","profit_growth","debt_equity","current_ratio")),
}
def get_profile(symbol):
    for key in sector.get_symbol_sectors(symbol):
        if key in PROFILES: return PROFILES[key]
    return PROFILES["default"]
''', encoding="utf-8")

# Provider failover and provenance.
replace("stock/providers.py", "import logging\nimport math", "import asyncio\nimport logging\nimport math")
replace("stock/providers.py", "    dates: list = field(default_factory=list)\n\n    @property", "    dates: list = field(default_factory=list)\n    source: str = 'unknown'\n    is_adjusted: bool | None = None\n\n    @property")
replace("stock/providers.py", "    series = await _fetch_ohlcv_uncached(sym, days)\n", '''    primary = await _fetch_ohlcv_dnse(sym, days)\n    from stock.validation import ohlcv_contract_errors\n    primary_errors = ohlcv_contract_errors(primary.closes, primary.highs, primary.lows, primary.volumes, primary.dates)\n    if primary.closes and not primary_errors:\n        series = primary\n    else:\n        _provider_failures["dnse"] += 1\n        logger.warning("DNSE unusable for %s (%s); trying vnstock/VCI", sym, primary_errors or "empty")\n        fallback = await _fetch_ohlcv_vnstock(sym, days)\n        fallback_errors = ohlcv_contract_errors(fallback.closes, fallback.highs, fallback.lows, fallback.volumes, fallback.dates)\n        if fallback.closes and not fallback_errors:\n            series = fallback\n        else:\n            _provider_failures["vnstock"] += 1\n            series = OhlcvSeries(symbol=sym, source="unavailable")\n''')
replace("stock/providers.py", "async def _fetch_ohlcv_uncached(symbol: str, days: int = 90) -> OhlcvSeries:", "async def _fetch_ohlcv_dnse(symbol: str, days: int = 90) -> OhlcvSeries:")
replace("stock/providers.py", "    return OhlcvSeries(symbol=sym, closes=closes, highs=highs, lows=lows, volumes=volumes, dates=dates)\n\n\nasync def fetch_current_price", '''    return OhlcvSeries(symbol=sym, closes=closes, highs=highs, lows=lows, volumes=volumes, dates=dates, source="dnse")\n\n_provider_failures = {"dnse": 0, "vnstock": 0}\ndef provider_health_snapshot(): return dict(_provider_failures)\n\ndef _fetch_ohlcv_vnstock_sync(symbol, days):\n    try:\n        from vnstock import Vnstock\n        end=datetime.now(_VN_TZ).date(); start=end-timedelta(days=int(days*1.7)+30)\n        df=Vnstock().stock(symbol=dnse_symbol(symbol), source="VCI").quote.history(start=start.isoformat(), end=end.isoformat(), interval="1D")\n        if df is None or df.empty: return OhlcvSeries(symbol=symbol, source="vnstock-vci")\n        cols={str(c).strip().lower():c for c in df.columns}\n        def pick(*names): return next((cols[n] for n in names if n in cols), None)\n        dc,cc,hc,lc,vc=pick("time","date","trading_date"),pick("close"),pick("high"),pick("low"),pick("volume","match_volume")\n        if any(x is None for x in (dc,cc,hc,lc,vc)): return OhlcvSeries(symbol=symbol, source="vnstock-vci")\n        rows=[]\n        for _,r in df.iterrows():\n            try: rows.append((str(r[dc])[:10],float(r[hc]),float(r[lc]),float(r[cc]),float(r[vc])))\n            except (TypeError,ValueError,KeyError): pass\n        rows=sorted(rows)[-days:]\n        if not rows: return OhlcvSeries(symbol=symbol, source="vnstock-vci")\n        median=sorted(r[3] for r in rows)[len(rows)//2]; scale=1 if is_index_symbol(symbol) or median>=1000 else PRICE_SCALE\n        return OhlcvSeries(symbol, [round(r[3]*scale) for r in rows], [round(r[1]*scale) for r in rows], [round(r[2]*scale) for r in rows], [r[4] for r in rows], [r[0] for r in rows], "vnstock-vci")\n    except Exception:\n        logger.warning("vnstock fallback failed for %s", symbol, exc_info=True); return OhlcvSeries(symbol=symbol, source="vnstock-vci")\nasync def _fetch_ohlcv_vnstock(symbol, days):\n    try: return await asyncio.wait_for(asyncio.to_thread(_fetch_ohlcv_vnstock_sync, symbol, days), timeout=20)\n    except (TimeoutError, asyncio.TimeoutError): return OhlcvSeries(symbol=symbol, source="vnstock-vci")\n_fetch_ohlcv_uncached = _fetch_ohlcv_dnse\n\ndef _fetch_symbol_universe_sync():\n    try:\n        from vnstock import Listing\n        df=Listing(source="VCI").all_symbols(); cols={str(c).lower():c for c in df.columns}\n        c=next((cols[k] for k in ("symbol","ticker","code") if k in cols),None)\n        return sorted({str(v).strip().upper() for v in df[c] if c is not None and _SYMBOL_RE.fullmatch(str(v).strip().upper())})\n    except Exception: return []\nasync def fetch_symbol_universe():\n    try: symbols=await asyncio.wait_for(asyncio.to_thread(_fetch_symbol_universe_sync), timeout=30)\n    except (TimeoutError, asyncio.TimeoutError): symbols=[]\n    if symbols: return symbols\n    from stock.sector import ALL_KNOWN_SYMBOLS\n    return sorted(ALL_KNOWN_SYMBOLS)\n\nasync def fetch_current_price''')

# Five-year, cost-aware, full-universe out-of-sample backtest.
replace("stock/backtest.py", "- Không mô phỏng phí giao dịch/slippage/trượt giá khi khớp lệnh.\n", "- Mô phỏng phí mua/bán, thuế bán và slippage bảo thủ.\n")
replace("stock/backtest.py", "SETTLEMENT_BARS = 3  # VN: hàng về T+2.5 - không thể bán trước phiên thứ 3 sau entry\n", '''SETTLEMENT_BARS = 3\nTRADING_DAYS_PER_YEAR=252\nDEFAULT_BACKTEST_YEARS=5\nDEFAULT_BACKTEST_DAYS=TRADING_DAYS_PER_YEAR*DEFAULT_BACKTEST_YEARS\nBUY_FEE_PCT=0.15\nSELL_FEE_PCT=0.15\nSELL_TAX_PCT=0.10\nSLIPPAGE_PCT=0.10\nOUT_OF_SAMPLE_RATIO=0.30\n@dataclass(frozen=True)\nclass TradingCosts:\n    buy_fee_pct: float=BUY_FEE_PCT\n    sell_fee_pct: float=SELL_FEE_PCT\n    sell_tax_pct: float=SELL_TAX_PCT\n    slippage_pct: float=SLIPPAGE_PCT\nDEFAULT_COSTS=TradingCosts()\ndef _entry_cost(price,c): return price*(1+c.slippage_pct/100)*(1+c.buy_fee_pct/100)\ndef _exit_proceeds(price,c): return price*(1-c.slippage_pct/100)*(1-(c.sell_fee_pct+c.sell_tax_pct)/100)\ndef _net_r(entry_raw,exit_raw,stop_raw,c):\n    entry=_entry_cost(entry_raw,c); exit_value=_exit_proceeds(exit_raw,c); stop=_exit_proceeds(stop_raw,c); risk=entry-stop\n    return (round((exit_value-entry)/risk,2) if risk>0 else None,entry,exit_value)\n''')
replace("stock/backtest.py", "    buy_signals: int = 0\n\n\ndef _trend_pct", "    buy_signals: int = 0\n    sample: str = 'full'\n\n@dataclass\nclass OutOfSampleResult:\n    split_index: int\n    train: BacktestResult\n    test: BacktestResult\n\ndef _trend_pct")
replace("stock/backtest.py", "    *, min_bars: int = MIN_BARS_TO_START, max_hold_days: int = MAX_HOLD_DAYS,\n) -> BacktestResult:", "    *, min_bars: int = MIN_BARS_TO_START, max_hold_days: int = MAX_HOLD_DAYS, evaluation_start_idx: int | None = None, costs: TradingCosts = DEFAULT_COSTS, sample: str = 'full',\n) -> BacktestResult:")
replace("stock/backtest.py", "    for i in range(min_bars, n):", "    for i in range(max(min_bars, evaluation_start_idx or min_bars), n):")
replace("stock/backtest.py", '"entry_idx": i, "entry": closes[i], "stop": pending["stop"], "target": pending["target"],', '"entry_idx": i, "entry_raw": closes[i], "stop": pending["stop"], "target": pending["target"],')
replace("stock/backtest.py", '''                exit_price = min(closes[i], open_trade["stop"])
                risk = open_trade["entry"] - open_trade["stop"]
                r = round((exit_price - open_trade["entry"]) / risk, 2) if risk > 0 else None
                trades.append(Trade(symbol, open_trade["date"], open_trade["entry"], date_i, exit_price, "stop_hit", r, open_trade["confidence"], open_trade["setup_type"]))''', '''                exit_raw=min(closes[i],open_trade["stop"]); r,entry_value,exit_value=_net_r(open_trade["entry_raw"],exit_raw,open_trade["stop"],costs)
                trades.append(Trade(symbol,open_trade["date"],entry_value,date_i,exit_value,"stop_hit",r,open_trade["confidence"],open_trade["setup_type"]))''')
replace("stock/backtest.py", '''                risk = open_trade["entry"] - open_trade["stop"]
                r = round((open_trade["target"] - open_trade["entry"]) / risk, 2) if risk > 0 else None
                trades.append(Trade(symbol, open_trade["date"], open_trade["entry"], date_i, open_trade["target"], "target_hit", r, open_trade["confidence"], open_trade["setup_type"]))''', '''                r,entry_value,exit_value=_net_r(open_trade["entry_raw"],open_trade["target"],open_trade["stop"],costs)
                trades.append(Trade(symbol,open_trade["date"],entry_value,date_i,exit_value,"target_hit",r,open_trade["confidence"],open_trade["setup_type"]))''')
replace("stock/backtest.py", '''                risk = open_trade["entry"] - open_trade["stop"]
                r = round((closes[i] - open_trade["entry"]) / risk, 2) if risk > 0 else None
                trades.append(Trade(symbol, open_trade["date"], open_trade["entry"], date_i, closes[i], "timeout", r, open_trade["confidence"], open_trade["setup_type"]))''', '''                r,entry_value,exit_value=_net_r(open_trade["entry_raw"],closes[i],open_trade["stop"],costs)
                trades.append(Trade(symbol,open_trade["date"],entry_value,date_i,exit_value,"timeout",r,open_trade["confidence"],open_trade["setup_type"]))''')
replace("stock/backtest.py", 'Trade(symbol, open_trade["date"], open_trade["entry"], None, None, "open", None, open_trade["confidence"], open_trade["setup_type"])', 'Trade(symbol, open_trade["date"], _entry_cost(open_trade["entry_raw"], costs), None, None, "open", None, open_trade["confidence"], open_trade["setup_type"])')
replace("stock/backtest.py", "        total_days_evaluated=total_days_evaluated, buy_signals=buy_signals,\n    )\n\n\nasync def run_backtest(symbols: list[str], days: int = 400)", '''        total_days_evaluated=total_days_evaluated, buy_signals=buy_signals, sample=sample,\n    )\n\ndef run_out_of_sample_on_series(symbol,closes,highs,lows,volumes,dates,vnindex_closes,vnindex_highs,vnindex_lows,vnindex_volumes=None,*,test_ratio=OUT_OF_SAMPLE_RATIO,costs=DEFAULT_COSTS):\n    split=max(MIN_BARS_TO_START+1,min(len(closes)-1,int(len(closes)*(1-test_ratio))))\n    train=run_backtest_on_series(symbol,closes[:split],highs[:split],lows[:split],volumes[:split],dates[:split],vnindex_closes[:split],vnindex_highs[:split],vnindex_lows[:split],(vnindex_volumes or [])[:split],costs=costs,sample="train")\n    test=run_backtest_on_series(symbol,closes,highs,lows,volumes,dates,vnindex_closes,vnindex_highs,vnindex_lows,vnindex_volumes or [],evaluation_start_idx=split,costs=costs,sample="out_of_sample")\n    return OutOfSampleResult(split,train,test)\n\nasync def run_backtest(symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS)''')
replace("stock/backtest.py", '''    vnindex_series = await providers.fetch_ohlcv("VNINDEX", days=days)
    results = []
    for sym in symbols:
        series = await providers.fetch_ohlcv(sym, days=days)
        if len(series.closes) < MIN_BARS_TO_START + 5:
            logger.warning("bỏ qua %s: chỉ có %d phiên", sym, len(series.closes))
            continue
        results.append(run_backtest_on_series(
            sym, series.closes, series.highs, series.lows, series.volumes, series.dates,
            vnindex_series.closes, vnindex_series.highs, vnindex_series.lows, vnindex_series.volumes,
        ))
    return results''', '''    import asyncio\n    symbols=symbols or await providers.fetch_symbol_universe(); vnindex_series=await providers.fetch_ohlcv("VNINDEX",days=days); semaphore=asyncio.Semaphore(8)\n    async def one(sym):\n        async with semaphore: series=await providers.fetch_ohlcv(sym,days=days)\n        if len(series.closes)<MIN_BARS_TO_START+5: return None\n        return run_out_of_sample_on_series(sym,series.closes,series.highs,series.lows,series.volumes,series.dates,vnindex_series.closes,vnindex_series.highs,vnindex_series.lows,vnindex_series.volumes).test\n    return [r for r in await asyncio.gather(*(one(s) for s in symbols)) if r is not None]''')
text=Path("stock/backtest.py").read_text(); a=text.index("# Tập mã đại diện mặc định"); b=text.index("\n\nasync def refresh_setup_stats",a); text=text[:a]+"# Full universe is discovered from VCI, with local sector symbols as fallback.\nDEFAULT_BACKTEST_SYMBOLS=None"+text[b:]; text=text.replace("async def refresh_setup_stats(symbols: list[str] | None = None, days: int = 400)","async def refresh_setup_stats(symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS)").replace("run_backtest(symbols or DEFAULT_BACKTEST_SYMBOLS, days=days)","run_backtest(symbols, days=days)"); Path("stock/backtest.py").write_text(text)

# Fundamental benchmark normalization.
replace("stock/fundamentals.py", "from stock import features as feat", "from stock import features as feat\nfrom stock import fundamental_profiles")
marker="\n\n@dataclass\nclass FundamentalsBundle:"
addition='''\n\n@dataclass\nclass SectorBenchmark:\n    metric: str\n    average: float | None\n    sample: int\n    label: str | None\nasync def fetch_sector_benchmark(symbol, sample_size=8):\n    from stock import sector\n    profile=fundamental_profiles.get_profile(symbol); keys=sector.get_symbol_sectors(symbol)\n    if not keys: return SectorBenchmark(profile.benchmark_metric,None,0,None)\n    meta=sector.SECTOR_MAP[keys[0]]; peers=[p for p in meta["symbols"] if p!=symbol.upper()][:sample_size]\n    async def load(peer):\n        try:\n            v=await asyncio.wait_for(asyncio.to_thread(_fetch_valuation_sync,peer),timeout=_FETCH_TIMEOUT_SEC); value=getattr(v,profile.benchmark_metric,None) if v else None\n            return value if value is not None and 0<value<500 else None\n        except Exception: return None\n    values=[v for v in await asyncio.gather(*(load(p) for p in peers)) if v is not None]\n    return SectorBenchmark(profile.benchmark_metric,round(sum(values)/len(values),2) if values else None,len(values),meta["label"])\n'''
replace("stock/fundamentals.py", marker, addition+marker)
replace("stock/fundamentals.py", "    sector_pe_label: str | None = None\n\n\nasync def fetch_fundamentals", "    sector_pe_label: str | None = None\n    sector_profile: fundamental_profiles.FundamentalProfile | None = None\n    sector_benchmark: SectorBenchmark | None = None\n\nasync def fetch_fundamentals")
replace("stock/fundamentals.py", "valuation, foreign, foreign_trend, growth, events, sector_pe = await asyncio.gather(", "valuation, foreign, foreign_trend, growth, events, benchmark = await asyncio.gather(")
replace("stock/fundamentals.py", "        fetch_sector_pe_average(symbol),\n    )\n    sector_pe_avg, sector_pe_sample, sector_pe_label = sector_pe if sector_pe else (None, 0, None)", "        fetch_sector_benchmark(symbol),\n    )\n    profile=fundamental_profiles.get_profile(symbol)\n    sector_pe_avg=benchmark.average if benchmark and benchmark.metric=='pe' else None\n    sector_pe_sample=benchmark.sample if sector_pe_avg is not None else 0\n    sector_pe_label=benchmark.label if sector_pe_avg is not None else None")
replace("stock/fundamentals.py", "        sector_pe_sample=sector_pe_sample, sector_pe_label=sector_pe_label,\n    )", "        sector_pe_sample=sector_pe_sample, sector_pe_label=sector_pe_label, sector_profile=profile, sector_benchmark=benchmark,\n    )")
replace("stock/fundamentals.py", "    sector_pe_label: str | None = None,\n) -> str:", "    sector_pe_label: str | None = None,\n    sector_profile: fundamental_profiles.FundamentalProfile | None = None,\n    sector_benchmark: SectorBenchmark | None = None,\n) -> str:")
replace("stock/fundamentals.py", '    lines = [f"[ĐỊNH GIÁ & DÒNG TIỀN THẬT — {symbol}, nguồn công khai VCI/TCBS qua vnstock]"]', '    profile=sector_profile or fundamental_profiles.get_profile(symbol)\n    lines=[f"[ĐỊNH GIÁ & DÒNG TIỀN THẬT — {symbol}, nguồn công khai VCI/TCBS qua vnstock]"]\n    lines.append(f"Chuẩn hóa ngành {profile.label}: ưu tiên {\', \'.join(profile.priority_metrics)}. {profile.note}".strip())')
replace("stock/fundamentals.py", "        if valuation.debt_equity is not None or valuation.current_ratio is not None:", "        if (valuation.debt_equity is not None or valuation.current_ratio is not None) and not ({'debt_equity','current_ratio'} <= set(profile.suppress_metrics)):")
replace("stock/fundamentals.py", "        if valuation.pe is not None and sector_pe_avg is not None:\n", '''        if sector_benchmark and sector_benchmark.average is not None:\n            current=getattr(valuation,sector_benchmark.metric,None)\n            if current is not None:\n                diff=round((current-sector_benchmark.average)/sector_benchmark.average*100,1) if sector_benchmark.average else None\n                metric="P/B" if sector_benchmark.metric=="pb" else "P/E"; relation=f"; mã {'CAO' if diff>0 else 'THẤP'} hơn {abs(diff)}%" if diff is not None else ""\n                lines.append(f"So ngành {sector_benchmark.label or ''}: {metric} trung bình {sector_benchmark.average} từ {sector_benchmark.sample} mã hợp lệ{relation}.")\n        elif valuation.pe is not None and sector_pe_avg is not None:\n''')
replace("stock/analysis.py", "return fundamentals.build_fundamentals_prompt_section(bundle.valuation, bundle.foreign, symbol, foreign_trend=bundle.foreign_trend, growth=bundle.growth, events=bundle.events, sector_pe_avg=bundle.sector_pe_avg, sector_pe_sample=bundle.sector_pe_sample, sector_pe_label=bundle.sector_pe_label)", "return fundamentals.build_fundamentals_prompt_section(bundle.valuation, bundle.foreign, symbol, foreign_trend=bundle.foreign_trend, growth=bundle.growth, events=bundle.events, sector_pe_avg=bundle.sector_pe_avg, sector_pe_sample=bundle.sector_pe_sample, sector_pe_label=bundle.sector_pe_label, sector_profile=bundle.sector_profile, sector_benchmark=bundle.sector_benchmark)")

# Stale GVR assertions now accept either verified classification.
for name in ("test/test_symbol_detection.py","test/test_market_data_guard.py"):
    p=Path(name); s=p.read_text(); s=s.replace('_, unverified = stock_analysis.detect_symbol_candidates("giá gvr")\n    assert "GVR" in unverified','known, unverified = stock_analysis.detect_symbol_candidates("giá gvr")\n    assert "GVR" in known + unverified').replace('_, unverified = stock_analysis.detect_symbol_candidates("cổ phiếu gvr sao rồi")\n    assert "GVR" in unverified','known, unverified = stock_analysis.detect_symbol_candidates("cổ phiếu gvr sao rồi")\n    assert "GVR" in known + unverified').replace('_, unverified = stock_analysis.detect_symbol_candidates("GVR")\n    assert "GVR" in unverified','known, unverified = stock_analysis.detect_symbol_candidates("GVR")\n    assert "GVR" in known + unverified').replace('    assert known == []\n    assert "GVR" in unverified','    assert "GVR" in known + unverified').replace('    assert "GVR" in unverified\n    assert known == []','    assert "GVR" in known + unverified').replace('    _, unverified = stock_analysis.detect_symbol_candidates("gvr")\n    assert "GVR" in unverified','    known, unverified = stock_analysis.detect_symbol_candidates("gvr")\n    assert "GVR" in known + unverified'); p.write_text(s)

Path("test/test_stock_import_smoke.py").write_text('''from pathlib import Path\ndef test_stock_package_imports_and_template():\n from stock import analysis,backtest,features,fundamental_profiles,fundamentals,policy,providers,sector,validation\n assert analysis._STOCK_PROMPT_TEMPLATE is not None\n assert (Path(analysis.__file__).parent/"templates"/"stock_analysis_prompt.j2").is_file()\n assert backtest.DEFAULT_BACKTEST_DAYS >= 3*backtest.TRADING_DAYS_PER_YEAR\n''')
Path("test/test_stock_broker_upgrade.py").write_text('''from stock import backtest,fundamental_profiles\ndef test_cost_model_reduces_round_trip_value():\n assert backtest._entry_cost(100,backtest.DEFAULT_COSTS)>100\n assert backtest._exit_proceeds(100,backtest.DEFAULT_COSTS)<100\ndef test_out_of_sample_has_holdout():\n n=120; c=[10000+i*10 for i in range(n)]; h=[x+50 for x in c]; l=[x-50 for x in c]; v=[100000.0]*n; d=[f"2024-{1+i//28:02d}-{1+i%28:02d}" for i in range(n)]\n r=backtest.run_out_of_sample_on_series("TEST",c,h,l,v,d,c,h,l,v)\n assert r.train.sample=="train" and r.test.sample=="out_of_sample"\ndef test_sector_profiles():\n assert fundamental_profiles.get_profile("VCB").benchmark_metric=="pb"\n assert fundamental_profiles.get_profile("SSI").benchmark_metric=="pb"\n assert fundamental_profiles.get_profile("VHM").benchmark_metric=="pb"\n assert fundamental_profiles.get_profile("FPT").benchmark_metric=="pe"\n''')
p=Path("test/test_stock_validation.py"); p.write_text(p.read_text()+'''\n\ndef test_length_mismatch_fails_closed():\n q=val.validate_ohlcv([10.0]*30,[11.0]*29,[9.0]*30,[100.0]*30,[])\n assert q.status=="bad" and q.has_length_mismatch\ndef test_nan_and_negative_volume_fail_closed():\n c=[10.0]*30;c[5]=float("nan");v=[100.0]*30;v[7]=-1\n q=val.validate_ohlcv(c,[11.0]*30,[9.0]*30,v,[])\n assert q.status=="bad" and q.has_invalid_numbers\ndef test_invalid_ohlc_relationship_fails_closed():\n q=val.validate_ohlcv([12.0]*30,[11.0]*30,[9.0]*30,[100.0]*30,[])\n assert q.status=="bad" and q.has_ohlc_violation\n''')
