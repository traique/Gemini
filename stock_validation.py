"""Backward-compatible import path for stock data validation.

New application code should import ``stock.validation``.
"""
from stock.validation import *  # noqa: F401,F403
