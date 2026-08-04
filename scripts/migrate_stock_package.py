"""One-time cleanup for stock package migration artifacts."""
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
for cache_dir in sorted(root.rglob("__pycache__"), reverse=True):
    if ".git" not in cache_dir.parts:
        shutil.rmtree(cache_dir)
for bytecode in root.rglob("*.py[co]"):
    if ".git" not in bytecode.parts:
        bytecode.unlink()
print("Removed Python cache artifacts.")
