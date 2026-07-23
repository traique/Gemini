"""Unit test cho services/price_service.py: validate/dedupe/min giá bằng
code (không tin model), cache hit/miss/TTL, force refresh, và fallback
text-based khi pipeline JSON không dùng được - mock toàn bộ theo pattern
test hiện có (tests/test_tools.py, tests/test_db.py)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import official_client  # noqa: E402
from ai.provider_state import provider_state  # noqa: E402
from core import database as db  # noqa: E402
from services import price_service  # noqa: E402


# ─── parse force refresh / normalize ────────────────────────────────────────

def test_parse_force_refresh_nhan_dien_hau_to_moi():
    assert price_service._parse_force_refresh("iPhone 16 Pro moi") == ("iPhone 16 Pro", True)
    assert price_service._parse_force_refresh("iPhone 16 Pro mới") == ("iPhone 16 Pro", True)


def test_parse_force_refresh_khong_co_hau_to():
    assert price_service._parse_force_refresh("iPhone 16 Pro") == ("iPhone 16 Pro", False)


def test_normalize_query_chuan_hoa_khoang_trang_va_hoa_thuong():
    assert price_service._normalize_query("  iPhone   16  Pro ") == "iphone 16 pro"


# ─── format số VN ────────────────────────────────────────────────────────────

def test_format_vnd():
    assert price_service._format_vnd(18990000) == "18.990.000đ"
    assert price_service._format_vnd(500000) == "500.000đ"


# ─── _build_payload: validate + dedupe bằng code, không tin model ──────────

def test_build_payload_loai_gia_am_va_gia_phi_ly():
    data = {
        "product": "iPhone 16 Pro",
        "results": [
            {"shop": "Shop A", "price_vnd": 18990000},
            {"shop": "Shop B", "price_vnd": -1000},  # giá âm -> loại
            {"shop": "Shop C", "price_vnd": 5000},  # dưới ngưỡng hợp lý -> loại
            {"shop": "Shop D", "price_vnd": 5_000_000_000},  # trên ngưỡng -> loại
            {"shop": "", "price_vnd": 20000000},  # thiếu tên shop -> loại
        ],
        "notes": "",
    }
    payload = price_service._build_payload("iPhone 16 Pro", data, [])
    shops = [it["shop"] for it in payload["items"]]
    assert shops == ["Shop A"]


def test_build_payload_dedupe_theo_shop_giu_ban_dau():
    data = {
        "product": "iPhone 16 Pro",
        "results": [
            {"shop": "FPT Shop", "price_vnd": 20000000, "variant": "256GB"},
            {"shop": "fpt shop", "price_vnd": 19000000, "variant": "256GB - trùng"},
        ],
        "notes": "",
    }
    payload = price_service._build_payload("iPhone 16 Pro", data, [])
    assert len(payload["items"]) == 1
    assert payload["items"][0]["price_vnd"] == 20000000


def test_build_payload_parse_gia_dang_string_co_dau_cham():
    data = {
        "product": "iPhone 16 Pro",
        "results": [{"shop": "CellphoneS", "price_vnd": "18.990.000"}],
        "notes": "",
    }
    payload = price_service._build_payload("iPhone 16 Pro", data, [])
    assert payload["items"][0]["price_vnd"] == 18990000


def test_build_payload_bo_qua_results_khong_phai_list():
    payload = price_service._build_payload("X", {"product": "X", "results": "khong phai list"}, [])
    assert payload["items"] == []


# ─── render: code tự chọn min(), không để model tự claim "rẻ nhất" ─────────

def test_render_chon_dung_gia_re_nhat_bang_code():
    payload = {
        "product": "iPhone 16 Pro",
        "items": [
            {"shop": "Shop A", "price_vnd": 20000000, "variant": "", "promo": "", "url": ""},
            {"shop": "Shop B", "price_vnd": 18990000, "variant": "256GB", "promo": "", "url": "https://b.vn"},
        ],
        "notes": "",
        "grounding_sources": [],
    }
    text = price_service._render_from_payload(payload)
    assert "18.990.000đ" in text
    assert "Shop B" in text.split("Chỗ rẻ nhất")[1]  # shop rẻ nhất đúng là Shop B
    assert "[Link](https://b.vn)" in text


def test_render_khong_co_item_nao_van_tra_text_hop_le():
    payload = {"product": "Đồ hiếm", "items": [], "notes": "Không tìm thấy giá", "grounding_sources": []}
    text = price_service._render_from_payload(payload)
    assert "Đồ hiếm" in text
    assert "Không tìm thấy giá" in text


def test_render_hien_thi_nguon_tham_khao_tu_grounding_that():
    payload = {
        "product": "X",
        "items": [{"shop": "A", "price_vnd": 100000, "variant": "", "promo": "", "url": ""}],
        "notes": "",
        "grounding_sources": [["Báo Giá", "https://vertexaisearch.example/xyz"]],
    }
    text = price_service._render_from_payload(payload)
    assert "https://vertexaisearch.example/xyz" in text


def test_render_cache_hien_thi_thoi_gian_va_goi_y_lam_moi():
    payload = {
        "product": "X",
        "items": [{"shop": "A", "price_vnd": 100000, "variant": "", "promo": "", "url": ""}],
        "notes": "",
        "grounding_sources": [],
    }
    cached_at = datetime.now(timezone.utc)
    text = price_service._render_from_payload(payload, cached_at=cached_at)
    assert "♻️" in text
    assert "moi" in text


# ─── fetch_price_message: cache hit/miss, force refresh, fallback ──────────

@pytest.mark.asyncio
async def test_fetch_price_message_cache_hit_khong_goi_api(monkeypatch):
    cached_payload_json = (
        '{"product": "iPhone 16", "items": '
        '[{"shop": "A", "price_vnd": 100000, "variant": "", "promo": "", "url": ""}], '
        '"notes": "", "grounding_sources": []}'
    )

    async def fake_get_price_cache(query_norm, ttl_seconds):
        assert query_norm == "iphone 16"
        return cached_payload_json, datetime.now(timezone.utc)

    async def fail_if_called(idx, prompt):
        pytest.fail("Không được gọi API khi cache còn hạn")

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(official_client, "generate_search_json", fail_if_called)

    text = await price_service.fetch_price_message("iPhone 16")
    assert "iPhone 16" in text
    assert "♻️" in text


@pytest.mark.asyncio
async def test_fetch_price_message_moi_bo_qua_cache(monkeypatch):
    async def fail_if_called(query_norm, ttl_seconds):
        pytest.fail("force refresh phải bỏ qua cache đọc")

    async def fake_ensure_loaded():
        return None

    async def fake_generate_search_json(idx, prompt):
        return official_client.SearchJsonResult(
            data={
                "product": "iPhone 16",
                "results": [{"shop": "A", "price_vnd": 100000, "variant": "", "promo": "", "url": ""}],
                "notes": "",
            },
            grounding_sources=[],
            raw_text="{}",
            used_api_idx=idx,
        )

    set_calls = []

    async def fake_set_price_cache(query_norm, payload_json):
        set_calls.append(query_norm)

    monkeypatch.setattr(db, "get_price_cache", fail_if_called)
    monkeypatch.setattr(db, "set_price_cache", fake_set_price_cache)
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(provider_state, "api_in_cooldown", lambda idx: False)
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: "fake-key" if idx == 1 else None)
    monkeypatch.setattr(official_client, "generate_search_json", fake_generate_search_json)

    text = await price_service.fetch_price_message("iPhone 16 moi")
    assert "100.000đ" in text
    assert set_calls == ["iphone 16"]


@pytest.mark.asyncio
async def test_fetch_price_message_api1_cooldown_chuyen_sang_api2(monkeypatch):
    async def fake_get_price_cache(query_norm, ttl_seconds):
        return None

    async def fake_ensure_loaded():
        return None

    called_idx = []

    async def fake_generate_search_json(idx, prompt):
        called_idx.append(idx)
        return official_client.SearchJsonResult(
            data={"product": "X", "results": [{"shop": "A", "price_vnd": 100000}], "notes": ""},
            grounding_sources=[],
            raw_text="{}",
            used_api_idx=idx,
        )

    async def fake_set_price_cache(query_norm, payload_json):
        return None

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(db, "set_price_cache", fake_set_price_cache)
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    # api1 có key nhưng đang cooldown -> phải bỏ qua, chỉ gọi api2.
    monkeypatch.setattr(provider_state, "api_in_cooldown", lambda idx: idx == 1)
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: "fake-key")
    monkeypatch.setattr(official_client, "generate_search_json", fake_generate_search_json)

    await price_service.fetch_price_message("X")
    assert called_idx == [2]


@pytest.mark.asyncio
async def test_fetch_price_message_fallback_text_khi_khong_co_api_key(monkeypatch):
    async def fake_get_price_cache(query_norm, ttl_seconds):
        return None

    class FakeResponse:
        text = "🏪 Shop A — **100.000đ**"
        used_fallback = False

    async def fake_ask(instruction, enable_search=False):
        assert enable_search is True
        return FakeResponse()

    async def fake_ensure_loaded():
        return None

    import ai.orchestrator as orchestrator_module

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: None)  # không có key nào
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(orchestrator_module, "ask", fake_ask)

    text = await price_service.fetch_price_message("iPhone 16")
    assert "Shop A" in text


@pytest.mark.asyncio
async def test_fetch_price_message_raise_khi_fallback_cung_rong(monkeypatch):
    async def fake_get_price_cache(query_norm, ttl_seconds):
        return None

    class EmptyResponse:
        text = ""
        used_fallback = False

    async def fake_ask(instruction, enable_search=False):
        return EmptyResponse()

    async def fake_ensure_loaded():
        return None

    import ai.orchestrator as orchestrator_module

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: None)
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(orchestrator_module, "ask", fake_ask)

    with pytest.raises(price_service.PriceServiceError):
        await price_service.fetch_price_message("iPhone 16")


@pytest.mark.asyncio
async def test_fetch_price_message_thieu_ten_san_pham_raise_ngay():
    with pytest.raises(price_service.PriceServiceError):
        await price_service.fetch_price_message("   ")
