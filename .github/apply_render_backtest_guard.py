from pathlib import Path


def replace(path, old, new):
    p=Path(path); text=p.read_text(encoding='utf-8')
    if old not in text: raise RuntimeError(f'missing marker in {path}: {old[:100]!r}')
    p.write_text(text.replace(old,new,1),encoding='utf-8')

replace('stock/backtest.py','import logging\nfrom dataclasses','import logging\nimport os\nfrom dataclasses')
replace('stock/backtest.py','OUT_OF_SAMPLE_RATIO=0.30\n@dataclass(frozen=True)', '''OUT_OF_SAMPLE_RATIO=0.30
BACKTEST_ALLOW_RENDER_ENV="STOCK_BACKTEST_ALLOW_ON_RENDER"
RENDER_MAX_SYMBOLS=50
RENDER_MAX_DAYS=TRADING_DAYS_PER_YEAR*3
RENDER_CONCURRENCY=2
DEFAULT_CONCURRENCY=4
BACKTEST_BATCH_SIZE=50

def is_render_runtime(environ=None):
    env=os.environ if environ is None else environ
    return str(env.get("RENDER","")).lower() in ("1","true","yes") or any(env.get(k) for k in ("RENDER_SERVICE_ID","RENDER_INSTANCE_ID","RENDER_EXTERNAL_URL"))

def assert_backtest_runtime_allowed(environ=None):
    env=os.environ if environ is None else environ
    if is_render_runtime(env) and str(env.get(BACKTEST_ALLOW_RENDER_ENV,"false")).lower() not in ("1","true","yes"):
        raise RuntimeError("Heavy backtest is disabled on Render web service. Run it in GitHub Actions/offline, then deploy backtest_stats.json.")

def backtest_runtime_limits(days, symbol_count, environ=None):
    env=os.environ if environ is None else environ
    if is_render_runtime(env):
        return min(days,RENDER_MAX_DAYS),min(symbol_count,RENDER_MAX_SYMBOLS),RENDER_CONCURRENCY
    requested=max(1,int(env.get("STOCK_BACKTEST_CONCURRENCY",DEFAULT_CONCURRENCY)))
    return days,symbol_count,min(requested,8)

@dataclass(frozen=True)''')
old='''async def run_backtest(symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS) -> list[BacktestResult]:
    """Cần mạng thật (DNSE) - chạy trong môi trường production/bot, KHÔNG chạy
    được trong sandbox không có egress tới DNSE."""
    import asyncio
    symbols=symbols or await providers.fetch_symbol_universe(); vnindex_series=await providers.fetch_ohlcv("VNINDEX",days=days); semaphore=asyncio.Semaphore(8)
    async def one(sym):
        async with semaphore: series=await providers.fetch_ohlcv(sym,days=days)
        if len(series.closes)<MIN_BARS_TO_START+5: return None
        return run_out_of_sample_on_series(sym,series.closes,series.highs,series.lows,series.volumes,series.dates,vnindex_series.closes,vnindex_series.highs,vnindex_series.lows,vnindex_series.volumes).test
    return [r for r in await asyncio.gather(*(one(s) for s in symbols)) if r is not None]
'''
new='''async def run_backtest(symbols: list[str] | None = None, days: int = DEFAULT_BACKTEST_DAYS) -> list[BacktestResult]:
    """Run offline research only. Render is denied by default to protect the web service.

    Even with the explicit Render override, work is capped at 50 symbols,
    three years and concurrency two. All runtimes submit bounded batches so
    a full universe does not create thousands of in-memory tasks at once.
    """
    import asyncio
    assert_backtest_runtime_allowed()
    symbols=list(symbols or await providers.fetch_symbol_universe())
    days,max_symbols,concurrency=backtest_runtime_limits(days,len(symbols))
    symbols=symbols[:max_symbols]
    vnindex_series=await providers.fetch_ohlcv("VNINDEX",days=days)
    semaphore=asyncio.Semaphore(concurrency)
    async def one(sym):
        async with semaphore:
            series=await providers.fetch_ohlcv(sym,days=days)
        if len(series.closes)<MIN_BARS_TO_START+5: return None
        return run_out_of_sample_on_series(sym,series.closes,series.highs,series.lows,series.volumes,series.dates,vnindex_series.closes,vnindex_series.highs,vnindex_series.lows,vnindex_series.volumes).test
    results=[]
    for start in range(0,len(symbols),BACKTEST_BATCH_SIZE):
        batch=await asyncio.gather(*(one(sym) for sym in symbols[start:start+BACKTEST_BATCH_SIZE]))
        results.extend(result for result in batch if result is not None)
    return results
'''
replace('stock/backtest.py',old,new)
replace('render.yaml','''      - key: CHAT_SESSION_TIMEOUT_SEC
        value: "21600"
''','''      - key: CHAT_SESSION_TIMEOUT_SEC
        value: "21600"
      # Heavy research jobs must never share the free web-service process.
      - key: STOCK_BACKTEST_ALLOW_ON_RENDER
        value: "false"
''')
p=Path('test/test_stock_broker_upgrade.py')
p.write_text(p.read_text(encoding='utf-8')+'''\n\ndef test_render_runtime_detected_and_denied_by_default():\n env={"RENDER":"true","STOCK_BACKTEST_ALLOW_ON_RENDER":"false"}\n assert backtest.is_render_runtime(env)\n try:\n  backtest.assert_backtest_runtime_allowed(env)\n except RuntimeError as error:\n  assert "disabled on Render" in str(error)\n else:\n  raise AssertionError("Render backtest must be denied")\n\ndef test_render_override_is_still_resource_capped():\n env={"RENDER":"true","STOCK_BACKTEST_ALLOW_ON_RENDER":"true"}\n backtest.assert_backtest_runtime_allowed(env)\n days,symbols,concurrency=backtest.backtest_runtime_limits(1260,1700,env)\n assert days==756 and symbols==50 and concurrency==2\n\ndef test_non_render_concurrency_is_bounded():\n assert backtest.backtest_runtime_limits(1260,1700,{"STOCK_BACKTEST_CONCURRENCY":"99"})==(1260,1700,8)\n''',encoding='utf-8')
