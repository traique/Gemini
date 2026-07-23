"""Unit test cho việc thống nhất cache cookie giữa DB (nguồn tin tưởng duy
nhất) và file cache riêng của thư viện gemini-webapi (bị xoá trước mỗi lần
init để không đọc lại giá trị cũ hơn DB) - xem
ai.cookie_client._reset_library_cookie_cache.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import cookie_client  # noqa: E402
from core import config  # noqa: E402


def test_duong_dan_cache_theo_dung_scheme_thu_vien(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path))
    path = cookie_client._library_cookie_cache_path("psid-abc")
    assert path == tmp_path / "gemini_webapi" / ".cached_cookies_psid-abc.json"


def test_xoa_file_cache_cu_neu_ton_tai(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path))
    monkeypatch.setattr(config, "GEMINI_SECURE_1PSID", "psid-abc")

    cache_dir = tmp_path / "gemini_webapi"
    cache_dir.mkdir()
    stale_file = cache_dir / ".cached_cookies_psid-abc.json"
    stale_file.write_text('[{"name": "__Secure-1PSIDTS", "value": "gia-tri-cu"}]')
    assert stale_file.exists()

    cookie_client._reset_library_cookie_cache()

    assert not stale_file.exists()


def test_khong_lam_gi_neu_khong_co_file_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path))
    monkeypatch.setattr(config, "GEMINI_SECURE_1PSID", "psid-khong-co-file")
    # Không có file nào tồn tại - phải chạy êm, không raise.
    cookie_client._reset_library_cookie_cache()


def test_khong_lam_gi_neu_chua_cau_hinh_1psid(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_COOKIE_PATH", str(tmp_path))
    monkeypatch.setattr(config, "GEMINI_SECURE_1PSID", "")
    # Không có 1PSID -> không có gì để tính đường dẫn -> no-op, không raise.
    cookie_client._reset_library_cookie_cache()
