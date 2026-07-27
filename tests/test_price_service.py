"""Unit test cho services/price_service.py: nhận diện hỏi giá tự nhiên (guard
cổ phiếu), extract giá VND, lọc noise, chấm điểm/rank theo giá rẻ đáng tin,
format Telegram, và fetch_price_message (cache/force-refresh/fallback)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import official_client  # noqa: E402
from ai.provider_state import provider_state  # noqa: E402
from core import database as db  # noqa: E402
from services import price_service as ps  # noqa: E402


# ─── is_product_price_query / extract_product_query_from_text ─────────────

@pytest.mark.parametrize("text", [
    "giá iphone 16 pro",
    "iphone 16 pro giá bao nhiêu",
    "hỏi giá máy lạnh panasonic",
    "xem giá tủ lạnh samsung inverter",
    "giá FPT Shop iphone 16",
    "giá VNM sữa",
    "giá đỡ điện thoại",
])
def test_is_product_price_query_true(text):
    assert ps.is_product_price_query(text)


@pytest.mark.parametrize("text", ["giá FPT", "giá VNM", "FPT hôm nay", ""])
def test_is_product_price_query_false(text):
    assert not ps.is_product_price_query(text)


def test_is_product_price_query_mua_don_le_qua_chung_khong_tinh():
    assert not ps.is_product_price_query("em muốn mua")


def test_is_product_price_query_mua_co_noi_dung_cu_the():
    assert ps.is_product_price_query("mua tai nghe airpods giúp anh")


@pytest.mark.parametrize("text, expected", [
    ("giá iphone 16 pro", "iphone 16 pro"),
    ("iphone 16 pro giá bao nhiêu", "iphone 16 pro"),
    ("hỏi giá máy lạnh panasonic 1.5hp", "máy lạnh panasonic 1.5hp"),
    ("xem giá giúp tôi tủ lạnh samsung inverter", "tủ lạnh samsung inverter"),
    ("giá đỡ điện thoại", "giá đỡ điện thoại"),
])
def test_extract_product_query_from_text(text, expected):
    assert ps.extract_product_query_from_text(text) == expected


# ─── extract_vnd_prices ──────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("24.990.000đ", [24_990_000]),
    ("24,990,000 ₫", [24_990_000]),
    ("24 990 000 VND", [24_990_000]),
    ("24.99 triệu", [24_990_000]),
    ("24tr990", [24_990_000]),
    ("25 triệu", [25_000_000]),
    ("25tr", [25_000_000]),
])
def test_extract_vnd_prices_valid_formats(text, expected):
    assert ps.extract_vnd_prices(text) == expected


@pytest.mark.parametrize("text", [
    "iPhone 16", "256GB", "0% trả góp", "12 tháng", "1 đổi 1", "2025", "8K",
    "24 tháng bảo hành",
])
def test_extract_vnd_prices_avoid_false_positive(text):
    assert ps.extract_vnd_prices(text) == []


# ─── normalize_price_query ───────────────────────────────────────────────

def test_normalize_price_query_force_refresh_hau_to():
    q = ps.normalize_price_query("iphone 16 pro moi")
    assert q.clean == "iphone 16 pro"
    assert q.force_refresh is True


def test_normalize_price_query_khong_force_refresh():
    q = ps.normalize_price_query("iphone 16 pro")
    assert q.force_refresh is False


def test_normalize_price_query_category_appliance_khong_nham_hp_brand():
    q = ps.normalize_price_query("máy lạnh panasonic 1.5hp inverter")
    assert q.category == "appliance"
    assert q.brand == "panasonic"


def test_normalize_price_query_category_phone():
    q = ps.normalize_price_query("iphone 16 pro")
    assert q.category == "phone"
    assert "pro" in q.variant_terms


# ─── build_search_plan ───────────────────────────────────────────────────

def test_build_search_plan_nhieu_query_khong_phai_1():
    q = ps.normalize_price_query("iphone 16 pro")
    plan = ps.build_search_plan(q)
    assert len(plan.primary_queries) > 1
    assert len(plan.retailer_queries) > 1
    assert any("site:" in rq for rq in plan.retailer_queries)
    assert len(plan.broad_queries) >= 1


# ─── detect_noise / score_candidate ──────────────────────────────────────

def _candidate(**kwargs):
    base = dict(title="Shop", url="https://shop.vn/x", source="shop.vn")
    base.update(kwargs)
    return ps.PriceCandidate(**base)


def test_detect_noise_phu_kien_khi_query_khong_hoi_phu_kien():
    query = ps.normalize_price_query("iphone 16 pro")
    candidate = _candidate(snippet="Ốp lưng chống sốc cho iPhone 16 Pro")
    is_noise, reasons = ps.detect_noise(candidate, query)
    assert is_noise
    assert "accessory" in reasons


def test_detect_noise_khong_phai_noise_khi_query_hoi_dung_phu_kien():
    query = ps.normalize_price_query("ốp lưng iphone 16 pro")
    candidate = _candidate(snippet="Ốp lưng chống sốc cho iPhone 16 Pro", price=200_000)
    is_noise, reasons = ps.detect_noise(candidate, query)
    assert "accessory" not in reasons


def test_detect_noise_review_khong_gia():
    query = ps.normalize_price_query("iphone 16 pro")
    candidate = _candidate(snippet="Đánh giá chi tiết iPhone 16 Pro sau 1 tháng")
    is_noise, reasons = ps.detect_noise(candidate, query)
    assert is_noise
    assert "review_or_news" in reasons


def test_detect_noise_khong_co_gia():
    query = ps.normalize_price_query("iphone 16 pro")
    candidate = _candidate(price=None)
    is_noise, reasons = ps.detect_noise(candidate, query)
    assert is_noise
    assert "no_price" in reasons


def test_score_candidate_retailer_uu_tien_diem_cao_hon():
    query = ps.normalize_price_query("iphone 16 pro")
    strong = _candidate(
        title="Hoàng Hà Mobile", source="hoanghamobile.com", price=24_990_000,
        snippet="iPhone 16 Pro chính hãng giá 24.990.000đ",
    )
    weak = _candidate(
        title="Shop lạ", source="shoplavn.vn", price=24_990_000,
        snippet="iPhone 16 Pro giá 24.990.000đ",
    )
    for c in (strong, weak):
        c.is_noise, c.noise_reasons = ps.detect_noise(c, query)
    assert ps.score_candidate(strong, query) > ps.score_candidate(weak, query)


def test_score_candidate_variant_mismatch_nghiem_trong_bi_tru_diem():
    query = ps.normalize_price_query("iphone 16 pro")
    pro = _candidate(price=24_990_000, snippet="iPhone 16 Pro 128GB giá 24.990.000đ")
    pro_max = _candidate(price=24_990_000, snippet="iPhone 16 Pro Max 256GB giá 24.990.000đ")
    for c in (pro, pro_max):
        c.is_noise, c.noise_reasons = ps.detect_noise(c, query)
    assert ps.score_candidate(pro, query) > ps.score_candidate(pro_max, query)


# ─── rank_by_price_and_confidence ────────────────────────────────────────

def test_rank_uu_tien_gia_re_dang_tin():
    query = ps.normalize_price_query("iphone 16 pro")
    raw = [
        {"shop": "Hoàng Hà Mobile", "url": "https://hoanghamobile.com/a", "price_vnd": 24_990_000,
         "snippet": "iPhone 16 Pro 128GB chính hãng giá 24.990.000đ"},
        {"shop": "CellphoneS", "url": "https://cellphones.com.vn/a", "price_vnd": 25_490_000,
         "snippet": "iPhone 16 Pro 128GB giá 25.490.000đ"},
        {"shop": "Trả Góp Nhanh", "url": "https://tragop.vn/a", "price_vnd": 990_000,
         "snippet": "iPhone 16 Pro trả góp 0% chỉ từ 990.000đ/tháng"},
    ]
    candidates = ps.extract_price_candidates(raw, query)
    candidates = ps.filter_noise(candidates, query)
    candidates = ps.score_candidates(candidates, query)
    ranked = ps.rank_by_price_and_confidence(candidates)

    assert [c.price for c in ranked] == [24_990_000, 25_490_000]  # trả góp bị loại (outlier)
    assert ranked[0].price < ranked[1].price


def test_rank_khong_du_du_lieu_tra_rong():
    query = ps.normalize_price_query("sản phẩm lạ hiếm khó tìm ABCXYZ")
    raw = [{"shop": "Shop A", "url": "https://a.vn", "price_vnd": None, "snippet": "review sản phẩm"}]
    candidates = ps.extract_price_candidates(raw, query)
    candidates = ps.filter_noise(candidates, query)
    candidates = ps.score_candidates(candidates, query)
    assert ps.rank_by_price_and_confidence(candidates) == []


# ─── format_telegram_price_message ───────────────────────────────────────

def test_format_message_hien_thi_re_nhat_va_top_3():
    query = ps.normalize_price_query("iphone 16 pro")
    candidates = [
        ps.PriceCandidate(title="Hoàng Hà Mobile", url="https://a.vn", source="hoanghamobile.com", price=24_990_000, confidence=90),
        ps.PriceCandidate(title="CellphoneS", url="https://b.vn", source="cellphones.com.vn", price=25_490_000, confidence=88),
    ]
    text = ps.format_telegram_price_message(query, candidates)
    assert "24.990.000đ" in text
    assert "Rẻ nhất em thấy" in text
    assert "Hoàng Hà Mobile" in text
    assert "🕒 Cập nhật" in text


def test_format_message_khong_du_du_lieu():
    query = ps.normalize_price_query("đồ hiếm ABCXYZ")
    text = ps.format_telegram_price_message(query, [])
    assert "chưa tìm được giá đủ tin cậy" in text
    assert "đồ hiếm abcxyz" in text.lower() or "đồ hiếm ABCXYZ" in text


def test_format_message_canh_bao_variant_conflict():
    query = ps.normalize_price_query("iphone 16")  # không nêu rõ Pro/Pro Max
    candidates = [
        ps.PriceCandidate(title="Shop A", url="https://a.vn", source="a.vn", price=20_000_000,
                           snippet="iPhone 16 Pro 128GB", confidence=90),
        ps.PriceCandidate(title="Shop B", url="https://b.vn", source="b.vn", price=25_000_000,
                           snippet="iPhone 16 Pro Max 256GB", confidence=85),
    ]
    text = ps.format_telegram_price_message(query, candidates)
    assert "có thể lẫn" in text


# ─── fetch_price_message: cache/force-refresh/fallback ────────────────────

@pytest.mark.asyncio
async def test_fetch_price_message_cache_hit_khong_goi_api(monkeypatch):
    cached_payload_json = (
        '{"clean": "iphone 16", "variant_terms": [], "candidates": '
        '[{"title": "Shop A", "url": "https://a.vn", "source": "a.vn", "snippet": "", '
        '"price": 100000, "prices": [], "variant": null, "is_priority_retailer": false, '
        '"is_noise": false, "noise_reasons": [], "confidence": 90}]}'
    )

    async def fake_get_price_cache(query_norm, ttl_seconds):
        assert query_norm == "iphone 16"
        return cached_payload_json, datetime.now(timezone.utc)

    async def fail_if_called(idx, prompt):
        pytest.fail("Không được gọi API khi cache còn hạn")

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(official_client, "generate_search_json", fail_if_called)

    text = await ps.fetch_price_message("iPhone 16")
    assert "100.000đ" in text
    assert "Shop A" in text


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
                "results": [{"shop": "A", "url": "https://a.vn", "price_vnd": 100_000, "snippet": "iPhone 16 giá 100.000đ"}],
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

    text = await ps.fetch_price_message("iPhone 16 moi")
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
            data={"product": "X", "results": [{"shop": "A", "url": "https://a.vn", "price_vnd": 100_000, "snippet": "X giá 100.000đ"}], "notes": ""},
            grounding_sources=[],
            raw_text="{}",
            used_api_idx=idx,
        )

    async def fake_set_price_cache(query_norm, payload_json):
        return None

    monkeypatch.setattr(db, "get_price_cache", fake_get_price_cache)
    monkeypatch.setattr(db, "set_price_cache", fake_set_price_cache)
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(provider_state, "api_in_cooldown", lambda idx: idx == 1)
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: "fake-key")
    monkeypatch.setattr(official_client, "generate_search_json", fake_generate_search_json)

    await ps.fetch_price_message("X")
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
    monkeypatch.setattr(official_client, "api_key_for", lambda idx: None)
    monkeypatch.setattr(provider_state, "ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(orchestrator_module, "ask", fake_ask)

    text = await ps.fetch_price_message("iPhone 16")
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

    with pytest.raises(ps.PriceServiceError):
        await ps.fetch_price_message("iPhone 16")


@pytest.mark.asyncio
async def test_fetch_price_message_thieu_ten_san_pham_raise_ngay():
    with pytest.raises(ps.PriceServiceError):
        await ps.fetch_price_message("   ")
