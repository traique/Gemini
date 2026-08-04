from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("analysis", "backtest", "features", "fundamentals", "policy", "providers", "sector")
REPLACEMENTS = {
    "analysis": (("import stock_backtest as backtest", "from stock import backtest"), ("import stock_features as feat", "from stock import features as feat"), ("import stock_fundamentals as fundamentals", "from stock import fundamentals"), ("import stock_policy as policy", "from stock import policy"), ("import stock_providers as providers", "from stock import providers"), ("import stock_sector as sector", "from stock import sector"), ("import stock_validation as validation", "from stock import validation"), ("from stock_sector import ALL_KNOWN_SYMBOLS", "from stock.sector import ALL_KNOWN_SYMBOLS")),
    "backtest": (("import stock_features as feat", "from stock import features as feat"), ("import stock_policy as policy", "from stock import policy"), ("import stock_providers as providers", "from stock import providers"), ("import stock_validation as validation", "from stock import validation")),
    "fundamentals": (("import stock_features as feat", "from stock import features as feat"), ("import stock_sector as sector", "from stock import sector")),
    "policy": (("import stock_features as feat", "from stock import features as feat"), ("from stock_validation import DataQuality", "from stock.validation import DataQuality")),
    "sector": (("import stock_providers as providers", "from stock import providers"),),
}
CALLERS = {
    "services/tools.py": (("import stock_analysis", "from stock import analysis as stock_analysis"),),
    "test/test_symbol_detection.py": (("import stock_analysis", "from stock import analysis as stock_analysis"),),
    "test/test_stock_validation.py": (("import stock_validation as val", "from stock import validation as val"),),
    "test/test_stock_policy.py": (("import stock_features as feat", "from stock import features as feat"), ("import stock_policy as pol", "from stock import policy as pol"), ("from stock_validation import DataQuality", "from stock.validation import DataQuality")),
    "test/test_stock_features.py": (("import stock_features as ind", "from stock import features as ind"),),
    "test/test_stock_backtest.py": (("import stock_backtest as bt", "from stock import backtest as bt"),),
    "test/test_sector_map.py": (("import stock_analysis", "from stock import analysis as stock_analysis"), ("import stock_sector", "from stock import sector as stock_sector")),
    "test/test_market_data_guard.py": (("import stock_analysis", "from stock import analysis as stock_analysis"), ("import stock_providers as providers", "from stock import providers")),
}

def replace_required(text, old, new, path):
    if old not in text:
        raise RuntimeError(f"Expected import not found in {path}: {old}")
    return text.replace(old, new)

for name in MODULES:
    source = ROOT / f"stock_{name}.py"
    destination = ROOT / "stock" / f"{name}.py"
    text = source.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS.get(name, ()):
        text = replace_required(text, old, new, source)
    destination.write_text(text, encoding="utf-8")
for relative, replacements in CALLERS.items():
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        text = replace_required(text, old, new, path)
    path.write_text(text, encoding="utf-8")
for name in (*MODULES, "validation"):
    (ROOT / f"stock_{name}.py").unlink()
violations = []
for path in ROOT.rglob("*.py"):
    if ".git" in path.parts:
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(f"{path}:{node.lineno}: {a.name}" for a in node.names if a.name.startswith("stock_"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("stock_"):
            violations.append(f"{path}:{node.lineno}: {node.module}")
if violations:
    raise RuntimeError("Legacy stock imports remain:\n" + "\n".join(violations))
print("Migrated stock implementation into /stock and removed root modules.")
